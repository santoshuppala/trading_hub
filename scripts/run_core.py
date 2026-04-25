#!/usr/bin/env python3
"""
V9 Core process — VWAP + Pro strategies + Pop scanning + broker + position management.

Pro and Pop merged into Core (V8→V9). Options stays separate (different instruments).
Data Collector stays separate (independent pipeline).

This is the primary process. It:
  - Fetches market data, runs VWAP strategy
  - Runs Pro's 11 technical strategies (merged in-process)
  - Scans for Pop momentum tickers (periodic, no independent trading)
  - Executes orders via Alpaca + Tradier (SmartRouter)
  - Manages positions (single PositionManager, single bot_state.json)
  - Publishes SIGNAL + POP_SIGNAL to Redpanda for Options process
"""
import os
import sys
import time
import signal
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')


def _handle_sigterm(signum, frame):
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, _handle_sigterm)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    TICKERS, STRATEGY, STRATEGY_PARAMS,
    OPEN_COST, CLOSE_COST, MAX_POSITIONS, ORDER_COOLDOWN, TRADE_BUDGET,
    ALERT_EMAIL, ALPACA_API_KEY, ALPACA_SECRET, TRADIER_TOKEN,
    PAPER_TRADING, DATA_SOURCE, BROKER, GLOBAL_MAX_POSITIONS,
    DB_ENABLED, DATABASE_URL, MAX_DAILY_LOSS,
    PRO_MAX_POSITIONS, PRO_TRADE_BUDGET, PRO_ORDER_COOLDOWN,
)

# Logging
log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
os.makedirs(log_dir, exist_ok=True)
date_dir = os.path.join(log_dir, datetime.now().strftime('%Y%m%d'))
os.makedirs(date_dir, exist_ok=True)
log_file = os.path.join(date_dir, 'core.log')

logging.root.handlers = []
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [core] %(message)s',
    handlers=[logging.FileHandler(log_file)],
    force=True,
)
log = logging.getLogger(__name__)


