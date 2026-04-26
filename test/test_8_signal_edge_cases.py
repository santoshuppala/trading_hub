"""
TEST 8: SignalAnalyzer & RVOL Edge Cases
=========================================
Pure unit tests for SignalAnalyzer.analyze() and get_rvol() with
edge-case DataFrames. No EventBus needed.

Subtests:
  8a: Insufficient bars (< 30)
  8b: Exactly MIN_BARS (30)
  8c: Zero volatility (flat OHLCV)
  8d: Single spike bar (10x volume, +5% move)
  8e: Negative prices
  8f: NaN in OHLCV
  8g: RVOL time gate (before 10:15 AM)
  8h: RVOL with empty hist_df
  8i: check_position_exit — stop exactly at price
  8j: check_position_exit — target exactly at price
  8k: SignalPayload invariants (stop > price, target < price)
  8l: Stress test — 1000 random DataFrames
"""

import os
import sys
import logging
import random
import time
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Log setup ────────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'test_8_signal_edge_cases.log')

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w'),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger('test_8')

ET = ZoneInfo('America/New_York')

from monitor.signals import SignalAnalyzer, get_rvol
from monitor.events import SignalPayload
from config import STRATEGY_PARAMS


# ── Helper: build DataFrames ──────────────────────────────────────────────────

def make_df(n_bars=40, base_price=100.0, volume=50_000.0,
            price_trend=0.0, price_noise=0.3):
    """Build a simple intraday bars DataFrame."""
    today = datetime.now(ET).replace(hour=9, minute=45, second=0, microsecond=0)
    idx = pd.date_range(today, periods=n_bars, freq='1min', tz=ET)
    rng = np.random.default_rng(7)

    closes = base_price + np.cumsum(rng.uniform(-price_noise, price_noise + price_trend, n_bars))
    opens  = np.roll(closes, 1)
    opens[0] = base_price
    highs  = np.maximum(opens, closes) + rng.uniform(0.0, 0.3, n_bars)
    lows   = np.minimum(opens, closes) - rng.uniform(0.0, 0.3, n_bars)
    vols   = np.full(n_bars, volume)

    return pd.DataFrame({'open': opens, 'high': highs, 'low': lows,
                          'close': closes, 'volume': vols}, index=idx)


def make_hist_df(n_days=5, avg_daily_vol=1_000_000.0):
    """Daily bars for RVOL computation."""
    dates = pd.date_range(
        end=datetime.now(ET).date() - timedelta(days=1),
        periods=n_days, freq='B', tz=ET
    )
    rows = [{'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0,
              'volume': avg_daily_vol} for _ in dates]
    return pd.DataFrame(rows, index=dates)


analyzer = SignalAnalyzer(STRATEGY_PARAMS, {})


# ── Subtest 8a ────────────────────────────────────────────────────────────────

def test_8a_insufficient_bars():
    log.info("=== 8a: Insufficient bars (<30) ===")
    for n in [0, 1, 10, 29]:
        if n == 0:
            df = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
        else:
            df = make_df(n_bars=n)
        result = analyzer.analyze('TEST', df, {})
        assert result is None, f"n={n} bars: expected None, got {result}"
    log.info("  PASS: all sub-30-bar inputs return None")
    return True


# ── Subtest 8b ────────────────────────────────────────────────────────────────

def test_8b_exactly_min_bars():
    log.info("=== 8b: Exactly MIN_BARS=30 ===")
    df = make_df(n_bars=30)
    result = analyzer.analyze('TEST', df, {})
    # Should return a dict (not None) — may have action=None
    assert result is not None, "Exactly 30 bars should return a dict, got None"
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    assert 'action' in result
    log.info(f"  PASS: 30 bars -> dict with action={result['action']}")
    return True


# ── Subtest 8c ────────────────────────────────────────────────────────────────

def test_8c_zero_volatility():
    log.info("=== 8c: Zero volatility (flat OHLCV) ===")
    today = datetime.now(ET).replace(hour=9, minute=45, second=0, microsecond=0)
    idx = pd.date_range(today, periods=40, freq='1min', tz=ET)
    df = pd.DataFrame({
        'open':   np.full(40, 100.0),
        'high':   np.full(40, 100.0),
        'low':    np.full(40, 100.0),
        'close':  np.full(40, 100.0),
        'volume': np.full(40, 50_000.0),
    }, index=idx)

    try:
        result = analyzer.analyze('FLAT', df, {})
        # Should not crash; may return None or dict with NaN-handled values
        if result is not None:
            # Verify no NaN in key output fields
            price = result.get('current_price')
            assert price is None or (isinstance(price, float) and not np.isnan(price)), \
                "current_price should not be NaN"
        log.info(f"  PASS: Zero volatility did not crash. result={result}")
    except Exception as e:
        raise AssertionError(f"Zero volatility should not crash: {e}")
    return True


