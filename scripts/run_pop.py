#!/usr/bin/env python3
"""Pop strategy process — momentum screener + Benzinga/StockTwits.

Reads market data from shared cache, fetches news/social data,
runs pop screener, publishes POP_SIGNAL + ORDER_REQ to Redpanda.
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
    TICKERS, ALPACA_POPUP_KEY, ALPACA_PUPUP_SECRET_KEY,
    POP_PAPER_TRADING, POP_MAX_POSITIONS, POP_TRADE_BUDGET, POP_ORDER_COOLDOWN,
    ALERT_EMAIL, BENZINGA_API_KEY, STOCKTWITS_TOKEN, GLOBAL_MAX_POSITIONS,
)

log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"pop_{datetime.now().strftime('%Y-%m-%d')}.log")

logging.root.handlers = []
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [pop] %(message)s',
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
    from monitor.ipc import EventPublisher, TOPIC_ORDERS, TOPIC_POP
    from monitor.distributed_registry import DistributedPositionRegistry
    from pop_strategy_engine import PopStrategyEngine

    log.info("=" * 60)
    log.info("POP STRATEGY PROCESS STARTING")
    log.info("=" * 60)

    bus = EventBus()
    publisher = EventPublisher(source_name='pop')
    cache_reader = CacheReader()
    dist_registry = DistributedPositionRegistry(global_max=GLOBAL_MAX_POSITIONS)

    # DB persistence (POP_SIGNAL, NEWS_DATA, SOCIAL_DATA → event_store)
    from scripts._db_helper import init_satellite_db
    db_cleanup = init_satellite_db(bus, process_name='pop')

    # News + social sources
    news_source = None
    if BENZINGA_API_KEY:
        from pop_screener.benzinga_news import BenzingaNewsSentimentSource
        news_source = BenzingaNewsSentimentSource(api_key=BENZINGA_API_KEY)
        log.info("Benzinga news adapter loaded")

    from pop_screener.stocktwits_social import StockTwitsSocialSource
    social_source = StockTwitsSocialSource(access_token=STOCKTWITS_TOKEN or None)
    log.info("StockTwits social adapter loaded")

    # Pop engine
    pop_engine = PopStrategyEngine(
        bus=bus,
        pop_alpaca_key=ALPACA_POPUP_KEY,
        pop_alpaca_secret=ALPACA_PUPUP_SECRET_KEY,
        pop_paper=POP_PAPER_TRADING,
        pop_max_positions=POP_MAX_POSITIONS,
        pop_trade_budget=float(POP_TRADE_BUDGET),
        pop_order_cooldown=POP_ORDER_COOLDOWN,
        alert_email=ALERT_EMAIL,
        news_source=news_source,
        social_source=social_source,
    )
    log.info("PopStrategyEngine ready | paper=%s | max_pos=%d | budget=$%d",
             POP_PAPER_TRADING, POP_MAX_POSITIONS, POP_TRADE_BUDGET)

    # Forward POP_SIGNAL to Redpanda (for Options process)
    def _forward_pop_signal(event):
        if event.type == EventType.POP_SIGNAL:
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
                'source': 'pop',
            })

    bus.subscribe(EventType.POP_SIGNAL, _forward_pop_signal, priority=10)

    log.info("Pop process running.")
    try:
        while True:
            now = datetime.now(ET)
            if now.hour >= 16:
                log.info("4:00 PM ET — stopping pop process.")
                break

            bars_cache, rvol_cache = cache_reader.get_bars()
            if not bars_cache:
                time.sleep(2)
                continue

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

            time.sleep(10)

    except KeyboardInterrupt:
        log.info("Pop process interrupted.")
    finally:
        bus.stop()
        publisher.stop()
        if db_cleanup:
            db_cleanup()
        log.info("Pop process stopped.")


if __name__ == '__main__':
    main()
