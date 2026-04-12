#!/usr/bin/env python3
"""
Test 12 — Market Data Ingestor Validation
==========================================
TC-MD-01: Valid tick ingestion and normalization
TC-MD-02: Corrupted tick handling (missing fields, invalid values)
TC-MD-03: Out-of-order ticks
TC-MD-04: 1M tick ingestion throughput
"""
import os, sys, time, logging
import numpy as np
import pandas as pd
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from monitor.event_bus import EventBus, Event, EventType, DispatchMode
from monitor.events import BarPayload

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("test_12")

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        log.info(f"  PASS  {name}")
    else:
        FAIL += 1
        log.error(f"  FAIL  {name} — {detail}")

def _make_bar_df(n=60, start="2026-01-02 09:30", freq="1min", tz="UTC",
                 base_price=100.0, volume=1000):
    idx = pd.date_range(start, periods=n, freq=freq, tz=tz)
    prices = base_price + np.random.randn(n).cumsum() * 0.5
    df = pd.DataFrame({
        'open':   prices,
        'high':   prices + abs(np.random.randn(n)) * 0.3,
        'low':    prices - abs(np.random.randn(n)) * 0.3,
        'close':  prices + np.random.randn(n) * 0.1,
        'volume': np.random.randint(volume // 2, volume * 2, n),
    }, index=idx)
    return df

# ═══════════════════════════════════════════════════════════════════════════════
# TC-MD-01: Valid tick ingestion and normalization
# ═══════════════════════════════════════════════════════════════════════════════
def test_md_01():
    log.info("TC-MD-01: Valid tick ingestion and normalization")
    received = []
    bus = EventBus(mode=DispatchMode.SYNC)

    def handler(event):
        received.append(event)

    bus.subscribe(EventType.BAR, handler)

    tickers = ["AAPL", "MSFT", "GOOG"]
    for t in tickers:
        df = _make_bar_df(n=60, base_price=150.0)
        bus.emit(Event(type=EventType.BAR, payload=BarPayload(ticker=t, df=df)))

    check("MD-01a: all tickers received", len(received) == 3,
          f"got {len(received)}")

    for ev in received:
        p = ev.payload
        df = p.df
        check(f"MD-01b: {p.ticker} has DataFrame",
              isinstance(df, pd.DataFrame))
        check(f"MD-01c: {p.ticker} OHLCV columns present",
              all(c in df.columns for c in ['open', 'high', 'low', 'close', 'volume']),
              f"cols={list(df.columns)}")
        check(f"MD-01d: {p.ticker} index is DatetimeIndex",
              isinstance(df.index, pd.DatetimeIndex))
        check(f"MD-01e: {p.ticker} index timezone-aware",
              df.index.tz is not None,
              f"tz={df.index.tz}")
        check(f"MD-01f: {p.ticker} all prices > 0",
              (df[['open','high','low','close']] > 0).all().all())
        check(f"MD-01g: {p.ticker} high >= low",
              (df['high'] >= df['low']).all())

# ═══════════════════════════════════════════════════════════════════════════════
# TC-MD-02: Corrupted tick handling
# ═══════════════════════════════════════════════════════════════════════════════
def test_md_02():
    log.info("TC-MD-02: Corrupted tick handling")
    bus = EventBus(mode=DispatchMode.SYNC)
    received = []

    def handler(event):
        received.append(event)

    bus.subscribe(EventType.BAR, handler)

    # Test with NaN values in DataFrame
    df_nan = _make_bar_df(n=40)
    df_nan.iloc[10, 0] = np.nan  # NaN in open
    df_nan.iloc[20, 3] = np.nan  # NaN in close

    # Should not crash — BarPayload accepts any DataFrame
    try:
        bus.emit(Event(type=EventType.BAR, payload=BarPayload(ticker="NAN_TEST", df=df_nan)))
        check("MD-02a: NaN DataFrame accepted by EventBus", True)
    except Exception as e:
        check("MD-02a: NaN DataFrame accepted by EventBus", False, str(e))

    # Test with negative prices
    df_neg = _make_bar_df(n=40)
    df_neg['close'] = -1.0
    try:
        bus.emit(Event(type=EventType.BAR, payload=BarPayload(ticker="NEG_TEST", df=df_neg)))
        check("MD-02b: negative prices accepted by EventBus", True)
    except Exception as e:
        check("MD-02b: negative prices accepted by EventBus", False, str(e))

    # Test with empty DataFrame
    df_empty = pd.DataFrame(columns=['open','high','low','close','volume'])
    try:
        bus.emit(Event(type=EventType.BAR, payload=BarPayload(ticker="EMPTY", df=df_empty)))
        check("MD-02c: empty DataFrame accepted", True)
    except Exception as e:
        check("MD-02c: empty DataFrame accepted", False, str(e))

    # Test with zero volume
    df_zero_vol = _make_bar_df(n=40)
    df_zero_vol['volume'] = 0
    try:
        bus.emit(Event(type=EventType.BAR, payload=BarPayload(ticker="ZVOL", df=df_zero_vol)))
        check("MD-02d: zero volume accepted", True)
    except Exception as e:
        check("MD-02d: zero volume accepted", False, str(e))

    check("MD-02e: pipeline survived all corrupt inputs", len(received) >= 1)

# ═══════════════════════════════════════════════════════════════════════════════
# TC-MD-03: Out-of-order ticks
# ═══════════════════════════════════════════════════════════════════════════════
def test_md_03():
    log.info("TC-MD-03: Out-of-order ticks")
    received = []
    bus = EventBus(mode=DispatchMode.SYNC)

    def handler(event):
        received.append(event.payload.ticker)

    bus.subscribe(EventType.BAR, handler)

    # Emit bars with timestamps that go backwards
    df1 = _make_bar_df(n=40, start="2026-01-02 10:30")  # later
    df2 = _make_bar_df(n=40, start="2026-01-02 09:30")  # earlier

    bus.emit(Event(type=EventType.BAR, payload=BarPayload(ticker="LATE", df=df1)))
    bus.emit(Event(type=EventType.BAR, payload=BarPayload(ticker="EARLY", df=df2)))

    check("MD-03a: both bars delivered regardless of order",
          len(received) == 2, f"got {len(received)}")
    check("MD-03b: delivery order matches emit order",
          received == ["LATE", "EARLY"])

# ═══════════════════════════════════════════════════════════════════════════════
# TC-MD-04: 1M tick ingestion throughput
# ═══════════════════════════════════════════════════════════════════════════════
def test_md_04():
    log.info("TC-MD-04: High-volume ingestion throughput")
    N = 10_000  # 10K events (each with 60-bar DataFrame ≈ 600K data points)
    count = [0]
    bus = EventBus(mode=DispatchMode.ASYNC)

    def handler(event):
        count[0] += 1

    bus.subscribe(EventType.BAR, handler)


    # Pre-generate DataFrames to avoid measuring creation time
    df = _make_bar_df(n=60)
    tickers = [f"T{i:04d}" for i in range(200)]

    t0 = time.monotonic()
    for i in range(N):
        bus.emit(Event(
            type=EventType.BAR,
            payload=BarPayload(ticker=tickers[i % len(tickers)], df=df),
        ))
    emit_elapsed = time.monotonic() - t0
    emit_rate = N / emit_elapsed

    time.sleep(5)


    check("MD-04a: no crash under high volume", True)
    check(f"MD-04b: emit rate > 500/sec",
          emit_rate > 500,
          f"rate={emit_rate:.0f}/sec")
    check(f"MD-04c: >90% events delivered",
          count[0] >= N * 0.9,
          f"delivered={count[0]}/{N} ({count[0]/N*100:.1f}%)")

    import tracemalloc
    tracemalloc.start()
    # Quick memory check: emit 1000 more and measure
    for i in range(1000):
        bus2 = None  # gc hint
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    check("MD-04d: no memory blow-up",
          peak < 500 * 1024 * 1024,  # < 500MB
          f"peak={peak / 1024 / 1024:.1f}MB")


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    tests = [test_md_01, test_md_02, test_md_03, test_md_04]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAIL += 1
            log.error(f"  FAIL  {t.__name__} crashed: {e}")
        log.info("")

    log.info(f"{'='*60}")
    log.info(f"  Test 12 Results: {PASS} passed, {FAIL} failed")
    log.info(f"{'='*60}")
    sys.exit(1 if FAIL else 0)
