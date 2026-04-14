"""
Shared technical-indicator computations for pro_setups detectors.
All functions are pure (no side effects) and work on pandas DataFrames/Series.
Expected DataFrame columns: open, high, low, close, volume (lowercase).
"""
from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd


# ── Core indicators ───────────────────────────────────────────────────────────

def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative session VWAP.  Returns a Series aligned with df.index."""
    tp = (df['high'] + df['low'] + df['close']) / 3.0
    cum_vol = df['volume'].cumsum()
    cum_tp  = (tp * df['volume']).cumsum()
    return (cum_tp / cum_vol.replace(0, np.nan)).ffill()


def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Wilder-smoothed ATR.  Returns a positive float."""
    if len(df) < 2:
        val = float(df['high'].iloc[-1] - df['low'].iloc[-1])
        return val if val > 0 else 0.01
    h, l, c = df['high'], df['low'], df['close']
    prev_c = c.shift(1)
    tr = pd.concat(
        [h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1
    ).max(axis=1)
    result = float(tr.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])
    return result if math.isfinite(result) and result > 0 else 0.01


def compute_rsi(df: pd.DataFrame, period: int = 14) -> float:
    """RSI via Wilder EWM.  Returns value in [0.0, 100.0]."""
    c = df['close']
    if len(c) < period + 1:
        return 50.0
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1.0 / period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1.0 / period, adjust=False).mean()
    rs    = gain / loss.replace(0, 1e-10)
    rsi   = float(100.0 - 100.0 / (1.0 + rs.iloc[-1]))
    return max(0.0, min(100.0, rsi)) if math.isfinite(rsi) else 50.0


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    """Standard exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def compute_rvol(df: pd.DataFrame, rvol_df: Optional[pd.DataFrame]) -> float:
    """
    Relative volume: uses canonical RVOLEngine when available,
    falls back to simple ratio for backward compatibility.
    """
    try:
        from monitor.rvol import _global_rvol_engine
        if _global_rvol_engine is not None:
            # Get ticker from df index name or use generic
            ticker = getattr(df, 'name', None) or 'UNKNOWN'
            if isinstance(ticker, str) and ticker != 'UNKNOWN':
                return _global_rvol_engine.get_rvol(ticker)
    except (ImportError, Exception):
        pass

    # Fallback: simple ratio
    if rvol_df is None or len(rvol_df) == 0:
        return 1.0
    hist_avg = float(rvol_df['volume'].mean())
    curr_avg = float(df['volume'].mean())
    if hist_avg <= 0:
        return 1.0
    r = curr_avg / hist_avg
    return r if math.isfinite(r) else 1.0


# ── Bollinger Bands ──────────────────────────────────────────────────────────

def compute_bollinger(
    df: pd.DataFrame,
    period: int = 20,
    std_mult: float = 2.0,
) -> Tuple[float, float, float, float, float]:
    """
    Returns (upper, mid, lower, width_pct, pct_b).

    width_pct : (upper - lower) / mid — dimensionless bandwidth
    pct_b     : position within bands [0=lower, 0.5=mid, 1=upper]
    """
    c = df['close']
    if len(c) < period:
        mid = float(c.iloc[-1])
        return mid, mid, mid, 0.0, 0.5
    roll_mean = c.rolling(period).mean()
    roll_std  = c.rolling(period).std(ddof=0)
    mid   = float(roll_mean.iloc[-1])
    std   = float(roll_std.iloc[-1])
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    width = (upper - lower) / mid if mid > 0 else 0.0
    band_range = upper - lower
    pct_b = (float(c.iloc[-1]) - lower) / band_range if band_range > 0 else 0.5
    return upper, mid, lower, width, pct_b


# ── Swing structure ──────────────────────────────────────────────────────────

def swing_high(df: pd.DataFrame, lookback: int = 20) -> float:
    return float(df['high'].tail(lookback).max())


def swing_low(df: pd.DataFrame, lookback: int = 20) -> float:
    return float(df['low'].tail(lookback).min())


def pivot_highs(df: pd.DataFrame, window: int = 2) -> list[float]:
    """
    Local pivot highs: bars where high is strictly greater than the
    ``window`` bars on each side.
    """
    h = df['high'].values
    result = []
    for i in range(window, len(h) - window):
        if all(h[i] > h[i - j] for j in range(1, window + 1)) and \
           all(h[i] > h[i + j] for j in range(1, window + 1)):
            result.append(float(h[i]))
    return result


def pivot_lows(df: pd.DataFrame, window: int = 2) -> list[float]:
    l = df['low'].values
    result = []
    for i in range(window, len(l) - window):
        if all(l[i] < l[i - j] for j in range(1, window + 1)) and \
           all(l[i] < l[i + j] for j in range(1, window + 1)):
            result.append(float(l[i]))
    return result


# ── Fibonacci retracement levels ─────────────────────────────────────────────

_FIB_RATIOS: Dict[str, float] = {
    '23.6': 0.236,
    '38.2': 0.382,
    '50.0': 0.500,
    '61.8': 0.618,
    '78.6': 0.786,
}


def fib_levels(swing_lo: float, swing_hi: float) -> Dict[str, float]:
    """Retracement levels from swing_hi down to swing_lo."""
    rng = swing_hi - swing_lo
    return {k: swing_hi - r * rng for k, r in _FIB_RATIOS.items()}


def nearest_fib(price: float, levels: Dict[str, float]) -> Tuple[str, float, float]:
    """
    Returns (label, fib_price, distance_pct) for the Fibonacci level
    closest to ``price``.
    """
    best_label = ''
    best_price = 0.0
    best_dist  = float('inf')
    for label, fp in levels.items():
        d = abs(price - fp) / price
        if d < best_dist:
            best_dist  = d
            best_price = fp
            best_label = label
    return best_label, best_price, best_dist
