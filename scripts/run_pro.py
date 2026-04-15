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
log_file = os.path.join(log_dir, f"pro_{datetime.now().strftime('%Y-%m-%d')}.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [pro] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ],
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
            })
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
                rvol_df = rvol_cache.get(ticker)
                if isinstance(rvol_df, dict):
                    rvol_df = pd.DataFrame(rvol_df)

                bar_events.append(Event(
                    type=EventType.BAR,
                    payload=BarPayload(ticker=ticker, df=df, rvol_df=rvol_df),
                ))

            if bar_events:
                bus.emit_batch(bar_events)

            # Wait for next cache update
            time.sleep(10)

    except KeyboardInterrupt:
        log.info("Pro process interrupted.")
    finally:
        bus.stop()
        publisher.stop()
        if db_cleanup:
            db_cleanup()
        log.info("Pro process stopped.")


if __name__ == '__main__':
    main()
