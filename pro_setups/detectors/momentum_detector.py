"""
MomentumDetector — volume thrust + price expansion (momentum ignition).

Fires when:
  1. Current bar volume >= 3× 20-bar average.
  2. Price move in last 3 bars >= 1.5× ATR.
  3. Bar closes in the top/bottom 25% of its range (confirming direction).
"""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import compute_atr, compute_rvol


class MomentumDetector(BaseDetector):
    """
    Momentum Ignition detector.

    Volume thrust: current bar volume ≥ ``_VOL_MULT``× rolling average.
    Price expansion: 3-bar close move ≥ ``_MOVE_ATR``× ATR.
    Directional close: close in top/bottom ``_CLOSE_PCT``% of bar range.
    """
    name:          str   = 'momentum'
    MIN_BARS:      int   = 25
    _VOL_MULT:     float = 3.0    # volume must be 3× average
    _MOVE_ATR:     float = 1.5    # 3-bar price move must be 1.5× ATR
    _CLOSE_PCT:    float = 0.25   # close must be in top/bottom 25% of range

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
    ) -> DetectorSignal:
        atr        = compute_atr(df)
        last_vol   = float(df['volume'].iloc[-1])
        avg_vol    = float(df['volume'].iloc[-21:-1].mean())   # 20-bar avg excl. current

        if avg_vol <= 0:
            return DetectorSignal.no_signal()

        vol_ratio  = last_vol / avg_vol
        if vol_ratio < self._VOL_MULT:
            return DetectorSignal.no_signal()

        # 3-bar price move
        close_3bar = float(df['close'].iloc[-1]) - float(df['close'].iloc[-4])
        if abs(close_3bar) < self._MOVE_ATR * atr:
            return DetectorSignal.no_signal()

        # Directional close (close in top or bottom of current bar range)
        cur_high = float(df['high'].iloc[-1])
        cur_low  = float(df['low'].iloc[-1])
        cur_close= float(df['close'].iloc[-1])
        bar_range = cur_high - cur_low

        if bar_range <= 0:
            return DetectorSignal.no_signal()

        close_position = (cur_close - cur_low) / bar_range   # 0=at low, 1=at high

        if close_3bar > 0:
            # Long momentum: close must be in top 25%
            if close_position < (1.0 - self._CLOSE_PCT):
                return DetectorSignal.no_signal()
            direction = 'long'
        else:
            # Short momentum: close must be in bottom 25%
            if close_position > self._CLOSE_PCT:
                return DetectorSignal.no_signal()
            direction = 'short'

        # Strength based on volume ratio and price expansion
        vol_score   = min((vol_ratio - self._VOL_MULT) / self._VOL_MULT, 1.0)
        move_score  = min(abs(close_3bar) / (self._MOVE_ATR * atr * 2), 1.0)
        strength    = 0.5 + vol_score * 0.3 + move_score * 0.2

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=min(strength, 1.0),
            metadata={
                'vol_ratio':      vol_ratio,
                'close_3bar':     close_3bar,
                'close_position': close_position,
                'atr':            atr,
            },
        )
