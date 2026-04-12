"""
VolatilityDetector — Bollinger Band squeeze + directional breakout.

A squeeze is defined as band-width below its own 20-bar average.
Breakout fires when price closes outside the bands after a squeeze.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import compute_bollinger


class VolatilityDetector(BaseDetector):
    """
    Bollinger Band squeeze detector.

    Squeeze condition: current band-width (std-based) is below the
    ``_SQUEEZE_PERIOD``-bar average band-width.  This flags compression.

    Breakout condition: close > upper band (long) or close < lower band
    (short) while in or just exiting a squeeze.

    Strength = how far price has moved beyond the band, normalised by
    the band half-width.
    """
    name:             str   = 'volatility'
    MIN_BARS:         int   = 45      # 20 BB + 20 squeeze lookback + buffer
    _BB_PERIOD:       int   = 20
    _BB_STD:          float = 2.0
    _SQUEEZE_PERIOD:  int   = 20      # bars to average band-width over
    _SQUEEZE_THRESH:  float = 0.85    # current width < 85% of avg → squeeze

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
    ) -> DetectorSignal:
        upper, mid, lower, width_now, pct_b = compute_bollinger(
            df, self._BB_PERIOD, self._BB_STD
        )

        # Historical widths for squeeze baseline
        close  = df['close']
        rm     = close.rolling(self._BB_PERIOD).mean()
        rs     = close.rolling(self._BB_PERIOD).std(ddof=0)
        widths = ((rm + self._BB_STD * rs) - (rm - self._BB_STD * rs)) / rm.replace(0, float('nan'))
        avg_width = float(widths.iloc[-(self._SQUEEZE_PERIOD + 1):-1].mean())

        if avg_width <= 0:
            return DetectorSignal.no_signal()

        squeeze = width_now < self._SQUEEZE_THRESH * avg_width

        last_close = float(df['close'].iloc[-1])

        breakout_up   = last_close > upper
        breakout_down = last_close < lower

        if not (breakout_up or breakout_down):
            return DetectorSignal.no_signal()

        direction  = 'long' if breakout_up else 'short'
        half_width = (upper - lower) / 2.0

        if half_width > 0:
            overshoot = abs(last_close - (upper if breakout_up else lower)) / half_width
        else:
            overshoot = 0.0

        strength = min(0.4 + (0.3 if squeeze else 0.0) + min(overshoot * 0.3, 0.3), 1.0)

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=strength,
            metadata={
                'upper':      upper,
                'mid':        mid,
                'lower':      lower,
                'width_now':  width_now,
                'avg_width':  avg_width,
                'squeeze':    squeeze,
                'pct_b':      pct_b,
            },
        )
