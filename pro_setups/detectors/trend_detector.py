"""TrendDetector — EMA alignment + higher-high/higher-low structure."""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import compute_ema, compute_atr


class TrendDetector(BaseDetector):
    """
    Fires when EMA9 > EMA20 > EMA50 (uptrend) or EMA9 < EMA20 < EMA50
    (downtrend), confirmed by a minimum fraction of higher-highs + higher-lows
    (or lower-highs + lower-lows for down).

    Strength = fraction of HH+HL pairs in last ``_LOOKBACK`` bars.
    """
    name:      str = 'trend'
    MIN_BARS:  int = 52      # need 50 bars for EMA50
    _LOOKBACK: int = 20
    _MIN_STR:  float = 0.45  # require 45% of bars to be trending structure

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
    ) -> DetectorSignal:
        close = df['close']
        ema9  = compute_ema(close, 9)
        ema20 = compute_ema(close, 20)
        ema50 = compute_ema(close, 50)

        last_e9  = float(ema9.iloc[-1])
        last_e20 = float(ema20.iloc[-1])
        last_e50 = float(ema50.iloc[-1])

        uptrend   = last_e9 > last_e20 > last_e50
        downtrend = last_e9 < last_e20 < last_e50

        if not (uptrend or downtrend):
            return DetectorSignal.no_signal()

        # Structural confirmation over last N bars
        highs = df['high'].values[-self._LOOKBACK:]
        lows  = df['low'].values[-self._LOOKBACK:]
        n     = len(highs) - 1
        if n <= 0:
            return DetectorSignal.no_signal()

        if uptrend:
            hh = sum(1 for i in range(1, n + 1) if highs[i] > highs[i - 1])
            hl = sum(1 for i in range(1, n + 1) if lows[i]  > lows[i - 1])
            strength = (hh + hl) / (2 * n)
            direction = 'long'
        else:
            lh = sum(1 for i in range(1, n + 1) if highs[i] < highs[i - 1])
            ll = sum(1 for i in range(1, n + 1) if lows[i]  < lows[i - 1])
            strength = (lh + ll) / (2 * n)
            direction = 'short'

        if strength < self._MIN_STR:
            return DetectorSignal.no_signal()

        # Pullback context: is price near EMA20 (good entry zone)?
        last_close  = float(close.iloc[-1])
        dist_ema20  = abs(last_close - last_e20) / last_close
        near_ema20  = dist_ema20 < 0.015      # within 1.5%
        dist_ema9   = abs(last_close - last_e9) / last_close
        near_ema9   = dist_ema9 < 0.008       # within 0.8%

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=min(strength, 1.0),
            metadata={
                'ema9':       last_e9,
                'ema20':      last_e20,
                'ema50':      last_e50,
                'near_ema9':  near_ema9,
                'near_ema20': near_ema20,
                'dist_ema20': dist_ema20,
            },
        )
