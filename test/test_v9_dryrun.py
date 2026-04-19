#!/usr/bin/env python3
"""
V9 Dry-Run Integration Test — Full pipeline validation with mocked brokers.

Replays 1 day of real Tradier data through the COMPLETE V9 pipeline:
  EventBus → StrategyEngine → ProSetupEngine → RiskEngine → PortfolioRiskGate
  → PaperBroker → PositionManager → FillLedger → Kill Switch

Uses PaperBroker (instant fills, no API calls).
Writes state to /tmp/ (not production data/).
Validates Monday readiness without touching production state.

Usage:
    python test/test_v9_dryrun.py
    python test/test_v9_dryrun.py --date 2026-04-17
    python test/test_v9_dryrun.py --tickers AAPL NVDA TSLA
"""
import os
import sys
import time
import logging
import tempfile
import argparse
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.split('#')[0].strip()
            if '=' in _line:
                _k, _v = _line.split('=', 1)
                _v = _v.strip().strip('"').strip("'")
                if _k.strip() and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
)
# Suppress noisy detectors/router logging
for name in ['pro_setups', 'monitor.risk_engine', 'monitor.portfolio_risk',
             'monitor.strategy_engine', 'monitor.kill_switch']:
    logging.getLogger(name).setLevel(logging.WARNING)

log = logging.getLogger('dryrun')

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')


class DryRunResult:
    """Collects metrics from the dry run."""
    def __init__(self):
        self.bars_emitted = 0
        self.signals_generated = defaultdict(int)  # event_type → count
        self.fills_processed = 0
        self.positions_opened = 0
        self.positions_closed = 0
        self.order_failures = 0
        self.risk_blocks = 0
        self.ledger_lots = 0
        self.errors = []
        self.kill_switch_triggered = False
        self.strategies_fired = defaultdict(int)


