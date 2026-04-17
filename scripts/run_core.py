#!/usr/bin/env python3
"""
V8 Core process — VWAP + Pro strategies + Pop scanning + broker + position management.

Pro and Pop merged into Core (V8). Options stays separate (different instruments).
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
    log.info("V8 CORE PROCESS STARTING (VWAP + Pro + Pop scan)")
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
            # Get open orders
            resp = _req.get(f'{t_base}/v1/accounts/{t_acct}/orders',
                           params={'filter': 'open'}, headers=t_headers, timeout=5)
            if resp.status_code == 200:
                orders = resp.json().get('orders', {})
                if orders and orders != 'null':
                    order_list = orders.get('order', [])
                    if isinstance(order_list, dict):
                        order_list = [order_list]
                    for o in order_list:
                        oid = o.get('id')
                        if oid:
                            _req.delete(f'{t_base}/v1/accounts/{t_acct}/orders/{oid}',
                                       headers=t_headers, timeout=5)
                            log.info("[Startup] Cancelled stale Tradier order: %s", oid)
    except Exception as exc:
        log.warning("[Startup] Stale Tradier order cancel failed: %s", exc)

    # ── DB persistence ────────────────────────────────────────────────────
    from scripts._db_helper import init_satellite_db
    db_cleanup = init_satellite_db(monitor._bus, process_name='core')

    # ── Portfolio-level risk gate ─────────────────────────────────────────
    from monitor.portfolio_risk import PortfolioRiskGate
    portfolio_risk = PortfolioRiskGate(bus=monitor._bus, monitor=monitor)
    log.info("PortfolioRiskGate active")
    portfolio_risk.reset_day()

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
        log.info("V8 ProSetupEngine merged into Core | max_pos=%d budget=$%.0f",
                 PRO_MAX_POSITIONS, PRO_TRADE_BUDGET)

        # Seed RiskAdapter._positions from restored state
        # (Pro positions in bot_state.json have strategy='pro:...')
        risk_adapter = pro_engine._risk_adapter
        for ticker, pos in monitor.positions.items():
            if str(pos.get('strategy', '')).startswith('pro:'):
                risk_adapter._positions.add(ticker)
        if risk_adapter._positions:
            log.info("V8 RiskAdapter seeded with %d restored Pro positions: %s",
                     len(risk_adapter._positions), sorted(risk_adapter._positions))
    except Exception as exc:
        log.error("V8 ProSetupEngine init failed: %s — Pro strategies disabled", exc)

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
            })
        except Exception as exc:
            log.debug("[IPC] POP_SIGNAL publish failed: %s", exc)

    monitor._bus.subscribe(EventType.SIGNAL, _on_signal_publish, priority=10)
    try:
        monitor._bus.subscribe(EventType.POP_SIGNAL, _on_pop_signal_publish, priority=10)
    except Exception:
        pass  # POP_SIGNAL event type may not exist

    # ── V8: Discovery ticker consumer (single, replaces Pro + Pop) ────────
    _ticker_set = set(monitor.tickers)

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

    # ── Start monitor ─────────────────────────────────────────────────────
    monitor.start()
    log.info("V8 Core running | VWAP + Pro strategies active | %d tickers",
             len(monitor.tickers))

    # Write initial cache (Options reads from this)
    if hasattr(monitor, '_bars_cache') and hasattr(monitor, '_rvol_cache'):
        cache_writer.write(monitor._bars_cache or {}, monitor._rvol_cache or {})

    # ── Main loop ─────────────────────────────────────────────────────────
    try:
        while True:
            now = datetime.now(ET)
            if now.hour >= 16:
                log.info("4:00 PM ET reached — stopping core process.")
                break

            # Update shared cache (Options reads from this)
            if hasattr(monitor, '_bars_cache') and monitor._bars_cache:
                cache_writer.write(monitor._bars_cache, monitor._rvol_cache or {})

            time.sleep(10)
    except KeyboardInterrupt:
        log.info("Core process interrupted.")
    finally:
        monitor.stop()
        if discovery_consumer:
            discovery_consumer.stop()
        publisher.stop()
        if db_cleanup:
            db_cleanup()
        log.info("V8 Core process stopped.")


if __name__ == '__main__':
    main()
