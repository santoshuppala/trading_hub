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
    name:          str = 'trend'
    MIN_BARS:      int = 52      # need 50 bars for EMA50 (1-min)
    _MIN_5M_BARS:  int = 12      # V9: 12 5-min bars = 60 min for session trend
    _LOOKBACK: int = 20
    _MIN_STR:  float = 0.45  # require 45% of bars to be trending structure

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
        precomputed: dict = None,
        **kw,
    ) -> DetectorSignal:
        # V9: Use 5-min bars for trend detection
        # EMA50 on 5-min = 250 min (full session trend) vs 50 min (incomplete)
        work_df = precomputed.get('df_5min', df) if precomputed else df
        if len(work_df) < self._MIN_5M_BARS:
            return DetectorSignal.no_signal()

        # Use precomputed 5-min EMAs if available
        ema9 = precomputed.get('ema_9_5m') if precomputed else compute_ema(work_df['close'], 9)
        ema20 = precomputed.get('ema_21_5m') if precomputed else compute_ema(work_df['close'], 20)
        ema50 = precomputed.get('ema_50_5m') if precomputed else compute_ema(work_df['close'], 50)

        if ema9 is None or ema20 is None or ema50 is None or len(ema50) == 0:
            return DetectorSignal.no_signal()

        last_e9  = float(ema9.iloc[-1])
        last_e20 = float(ema20.iloc[-1])
        last_e50 = float(ema50.iloc[-1])

        uptrend   = last_e9 > last_e20 > last_e50
        downtrend = last_e9 < last_e20 < last_e50

        if not (uptrend or downtrend):
            return DetectorSignal.no_signal()

        # Structural confirmation over last N bars (on 5-min)
        highs = work_df['high'].values[-self._LOOKBACK:]
        lows  = work_df['low'].values[-self._LOOKBACK:]
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
        # Use latest 1-min close for most current price
        last_close  = float(df['close'].iloc[-1])
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
