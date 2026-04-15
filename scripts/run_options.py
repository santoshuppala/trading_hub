#!/usr/bin/env python3
"""Options process — multi-leg options trading on dedicated Alpaca account.

Consumes SIGNAL and POP_SIGNAL from Redpanda, runs OptionsEngine,
executes via its own AlpacaOptionsBroker.
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
    TICKERS, ALPACA_OPTIONS_KEY, ALPACA_OPTIONS_SECRET,
    OPTIONS_PAPER_TRADING, OPTIONS_MAX_POSITIONS, OPTIONS_TRADE_BUDGET,
    OPTIONS_TOTAL_BUDGET, OPTIONS_ORDER_COOLDOWN,
    OPTIONS_MIN_DTE, OPTIONS_MAX_DTE, OPTIONS_LEAPS_DTE,
    ALERT_EMAIL, GLOBAL_MAX_POSITIONS,
)

log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"options_{datetime.now().strftime('%Y-%m-%d')}.log")

logging.root.handlers = []
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [options] %(message)s',
    handlers=[logging.FileHandler(log_file)],
)
log = logging.getLogger(__name__)


def main():
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import BarPayload
    from monitor.shared_cache import CacheReader
    from monitor.ipc import EventConsumer, TOPIC_SIGNALS, TOPIC_POP, TOPIC_FILLS
    from monitor.distributed_registry import DistributedPositionRegistry
    from options.engine import OptionsEngine

    log.info("=" * 60)
    log.info("OPTIONS PROCESS STARTING")
    log.info("=" * 60)

    bus = EventBus()
    cache_reader = CacheReader()
    dist_registry = DistributedPositionRegistry(global_max=GLOBAL_MAX_POSITIONS)

    # DB persistence (OPTIONS_SIGNAL → event_store)
    from scripts._db_helper import init_satellite_db
    db_cleanup = init_satellite_db(bus, process_name='options')

    # Options engine (subscribes to local bus for BAR, SIGNAL, POP_SIGNAL)
    options_engine = OptionsEngine(
        bus=bus,
        options_key=ALPACA_OPTIONS_KEY,
        options_secret=ALPACA_OPTIONS_SECRET,
        paper=OPTIONS_PAPER_TRADING,
        max_positions=OPTIONS_MAX_POSITIONS,
        trade_budget=float(OPTIONS_TRADE_BUDGET),
        total_budget=float(OPTIONS_TOTAL_BUDGET),
        order_cooldown=OPTIONS_ORDER_COOLDOWN,
        min_dte=OPTIONS_MIN_DTE,
        max_dte=OPTIONS_MAX_DTE,
        leaps_dte=OPTIONS_LEAPS_DTE,
        alert_email=ALERT_EMAIL,
    )
    log.info("OptionsEngine ready | paper=%s | max_pos=%d | budget=$%d",
             OPTIONS_PAPER_TRADING, OPTIONS_MAX_POSITIONS, OPTIONS_TRADE_BUDGET)

    # IPC consumer: receive SIGNAL and POP_SIGNAL from other processes
    consumer = EventConsumer(
        group_id='options-signal-consumer',
        topics=[TOPIC_SIGNALS, TOPIC_POP],
        source_name='options',
    )

    def _on_remote_signal(key, payload):
        """Receive SIGNAL from core VWAP process → inject into local bus."""
        from monitor.events import SignalPayload, SignalAction
        try:
            action = payload.get('action', '')
            signal_payload = SignalPayload(
                ticker=payload['ticker'],
                action=SignalAction(action),
                current_price=float(payload.get('current_price', 0)),
                rsi_value=float(payload.get('rsi_value', 50)),
                rvol=float(payload.get('rvol', 1.0)),
                atr_value=float(payload.get('atr_value', 0)),
                stop_price=float(payload.get('stop_price', 0)),
                target_price=float(payload.get('target_price', 0)),
            )
            bus.emit(Event(type=EventType.SIGNAL, payload=signal_payload))
        except Exception as exc:
            log.debug("[IPC] Failed to process remote SIGNAL: %s", exc)

    def _on_remote_pop_signal(key, payload):
        """Receive POP_SIGNAL from pop process → inject into local bus."""
        from monitor.events import PopSignalPayload
        try:
            pop_payload = PopSignalPayload(
                symbol=payload['symbol'],
                strategy_type=payload['strategy_type'],
                entry_price=float(payload['entry_price']),
                stop_price=float(payload['stop_price']),
                target_1=float(payload['target_1']),
                target_2=float(payload['target_2']),
                pop_reason=payload.get('pop_reason', 'UNKNOWN'),
                atr_value=float(payload.get('atr_value', 0)),
                rvol=float(payload.get('rvol', 1.0)),
                vwap_distance=float(payload.get('vwap_distance', 0)),
                strategy_confidence=float(payload.get('strategy_confidence', 0)),
                features_json=payload.get('features_json', '{}'),
            )
            bus.emit(Event(type=EventType.POP_SIGNAL, payload=pop_payload))
        except Exception as exc:
            log.debug("[IPC] Failed to process remote POP_SIGNAL: %s", exc)

    consumer.on(TOPIC_SIGNALS, _on_remote_signal)
    consumer.on(TOPIC_POP, _on_remote_pop_signal)
    consumer.start()
    log.info("IPC consumer started (SIGNAL + POP_SIGNAL)")

    log.info("Options process running.")
    try:
        while True:
            now = datetime.now(ET)
            if now.hour >= 16:
                log.info("4:00 PM ET — stopping options process.")
                break

            # Emit BAR events from shared cache (for position monitoring)
            bars_cache, rvol_cache = cache_reader.get_bars()
            if bars_cache:
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
        log.info("Options process interrupted.")
    finally:
        bus.stop()
        consumer.stop()
        if db_cleanup:
            db_cleanup()
        log.info("Options process stopped.")


if __name__ == '__main__':
    main()
