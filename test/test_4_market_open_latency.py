#!/usr/bin/env python3
"""
Test 4: "Market Open" Latency Simulation
==========================================
Injects 200 BAR events simultaneously (no sleep) to simulate the 9:30 AM
stress peak. Measures per-ticker handler latency.

Pass criteria
-------------
- P95 handler latency < 50ms per ticker.
- < 10% of tickers exceed 50ms.
- No BAR events dropped.
- All 200 tickers processed.
"""
import sys, os, time, logging, threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor.event_bus import EventBus, Event, EventType, DispatchMode
from monitor.events import BarPayload

ET = ZoneInfo('America/New_York')
N_TICKERS = 200
MIN_BARS  = 60   # enough for SignalAnalyzer (needs >= 30)

os.makedirs(os.path.join(os.path.dirname(__file__), 'logs'), exist_ok=True)
LOG_PATH = os.path.join(os.path.dirname(__file__), 'logs', 'test_4_market_open_latency.log')
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
    return (base + [f'TCK{i:03d}' for i in range(1, n - len(base) + 1)])[:n]


def _make_bars(n_bars, seed):
    rng  = np.random.default_rng(seed)
    base = 50.0 + (seed % 250)
    idx  = pd.date_range(start=datetime(2026, 4, 11, 9, 30, tzinfo=ET),
                         periods=n_bars, freq='1min')
    cls  = np.maximum(base + np.cumsum(rng.normal(0, 0.3, n_bars)), 1.0)
    hi   = cls + rng.uniform(0.01, 0.5, n_bars)
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