def run_dryrun(
    tickers: list,
    replay_date: str = None,
    trade_budget: float = 1000.0,
) -> DryRunResult:
    """
    Run full V9 pipeline with PaperBroker on 1 day of real data.

    Returns DryRunResult with all metrics.
    """
    import pandas as pd
    from monitor.event_bus import EventBus, EventType, Event, DispatchMode
    from monitor.events import BarPayload
    from monitor.brokers import PaperBroker
    from monitor.position_manager import PositionManager
    from monitor.position_registry import registry

    result = DryRunResult()

    # ── Temp directory for state files ────────────────────────────────────
    tmp_dir = tempfile.mkdtemp(prefix='v9_dryrun_')
    log.info("State files: %s", tmp_dir)

    # ── Load 1 day of bar data from DB ────────────────────────────────────
    from backtests.data_loader import BarDataLoader

    if replay_date:
        target_date = datetime.strptime(replay_date, '%Y-%m-%d').date()
    else:
        target_date = (datetime.now(ET) - timedelta(days=1)).date()
        # Skip weekends
        while target_date.weekday() >= 5:
            target_date -= timedelta(days=1)

    start = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=ET)
    end = datetime.combine(target_date, datetime.max.time()).replace(tzinfo=ET)

    log.info("Loading bars for %s (%d tickers)...", target_date, len(tickers))
    loader = BarDataLoader(source='db')
    intraday = loader.load_intraday(tickers, start, end, interval='1m')

    if not intraday:
        # Fallback: try Tradier API
        log.info("No DB data, fetching from Tradier...")
        token = os.getenv('TRADIER_TOKEN', '')
        if token:
            loader = BarDataLoader(source='tradier', tradier_token=token)
            intraday = loader.load_intraday(tickers, start, end, interval='1m')

    if not intraday:
        log.error("No bar data available for %s", target_date)
        result.errors.append(f"No data for {target_date}")
        return result

    loaded_tickers = list(intraday.keys())
    log.info("Loaded %d tickers with data for %s", len(loaded_tickers), target_date)

    # Load daily RVOL baseline
    rvol_data = {}
    try:
        rvol_data = loader.load_rvol_baseline(loaded_tickers, start, lookback_days=14)
    except Exception:
        pass

    # ── Initialize EventBus (SYNC mode for deterministic replay) ──────────
    bus = EventBus(mode=DispatchMode.SYNC)
    registry._positions.clear()

    # ── V9: FillLedger (temp file) ────────────────────────────────────────
    fill_ledger = None
    try:
        from monitor.fill_ledger import FillLedger
        ledger_path = os.path.join(tmp_dir, 'fill_ledger.json')
        fill_ledger = FillLedger(state_file=ledger_path)
        log.info("[OK] FillLedger initialized")
    except Exception as e:
        result.errors.append(f"FillLedger init: {e}")
        log.error("[FAIL] FillLedger: %s", e)

    # ── PaperBroker (instant fills, no API) ───────────────────────────────
    broker = PaperBroker(bus=bus, broker_name='paper')
    log.info("[OK] PaperBroker wired")

    # ── V9: BrokerRegistry ────────────────────────────────────────────────
    broker_registry = None
    try:
        from monitor.broker_registry import BrokerRegistry
        broker_registry = BrokerRegistry()
        broker_registry.register('paper', broker)
        log.info("[OK] BrokerRegistry (1 broker)")
    except Exception as e:
        result.errors.append(f"BrokerRegistry: {e}")
        log.error("[FAIL] BrokerRegistry: %s", e)

    # ── Shared state dicts ────────────────────────────────────────────────
    positions = {}
    reclaimed_today = set()
    last_order_time = {}
    trade_log = []

    # ── PositionManager ───────────────────────────────────────────────────
    try:
        pm = PositionManager(
            bus=bus,
            positions=positions,
            reclaimed_today=reclaimed_today,
            last_order_time=last_order_time,
            trade_log=trade_log,
            alert_email=None,
            fill_ledger=fill_ledger,
        )
        # Override save_state to no-op (don't write production files)
        pm.save_state = lambda: None
        log.info("[OK] PositionManager wired")
    except Exception as e:
        result.errors.append(f"PositionManager init: {e}")
        log.error("[FAIL] PositionManager: %s", e)

    # ── RiskEngine ────────────────────────────────────────────────────────
    try:
        from monitor.risk_engine import RiskEngine
        risk_engine = RiskEngine(
            bus=bus,
            positions=positions,
            reclaimed_today=reclaimed_today,
            last_order_time=last_order_time,
            data_client=None,
            max_positions=5,
            order_cooldown=60,
            trade_budget=trade_budget,
            alert_email=None,
        )
        log.info("[OK] RiskEngine wired")
    except Exception as e:
        result.errors.append(f"RiskEngine: {e}")
        log.error("[FAIL] RiskEngine: %s", e)

    # ── PortfolioRiskGate ─────────────────────────────────────────────────
    try:
        from monitor.portfolio_risk import PortfolioRiskGate

        class _MockMonitor:
            """Minimal mock for PortfolioRiskGate."""
            def __init__(self):
                self.positions = positions
                self.trade_log = trade_log
                self._broker = broker

        portfolio_risk = PortfolioRiskGate(bus=bus, monitor=_MockMonitor())
        portfolio_risk.reset_day()
        log.info("[OK] PortfolioRiskGate wired")
    except Exception as e:
        result.errors.append(f"PortfolioRiskGate: {e}")
        log.error("[FAIL] PortfolioRiskGate: %s", e)

    # ── Kill Switch ───────────────────────────────────────────────────────
    try:
        from monitor.kill_switch import PerStrategyKillSwitch
        kill_switch = PerStrategyKillSwitch(
            bus=bus,
            limits={'vwap': -10000.0, 'pro': -2000.0},
            alert_email=None,
        )
        kill_switch.reset_day()
        log.info("[OK] Kill Switch wired")
    except Exception as e:
        result.errors.append(f"KillSwitch: {e}")
        log.error("[FAIL] KillSwitch: %s", e)

    # ── StrategyEngine (VWAP) ─────────────────────────────────────────────
    try:
        from monitor.strategy_engine import StrategyEngine
        from config import STRATEGY_PARAMS
        strategy_engine = StrategyEngine(
            bus=bus,
            positions=positions,
            strategy_params=STRATEGY_PARAMS,
        )
        log.info("[OK] StrategyEngine (VWAP) wired")
    except Exception as e:
        result.errors.append(f"StrategyEngine init: {e}")
        log.error("[FAIL] StrategyEngine: %s", e)

    # ── ProSetupEngine ────────────────────────────────────────────────────
    pro_engine = None
    try:
        from pro_setups.engine import ProSetupEngine
        pro_engine = ProSetupEngine(
            bus=bus,
            max_positions=10,
            order_cooldown=60,
            trade_budget=trade_budget,
            shared_positions=positions,
        )
        log.info("[OK] ProSetupEngine wired (13 detectors)")
    except Exception as e:
        result.errors.append(f"ProSetupEngine: {e}")
        log.error("[FAIL] ProSetupEngine: %s", e)

    # ── V9: EquityTracker ─────────────────────────────────────────────────
    try:
        if broker_registry:
            from monitor.equity_tracker import EquityTracker
            pnl_fn = lambda: sum(t.get('pnl', 0) for t in trade_log)
            equity_tracker = EquityTracker(broker_registry, pnl_fn=pnl_fn)
            log.info("[OK] EquityTracker wired")
    except Exception as e:
        result.errors.append(f"EquityTracker: {e}")
        log.error("[FAIL] EquityTracker: %s", e)

    # ── Event counters ────────────────────────────────────────────────────
    def _count_signals(event):
        result.signals_generated[event.type.name] += 1
        payload = event.payload
        if hasattr(payload, 'strategy_name'):
            result.strategies_fired[payload.strategy_name] += 1

    def _count_fills(event):
        result.fills_processed += 1
        if event.payload.side == 'BUY':
            result.positions_opened += 1
        else:
            result.positions_closed += 1

    def _count_failures(event):
        result.order_failures += 1

    bus.subscribe(EventType.SIGNAL, _count_signals, priority=1)
    bus.subscribe(EventType.PRO_STRATEGY_SIGNAL, _count_signals, priority=1)
    bus.subscribe(EventType.FILL, _count_fills, priority=1)
    bus.subscribe(EventType.ORDER_FAIL, _count_failures, priority=1)

    # ── Replay bars ───────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info("REPLAY START: %s | %d tickers", target_date, len(loaded_tickers))
    log.info("=" * 60)

    # Collect all timestamps
    all_timestamps = set()
    for df in intraday.values():
        all_timestamps.update(df.index.tolist())
    all_timestamps = sorted(all_timestamps)

    if not all_timestamps:
        result.errors.append("No timestamps to replay")
        return result

    log.info("Replaying %d bar timestamps...", len(all_timestamps))

    # Rolling windows per ticker
    rolling_windows = {t: pd.DataFrame() for t in loaded_tickers}
    bars_per_ticker = defaultdict(int)

    replay_start = time.time()

    for i, timestamp in enumerate(all_timestamps):
        for ticker in loaded_tickers:
            if ticker not in intraday:
                continue
            df = intraday[ticker]
            if timestamp not in df.index:
                continue

            bar = df.loc[timestamp]

            # Build rolling window
            bar_dict = {
                'open': float(bar['open']), 'high': float(bar['high']),
                'low': float(bar['low']), 'close': float(bar['close']),
                'volume': float(bar['volume']),
            }
            new_row = pd.DataFrame([bar_dict], index=[timestamp])
            rolling_windows[ticker] = pd.concat(
                [rolling_windows[ticker], new_row]
            ).iloc[-200:]  # cap at 200

            bars_per_ticker[ticker] += 1

            # Skip until enough data
            if len(rolling_windows[ticker]) < 30:
                continue

            # Emit BAR event
            payload = BarPayload(
                ticker=ticker,
                df=rolling_windows[ticker].copy(),
                rvol_df=rvol_data.get(ticker),
            )
            event = Event(EventType.BAR, payload)
            try:
                bus.emit(event)
                result.bars_emitted += 1
            except Exception as e:
                result.errors.append(f"BAR emit {ticker}: {e}")

        # Progress every 50 timestamps
        if i > 0 and i % 50 == 0:
            elapsed = time.time() - replay_start
            pct = (i / len(all_timestamps)) * 100
            log.info(
                "  [%3.0f%%] %d/%d bars | signals=%d fills=%d positions=%d | %.1fs",
                pct, result.bars_emitted, i * len(loaded_tickers),
                sum(result.signals_generated.values()),
                result.fills_processed, len(positions), elapsed,
            )

    replay_elapsed = time.time() - replay_start

    # ── Collect final metrics ─────────────────────────────────────────────
    if fill_ledger:
        result.ledger_lots = fill_ledger.lot_count

    # Check kill switch
    try:
        result.kill_switch_triggered = any(
            kill_switch._halted.values()
        ) if hasattr(kill_switch, '_halted') else False
    except Exception:
        pass

    # ── Report ────────────────────────────────────────────────────────────
    log.info("")
    log.info("=" * 60)
    log.info("DRY RUN COMPLETE: %s", target_date)
    log.info("=" * 60)
    log.info("")

    total_signals = sum(result.signals_generated.values())
    total_pnl = sum(t.get('pnl', 0) for t in trade_log)

    log.info("  Replay time:        %.1f seconds", replay_elapsed)
    log.info("  Bars emitted:       %d", result.bars_emitted)
    log.info("  Tickers with data:  %d", len(loaded_tickers))
    log.info("")
    log.info("  ── Signals ──")
    for evt_type, count in sorted(result.signals_generated.items()):
        log.info("    %s: %d", evt_type, count)
    log.info("")
    log.info("  ── Strategies Fired ──")
    for strat, count in sorted(result.strategies_fired.items(), key=lambda x: -x[1]):
        log.info("    %s: %d signals", strat, count)
    log.info("")
    log.info("  ── Execution ──")
    log.info("    Fills:            %d", result.fills_processed)
    log.info("    Positions opened: %d", result.positions_opened)
    log.info("    Positions closed: %d", result.positions_closed)
    log.info("    Open positions:   %d", len(positions))
    log.info("    Order failures:   %d", result.order_failures)
    log.info("    Trade log:        %d entries", len(trade_log))
    log.info("    P&L:              $%.2f", total_pnl)
    log.info("")
    log.info("  ── V9 Components ──")
    log.info("    FillLedger lots:  %d", result.ledger_lots)
    log.info("    Kill switch:      %s",
             "TRIGGERED" if result.kill_switch_triggered else "OK (not triggered)")
    log.info("    Errors:           %d", len(result.errors))

    if result.errors:
        log.info("")
        log.info("  ── Errors ──")
        for err in result.errors[:10]:
            log.info("    - %s", err)

    # ── Pass/Fail ─────────────────────────────────────────────────────────
    log.info("")
    critical_failures = [e for e in result.errors if 'init' in e.lower()]
    if critical_failures:
        log.error("RESULT: FAIL — %d critical errors", len(critical_failures))
        return result

    checks = [
        ("Bars emitted", result.bars_emitted > 0),
        ("Signals generated", total_signals > 0),
        ("Fills processed", result.fills_processed > 0),
        ("No crashes", len(result.errors) == 0),
        ("FillLedger recorded lots", result.ledger_lots > 0 if fill_ledger else True),
        ("Kill switch not triggered", not result.kill_switch_triggered),
    ]

    all_passed = True
    for name, passed in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        log.info("  [%s] %s", status, name)

    log.info("")
    if all_passed:
        log.info("RESULT: ALL CHECKS PASSED — V9 pipeline ready for Monday")
    else:
        log.warning("RESULT: SOME CHECKS FAILED — review before Monday")

    # Cleanup temp dir
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)

    return result


def main():
    parser = argparse.ArgumentParser(description='V9 Dry-Run Integration Test')
    parser.add_argument('--date', type=str, default=None,
                        help='Replay date YYYY-MM-DD (default: last trading day)')
    parser.add_argument('--tickers', nargs='+', default=None,
                        help='Tickers to test (default: top 15)')
    args = parser.parse_args()

    tickers = args.tickers or [
        'AAPL', 'NVDA', 'TSLA', 'META', 'MSFT',
        'AMD', 'GOOGL', 'AMZN', 'NFLX', 'SPY',
        'QQQ', 'COIN', 'PLTR', 'CRM', 'UBER',
    ]

    result = run_dryrun(tickers, replay_date=args.date)
    sys.exit(0 if len(result.errors) == 0 else 1)


if __name__ == '__main__':
    main()
