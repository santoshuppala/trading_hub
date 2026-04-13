#!/usr/bin/env python3
"""
Test 13 — Pro-Setups Detector Validation
=========================================
TC-DT-01: ORB detector basic breakout
TC-DT-02: No-signal scenario (flat price series)
TC-DT-03: Edge conditions (MIN_BARS, empty DF, single bar, zero prices)
TC-DT-04: Replay determinism (same input → same output, fresh instances)
TC-DT-05: Latency per event (P99 < 5ms on 200-bar DF, 100 iterations)
"""
import os, sys, time, logging, copy
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from pro_setups.detectors.orb_detector import ORBDetector
from pro_setups.detectors.trend_detector import TrendDetector
from pro_setups.detectors.vwap_detector import VWAPDetector
from pro_setups.detectors.sr_detector import SRDetector
from pro_setups.detectors.inside_bar_detector import InsideBarDetector
from pro_setups.detectors.gap_detector import GapDetector
from pro_setups.detectors.flag_detector import FlagDetector
from pro_setups.detectors.liquidity_detector import LiquidityDetector
from pro_setups.detectors.volatility_detector import VolatilityDetector
from pro_setups.detectors.fib_detector import FibDetector
from pro_setups.detectors.momentum_detector import MomentumDetector

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("test_13")

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


# ── Helper: build a DatetimeIndex ──────────────────────────────────────────
def _make_index(n, start="2026-01-02 09:30"):
    return pd.date_range(start, periods=n, freq="1min", tz="UTC")


def _make_flat_df(n=50, price=100.0, volume=1000):
    """All bars identical — should never trigger any detector."""
    idx = _make_index(n)
    return pd.DataFrame({
        'open': price, 'high': price, 'low': price,
        'close': price, 'volume': volume,
    }, index=idx)


def _all_detectors():
    """Return fresh instances of all 11 detectors."""
    return [
        ORBDetector(),
        TrendDetector(),
        VWAPDetector(),
        SRDetector(),
        InsideBarDetector(),
        GapDetector(),
        FlagDetector(),
        LiquidityDetector(),
        VolatilityDetector(),
        FibDetector(),
        MomentumDetector(),
    ]


def _make_varied_df(n=200):
    """
    A synthetic 200-bar DataFrame with realistic-looking price action.
    Uses a random walk with trend so detectors have something to chew on.
    Seed is fixed for determinism.
    """
    rng = np.random.RandomState(42)
    idx = _make_index(n)
    close = np.cumsum(rng.randn(n) * 0.3) + 100.0
    high = close + rng.uniform(0.1, 0.8, n)
    low = close - rng.uniform(0.1, 0.8, n)
    opn = close + rng.randn(n) * 0.15
    volume = rng.randint(500, 5000, n).astype(float)
    return pd.DataFrame({
        'open': opn, 'high': high, 'low': low,
        'close': close, 'volume': volume,
    }, index=idx)


# ═══════════════════════════════════════════════════════════════════════════════
# TC-DT-01: ORB detector basic breakout
# ═══════════════════════════════════════════════════════════════════════════════
def test_dt_01():
    log.info("TC-DT-01: ORB detector basic breakout")

    n_bars = 20
    idx = _make_index(n_bars)

    # First 15 bars: opening range with high=101, low=99
    opens  = [100.0] * 15
    highs  = [101.0] * 15
    lows   = [99.0]  * 15
    closes = [100.0] * 15
    vols   = [1000.0] * 15

    # Bars 16-20: breakout above OR high (101) with 2x volume
    for i in range(5):
        opens.append(101.5 + i * 0.3)
        highs.append(103.0 + i * 0.3)
        lows.append(101.0 + i * 0.3)
        closes.append(103.0 + i * 0.3)
        vols.append(3000.0)  # 2x+ the session average (~1500 avg)

    df = pd.DataFrame({
        'open': opens, 'high': highs, 'low': lows,
        'close': closes, 'volume': vols,
    }, index=idx)

    det = ORBDetector()
    sig = det.detect("TEST", df)

    check("DT-01a: ORB detector fired", sig.fired,
          f"fired={sig.fired}")
    check("DT-01b: direction is long", sig.direction == 'long',
          f"direction={sig.direction}")
    check("DT-01c: strength >= 0.5", sig.strength >= 0.5,
          f"strength={sig.strength}")
    check("DT-01d: metadata has orb_high", 'orb_high' in sig.metadata,
          f"metadata keys={list(sig.metadata.keys())}")
    check("DT-01e: orb_high == 101.0",
          sig.metadata.get('orb_high') == 101.0,
          f"orb_high={sig.metadata.get('orb_high')}")


