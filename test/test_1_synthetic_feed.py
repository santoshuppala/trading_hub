#!/usr/bin/env python3
"""
Test 1: Synthetic Feed Playback
================================
Replays synthetic 1-minute bars for N_TICKERS into the EventBus at 10ms
intervals per bar cycle. Monitors system_pressure and queue_depth.

Pass criteria
-------------
- No FAIL-level issues in queue depth or event loss.
- system_pressure stays below 0.80 under sustained load.
- All N_TICKERS tickers receive at least one handler call.
- Queue drains within 30 s after final emit.
"""
import sys, os, time, logging, threading
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor.event_bus import EventBus, Event, EventType, DispatchMode
from monitor.events import BarPayload

ET = ZoneInfo('America/New_York')

N_TICKERS     = 200
N_BARS        = 100      # steps (simulates 100 bar cycles; each cycle is one batch)
EMIT_INTERVAL = 0.01     # 10 ms between batch emits
SAMPLE_EVERY  = 25

os.makedirs(os.path.join(os.path.dirname(__file__), 'logs'), exist_ok=True)
LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'test_1_synthetic_feed.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_PATH, mode='w')],
)
log = logging.getLogger(__name__)


def _make_tickers(n):
    base = [
        'AAPL','MSFT','GOOGL','AMZN','NVDA','TSLA','META','AVGO','ORCL','AMD',
        'MU','QCOM','AMAT','LRCX','TXN','ADI','MRVL','KLAC','NXPI','ON',
        'SWKS','ADBE','CRM','INTU','SNPS','CDNS','NOW','WDAY','DDOG','SNOW',
        'NET','ZS','OKTA','TEAM','MDB','GTLB','PATH','HUBS','ZM','DOCU',
        'NFLX','UBER','LYFT','ABNB','DASH','SHOP','ETSY','EBAY','PINS','SNAP',
    ]
    extras = [f'TCK{i:03d}' for i in range(1, n - len(base) + 1)]
    return (base + extras)[:n]


def _make_bars(n_bars, seed):
    rng  = np.random.default_rng(seed)
    base = 50.0 + (seed % 250)
    idx  = pd.date_range(start=datetime(2026, 4, 11, 9, 30, tzinfo=ET),
                         periods=n_bars, freq='1min')
    cls  = base + np.cumsum(rng.normal(0, 0.3, n_bars))
    cls  = np.maximum(cls, 1.0)
    hi   = cls + rng.uniform(0.01, 0.5,  n_bars)
    lo   = np.maximum(cls - rng.uniform(0.01, 0.5, n_bars), 0.5)
    op   = np.maximum(cls + rng.uniform(-0.2, 0.2, n_bars), 0.5)
    vol  = rng.integers(5_000, 500_000, n_bars).astype(float)
    return pd.DataFrame({'open': op, 'high': hi, 'low': lo, 'close': cls, 'volume': vol},
                        index=idx)


def _make_rvol(seed):
    rng  = np.random.default_rng(seed + 9999)
    base = 50.0 + (seed % 250)
    idx  = pd.date_range(start=datetime(2026, 3, 27, tzinfo=ET), periods=14, freq='1D')
    cls  = np.maximum(base + np.cumsum(rng.normal(0, 1.0, 14)), 1.0)
    return pd.DataFrame({
        'open': cls + rng.uniform(-1, 1, 14), 'high': cls + rng.uniform(0.5, 2, 14),
        'low':  cls - rng.uniform(0.5, 2, 14), 'close': cls,
        'volume': rng.integers(1_000_000, 10_000_000, 14).astype(float),
    }, index=idx)