def main():
    from monitor import RealTimeMonitor
    from monitor.shared_cache import CacheWriter
    from monitor.ipc import (EventPublisher, EventConsumer,
                              TOPIC_SIGNALS, TOPIC_POP, TOPIC_DISCOVERY)
    from monitor.distributed_registry import DistributedPositionRegistry
    from monitor.event_bus import EventType, Event

    log.info("=" * 60)
    log.info("V9 CORE PROCESS STARTING")
    log.info("=" * 60)

    # ── V8: Validate SMTP at startup ──────────────────────────────────────
    try:
        from monitor.alerts import validate_smtp
        validate_smtp()
    except Exception:
        log.warning("SMTP validation skipped — alerts module not ready")

    # ── Distributed registry ──────────────────────────────────────────────
    dist_registry = DistributedPositionRegistry(global_max=GLOBAL_MAX_POSITIONS)
    # V8: Clean stale entries from previous crash (lease-based cleanup)
    try:
        dist_registry.cleanup_stale(max_age_seconds=3600)
    except Exception:
        pass
    dist_registry.reset()
    log.info("Distributed position registry initialized (max=%d)", GLOBAL_MAX_POSITIONS)

    # ── Shared cache writer (Options still reads from this) ───────────────
    cache_writer = CacheWriter()
    log.info("Shared cache writer ready")

    # ── IPC publisher (for Options — SIGNAL + POP_SIGNAL) ─────────────────
    publisher = EventPublisher(source_name='core')
    log.info("IPC publisher ready")

    # ── V8: Initialize RVOL engine BEFORE Pro (Pro detectors need it) ─────
    try:
        from monitor.rvol import init_global_rvol_engine
        rvol_engine = init_global_rvol_engine()
        log.info("RVOL engine initialized")
    except Exception as exc:
        log.warning("RVOL engine init failed (non-fatal): %s", exc)
        rvol_engine = None

    # ── Monitor (VWAP strategy + broker + position management) ────────────
    redpanda_brokers = os.getenv('REDPANDA_BROKERS', '127.0.0.1:9092')

    monitor = RealTimeMonitor(
        tickers=TICKERS,
        redpanda_brokers=redpanda_brokers,
        strategy_name=STRATEGY,
        strategy_params=STRATEGY_PARAMS,
        open_cost=OPEN_COST,
        close_cost=CLOSE_COST,
        alert_email=ALERT_EMAIL,
        alpaca_api_key=ALPACA_API_KEY,
        alpaca_secret_key=ALPACA_SECRET,
        tradier_token=TRADIER_TOKEN,
        paper=PAPER_TRADING,
        max_positions=MAX_POSITIONS,
        order_cooldown=ORDER_COOLDOWN,
        trade_budget=TRADE_BUDGET,
        data_source=DATA_SOURCE,
    )

    # ── V9 (R2): Wrap data client in FailoverDataClient ────────────────────
    try:
        from monitor.data_client import FailoverDataClient
        from monitor.alpaca_data_client import AlpacaDataClient
        alpaca_fallback = AlpacaDataClient(ALPACA_API_KEY, ALPACA_SECRET)
        failover_client = FailoverDataClient(
            primary=monitor._data,
            secondary=alpaca_fallback,
            alert_fn=lambda msg: send_alert(ALERT_EMAIL, msg, severity='CRITICAL')
                     if ALERT_EMAIL else None,
        )
        monitor._data = failover_client
        log.info("FailoverDataClient active | primary=Tradier secondary=Alpaca(IEX)")
    except Exception as exc:
        log.warning("FailoverDataClient init failed (non-fatal): %s", exc)

    # ── V8: Seed RVOL profiles from monitor's data client ─────────────────
    if rvol_engine:
        try:
            rvol_engine.seed_profiles(monitor.tickers, monitor._data)
            log.info("RVOL profiles seeded for %d tickers", len(monitor.tickers))
        except Exception as exc:
            log.warning("RVOL profile seeding failed (non-fatal): %s", exc)

    # ── V8: Pre-seed beta cache for all tickers ────────────────────────────
    try:
        from monitor.risk_sizing import RiskSizer
        _sizer = RiskSizer()
        _sizer.seed_betas_from_cache(monitor.tickers)
    except Exception as exc:
        log.warning("Beta pre-seeding failed (non-fatal): %s", exc)

    # ── V8: Cancel stale orders at BOTH brokers on startup ────────────────
    # After SIGKILL, open BUY/SELL orders may still be pending.
    try:
        trading_client = getattr(monitor, '_broker', None)
        if trading_client and hasattr(trading_client, '_client') and trading_client._client:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            open_orders = trading_client._client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
            for order in open_orders:
                try:
                    trading_client._client.cancel_order_by_id(str(order.id))
                    log.info("[Startup] Cancelled stale Alpaca order: %s %s %s",
                             order.side, order.symbol, order.id)
                except Exception:
                    pass
    except Exception as exc:
        log.warning("[Startup] Stale Alpaca order cancel failed: %s", exc)

    try:
        import requests as _req
        t_token = os.getenv('TRADIER_SANDBOX_TOKEN', '')
        t_acct = os.getenv('TRADIER_ACCOUNT_ID', '')
        if t_token and t_acct:
            t_sandbox = os.getenv('TRADIER_SANDBOX', 'true').lower() == 'true'
            t_base = 'https://sandbox.tradier.com' if t_sandbox else 'https://api.tradier.com'
            t_headers = {'Authorization': f'Bearer {t_token}', 'Accept': 'application/json'}
            # Cancel only truly open/pending orders (not filled/rejected/expired)
            # V8: Don't use filter=open — Tradier sandbox returns wrong results
            resp = _req.get(f'{t_base}/v1/accounts/{t_acct}/orders',
                           headers=t_headers, timeout=10)
            if resp.status_code == 200:
                orders = resp.json().get('orders', {})
                if orders and orders != 'null':
                    order_list = orders.get('order', [])
                    if isinstance(order_list, dict):
                        order_list = [order_list]
                    cancelled = 0
                    for o in order_list:
                        oid = o.get('id')
                        status = o.get('status', '')
                        if oid and status in ('pending', 'open', 'partially_filled'):
                            _req.delete(f'{t_base}/v1/accounts/{t_acct}/orders/{oid}',
                                       headers=t_headers, timeout=5)
                            cancelled += 1
                            log.info("[Startup] Cancelled stale Tradier order: %s (%s %s)",
                                     oid, o.get('symbol', '?'), o.get('type', '?'))
                    log.info("[Startup] Cancelled %d open Tradier orders (of %d total)",
                             cancelled, len(order_list))
    except Exception as exc:
        log.warning("[Startup] Stale Tradier order cancel failed: %s", exc)

    # ── DB persistence ────────────────────────────────────────────────────
    from scripts._db_helper import init_satellite_db
    db_cleanup = init_satellite_db(monitor._bus, process_name='core')

    # ── V9: FillLedger (shadow mode — parallel tracking) ─────────────────
    fill_ledger = None
    try:
        from monitor.fill_ledger import FillLedger
        fill_ledger = FillLedger()  # uses config.FILL_LEDGER_PATH
        fill_ledger.load()
        monitor.set_fill_ledger(fill_ledger)
        log.info("FillLedger attached (shadow mode) | lots=%d open=%d daily_pnl=$%.2f",
                 fill_ledger.lot_count, fill_ledger.open_position_count,
                 fill_ledger.daily_realized_pnl())
    except Exception as exc:
        log.warning("FillLedger init failed (non-fatal, shadow mode skipped): %s", exc)

    # ── V9: BrokerRegistry (extensible broker collection) ────────────────
    broker_registry = None
    try:
        from monitor.broker_registry import BrokerRegistry
        broker_registry = BrokerRegistry()

        # Register Alpaca broker
        alpaca_broker = getattr(monitor, '_broker', None)
        if alpaca_broker:
            broker_registry.register('alpaca', alpaca_broker)

        # Tradier broker will be registered after SmartRouter creation (below)
        monitor.set_broker_registry(broker_registry)
    except Exception as exc:
        log.warning("BrokerRegistry init failed (non-fatal): %s", exc)

    # ── V9: EquityTracker (hourly P&L drift detection) ───────────────────
    # NOTE: Do NOT call set_baseline() here — Tradier broker hasn't been
    # registered yet (happens at SmartRouter init below). Baseline is set
    # lazily on first check_drift() call, which runs after all brokers
    # are registered. This ensures both Alpaca AND Tradier equity are tracked.
    try:
        if broker_registry:
            from monitor.equity_tracker import EquityTracker
            pnl_fn = lambda: sum(t.get('pnl', 0) for t in monitor.trade_log)
            equity_tracker = EquityTracker(broker_registry, pnl_fn=pnl_fn,
                                            alert_email=ALERT_EMAIL)
            monitor.set_equity_tracker(equity_tracker)
    except Exception as exc:
        log.warning("EquityTracker init failed (non-fatal): %s", exc)

    # ── Portfolio-level risk gate ─────────────────────────────────────────
    from monitor.portfolio_risk import PortfolioRiskGate
    portfolio_risk = PortfolioRiskGate(bus=monitor._bus, monitor=monitor)
    log.info("PortfolioRiskGate active")
    portfolio_risk.reset_day()
    # V9 (L3): Start background buying power refresh (30s interval, <10ms reads)
    try:
        portfolio_risk.start_buying_power_refresher()
    except Exception as exc:
        log.warning("BuyingPower refresher failed (non-fatal): %s", exc)

    # ── V8: Per-strategy kill switch ─────────────────────────────────────
    from monitor.kill_switch import PerStrategyKillSwitch
    kill_switch = PerStrategyKillSwitch(
        bus=monitor._bus,
        limits={'vwap': float(MAX_DAILY_LOSS), 'pro': -2000.0},
        alert_email=ALERT_EMAIL,
    )
    kill_switch.reset_day()
    log.info("PerStrategyKillSwitch active | vwap=$%.0f pro=$%.0f",
             MAX_DAILY_LOSS, -2000.0)

    # ── Centralized registry gate ─────────────────────────────────────────
    from monitor.registry_gate import RegistryGate
    registry_gate = RegistryGate(bus=monitor._bus, registry=dist_registry)

    # ── Smart routing (multi-broker) ──────────────────────────────────────
    from config import BROKER_MODE
    smart_router = None
    if BROKER_MODE == 'smart':
        try:
            from monitor.tradier_broker import TradierBroker
            from monitor.smart_router import SmartRouter

            tradier_token = os.getenv('TRADIER_SANDBOX_TOKEN', '') or os.getenv('TRADIER_TRADING_TOKEN', '')
            tradier_account = os.getenv('TRADIER_ACCOUNT_ID', '')
            tradier_sandbox = os.getenv('TRADIER_SANDBOX', 'true').lower() == 'true'

            if tradier_token and tradier_account:
                tradier_broker = TradierBroker(
                    bus=monitor._bus,
                    token=tradier_token,
                    account_id=tradier_account,
                    sandbox=tradier_sandbox,
                    subscribe=False,
                )
                alpaca_broker = getattr(monitor, '_broker', None)

                smart_router = SmartRouter(
                    bus=monitor._bus,
                    alpaca_broker=alpaca_broker,
                    tradier_broker=tradier_broker,
                    default_broker='alpaca',
                )
                # V8: Share positions ref so SmartRouter can read _broker_qty for split sells
                smart_router._positions_ref = monitor.positions
                log.info("SmartRouter active | brokers=alpaca+tradier | mode=%s", BROKER_MODE)

                # V7.2: Seed from reconciliation results
                try:
                    alpaca_tickers = getattr(monitor, '_reconciled_alpaca', set()) or set()
                    tradier_tickers = getattr(monitor, '_reconciled_tradier', set()) or set()
                    overlap = alpaca_tickers & tradier_tickers
                    if overlap:
                        log.error("DUAL-BROKER OVERLAP: %s at both brokers", overlap)
                    smart_router.seed_position_broker(alpaca_tickers, tradier_tickers)
                except Exception as seed_exc:
                    log.warning("SmartRouter position seed failed: %s", seed_exc)
                # V9: Register Tradier in BrokerRegistry
                if broker_registry and tradier_broker:
                    broker_registry.register('tradier', tradier_broker)
            else:
                log.warning("BROKER_MODE=smart but Tradier credentials missing")
        except Exception as exc:
            log.warning("SmartRouter init failed: %s — using Alpaca only", exc)
    else:
        log.info("BROKER_MODE=%s — single broker mode", BROKER_MODE)

    if smart_router:
        smart_router.set_registry_gate(registry_gate)

    # ── V8: ProSetupEngine (merged in-process) ────────────────────────────
    # Pro's 11 strategies subscribe to BAR events directly on Core's bus.
    # No IPC needed — same process, same EventBus.
    pro_engine = None
    try:
        from pro_setups.engine import ProSetupEngine
        pro_engine = ProSetupEngine(
            bus=monitor._bus,
            max_positions=PRO_MAX_POSITIONS,
            order_cooldown=PRO_ORDER_COOLDOWN,
            trade_budget=float(PRO_TRADE_BUDGET),
            shared_positions=monitor.positions,  # V8: cross-engine sector counting
        )
        log.info("V9 ProSetupEngine merged into Core | max_pos=%d budget=$%.0f",
                 PRO_MAX_POSITIONS, PRO_TRADE_BUDGET)

        # Seed RiskAdapter._positions from restored state
        # (Pro positions in bot_state.json have strategy='pro:...')
        risk_adapter = pro_engine._risk_adapter
        for ticker, pos in monitor.positions.items():
            if str(pos.get('strategy', '')).startswith('pro:'):
                risk_adapter._positions.add(ticker)
        if risk_adapter._positions:
            log.info("V9 RiskAdapter seeded with %d restored Pro positions: %s",
                     len(risk_adapter._positions), sorted(risk_adapter._positions))
    except Exception as exc:
        log.error("V9 ProSetupEngine init failed: %s — Pro strategies disabled", exc)

    # ── IPC: Publish SIGNAL events to Redpanda for Options ────────────────
    def _on_signal_publish(event):
        if event.type == EventType.SIGNAL:
            p = event.payload
            publisher.publish(TOPIC_SIGNALS, p.ticker, {
                'ticker': p.ticker,
                'action': str(p.action),
                'current_price': float(p.current_price),
                'rsi_value': float(p.rsi_value),
                'rvol': float(p.rvol),
                'atr_value': float(p.atr_value),
                'stop_price': float(getattr(p, 'stop_price', 0)),
                'target_price': float(getattr(p, 'target_price', 0)),
            })

    # V8: Publish POP_SIGNAL to Redpanda for Options (Pop scanner in Core now)
    def _on_pop_signal_publish(event):
        try:
            p = event.payload
            publisher.publish(TOPIC_POP, p.symbol, {
                'symbol': p.symbol,
                'strategy_type': str(p.strategy_type),
                'entry_price': float(p.entry_price),
                'stop_price': float(p.stop_price),
                'target_1': float(p.target_1),
                'target_2': float(p.target_2),
                'pop_reason': str(p.pop_reason),
                'atr_value': float(p.atr_value),
                'rvol': float(p.rvol),
                'vwap_distance': float(getattr(p, 'vwap_distance', 0)),
                'strategy_confidence': float(getattr(p, 'strategy_confidence', 0)),
                'features_json': getattr(p, 'features_json', '{}'),
                'source': 'core',
                # V10: Edge context
                'timeframe': getattr(p, 'timeframe', '1min'),
                'regime_at_entry': getattr(p, 'regime_at_entry', ''),
                'time_bucket': getattr(p, 'time_bucket', ''),
            })
        except Exception as exc:
            log.warning("[IPC] POP_SIGNAL publish failed: %s", exc)

    monitor._bus.subscribe(EventType.SIGNAL, _on_signal_publish, priority=10)
    try:
        monitor._bus.subscribe(EventType.POP_SIGNAL, _on_pop_signal_publish, priority=10)
    except Exception:
        pass  # POP_SIGNAL event type may not exist

    # V10: Forward PRO_STRATEGY_SIGNAL to Redpanda for Options.
    # Previously only SIGNAL (VWAP) and POP_SIGNAL were forwarded —
    # Options never received the 40+ daily pro signals.
    def _on_pro_signal_publish(event):
        try:
            p = event.payload
            publisher.publish(TOPIC_SIGNALS, p.ticker, {
                'ticker': p.ticker,
                'action': 'BUY',
                'current_price': float(p.entry_price),
                'rsi_value': float(p.rsi_value),
                'rvol': float(p.rvol),
                'atr_value': float(p.atr_value),
                'stop_price': float(p.stop_price),
                'target_price': float(p.target_2),
                'source': f'pro:{p.strategy_name}',
            })
        except Exception as exc:
            log.warning("[IPC] PRO_STRATEGY_SIGNAL publish failed: %s", exc)

    try:
        monitor._bus.subscribe(EventType.PRO_STRATEGY_SIGNAL, _on_pro_signal_publish, priority=10)
    except Exception:
        pass

    # ── V8: Discovery ticker consumer (single, replaces Pro + Pop) ────────
    _ticker_set = set(monitor.tickers)

    # V9: Restore discovered tickers from alt_data_cache (survive restart)
    try:
        import json as _json
        _cache_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   'data', 'alt_data_cache.json')
        if os.path.exists(_cache_path):
            with open(_cache_path) as _f:
                _cached = _json.load(_f)
            _restored = _cached.get('discovered_tickers', [])
            _added = 0
            for t in _restored:
                if t and t not in _ticker_set:
                    monitor.tickers.append(t)
                    _ticker_set.add(t)
                    _added += 1
            if _added:
                log.info("[Discovery] Restored %d discovered tickers from cache (%d total)",
                         _added, len(monitor.tickers))
    except Exception as exc:
        log.warning("[Discovery] Cache restore failed (non-fatal): %s", exc)

    def _on_discovery(key, payload):
        ticker = payload.get('ticker', '')
        if ticker and ticker not in _ticker_set:
            monitor.tickers.append(ticker)
            _ticker_set.add(ticker)
            log.info("[Discovery] Added %s to scan universe (%d total)",
                     ticker, len(monitor.tickers))

    discovery_consumer = None
    try:
        discovery_consumer = EventConsumer(
            group_id='core-discovery-consumer',
            topics=[TOPIC_DISCOVERY],
            source_name='core',
        )
        discovery_consumer.on(TOPIC_DISCOVERY, _on_discovery)
        discovery_consumer.start()
        log.info("Discovery consumer started")
    except Exception as exc:
        log.warning("Discovery consumer failed (non-fatal): %s", exc)

    # ── V9 (L1): Tradier WebSocket streaming (PRODUCTION token for data) ──
    tradier_stream = None
    try:
        tradier_prod_token = os.getenv('TRADIER_TOKEN', '')
        if tradier_prod_token:
            from monitor.tradier_stream import TradierStreamClient
            tradier_stream = TradierStreamClient(
                token=tradier_prod_token, bus=monitor._bus,
            )
            # Subscribe to tickers with open positions (HOT tickers)
            hot_tickers = set(monitor.positions.keys()) if monitor.positions else set()
            if tradier_stream.start(initial_tickers=hot_tickers):
                monitor._stream_active = True
                # V9: Wire streaming prices into RiskEngine for <1ms spread checks
                monitor.set_stream_client(tradier_stream)
                # V9: Wire streaming cvol into RVOLEngine for real-time RVOL
                if rvol_engine:
                    rvol_engine.set_stream_client(tradier_stream)

                # V10: Wire BarBuilder for sub-second BAR delivery.
                # Starts with emit_bars=False. After monitor's first REST cycle
                # populates bars_cache, monitor.seed_from_cache() seeds BarBuilder
                # with full day history → auto-enables emit_bars=True.
                # REST BAR emission is then skipped for tickers BarBuilder covers.
                # Edge cases:
                #   - Crash/restart: BarBuilder starts empty → reseeded on first cycle
                #   - WebSocket disconnect: covers_ticker() returns False → REST resumes
                #   - Illiquid tickers: no ticks → covers_ticker() False → REST fallback
                #   - Manual kill+rerun: same as crash — clean slate, reseed from REST
                try:
                    from monitor.bar_builder import BarBuilder
                    bar_builder = BarBuilder(bus=monitor._bus, max_history=200, emit_bars=False)
                    if rvol_engine and hasattr(rvol_engine, '_baselines'):
                        bar_builder.set_rvol_baselines(rvol_engine._baselines or {})
                    tradier_stream.set_bar_builder(bar_builder)
                    bar_builder.start()
                    # Wire to monitor for automatic seeding after first REST fetch
                    monitor.set_bar_builder(bar_builder)
                    log.info("BarBuilder active — will seed from REST on first cycle "
                             "then emit sub-second BARs")
                except Exception as bb_exc:
                    log.warning("BarBuilder init failed (non-fatal): %s", bb_exc)

                log.info("Tradier streaming active | HOT tickers=%d", len(hot_tickers))
            else:
                log.warning("Tradier streaming unavailable — using REST polling fallback")
    except Exception as exc:
        log.warning("Tradier streaming init failed (non-fatal): %s", exc)

    # ── Start monitor ─────────────────────────────────────────────────────
    monitor.start()

    # V10: Clean stale FillLedger lots after reconciliation.
    # Reconciliation updated bot_state.positions to match broker reality.
    # FillLedger may have open lots for positions that were closed at the
    # broker while we were down. Close them now.
    if fill_ledger:
        try:
            active = set(monitor.positions.keys())
            closed = fill_ledger.close_stale_lots(active)
            if closed:
                log.info("[startup] Closed %d stale FillLedger lots (positions closed at broker)", closed)
        except Exception as exc:
            log.warning("[startup] FillLedger stale lot cleanup failed: %s", exc)

    # ── V9 Component Status Banner ────────────────────────────────────────
    # V10: Wait up to 5s for streaming WebSocket handshake before printing banner.
    # 1s was too short — caused [OFF] when streaming was actually connecting.
    if tradier_stream:
        import time as _t
        for _ in range(10):
            if getattr(tradier_stream, 'is_connected', False):
                break
            _t.sleep(0.5)


    _v9_status = {
        'FillLedger (shadow)':     'ON' if fill_ledger else 'OFF',
        'BrokerRegistry':          f'ON ({len(broker_registry)} brokers)' if broker_registry else 'OFF',
        'EquityTracker':           'ON' if getattr(monitor, '_equity_tracker', None) else 'OFF',
        'Tradier Streaming':       'ON' if tradier_stream and getattr(tradier_stream, '_running', False) else 'OFF (REST fallback)',
        'Streaming → RiskEngine':  'ON' if getattr(getattr(monitor, '_risk', None), '_stream_client', None) else 'OFF',
        'Buying Power Background': 'ON' if getattr(portfolio_risk, '_bp_refresher', None) else 'OFF (inline)',
        'Data Failover':           'ON' if hasattr(monitor._data, '_secondary') else 'OFF',
        'Detector Cascade (L4)':   'ON',
        'Bar Validation (R1)':     'ON',
        'Kill Switch Persist':     'ON',
        'Cache Format':            'ON (JSON)' if getattr(cache_writer, '_path', '').endswith('.json') else 'ON (Pickle)',
        'Dual-Freq Polling (L2)':  'ON (STANDBY — streaming active)' if tradier_stream else 'ON (12s HOT polling)',
    }

    _version = 'V9' if any(v.startswith('ON') for v in _v9_status.values()) else 'V8 (legacy)'
    _active_count = sum(1 for v in _v9_status.values() if v.startswith('ON'))

    log.info("=" * 60)
    log.info("SYSTEM VERSION: %s (%d/%d V9 components active)",
             _version, _active_count, len(_v9_status))
    log.info("-" * 60)
    for component, status in _v9_status.items():
        icon = 'ON ' if status.startswith('ON') else 'OFF'
        log.info("  [%s] %s: %s", icon, component, status)
    log.info("-" * 60)
    log.info("Tickers: %d | Positions: %d | Strategies: VWAP + Pro",
             len(monitor.tickers), len(monitor.positions))
    log.info("=" * 60)

    # Write initial cache (Options reads from this)
    if hasattr(monitor, '_bars_cache') and hasattr(monitor, '_rvol_cache'):
        cache_writer.write(monitor._bars_cache or {}, monitor._rvol_cache or {})

    # ── V10: WAL Recovery — resolve incomplete orders from prior session ──
    try:
        from monitor.order_wal import wal as order_wal
        incomplete = order_wal.get_incomplete_orders()
        if incomplete:
            log.warning("[WAL] Found %d incomplete orders from prior session", len(incomplete))
            alpaca_broker = getattr(monitor, '_broker', None)
            for order in incomplete:
                cid = order.get('client_id', '')
                state = order.get('state', '')
                ticker = order.get('ticker', '?')
                broker_oid = order.get('broker_order_id', '')
                client_oid = order.get('client_order_id', '')

                if state == 'INTENT':
                    # Never submitted — safe to ignore
                    log.info("[WAL] %s INTENT only (never submitted) — marking cancelled", ticker)
                    order_wal.cancelled(cid, reason='never_submitted_recovery')
                    continue

                if state in ('SUBMITTED', 'ACKED', 'FILLED'):
                    # Order may be live at broker — check
                    log.warning("[WAL] %s was %s — checking broker", ticker, state)
                    resolved = False

                    # Try Alpaca first (has client_order_id lookup)
                    if alpaca_broker and client_oid:
                        try:
                            broker_order = alpaca_broker._client.get_order_by_client_id(client_oid)
                            b_status = str(getattr(broker_order, 'status', ''))
                            if b_status == 'filled':
                                fill_price = float(broker_order.filled_avg_price or 0)
                                fill_qty = float(broker_order.filled_qty or 0)
                                log.warning("[WAL] %s FILLED at Alpaca: %s qty @ $%.2f — "
                                           "reconciliation will import", ticker, fill_qty, fill_price)
                                order_wal.filled(cid, fill_price=fill_price, fill_qty=fill_qty,
                                                broker_order_id=str(broker_order.id))
                                # Don't create FillLot here — reconciliation handles it
                                order_wal.recorded(cid, lot_id='reconciliation_pending')
                                resolved = True
                            elif b_status in ('cancelled', 'expired', 'rejected'):
                                log.info("[WAL] %s %s at Alpaca — marking cancelled", ticker, b_status)
                                order_wal.cancelled(cid, reason=f'broker_{b_status}')
                                resolved = True
                            elif b_status in ('new', 'accepted', 'pending_new'):
                                log.warning("[WAL] %s still OPEN at Alpaca — cancelling stale order", ticker)
                                try:
                                    alpaca_broker._client.cancel_order_by_id(str(broker_order.id))
                                except Exception:
                                    pass
                                order_wal.cancelled(cid, reason='stale_on_recovery')
                                resolved = True
                        except Exception as exc:
                            log.warning("[WAL] Alpaca lookup for %s failed: %s", ticker, exc)

                    if not resolved and broker_oid:
                        # Try Tradier by order_id
                        try:
                            if broker_registry and 'tradier' in broker_registry:
                                tradier = broker_registry.get('tradier')
                                if tradier:
                                    resp = tradier._session.get(
                                        f"{tradier._base}/v1/accounts/{tradier._account}/orders/{broker_oid}",
                                        headers={'Authorization': f'Bearer {tradier._token}',
                                                 'Accept': 'application/json'},
                                        timeout=5,
                                    )
                                    if resp.ok:
                                        t_order = resp.json().get('order', {})
                                        t_status = t_order.get('status', '')
                                        if t_status == 'filled':
                                            log.warning("[WAL] %s FILLED at Tradier — reconciliation will import", ticker)
                                            order_wal.recorded(cid, lot_id='reconciliation_pending')
                                            resolved = True
                                        elif t_status in ('canceled', 'expired', 'rejected'):
                                            order_wal.cancelled(cid, reason=f'tradier_{t_status}')
                                            resolved = True
                        except Exception as exc:
                            log.warning("[WAL] Tradier lookup for %s failed: %s", ticker, exc)

                    if not resolved:
                        log.warning("[WAL] %s (%s) could not be resolved — marking failed", ticker, state)
                        order_wal.failed(cid, reason='unresolved_on_recovery')
        else:
            log.info("[WAL] No incomplete orders from prior session")
    except Exception as exc:
        log.warning("[WAL] Recovery failed (non-fatal): %s", exc)

    # ── Main loop ─────────────────────────────────────────────────────────
    try:
        while True:
            now = datetime.now(ET)
            if now.hour >= 16:
                log.info("4:00 PM ET reached — stopping core process.")
                break

            # Update shared cache (Options reads from this)
            if hasattr(monitor, '_bars_cache') and monitor._bars_cache:
                try:
                    cache_writer.write(monitor._bars_cache, monitor._rvol_cache or {})
                except RuntimeError:
                    pass  # dict changed size during iteration — retry next cycle

            time.sleep(10)
    except KeyboardInterrupt:
        log.info("Core process interrupted.")
    finally:
        # V9: Stop streaming first (clean WebSocket disconnect)
        if tradier_stream:
            try:
                tradier_stream.stop()
            except Exception:
                pass
        monitor.stop()
        if discovery_consumer:
            discovery_consumer.stop()
        publisher.stop()
        if db_cleanup:
            db_cleanup()
        log.info("V9 Core process stopped.")


if __name__ == '__main__':
    main()
