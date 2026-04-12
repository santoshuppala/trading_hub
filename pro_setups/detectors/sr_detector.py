"""SRDetector — pivot-based support/resistance levels + S/R flip detection."""
from __future__ import annotations
from typing import List, Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import pivot_highs, pivot_lows


class SRDetector(BaseDetector):
    """
    Identifies pivot highs/lows over the last ``_LOOKBACK`` bars and checks
    whether the current price is within ``_PROXIMITY_PCT`` of any level.

    S/R flip logic: a level that was previously resistance (price was below
    it) and now price is above it acts as support → long signal.  The reverse
    applies for shorts.
    """
    name:           str   = 'sr'
    MIN_BARS:       int   = 30
    _LOOKBACK:      int   = 40
    _PROXIMITY_PCT: float = 0.006   # within 0.6%
    _PIVOT_WINDOW:  int   = 3       # bars on each side for pivot detection

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
    ) -> DetectorSignal:
        window = df.tail(self._LOOKBACK)
        ph = pivot_highs(window, self._PIVOT_WINDOW)
        pl = pivot_lows(window,  self._PIVOT_WINDOW)

        if not ph and not pl:
            return DetectorSignal.no_signal()

        last_close = float(df['close'].iloc[-1])
        all_levels: List[float] = ph + pl

        # Find closest level
        nearest = min(all_levels, key=lambda x: abs(x - last_close))
        dist    = abs(nearest - last_close) / last_close

        if dist > self._PROXIMITY_PCT:
            return DetectorSignal.no_signal()

        # Determine context: price above level → acting as support (long)
        direction = 'long' if last_close > nearest else 'short'
        strength  = 1.0 - (dist / self._PROXIMITY_PCT)

        # S/R flip: level from pivot-high list now acting as support
        flip = (nearest in ph and direction == 'long') or \
               (nearest in pl and direction == 'short')

        if flip:
            strength = min(strength + 0.2, 1.0)

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=strength,
            metadata={
                'level':      nearest,
                'dist_pct':   dist,
                'flip':       flip,
                'ph_count':   len(ph),
                'pl_count':   len(pl),
                'near_levels': sorted(
                    all_levels,
                    key=lambda x: abs(x - last_close)
                )[:3],
            },
        )
