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
date_dir = os.path.join(log_dir, datetime.now().strftime('%Y%m%d'))
os.makedirs(date_dir, exist_ok=True)
log_file = os.path.join(date_dir, 'options.log')

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

    # ── Lifecycle management ──────────────────────────────────────────
    from lifecycle import EngineLifecycle
    from lifecycle.adapters.options_adapter import OptionsLifecycleAdapter
    from config import OPTIONS_MAX_DAILY_LOSS

    lifecycle = EngineLifecycle(
        engine_name='options',
        adapter=OptionsLifecycleAdapter(options_engine),
        bus=bus,
        alert_email=ALERT_EMAIL,
        max_daily_loss=OPTIONS_MAX_DAILY_LOSS,
    )
    lifecycle.startup()

    # IPC consumer: receive SIGNAL and POP_SIGNAL from other processes
    consumer = EventConsumer(
        group_id='options-signal-consumer',
        topics=[TOPIC_SIGNALS, TOPIC_POP],
        source_name='options',
    )

    def _on_remote_signal(key, payload):
        """Receive SIGNAL from core (VWAP or Pro) → inject into local bus."""
        from monitor.events import SignalPayload, SignalAction
        try:
            action = payload.get('action', '')
            price = float(payload.get('current_price', 0))
            stop = float(payload.get('stop_price', 0))
            target = float(payload.get('target_price', 0))
            atr = float(payload.get('atr_value', 0))
            signal_payload = SignalPayload(
                ticker=payload['ticker'],
                action=SignalAction(action),
                current_price=price,
                ask_price=price,                     # required field
                rsi_value=float(payload.get('rsi_value', 50)),
                rvol=float(payload.get('rvol', 1.0)),
                atr_value=atr,
                vwap=price,                          # required field (approximate)
                stop_price=stop,
                target_price=target,
                half_target=(price + target) / 2 if target > price else price,  # required
                reclaim_candle_low=stop,              # required field (approximate)
            )
            bus.emit(Event(type=EventType.SIGNAL, payload=signal_payload))
            log.info("[IPC] Remote SIGNAL received: %s %s @ $%.2f (source=%s)",
                     payload['ticker'], action, price, payload.get('source', 'core'))
        except Exception as exc:
            log.warning("[IPC] Failed to process remote SIGNAL: %s", exc)

    # V7.1: Dedup POP_SIGNAL — Pop emits on local bus AND IPC, causing duplicates
    _pop_signal_seen = {}  # {(symbol, price): monotonic_time}

    def _on_remote_pop_signal(key, payload):
        """Receive POP_SIGNAL from pop process → inject into local bus (deduplicated)."""
        from monitor.events import PopSignalPayload

        # V8: Dedup: skip if same (symbol, entry_price) seen within 300 seconds
        symbol = payload.get('symbol', '')
        price = float(payload.get('entry_price', 0))
        dedup_key = (symbol, round(price, 2))
        now = time.monotonic()
        last_seen = _pop_signal_seen.get(dedup_key, 0)
        if now - last_seen < 300.0:
            return  # duplicate — skip silently
        _pop_signal_seen[dedup_key] = now

        # V8: Prune by age instead of dict.clear() (preserves recent entries)
        if len(_pop_signal_seen) > 500:
            cutoff = now - 600
            stale_keys = [k for k, ts in _pop_signal_seen.items() if ts < cutoff]
            for k in stale_keys:
                del _pop_signal_seen[k]

        try:
            pop_payload = PopSignalPayload(
                symbol=symbol,
                strategy_type=payload['strategy_type'],
                entry_price=price,
                stop_price=float(payload['stop_price']),
                target_1=float(payload['target_1']),
                target_2=float(payload['target_2']),
                pop_reason=payload.get('pop_reason', 'UNKNOWN'),
                atr_value=float(payload.get('atr_value', 0)),
                rvol=float(payload.get('rvol', 1.0)),
                vwap_distance=float(payload.get('vwap_distance', 0)),
                strategy_confidence=float(payload.get('strategy_confidence', 0)),
                features_json=payload.get('features_json', '{}'),
                # V10: Edge context (forwarded from core via IPC)
                timeframe=payload.get('timeframe', '1min'),
                regime_at_entry=payload.get('regime_at_entry', ''),
                time_bucket=payload.get('time_bucket', ''),
            )
            bus.emit(Event(type=EventType.POP_SIGNAL, payload=pop_payload))
        except Exception as exc:
            log.debug("[IPC] Failed to process remote POP_SIGNAL: %s", exc)

    consumer.on(TOPIC_SIGNALS, _on_remote_signal)
    consumer.on(TOPIC_POP, _on_remote_pop_signal)
    consumer.start()
    log.info("IPC consumer started (SIGNAL + POP_SIGNAL)")

    log.info("Options process running.")
    _kill_switch_exit = False
    try:
        while True:
            now = datetime.now(ET)
            if now.hour >= 16:
                log.info("4:00 PM ET — stopping options process.")
                break

            # V9: Kill switch check FIRST — before emitting BAR events that
            # trigger new entries. Defense-in-depth alongside engine._halted flag.
            if not lifecycle.tick():
                log.error("Options engine halted by kill switch.")
                _kill_switch_exit = True
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
        lifecycle.shutdown()
        consumer.stop()
        if db_cleanup:
            db_cleanup()
        log.info("Options process stopped.")
        # V8: Exit code 3 = kill switch halt. Supervisor should NOT restart.
        if _kill_switch_exit:
            sys.exit(3)


if __name__ == '__main__':
    main()
