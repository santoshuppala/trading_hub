"""
Smart Stop Placement — context-aware stop distance computation.

Computes the minimum stop buffer based on three real-time factors
that the structural stop alone ignores:

  1. Volatility regime (recent range vs ATR)
     Calm stock needs tight stop, wild stock needs wide stop.

  2. Time of day
     9:30-10:00 has 3-5x the volatility of 11:00-14:00.
     Same setup needs more room at open, less at lunch.

  3. Structural minimum
     The thesis-invalidation level from the detector is the anchor.
     This module only computes the BUFFER below/above that level.

Usage:
    from monitor.smart_stop import compute_stop_buffer

    buffer = compute_stop_buffer(atr, df)          # bar-level (ProSetupEngine)
    buffer = compute_stop_buffer(atr, tick_state=s) # tick-level (TickDetector)

    stop = structural_level - buffer   (for longs)
    stop = structural_level + buffer   (for shorts)
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

# ── Volatility regime: recent_range / ATR ────────────────────────────
# Measures how active the stock is RIGHT NOW vs its average.
# A stock with ATR=$1 moving $0.10/min is calm. Same stock moving
# $0.80/min is hot. Stop distance must scale with current activity.

_VOL_CALM_THRESHOLD = 0.5    # recent_range < 0.5 ATR = calm
_VOL_HOT_THRESHOLD = 1.5     # recent_range > 1.5 ATR = hot

_VOL_SCALAR_CALM = 0.3       # calm: tight buffer (less noise to survive)
_VOL_SCALAR_NORMAL = 0.5     # normal: standard buffer
_VOL_SCALAR_HOT = 0.8        # hot: wide buffer (lots of noise)

# ── Time of day scalars ──────────────────────────────────────────────
# Market open (9:30-10:00) has 3-5x the tick range of midday.
# Stop that works at noon gets murdered at 9:35.
# These scalars multiply the base buffer.

_TIME_SCALARS = {
    # (hour, minute_start, minute_end): scalar
    # 9:30-10:00: opening volatility, wide stops
    (9, 30, 59): 1.5,
    # 10:00-11:00: settling but still active
    (10, 0, 59): 1.2,
    # 11:00-14:00: lunch/midday, tightest stops
    (11, 0, 59): 0.8,
    (12, 0, 59): 0.8,
    (13, 0, 59): 0.8,
    # 14:00-15:00: afternoon pickup
    (14, 0, 59): 1.0,
    # 15:00-16:00: close volatility, widen again
    (15, 0, 59): 1.3,
}

# ── Absolute bounds ──────────────────────────────────────────────────
_MIN_BUFFER_ATR = 0.25   # never tighter than 0.25 ATR (even calm + lunch)
_MAX_BUFFER_ATR = 1.5    # never wider than 1.5 ATR (even hot + open)
_MAX_RISK_ATR = 2.0      # reject trade if structural stop > 2 ATR away


def _volatility_scalar(recent_range: float, atr: float) -> float:
    """Compute volatility scalar from recent price range vs ATR.

    Args:
        recent_range: high - low of recent bars (last 10 bars for bar-level,
                      or last 30s tick range for tick-level).
        atr: 14-period ATR.

    Returns:
        Scalar in [_VOL_SCALAR_CALM, _VOL_SCALAR_HOT].
    """
    if atr <= 0:
        return _VOL_SCALAR_NORMAL

    ratio = recent_range / atr
    if ratio < _VOL_CALM_THRESHOLD:
        return _VOL_SCALAR_CALM
    elif ratio > _VOL_HOT_THRESHOLD:
        return _VOL_SCALAR_HOT
    else:
        # Linear interpolation between calm and hot
        t = (ratio - _VOL_CALM_THRESHOLD) / (_VOL_HOT_THRESHOLD - _VOL_CALM_THRESHOLD)
        return _VOL_SCALAR_CALM + t * (_VOL_SCALAR_HOT - _VOL_SCALAR_CALM)


def _time_scalar(now_et: datetime = None) -> float:
    """Time-of-day scalar for stop width.

    Returns scalar that widens stops during volatile periods (open, close)
    and tightens during calm periods (lunch).
    """
    if now_et is None:
        now_et = datetime.now(ET)

    h, m = now_et.hour, now_et.minute
    for (th, m_start, m_end), scalar in _TIME_SCALARS.items():
        if h == th and m_start <= m <= m_end:
            return scalar
    return 1.0  # default: outside market hours or unmatched


def compute_stop_buffer(
    atr: float,
    df=None,
    tick_range: float = 0.0,
    n_recent_bars: int = 10,
) -> float:
    """Compute the smart stop buffer distance.

    This is the minimum distance between entry and stop, accounting for
    current volatility regime and time of day.

    Args:
        atr: 14-period ATR (from indicators).
        df: DataFrame of recent bars (for bar-level volatility calc).
            If None, uses tick_range instead.
        tick_range: high-low range from recent ticks (for tick-level).
            Only used if df is None.
        n_recent_bars: number of recent bars to use for volatility calc.

    Returns:
        Buffer distance in price units. Apply as:
            long stop  = max(structural_stop, entry - buffer)
            short stop = min(structural_stop, entry + buffer)
    """
    if atr <= 0:
        return 0.0

    # Step 1: Compute recent range for volatility regime
    if df is not None and len(df) >= 2:
        recent = df.tail(min(n_recent_bars, len(df)))
        recent_range = float(recent['high'].max() - recent['low'].min())
    elif tick_range > 0:
        recent_range = tick_range
    else:
        recent_range = atr  # fallback: assume normal

    # Step 2: Get scalars
    vol_scalar = _volatility_scalar(recent_range, atr)
    time_scalar = _time_scalar()

    # Step 3: Compute buffer
    buffer = atr * vol_scalar * time_scalar

    # Step 4: Clamp to absolute bounds
    buffer = max(buffer, atr * _MIN_BUFFER_ATR)
    buffer = min(buffer, atr * _MAX_BUFFER_ATR)

    return buffer


def should_reject_wide_stop(stop_distance: float, atr: float) -> bool:
    """Returns True if the stop is too far away (bad R:R).

    A trade where risk > 2 ATR almost never has good enough reward
    to justify the position size reduction needed.
    """
    if atr <= 0:
        return False
    return stop_distance > atr * _MAX_RISK_ATR
