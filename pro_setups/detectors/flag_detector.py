"""FlagDetector — bull/bear flag: pole + tight consolidation + breakout."""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import compute_atr


class FlagDetector(BaseDetector):
    """
    Bull flag: strong upward pole (≥3× ATR in ≤8 bars) followed by a
    tight downward-sloping channel (consolidation ≤ 1.5× ATR range) that
    has now broken upward.

    Bear flag: mirror image.

    Strength is based on pole size / ATR ratio and tightness of the flag.
    """
    name:            str   = 'flag'
    MIN_BARS:        int   = 25
    _POLE_BARS_MAX:  int   = 8
    _POLE_ATR_MULT:  float = 3.0   # pole must be ≥ 3× ATR
    _FLAG_BARS:      int   = 10    # consolidation window to check
    _FLAG_ATR_MULT:  float = 1.5   # flag range must be ≤ 1.5× ATR
    _BREAKOUT_MULT:  float = 0.3   # current close must exceed flag by 0.3× ATR

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
    ) -> DetectorSignal:
        atr = compute_atr(df)

        # Identify pole: look for large directional move before the flag window
        flag_start = max(0, len(df) - self._FLAG_BARS - self._POLE_BARS_MAX)
        flag_end   = max(0, len(df) - self._FLAG_BARS)

        if flag_end <= flag_start:
            return DetectorSignal.no_signal()

        pole_slice = df.iloc[flag_start:flag_end]
        pole_move  = float(pole_slice['close'].iloc[-1]) - float(pole_slice['close'].iloc[0])

        if abs(pole_move) < self._POLE_ATR_MULT * atr:
            return DetectorSignal.no_signal()

        bull_flag = pole_move > 0

        # Flag: consolidation in last _FLAG_BARS bars
        flag_slice  = df.tail(self._FLAG_BARS)
        flag_high   = float(flag_slice['high'].max())
        flag_low    = float(flag_slice['low'].min())
        flag_range  = flag_high - flag_low

        if flag_range > self._FLAG_ATR_MULT * atr:
            return DetectorSignal.no_signal()

        last_close = float(df['close'].iloc[-1])

        # Breakout: close breaks out of flag
        if bull_flag:
            if last_close < flag_high + self._BREAKOUT_MULT * atr:
                return DetectorSignal.no_signal()
            direction = 'long'
        else:
            if last_close > flag_low - self._BREAKOUT_MULT * atr:
                return DetectorSignal.no_signal()
            direction = 'short'

        tightness = 1.0 - min(flag_range / (self._FLAG_ATR_MULT * atr), 1.0)
        pole_power = min(abs(pole_move) / (self._POLE_ATR_MULT * atr * 2), 1.0)
        strength   = (tightness * 0.4 + pole_power * 0.6)

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=min(strength, 1.0),
            metadata={
                'pole_move':   pole_move,
                'flag_range':  flag_range,
                'flag_high':   flag_high,
                'flag_low':    flag_low,
                'atr':         atr,
            },
        )
