"""FibDetector — price at a key Fibonacci retracement level.

V9: Two-phase Fib detection:
  Phase 1 (9:45 AM): Fib levels from yesterday's range (H→L) via rvol_df.
                     "Where does the overnight gap retrace to?" — standard practice.
  Phase 2 (10:20 AM): Fib levels from today's 5-min swing structure.
                      Replaces daily levels as session develops.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import swing_high, swing_low, fib_levels, nearest_fib


class FibDetector(BaseDetector):
    """
    Computes Fibonacci retracement levels and fires when the current
    close is within proximity of any key level.

    V9: Two-phase approach:
      Phase 1: Fib levels from yesterday's H/L (via rvol_df).
               Available from first bar. Answers: "where does the gap retrace?"
      Phase 2: Fib levels from today's 5-min swing (via df_5min).
               Available after _MIN_5M_BARS. Replaces daily as session matures.

    The 61.8% and 38.2% levels are primary confluences (strength bonus).
    """
    name:            str   = 'fib'
    MIN_BARS:        int   = 15       # reduced: daily Fib needs minimal bars
    _MIN_5M_BARS:    int   = 10      # 10 5-min bars = 50 min for intraday swings
    _LOOKBACK:       int   = 40
    _PROXIMITY_PCT:  float = 0.0025  # within 0.25% of fib level
    _PRIMARY_LEVELS: tuple = ('61.8', '38.2')

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
        precomputed: dict = None,
        **kw,
    ) -> DetectorSignal:
        last_close = float(df['close'].iloc[-1])
        s_low = 0.0
        s_high = 0.0
        source = 'none'

        # ── Phase 2: Intraday 5-min swings (preferred when available) ────
        df_5min = precomputed.get('df_5min') if precomputed else None
        if df_5min is not None and len(df_5min) >= self._MIN_5M_BARS:
            window = df_5min.tail(self._LOOKBACK)
            s_low = swing_low(window, self._LOOKBACK)
            s_high = swing_high(window, self._LOOKBACK)
            source = 'intraday_5m'

        # ── Phase 1: Yesterday's range (fallback for early session) ──────
        if s_high <= s_low and rvol_df is not None and not rvol_df.empty:
            try:
                prev_day = rvol_df.iloc[-1]
                s_high = float(prev_day.get('high', prev_day.get('h', 0)))
                s_low = float(prev_day.get('low', prev_day.get('l', 0)))
                source = 'daily_prev'
            except Exception:
                pass

        if s_high <= s_low:
            return DetectorSignal.no_signal()

        levels = fib_levels(s_low, s_high)
        label, fib_price, dist = nearest_fib(last_close, levels)

        if dist > self._PROXIMITY_PCT:
            return DetectorSignal.no_signal()

        # Determine trend direction from EMA slope
        # Use 5-min EMA when available (stable), else 1-min EMA (noisy but present)
        if precomputed and 'ema_21_5m' in precomputed:
            ema20 = precomputed['ema_21_5m']
        else:
            ema20 = df['close'].ewm(span=20, adjust=False).mean()

        slope_bars = min(6, len(ema20))
        ema20_slope = float(ema20.iloc[-1]) - float(ema20.iloc[-slope_bars])
        direction = 'long' if ema20_slope >= 0 else 'short'

        # Strength: closer = higher; primary levels get bonus
        base_strength = 1.0 - (dist / self._PROXIMITY_PCT)
        primary_bonus = 0.2 if label in self._PRIMARY_LEVELS else 0.0
        strength = min(base_strength + primary_bonus, 1.0)

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=strength,
            metadata={
                'fib_label':   label,
                'fib_price':   fib_price,
                'dist_pct':    dist,
                'swing_low':   s_low,
                'swing_high':  s_high,
                'source':      source,
                'all_levels':  levels,
            },
        )
