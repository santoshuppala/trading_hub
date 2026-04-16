#!/usr/bin/env python3
"""Pro-setups process — 11 technical detectors + strategy classifier.

Reads market data from shared cache, runs detectors, publishes ORDER_REQ
to Redpanda for the core process to execute.
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
    TICKERS, PRO_MAX_POSITIONS, PRO_TRADE_BUDGET, PRO_ORDER_COOLDOWN,
    GLOBAL_MAX_POSITIONS,
)

log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
os.makedirs(log_dir, exist_ok=True)
date_dir = os.path.join(log_dir, datetime.now().strftime('%Y%m%d'))
os.makedirs(date_dir, exist_ok=True)
log_file = os.path.join(date_dir, 'pro.log')

logging.root.handlers = []
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [pro] %(message)s',
    handlers=[logging.FileHandler(log_file)],
)
log = logging.getLogger(__name__)


def main():
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import BarPayload
    from monitor.shared_cache import CacheReader
    from monitor.ipc import EventPublisher, EventConsumer, TOPIC_ORDERS, TOPIC_BAR_READY
    from monitor.distributed_registry import DistributedPositionRegistry
    from pro_setups.engine import ProSetupEngine

    log.info("=" * 60)
    log.info("PRO-SETUPS PROCESS STARTING")
    log.info("=" * 60)

    # Initialize RVOL engine so Pro detectors get real RVOL values
    try:
        from monitor.rvol import init_global_rvol_engine
        from monitor.data_client import make_data_client
        rvol_engine = init_global_rvol_engine()
        data_client = make_data_client(
            os.getenv('DATA_SOURCE', 'tradier'),
            tradier_token=os.getenv('TRADIER_TOKEN', ''),
            alpaca_api_key=os.getenv('APCA_API_KEY_ID', ''),
            alpaca_secret=os.getenv('APCA_API_SECRET_KEY', ''),
        )
        rvol_engine.seed_profiles(TICKERS, data_client)
        log.info("[RVOLEngine] seeded profiles for %d tickers in Pro process", len(TICKERS))
    except Exception as exc:
        log.warning("[RVOLEngine] init failed in Pro — RVOL will be approximate: %s", exc)

    # Local EventBus for this process
    bus = EventBus()

    # IPC
    publisher = EventPublisher(source_name='pro')
    cache_reader = CacheReader()
    dist_registry = DistributedPositionRegistry(global_max=GLOBAL_MAX_POSITIONS)

    # DB persistence (so PRO_STRATEGY_SIGNAL events are stored in event_store)
    from scripts._db_helper import init_satellite_db
    db_cleanup = init_satellite_db(bus, process_name='pro')

    # Pro-setups engine (subscribes to BAR on local bus)
    pro_engine = ProSetupEngine(
        bus=bus,
        max_positions=PRO_MAX_POSITIONS,
        order_cooldown=PRO_ORDER_COOLDOWN,
        trade_budget=float(PRO_TRADE_BUDGET),
    )
    log.info("ProSetupEngine ready | max_pos=%d | budget=$%d", PRO_MAX_POSITIONS, PRO_TRADE_BUDGET)

    # ── Lifecycle management ──────────────────────────────────────────
    from lifecycle import EngineLifecycle
    from lifecycle.adapters.pro_adapter import ProLifecycleAdapter
    from config import PRO_MAX_DAILY_LOSS, ALERT_EMAIL

    risk_adapter = getattr(pro_engine, '_risk_adapter', None)
    lifecycle = EngineLifecycle(
        engine_name='pro',
        adapter=ProLifecycleAdapter(pro_engine, risk_adapter),
        bus=bus,
        alert_email=ALERT_EMAIL,
        max_daily_loss=PRO_MAX_DAILY_LOSS,
    )
    lifecycle.startup()

    # Intercept ORDER_REQ from local bus → publish to Redpanda
    def _forward_order(event):
        if event.type == EventType.ORDER_REQ:
            p = event.payload
            publisher.publish(TOPIC_ORDERS, p.ticker, {
                'ticker': p.ticker,
                'side': str(p.side),
                'qty': p.qty,
                'price': float(p.price),
                'reason': str(getattr(p, 'reason', 'pro_setup')),
                'stop_price': float(getattr(p, 'stop_price', 0)),
                'target_price': float(getattr(p, 'target_price', 0)),
                'source': 'pro',
                'layer': 'pro',
            }, correlation_id=event.event_id)  # V7 P3-1: cross-process tracing
            log.info("[IPC] Published ORDER_REQ: %s %s qty=%d", p.ticker, p.side, p.qty)

    bus.subscribe(EventType.ORDER_REQ, _forward_order, priority=0)

    # Main loop: read shared cache → emit BAR events on local bus
    log.info("Pro process running. Reading bars from shared cache.")
    try:
        while True:
            now = datetime.now(ET)
            if now.hour >= 16:
                log.info("4:00 PM ET — stopping pro process.")
                break

            # Read fresh bars from shared cache
            bars_cache, rvol_cache = cache_reader.get_bars()
            if not bars_cache:
                time.sleep(2)
                continue

            # Emit BAR events for each ticker
            import pandas as pd
            bar_events = []
            for ticker in TICKERS:
                if ticker not in bars_cache:
                    continue
                df = bars_cache[ticker]
                if isinstance(df, dict):
                    df = pd.DataFrame(df)
                if df.empty:
                    continue
                df.name = ticker  # so compute_rvol can look up the ticker
                rvol_df = rvol_cache.get(ticker)
                if isinstance(rvol_df, dict):
                    rvol_df = pd.DataFrame(rvol_df)

                bar_events.append(Event(
                    type=EventType.BAR,
                    payload=BarPayload(ticker=ticker, df=df, rvol_df=rvol_df),
                ))

            if bar_events:
                bus.emit_batch(bar_events)

            # Lifecycle tick: heartbeat, kill switch, reconciliation, state save
            if not lifecycle.tick():
                log.error("Pro engine halted by kill switch.")
                break

            # Wait for next cache update
            time.sleep(10)

    except KeyboardInterrupt:
        log.info("Pro process interrupted.")
    finally:
        lifecycle.shutdown()
        publisher.stop()
        if db_cleanup:
            db_cleanup()
        log.info("Pro process stopped.")


if __name__ == '__main__':
    main()
