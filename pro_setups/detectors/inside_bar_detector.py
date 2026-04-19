"""InsideBarDetector — inside bar (harami) pattern with trend context."""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import compute_ema


class InsideBarDetector(BaseDetector):
    """
    Inside bar: current bar's high <= previous bar's high AND
                current bar's low  >= previous bar's low.

    Strength is based on how 'tight' the inside bar is relative to the
    mother bar (smaller inside bar = tighter coil = higher strength).

    Direction is determined by the trend context (EMA20 slope).
    """
    name:          str = 'inside_bar'
    MIN_BARS:      int = 22
    _MIN_5M_BARS:  int = 4       # V9: 4 5-min bars = 20 min

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
        precomputed: dict = None,
        **kw,
    ) -> DetectorSignal:
        # V9: Use 5-min bars — inside bars on 1-min are noise (every 30s)
        work_df = precomputed.get('df_5min', df) if precomputed else df
        if len(work_df) < self._MIN_5M_BARS:
            return DetectorSignal.no_signal()

        cur_high  = float(work_df['high'].iloc[-1])
        cur_low   = float(work_df['low'].iloc[-1])
        prev_high = float(work_df['high'].iloc[-2])
        prev_low  = float(work_df['low'].iloc[-2])

        if not (cur_high <= prev_high and cur_low >= prev_low):
            return DetectorSignal.no_signal()

        # Tightness: inside bar range / mother bar range
        mother_range = max(prev_high - prev_low, 1e-6)
        inside_range = cur_high - cur_low
        tightness    = 1.0 - (inside_range / mother_range)   # 0=same size, 1=zero size

        # Direction from EMA20 slope (last 5 bars of 5-min = 25 min)
        ema20 = precomputed.get('ema_21_5m') if precomputed else compute_ema(work_df['close'], 20)
        slope_bars = min(6, len(ema20))
        slope = float(ema20.iloc[-1]) - float(ema20.iloc[-slope_bars])
        direction = 'long' if slope >= 0 else 'short'

        strength = max(0.3, min(tightness, 1.0))

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=strength,
            metadata={
                'mother_high':  prev_high,
                'mother_low':   prev_low,
                'inside_range': inside_range,
                'mother_range': mother_range,
                'tightness':    tightness,
                'ema20_slope':  slope,
            },
        )