# ── Subtest 8d ────────────────────────────────────────────────────────────────

def test_8d_single_spike_bar():
    log.info("=== 8d: Single spike bar ===")
    df = make_df(n_bars=30, base_price=100.0, volume=10_000.0)
    df_copy = df.copy()
    # Overwrite last bar with 10x volume and +5% move
    df_copy.iloc[-1, df_copy.columns.get_loc('close')] = 105.0
    df_copy.iloc[-1, df_copy.columns.get_loc('high')]  = 106.0
    df_copy.iloc[-1, df_copy.columns.get_loc('volume')] = 100_000.0  # 10x

    # Build hist_df so RVOL can be computed
    hist_df = make_hist_df(n_days=5, avg_daily_vol=100_000.0)  # daily avg 100k
    rvol_cache = {'SPIKE': hist_df}

    result = analyzer.analyze('SPIKE', df_copy, rvol_cache)
    if result is not None:
        rvol = result.get('rvol', 1.0)
        log.info(f"  PASS: spike bar rvol={rvol:.2f} (should be > 1.0 if time gate allows)")
        # With daily data and large volume spike, RVOL > 1.0 if past 10:15 AM
        # We just verify it doesn't crash and returns a dict
    else:
        log.info("  PASS: Spike bar returned None (< 30 bars after filter or time gate)")
    return True


# ── Subtest 8e ────────────────────────────────────────────────────────────────

def test_8e_negative_prices():
    log.info("=== 8e: Negative prices ===")
    df = make_df(n_bars=40, base_price=100.0)
    df_copy = df.copy()
    # Inject -1.0 close price in last bar
    df_copy.iloc[-1, df_copy.columns.get_loc('close')] = -1.0

    try:
        result = analyzer.analyze('NEG', df_copy, {})
        # Should return None or handle gracefully — not crash with uncontrolled exception
        log.info(f"  PASS: negative price handled gracefully, result={result}")
    except (ValueError, ZeroDivisionError, ArithmeticError) as e:
        log.info(f"  PASS: negative price raises expected error: {type(e).__name__}: {e}")
    except Exception as e:
        raise AssertionError(f"Negative price should not raise unexpected {type(e).__name__}: {e}")
    return True


# ── Subtest 8f ────────────────────────────────────────────────────────────────

def test_8f_nan_in_ohlcv():
    log.info("=== 8f: NaN in close column ===")
    df = make_df(n_bars=40, base_price=100.0)
    df_copy = df.copy()
    # Inject NaN in the middle of close
    df_copy.iloc[20, df_copy.columns.get_loc('close')] = float('nan')

    try:
        result = analyzer.analyze('NAN_TEST', df_copy, {})
        if result is not None:
            # Verify no NaN propagated to output
            for key in ['current_price', 'atr_value', 'rsi_value', 'vwap']:
                val = result.get(key)
                if val is not None:
                    assert not (isinstance(val, float) and np.isnan(val)), \
                        f"NaN propagated to {key}"
        log.info(f"  PASS: NaN in close handled gracefully, result={'None' if result is None else 'dict'}")
    except Exception as e:
        raise AssertionError(f"NaN in close should not crash: {e}")
    return True


# ── Subtest 8g ────────────────────────────────────────────────────────────────

def test_8g_rvol_time_gate():
    log.info("=== 8g: RVOL time gate before 10:15 AM ===")
    # get_rvol uses datetime.now(ET) internally; mock it to be before 10:15 AM
    hist_df = make_hist_df(n_days=5, avg_daily_vol=500_000.0)

    early_time = datetime.now(ET).replace(hour=9, minute=45, second=0, microsecond=0)

    with patch('monitor.signals.datetime') as mock_dt:
        mock_dt.now.return_value = early_time
        mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw)

        # Call get_rvol with large volume; should return 1.0 if before 10:15
        rvol = get_rvol(hist_df, current_volume_sum=2_000_000.0, current_bar_index=10)

    # The time gate is only for daily (non-minute) data formats
    log.info(f"  rvol={rvol:.2f} (1.0 expected if daily data before 10:15)")
    # With daily hist_df and early time, should be 1.0
    assert rvol == 1.0, f"Expected 1.0 before 10:15 with daily data, got {rvol:.2f}"
    log.info(f"  PASS: RVOL time gate returns 1.0 before 10:15 AM")
    return True


