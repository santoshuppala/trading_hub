"""FibDetector — price at a key Fibonacci retracement level."""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import swing_high, swing_low, fib_levels, nearest_fib


class FibDetector(BaseDetector):
    """
    Identifies swing high and swing low over ``_LOOKBACK`` bars, computes
    the five standard Fibonacci retracement levels, and fires when the
    current close is within ``_PROXIMITY_PCT`` of any key level.

    The 61.8% and 38.2% levels are considered primary confluences and
    receive a strength bonus.

    Direction is determined by whether price is bouncing from a retracement
    in an uptrend (long) or downtrend (short), using EMA20 slope.
    """
    name:            str   = 'fib'
    MIN_BARS:        int   = 30
    _LOOKBACK:       int   = 40
    _PROXIMITY_PCT:  float = 0.004   # within 0.4% of fib level
    _PRIMARY_LEVELS: tuple = ('61.8', '38.2')

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
    ) -> DetectorSignal:
        window   = df.tail(self._LOOKBACK)
        s_low    = swing_low(window, self._LOOKBACK)
        s_high   = swing_high(window, self._LOOKBACK)

        if s_high <= s_low:
            return DetectorSignal.no_signal()

        levels     = fib_levels(s_low, s_high)
        last_close = float(df['close'].iloc[-1])

        label, fib_price, dist = nearest_fib(last_close, levels)

        if dist > self._PROXIMITY_PCT:
            return DetectorSignal.no_signal()

        # Determine trend direction from EMA20 slope
        close_series = df['close']
        ema20 = close_series.ewm(span=20, adjust=False).mean()
        ema20_slope = float(ema20.iloc[-1]) - float(ema20.iloc[-6])
        direction = 'long' if ema20_slope >= 0 else 'short'

        # Strength: closer = higher; primary levels get bonus
        base_strength = 1.0 - (dist / self._PROXIMITY_PCT)
        primary_bonus = 0.2 if label in self._PRIMARY_LEVELS else 0.0
        strength      = min(base_strength + primary_bonus, 1.0)

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=strength,
            metadata={
                'fib_label':  label,
                'fib_price':  fib_price,
                'dist_pct':   dist,
                'swing_low':  s_low,
                'swing_high': s_high,
                'all_levels': levels,
            },
        )