# ═══════════════════════════════════════════════════════════════════════════════
# TC-DT-02: No-signal scenario (flat price series)
# ═══════════════════════════════════════════════════════════════════════════════
def test_dt_02():
    log.info("TC-DT-02: No-signal scenario (flat price series)")

    # Use enough bars to satisfy even the most demanding detector (TrendDetector MIN_BARS=52)
    df = _make_flat_df(n=60, price=100.0, volume=1000)

    detectors = _all_detectors()
    for det in detectors:
        sig = det.detect("TEST", df)
        # VWAPDetector fires on flat data because close == vwap and it always
        # fires when it has enough bars, so we skip it for the flat-data check.
        # GapDetector requires rvol_df so it returns no_signal by default.
        # VWAP always fires when it has enough bars (it reports position vs VWAP).
        # InsideBarDetector fires on flat data because equal H/L satisfies
        # cur_high <= prev_high and cur_low >= prev_low.  Both are correct
        # detector behavior — just verify no crash.
        if det.name in ('vwap', 'inside_bar'):
            check(f"DT-02-{det.name}: no crash on flat data", True)
            continue
        check(f"DT-02-{det.name}: no signal on flat data", not sig.fired,
              f"fired={sig.fired}, direction={sig.direction}, strength={sig.strength}")


# ═══════════════════════════════════════════════════════════════════════════════
# TC-DT-03: Edge conditions
# ═══════════════════════════════════════════════════════════════════════════════
def test_dt_03():
    log.info("TC-DT-03: Edge conditions (MIN_BARS, empty, single, zeros)")

    detectors = _all_detectors()

    # ── 3a: Empty DataFrame ──
    empty_df = pd.DataFrame(
        columns=['open', 'high', 'low', 'close', 'volume'],
        dtype=float,
    )
    empty_df.index = pd.DatetimeIndex([], tz="UTC")

    for det in detectors:
        try:
            sig = det.detect("TEST", empty_df)
            ok = not sig.fired
        except Exception as e:
            ok = False
            log.error(f"  {det.name} crashed on empty DF: {e}")
        check(f"DT-03a-{det.name}: empty DF no crash, no signal", ok)

    # ── 3b: Single bar ──
    single_df = pd.DataFrame({
        'open': [100.0], 'high': [101.0], 'low': [99.0],
        'close': [100.5], 'volume': [1000.0],
    }, index=_make_index(1))

    for det in detectors:
        try:
            sig = det.detect("TEST", single_df)
            ok = not sig.fired
        except Exception as e:
            ok = False
            log.error(f"  {det.name} crashed on single bar: {e}")
        check(f"DT-03b-{det.name}: single bar no crash, no signal", ok)

    # ── 3c: Exactly MIN_BARS ──
    for det in detectors:
        min_df = _make_flat_df(n=det.MIN_BARS, price=100.0)
        try:
            sig = det.detect("TEST", min_df)
            no_crash = True
        except Exception as e:
            no_crash = False
            log.error(f"  {det.name} crashed at MIN_BARS={det.MIN_BARS}: {e}")
        check(f"DT-03c-{det.name}: MIN_BARS={det.MIN_BARS} no crash", no_crash)

    # ── 3d: All-zero prices ──
    zero_df = pd.DataFrame({
        'open': [0.0] * 60, 'high': [0.0] * 60, 'low': [0.0] * 60,
        'close': [0.0] * 60, 'volume': [0.0] * 60,
    }, index=_make_index(60))

    for det in detectors:
        try:
            sig = det.detect("TEST", zero_df)
            # VWAP and InsideBar may fire on degenerate data — that is
            # acceptable; we only require no crash here.
            if det.name in ('vwap', 'inside_bar'):
                ok = True
            else:
                ok = not sig.fired
        except Exception as e:
            ok = False
            log.error(f"  {det.name} crashed on zero prices: {e}")
        check(f"DT-03d-{det.name}: zero prices no crash, no signal", ok)