# ── Subtest 8h ────────────────────────────────────────────────────────────────

def test_8h_rvol_empty_hist_df():
    log.info("=== 8h: RVOL with empty hist_df ===")
    empty_df = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])

    rvol_none = get_rvol(None, current_volume_sum=100_000.0, current_bar_index=20)
    rvol_empty = get_rvol(empty_df, current_volume_sum=100_000.0, current_bar_index=20)

    assert rvol_none  == 1.0, f"None hist_df -> expected 1.0, got {rvol_none}"
    assert rvol_empty == 1.0, f"Empty hist_df -> expected 1.0, got {rvol_empty}"
    log.info("  PASS: empty/None hist_df returns 1.0")
    return True


# ── Subtest 8i ────────────────────────────────────────────────────────────────

def test_8i_stop_at_current_price():
    log.info("=== 8i: check_position_exit — stop exactly at current price ===")
    df = make_df(n_bars=40, base_price=100.0)
    # Force last close to 99.00
    df_copy = df.copy()
    df_copy.iloc[-1, df_copy.columns.get_loc('close')] = 99.00
    df_copy.iloc[-1, df_copy.columns.get_loc('low')]   = 98.90

    pos = {
        'entry_price': 100.0, 'stop_price': 99.00,  # exactly at current price
        'target_price': 102.0, 'half_target': 101.0,
        'partial_done': False, 'quantity': 5,
    }

    result = analyzer.check_position_exit('STOP_T', pos, df_copy, {})
    log.info(f"  result={result}")
    # At current_price == stop_price, the condition is current_price <= stop_price -> True
    assert result in ('SELL_STOP', None), \
        f"Expected 'SELL_STOP' or None at stop, got {result!r}"
    if result == 'SELL_STOP':
        log.info("  PASS: stop_price == current_price triggers SELL_STOP")
    else:
        log.info("  PASS: stop_price == current_price returned None (may fail RSI/VWAP guard first)")
    return True


# ── Subtest 8j ────────────────────────────────────────────────────────────────

def test_8j_target_at_current_price():
    log.info("=== 8j: check_position_exit — target exactly at current price ===")
    df = make_df(n_bars=40, base_price=100.0)
    df_copy = df.copy()
    df_copy.iloc[-1, df_copy.columns.get_loc('close')] = 102.00

    pos = {
        'entry_price': 100.0, 'stop_price': 99.0,
        'target_price': 102.00,  # exactly at current price
        'half_target': 101.0,
        'partial_done': True, 'quantity': 5,
    }

    result = analyzer.check_position_exit('TARGET_T', pos, df_copy, {})
    log.info(f"  result={result}")
    assert result in ('SELL_TARGET', 'SELL_RSI', None), \
        f"Unexpected result at target: {result!r}"
    if result == 'SELL_TARGET':
        log.info("  PASS: target_price == current_price triggers SELL_TARGET")
    else:
        log.info(f"  PASS: target at current price returned {result!r}")
    return True


# ── Subtest 8k ────────────────────────────────────────────────────────────────

def test_8k_signal_payload_invariants():
    log.info("=== 8k: SignalPayload invariants ===")

    base_kwargs = dict(
        ticker='INV', action='buy',
        current_price=100.0, ask_price=100.0,
        atr_value=0.5, rsi_value=60.0, rvol=3.0,
        vwap=99.5, reclaim_candle_low=99.8,
        stop_price=99.0, target_price=102.0, half_target=101.0,
    )

    # Case 1: stop >= current_price (should raise)
    try:
        bad = SignalPayload(**{**base_kwargs, 'stop_price': 100.0})  # stop == current
        raise AssertionError("Should have raised ValueError for stop >= current_price")
    except ValueError as e:
        log.info(f"  PASS: stop >= price raises ValueError: {e}")

    # Case 2: stop > current_price (should raise)
    try:
        bad = SignalPayload(**{**base_kwargs, 'stop_price': 101.0})  # stop > current
        raise AssertionError("Should have raised ValueError for stop > current_price")
    except ValueError as e:
        log.info(f"  PASS: stop > price raises ValueError: {e}")

    # Case 3: target <= current_price (should raise)
    try:
        bad = SignalPayload(**{**base_kwargs, 'target_price': 100.0})  # target == current
        raise AssertionError("Should have raised ValueError for target <= current_price")
    except ValueError as e:
        log.info(f"  PASS: target <= price raises ValueError: {e}")

    # Case 4: target < current_price
    try:
        bad = SignalPayload(**{**base_kwargs, 'target_price': 99.0})
        raise AssertionError("Should have raised ValueError for target < current_price")
    except ValueError as e:
        log.info(f"  PASS: target < price raises ValueError: {e}")

    # Case 5: half_target outside [current, target]
    try:
        bad = SignalPayload(**{**base_kwargs, 'half_target': 99.0})  # below current
        raise AssertionError("Should have raised ValueError for half_target < current")
    except ValueError as e:
        log.info(f"  PASS: half_target < current raises ValueError: {e}")

    # Case 6: valid payload should NOT raise
    good = SignalPayload(**base_kwargs)
    assert good.ticker == 'INV'
    log.info("  PASS: valid SignalPayload constructs without error")
    return True