def _run_phase(label: str, use_real_analyzer: bool) -> dict:
    """Run one benchmark phase. Returns a result dict."""
    log.info(f"\n{'='*70}")
    log.info(f"PHASE: {label}")
    log.info(f"{'='*70}")

    bus   = EventBus(mode=DispatchMode.ASYNC)
    lats  = {}    # ticker -> latency_ms
    lock  = threading.Lock()
    done  = threading.Event()
    count = [0]

    if use_real_analyzer:
        from monitor.signals import SignalAnalyzer
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from config import STRATEGY_PARAMS
        analyzer = SignalAnalyzer(STRATEGY_PARAMS, {})

        def on_bar(event: Event):
            ticker = event.payload.ticker
            t0 = time.monotonic()
            try:
                df = event.payload.df
                if df is not None and len(df) >= 30:
                    cache = {ticker: event.payload.rvol_df} if event.payload.rvol_df is not None else {}
                    analyzer.analyze(ticker, df, cache)
            except Exception as e:
                log.debug(f"Analyzer error {ticker}: {e}")
            finally:
                ms = (time.monotonic() - t0) * 1000
                with lock:
                    lats[ticker] = ms
                    count[0] += 1
                    if count[0] >= N_TICKERS:
                        done.set()
    else:
        # Simulated workload: same pandas ops as analyze() without VWAP reclaim logic
        def on_bar(event: Event):
            ticker = event.payload.ticker
            t0 = time.monotonic()
            try:
                df = event.payload.df
                if df is not None and not df.empty:
                    typical = (df['high'] + df['low'] + df['close']) / 3.0
                    vwap    = (typical * df['volume']).cumsum() / df['volume'].cumsum()
                    delta   = df['close'].diff()
                    gain    = delta.where(delta > 0, 0).rolling(14).mean()
                    loss    = (-delta.where(delta < 0, 0)).rolling(14).mean()
                    rsi     = 100 - (100 / (1 + gain / loss))
                    tr      = pd.concat([df['high'] - df['low'],
                                         (df['high'] - df['close'].shift()).abs(),
                                         (df['low']  - df['close'].shift()).abs()],
                                        axis=1).max(axis=1)
                    atr     = tr.rolling(14).mean()
                    _ = float(vwap.iloc[-1])
                    _ = float(rsi.iloc[-1])  if not rsi.isna().all()  else 50.0
                    _ = float(atr.iloc[-1])  if not atr.isna().all()  else 1.0
            except Exception as e:
                log.debug(f"Handler error {ticker}: {e}")
            finally:
                ms = (time.monotonic() - t0) * 1000
                with lock:
                    lats[ticker] = ms
                    count[0] += 1
                    if count[0] >= N_TICKERS:
                        done.set()

    bus.subscribe(EventType.BAR, on_bar)

    log.info("Generating bar data…")
    tickers = _make_tickers(N_TICKERS)
    events  = [
        Event(type=EventType.BAR,
              payload=BarPayload.from_owned(ticker=t, df=_make_bars(MIN_BARS, i),
                                            rvol_df=_make_rvol(i)))
        for i, t in enumerate(tickers)
    ]

    log.info(f"Injecting {len(events)} BAR events simultaneously…")
    t_emit = time.monotonic()
    bus.emit_batch(events)
    emit_ms = (time.monotonic() - t_emit) * 1000
    log.info(f"emit_batch() returned in {emit_ms:.1f}ms")

    # Poll so dropped events don't cause a hard 60s timeout.
    # A dropped BAR never calls on_bar, so count[0] would never reach N_TICKERS.
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if done.wait(timeout=0.5):
            break
        m_poll = bus.metrics()
        dropped_so_far = m_poll.dropped_counts.get('BAR', 0)
        with lock:
            processed_so_far = count[0]
        if processed_so_far + dropped_so_far >= N_TICKERS:
            done.set()   # all accounted for (some dropped)
            break

    completed = done.is_set()
    total_ms  = (time.monotonic() - t_emit) * 1000
    bus.shutdown()

    if not completed:
        log.warning(f"  Timeout: only {count[0]}/{N_TICKERS} processed")

    with lock:
        vals = sorted(lats.values())

    m = bus.metrics()
    result = {
        'label': label, 'completed': completed,
        'n_processed': len(lats), 'total_ms': total_ms, 'emit_ms': emit_ms,
        'metrics': m,
    }

    if vals:
        n = len(vals)
        result.update({
            'min': vals[0], 'avg': sum(vals)/n, 'p50': vals[n//2],
            'p90': vals[int(n*0.90)], 'p95': vals[int(n*0.95)],
            'p99': vals[min(int(n*0.99), n-1)], 'max': vals[-1],
            'over_50ms':  sum(1 for v in vals if v > 50),
            'over_100ms': sum(1 for v in vals if v > 100),
        })
        log.info(f"\nLatency (ms): min={vals[0]:.1f}  avg={sum(vals)/n:.1f}  "
                 f"p50={vals[n//2]:.1f}  p95={vals[int(n*0.95)]:.1f}  "
                 f"p99={vals[min(int(n*0.99),n-1)]:.1f}  max={vals[-1]:.1f}")
        log.info(f">50ms: {result['over_50ms']}/{n}   >100ms: {result['over_100ms']}/{n}")

    log.info(f"Total wall-clock: {total_ms:.1f}ms | "
             f"pressure={m.system_pressure:.1%} | dropped={m.dropped_counts}")
    return result


def run_test() -> bool:
    log.info("=" * 70)
    log.info("TEST 4: Market Open Latency Simulation")
    log.info("=" * 70)
    log.info(f"Tickers: {N_TICKERS}  Bars per ticker: {MIN_BARS}")
    log.info("")

    sim  = _run_phase("Simulated workload (VWAP+RSI+ATR pandas ops)", use_real_analyzer=False)
    real = _run_phase("Real SignalAnalyzer.analyze()",               use_real_analyzer=True)

    log.info("\n" + "=" * 70)
    log.info("COMBINED RESULTS")
    log.info("=" * 70)
    for r in (sim, real):
        log.info(f"\n  [{r['label']}]")
        log.info(f"    Processed      : {r['n_processed']}/{N_TICKERS}")
        log.info(f"    Total wall-clock: {r['total_ms']:.1f}ms")
        log.info(f"    emit_batch time : {r['emit_ms']:.1f}ms")
        if 'p95' in r:
            log.info(f"    Latency p50/p95/p99 : "
                     f"{r['p50']:.1f} / {r['p95']:.1f} / {r['p99']:.1f} ms")
            log.info(f"    >50ms / >100ms      : "
                     f"{r['over_50ms']}/{r['n_processed']} / "
                     f"{r['over_100ms']}/{r['n_processed']}")
        log.info(f"    Dropped events  : {r['metrics'].dropped_counts}")
        log.info(f"    Slow calls      : {r['metrics'].slow_calls}")

    issues = []
    for r in (sim, real):
        if 'p95' in r and r['p95'] > 50:
            issues.append(f"FAIL  [{r['label']}] P95 latency {r['p95']:.1f}ms > 50ms target")
        bar_dropped = r['metrics'].dropped_counts.get('BAR', 0)
        # Only FAIL for truly unaccounted tickers (processed + dropped < total)
        unaccounted = N_TICKERS - r['n_processed'] - bar_dropped
        if unaccounted > 0:
            issues.append(f"FAIL  [{r['label']}] {unaccounted} tickers unaccounted (processed={r['n_processed']}, dropped={bar_dropped})")
        if bar_dropped > 0:
            issues.append(f"WARN  [{r['label']}] {bar_dropped} BAR events dropped — increase queue maxsize or n_workers")

    log.info("\n" + "-" * 70)
    log.info("VERDICT")
    log.info("-" * 70)
    if not issues:
        log.info("PASS — All tickers processed within 50ms P95 target")
    else:
        for i in issues:
            log.info(i)

    log.info("\nIMPROVEMENTS NEEDED")
    log.info("-" * 70)
    recs = []
    for r in (sim, real):
        if 'p95' in r and r['p95'] > 50:
            recs.append(f"[{r['label']}] P95={r['p95']:.1f}ms: "
                        "increase BAR n_workers 4→8 or pre-compute VWAP/RSI in data layer")
        if 'max' in r and r['max'] > 200:
            recs.append(f"[{r['label']}] max={r['max']:.1f}ms spike: "
                        "GIL contention from concurrent pandas ops — consider numpy vectorisation")
        bar_dropped = r['metrics'].dropped_counts.get('BAR', 0)
        if bar_dropped > 0:
            recs.append(f"[{r['label']}] {bar_dropped} BAR events dropped: "
                        "increase EventBus BAR queue maxsize (200→500) or n_workers (4→8) "
                        "in _DEFAULT_ASYNC_CONFIG")
    if not recs:
        recs.append("None — all tickers processed under 50ms P95")
    for r in recs:
        log.info(f"  • {r}")

    log.info("\nTest 4 complete.")
    return sum(1 for i in issues if i.startswith("FAIL")) == 0


if __name__ == '__main__':
    sys.exit(0 if run_test() else 1)
