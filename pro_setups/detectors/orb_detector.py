"""ORBDetector — Opening Range Breakout (first 15 bars)."""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal


class ORBDetector(BaseDetector):
    """
    Defines the Opening Range as the first ``_ORB_BARS`` 1-minute bars
    (default 15, covering 9:30–9:45 ET).

    Fires when the current close breaks above the OR high or below the OR
    low.  Volume confirmation (last bar >= 1.5× session average) boosts
    strength.
    """
    name:         str   = 'orb'
    MIN_BARS:     int   = 17      # need at least ORB + 2 breakout bars
    _ORB_BARS:    int   = 15
    _VOL_MULT:    float = 1.5
    _MIN_RANGE:   float = 0.003   # OR must be at least 0.3% of price to be meaningful

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
    ) -> DetectorSignal:
        if len(df) < self._ORB_BARS + 2:
            return DetectorSignal.no_signal()

        orb_slice = df.iloc[:self._ORB_BARS]
        orb_high  = float(orb_slice['high'].max())
        orb_low   = float(orb_slice['low'].min())
        orb_range = orb_high - orb_low

        if orb_range / orb_high < self._MIN_RANGE:
            return DetectorSignal.no_signal()

        last_close = float(df['close'].iloc[-1])
        last_vol   = float(df['volume'].iloc[-1])
        avg_vol    = float(df['volume'].mean())

        breakout_up   = last_close > orb_high
        breakout_down = last_close < orb_low

        if not (breakout_up or breakout_down):
            return DetectorSignal.no_signal()

        vol_confirm = avg_vol > 0 and (last_vol / avg_vol) >= self._VOL_MULT

        # Extension beyond the OR level
        if breakout_up:
            extension = (last_close - orb_high) / orb_range
            direction = 'long'
        else:
            extension = (orb_low - last_close) / orb_range
            direction = 'short'

        strength = min(0.5 + (0.25 if vol_confirm else 0.0) + min(extension * 0.5, 0.25), 1.0)

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=strength,
            metadata={
                'orb_high':    orb_high,
                'orb_low':     orb_low,
                'orb_range':   orb_range,
                'vol_confirm': vol_confirm,
                'extension':   extension,
            },
        )