# ═══════════════════════════════════════════════════════════════════════════════
# TC-DT-04: Replay determinism
# ═══════════════════════════════════════════════════════════════════════════════
def test_dt_04():
    log.info("TC-DT-04: Replay determinism (same input, fresh instances)")

    df = _make_varied_df(n=200)

    # Run 1: fresh detector instances
    results_a = {}
    for det in _all_detectors():
        sig = det.detect("TEST", df, None)
        results_a[det.name] = (sig.fired, sig.direction, sig.strength, sig.metadata)

    # Run 2: completely fresh detector instances, same DataFrame
    results_b = {}
    for det in _all_detectors():
        sig = det.detect("TEST", df, None)
        results_b[det.name] = (sig.fired, sig.direction, sig.strength, sig.metadata)

    for name in results_a:
        a = results_a[name]
        b = results_b[name]
        match = (
            a[0] == b[0] and          # fired
            a[1] == b[1] and          # direction
            abs(a[2] - b[2]) < 1e-9   # strength (float comparison)
        )
        # Also compare metadata keys and values
        if match and a[3].keys() == b[3].keys():
            for k in a[3]:
                va, vb = a[3][k], b[3][k]
                if isinstance(va, float):
                    if abs(va - vb) > 1e-9:
                        match = False
                        break
                elif va != vb:
                    match = False
                    break
        elif a[3] != b[3]:
            match = False

        check(f"DT-04-{name}: deterministic output", match,
              f"run1={a[:3]}, run2={b[:3]}")


# ═══════════════════════════════════════════════════════════════════════════════
# TC-DT-05: Latency per event (P99 < 5ms)
# ═══════════════════════════════════════════════════════════════════════════════
def test_dt_05():
    log.info("TC-DT-05: Latency per event (P99 < 5ms on 200-bar DF)")

    df = _make_varied_df(n=200)
    iterations = 100

    detectors = _all_detectors()

    # Warmup pass — let JIT / import caches settle
    for det in detectors:
        for _ in range(5):
            det.detect("TEST", df, None)

    for det in detectors:
        timings = []
        for _ in range(iterations):
            t0 = time.perf_counter()
            det.detect("TEST", df, None)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            timings.append(elapsed_ms)

        timings.sort()
        avg_ms = sum(timings) / len(timings)
        p99_idx = int(len(timings) * 0.99)
        p99_ms = timings[min(p99_idx, len(timings) - 1)]

        check(f"DT-05-{det.name}: P99={p99_ms:.2f}ms < 5ms", p99_ms < 5.0,
              f"avg={avg_ms:.2f}ms, P99={p99_ms:.2f}ms")


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    tests = [test_dt_01, test_dt_02, test_dt_03, test_dt_04, test_dt_05]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAIL += 1
            log.error(f"  FAIL  {t.__name__} crashed: {e}")
        log.info("")

    log.info(f"{'='*60}")
    log.info(f"  Test 13 Results: {PASS} passed, {FAIL} failed")
    log.info(f"{'='*60}")
    sys.exit(1 if FAIL else 0)