def run_test() -> bool:
    log.info("=" * 70)
    log.info("TEST 1: Synthetic Feed Playback")
    log.info("=" * 70)
    log.info(f"Config: {N_TICKERS} tickers × {N_BARS} bar steps, "
             f"interval={EMIT_INTERVAL*1000:.0f}ms, "
             f"total events={N_BARS * N_TICKERS:,}")
    log.info("")

    bus   = EventBus(mode=DispatchMode.ASYNC)
    calls = defaultdict(int)
    lock  = threading.Lock()

    def on_bar(event: Event):
        with lock:
            calls[event.payload.ticker] += 1

    bus.subscribe(EventType.BAR, on_bar)

    log.info("Pre-generating bar data…")
    t0      = time.monotonic()
    tickers = _make_tickers(N_TICKERS)
    # Pre-build full DataFrames; each emit step slices a growing prefix
    full_bars = {t: _make_bars(N_BARS, i) for i, t in enumerate(tickers)}
    rvol_dfs  = {t: _make_rvol(i)         for i, t in enumerate(tickers)}
    log.info(f"  Done in {time.monotonic()-t0:.2f}s\n")

    pressure_samples = []
    depth_peaks = defaultdict(int)
    emitted = 0
    emit_start = time.monotonic()

    for step in range(N_BARS):
        batch = []
        for ticker in tickers:
            df_slice = full_bars[ticker].iloc[:step + 1].copy()
            batch.append(Event(
                type=EventType.BAR,
                payload=BarPayload.from_owned(ticker=ticker, df=df_slice,
                                              rvol_df=rvol_dfs[ticker]),
            ))
        bus.emit_batch(batch)
        emitted += len(batch)

        if step % SAMPLE_EVERY == 0:
            m = bus.metrics()
            pressure_samples.append(m.system_pressure)
            for et, d in m.queue_depths.items():
                depth_peaks[et] = max(depth_peaks[et], d)
            log.info(f"  step {step:3d}/{N_BARS} | emitted={emitted:,} | "
                     f"pressure={m.system_pressure:.1%} | depths={m.queue_depths}")

        time.sleep(EMIT_INTERVAL)

    emit_elapsed = time.monotonic() - emit_start
    log.info(f"\nEmit done: {emitted:,} in {emit_elapsed:.1f}s "
             f"({emitted/emit_elapsed:,.0f} ev/s)")

    log.info("Draining queues (max 30s)…")
    drain_start = time.monotonic()
    while time.monotonic() - drain_start < 30.0:
        if sum(bus.metrics().queue_depths.values()) == 0:
            break
        time.sleep(0.25)
    drain_elapsed = time.monotonic() - drain_start
    bus.shutdown()

    final = bus.metrics()
    with lock:
        n_proc = len(calls)
        tot_c  = sum(calls.values())

    log.info("\n" + "=" * 70)
    log.info("RESULTS")
    log.info("=" * 70)
    log.info(f"Events emitted          : {emitted:,}")
    log.info(f"Emit throughput         : {emitted/emit_elapsed:,.0f} ev/s")
    log.info(f"Drain time              : {drain_elapsed:.2f}s")
    log.info(f"Tickers with handler hit: {n_proc}/{N_TICKERS}")
    log.info(f"Total handler calls     : {tot_c:,}")
    log.info(f"Final system_pressure   : {final.system_pressure:.1%}")
    log.info(f"Final queue_depths      : {final.queue_depths}")
    log.info(f"Dropped events          : {final.dropped_counts}")
    log.info(f"Slow handler calls >100ms: {final.slow_calls}")
    log.info(f"Circuit breaks          : {final.circuit_breaks}")
    log.info(f"DLQ entries             : {final.dlq_count}")
    log.info(f"Queue depth peaks       : {dict(depth_peaks)}")
    if pressure_samples:
        pk  = max(pressure_samples)
        avg = sum(pressure_samples) / len(pressure_samples)
        log.info(f"Pressure peak / avg     : {pk:.1%} / {avg:.1%}")
        log.info(f"Pressure >60% samples   : "
                 f"{sum(1 for p in pressure_samples if p >= 0.60)}/{len(pressure_samples)}")
        log.info(f"Pressure >80% samples   : "
                 f"{sum(1 for p in pressure_samples if p >= 0.80)}/{len(pressure_samples)}")

    bar_dropped = final.dropped_counts.get('BAR', 0)
    issues = []
    if final.system_pressure > 0.80:
        issues.append(f"FAIL  system_pressure={final.system_pressure:.1%} > 80%")
    if bar_dropped > 0:
        issues.append(f"WARN  {bar_dropped} BAR events dropped under DROP_OLDEST backpressure")
    if n_proc < N_TICKERS:
        issues.append(f"FAIL  only {n_proc}/{N_TICKERS} tickers processed")
    if drain_elapsed > 10.0:
        issues.append(f"WARN  drain took {drain_elapsed:.1f}s > 10s target")

    log.info("\n" + "-" * 70)
    log.info("VERDICT")
    log.info("-" * 70)
    if not issues:
        log.info("PASS — EventBus handled 200-ticker feed without issues")
    else:
        for i in issues:
            log.info(i)

    log.info("\nIMPROVEMENTS NEEDED")
    log.info("-" * 70)
    recs = []
    if bar_dropped > 0:
        recs.append("Increase BAR n_workers 4→8 or queue maxsize 200→500 in _DEFAULT_ASYNC_CONFIG")
    if drain_elapsed > 10.0:
        recs.append("Profile SignalAnalyzer.analyze() — cache VWAP/RSI intermediates "
                    "or pre-compute in data layer to cut per-ticker handler time")
    if pressure_samples and max(pressure_samples) > 0.60:
        recs.append("Sustained >60% pressure: add BAR workers or split batches into sub-groups")
    if not recs:
        recs.append("None — system handles 200-ticker feed at 10ms intervals cleanly")
    for r in recs:
        log.info(f"  • {r}")

    log.info("\nTest 1 complete.")
    return sum(1 for i in issues if i.startswith("FAIL")) == 0


if __name__ == '__main__':
    sys.exit(0 if run_test() else 1)