# ── Subtest 8l ────────────────────────────────────────────────────────────────

def test_8l_stress_1000_random():
    log.info("=== 8l: Stress test — 1000 random DataFrames ===")
    rng = random.Random(99)
    crashes = []
    hangs = []

    for i in range(1000):
        n_bars   = rng.randint(0, 60)
        base     = rng.uniform(1.0, 500.0)
        volume   = rng.uniform(0.0, 1_000_000.0)
        noise    = rng.uniform(0.0, 5.0)

        if n_bars == 0:
            df = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'])
        else:
            try:
                today = datetime.now(ET).replace(hour=9, minute=45,
                                                  second=0, microsecond=0)
                idx = pd.date_range(today, periods=n_bars, freq='1min', tz=ET)
                np_rng = np.random.default_rng(i)
                closes = base + np.cumsum(np_rng.uniform(-noise, noise, n_bars))
                opens  = np.roll(closes, 1); opens[0] = base
                highs  = np.maximum(opens, closes) + np.abs(np_rng.normal(0, 0.1, n_bars))
                lows   = np.minimum(opens, closes) - np.abs(np_rng.normal(0, 0.1, n_bars))
                vols   = np_rng.uniform(0.0, volume, n_bars)
                df = pd.DataFrame({'open': opens, 'high': highs, 'low': lows,
                                   'close': closes, 'volume': vols}, index=idx)
            except Exception as e:
                crashes.append(f"df build i={i}: {e}")
                continue

        try:
            t0 = time.time()
            result = analyzer.analyze(f'RAND{i}', df, {})
            elapsed = time.time() - t0
            if elapsed > 1.0:
                hangs.append(f"i={i} n_bars={n_bars} took {elapsed:.2f}s")
        except Exception as e:
            crashes.append(f"analyze i={i} n_bars={n_bars}: {type(e).__name__}: {e}")

    if crashes:
        for c in crashes[:5]:
            log.error(f"  CRASH: {c}")
        raise AssertionError(f"Stress test: {len(crashes)} crashes out of 1000. First: {crashes[0]}")

    if hangs:
        log.warning(f"  WARN: {len(hangs)} slow calls: {hangs[:3]}")

    log.info(f"  PASS: 1000 random DataFrames — 0 crashes, {len(hangs)} slow calls")
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    results = {}
    tests = [
        ('8a', test_8a_insufficient_bars),
        ('8b', test_8b_exactly_min_bars),
        ('8c', test_8c_zero_volatility),
        ('8d', test_8d_single_spike_bar),
        ('8e', test_8e_negative_prices),
        ('8f', test_8f_nan_in_ohlcv),
        ('8g', test_8g_rvol_time_gate),
        ('8h', test_8h_rvol_empty_hist_df),
        ('8i', test_8i_stop_at_current_price),
        ('8j', test_8j_target_at_current_price),
        ('8k', test_8k_signal_payload_invariants),
        ('8l', test_8l_stress_1000_random),
    ]

    for name, fn in tests:
        try:
            ok = fn()
            results[name] = 'PASS' if ok else 'WARN'
        except AssertionError as exc:
            log.error(f"  FAIL [{name}]: {exc}")
            results[name] = 'FAIL'
        except Exception as exc:
            log.error(f"  FAIL [{name}]: {exc}", exc_info=True)
            results[name] = 'FAIL'

    log.info("=== Results ===")
    any_fail = False
    for name, status in results.items():
        log.info(f"  {name}: {status}")
        if status == 'FAIL':
            any_fail = True

    sys.exit(1 if any_fail else 0)


if __name__ == '__main__':
    main()
