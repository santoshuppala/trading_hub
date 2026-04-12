"""VWAPDetector — price position relative to session VWAP + reclaim pattern."""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import compute_vwap


class VWAPDetector(BaseDetector):
    """
    Detects whether price is above or below VWAP and flags a reclaim event
    (price dipped below VWAP on the previous bar, now closed above).

    Strength is proportional to the distance from VWAP, normalised to 1.0
    at 3% distance.
    """
    name:     str   = 'vwap'
    MIN_BARS: int   = 5
    _NORM:    float = 0.03   # 3% distance → strength = 1.0

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
    ) -> DetectorSignal:
        vwap        = compute_vwap(df)
        last_close  = float(df['close'].iloc[-1])
        last_vwap   = float(vwap.iloc[-1])

        if last_vwap <= 0:
            return DetectorSignal.no_signal()

        above_vwap  = last_close > last_vwap
        distance    = (last_close - last_vwap) / last_vwap   # signed

        # Reclaim: was below, now above
        reclaim = False
        if len(vwap) >= 2:
            prev_close = float(df['close'].iloc[-2])
            prev_vwap  = float(vwap.iloc[-2])
            reclaim    = (prev_close < prev_vwap) and (last_close > last_vwap)

        direction = 'long' if above_vwap else 'short'
        strength  = min(abs(distance) / self._NORM, 1.0)

        # Boost strength on reclaim (fresh entry signal)
        if reclaim:
            strength = min(strength + 0.25, 1.0)

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=strength,
            metadata={
                'vwap':        last_vwap,
                'distance_pct': distance,
                'above_vwap':  above_vwap,
                'reclaim':     reclaim,
            },
        )
