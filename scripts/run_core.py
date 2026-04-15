#!/usr/bin/env python3
"""Core process — VWAP strategy + broker + position management.

This is the primary process. It fetches market data, runs the VWAP strategy,
executes orders via Alpaca, and manages positions. Other engines (Pro, Pop, Options)
run as separate processes and communicate via Redpanda.
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
)

# Logging
log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"core_{datetime.now().strftime('%Y-%m-%d')}.log")

# Only FileHandler — supervisor already redirects stdout to this log file
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
    from monitor.ipc import EventPublisher, EventConsumer, TOPIC_ORDERS, TOPIC_FILLS, TOPIC_SIGNALS, TOPIC_BAR_READY
    from monitor.distributed_registry import DistributedPositionRegistry
    from monitor.event_bus import EventType, Event

    log.info("=" * 60)
    log.info("CORE PROCESS STARTING")
    log.info("=" * 60)

    # Initialize distributed registry (replaces in-memory singleton)
    dist_registry = DistributedPositionRegistry(global_max=GLOBAL_MAX_POSITIONS)
    dist_registry.reset()  # Clean slate at session start
    log.info("Distributed position registry initialized (max=%d)", GLOBAL_MAX_POSITIONS)

    # Initialize shared cache writer
    cache_writer = CacheWriter()
    log.info("Shared cache writer ready")

    # Initialize IPC
    publisher = EventPublisher(source_name='core')
    log.info("IPC publisher ready")

    # Initialize monitor (same as run_monitor.py but without Pro/Pop/Options)
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

    # ── DB persistence for core events ─────────────────────────────────────
    from scripts._db_helper import init_satellite_db
    db_cleanup = init_satellite_db(monitor._bus, process_name='core')

    # ── Portfolio-level risk gate (aggregate limits across all engines) ─────
    from monitor.portfolio_risk import PortfolioRiskGate
    portfolio_risk = PortfolioRiskGate(bus=monitor._bus, monitor=monitor)
    log.info("PortfolioRiskGate active | drawdown=$%.0f | notional=$%.0f | delta=%.1f | gamma=%.1f",
             portfolio_risk._max_drawdown if hasattr(portfolio_risk, '_max_drawdown') else -5000,
             100000, 5.0, 1.0)
    portfolio_risk.reset_day()

    # Hook: publish SIGNAL events to Redpanda for Options process
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

    def _on_fill_publish(event):
        if event.type in (EventType.FILL, EventType.POSITION):
            p = event.payload
            ticker = getattr(p, 'ticker', '')
            publisher.publish(TOPIC_FILLS, ticker, {
                'event_type': event.type.name,
                'ticker': ticker,
                'payload': str(p),
            })

    monitor._bus.subscribe(EventType.SIGNAL, _on_signal_publish, priority=10)
    monitor._bus.subscribe(EventType.FILL, _on_fill_publish, priority=10)
    monitor._bus.subscribe(EventType.POSITION, _on_fill_publish, priority=10)

    # Consumer: ORDER_REQ from satellite processes
    consumer = EventConsumer(
        group_id='core-order-consumer',
        topics=[TOPIC_ORDERS],
        source_name='core',
    )

    def _on_remote_order(key, payload):
        """Receive ORDER_REQ from satellite processes and route to broker."""
        log.info("[IPC] Received ORDER_REQ from satellite: %s %s",
                 payload.get('ticker', '?'), payload.get('side', '?'))
        # Inject into local EventBus as an ORDER_REQ event
        from monitor.events import OrderRequestPayload
        try:
            order_payload = OrderRequestPayload(
                ticker=payload['ticker'],
                side=payload['side'],
                qty=int(payload['qty']),
                price=float(payload['price']),
                reason=payload.get('reason', 'remote'),
                stop_price=float(payload.get('stop_price', 0)),
                target_price=float(payload.get('target_price', 0)),
            )
            monitor._bus.emit(Event(type=EventType.ORDER_REQ, payload=order_payload))
        except Exception as exc:
            log.warning("[IPC] Failed to process remote ORDER_REQ: %s", exc)

    consumer.on(TOPIC_ORDERS, _on_remote_order)
    consumer.start()
    log.info("IPC consumer started (listening for satellite ORDER_REQ)")

    # Start monitor
    monitor.start()
    log.info("Core monitor running. VWAP strategy active.")

    # Write initial cache
    if hasattr(monitor, '_bars_cache') and hasattr(monitor, '_rvol_cache'):
        cache_writer.write(monitor._bars_cache or {}, monitor._rvol_cache or {})
        publisher.publish(TOPIC_BAR_READY, 'all', {'count': len(monitor._bars_cache or {})})

    # Main loop (same heartbeat as run_monitor.py)
    try:
        while True:
            now = datetime.now(ET)
            if now.hour >= 16:
                log.info("4:00 PM ET reached — stopping core process.")
                break

            # Update shared cache after each cycle
            if hasattr(monitor, '_bars_cache') and monitor._bars_cache:
                cache_writer.write(monitor._bars_cache, monitor._rvol_cache or {})
                publisher.publish(TOPIC_BAR_READY, 'all', {
                    'count': len(monitor._bars_cache),
                    'time': now.isoformat(),
                })

            time.sleep(10)
    except KeyboardInterrupt:
        log.info("Core process interrupted.")
    finally:
        monitor.stop()
        consumer.stop()
        publisher.stop()
        if db_cleanup:
            db_cleanup()
        log.info("Core process stopped.")


if __name__ == '__main__':
    main()
