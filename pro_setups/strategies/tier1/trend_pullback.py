"""
Tier 1 — Trend Pullback strategy.

Entry: EMA9/20/50 aligned uptrend + price pulled back to within 1% of
       EMA9 or EMA20 + bullish close (close > open).

Stop:  0.4 ATR below entry (or below swing low of last 5 bars, whichever
       is tighter).

Exit:  Partial at 1R, Full at 2R.
Trail: Higher lows (long) — handled at PositionManager level.

Log detail: entry/stop/target levels with R value and ATR context.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_ema, compute_atr


class TrendPullback(BaseProStrategy):
    TIER:      int   = 1
    SL_ATR:    float = 0.5    # V8: widened from 0.4
    PARTIAL_R: float = 1.0
    FULL_R:    float = 2.0

    # Thresholds
    _EMA9_PROXIMITY:  float = 0.010   # within 1.0% of EMA9
    _EMA20_PROXIMITY: float = 0.015   # within 1.5% of EMA20
    _MIN_TREND_STR:   float = 0.50    # trend detector strength threshold

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        trend = detector_outputs.get('trend')
        if not (trend and trend.fired and trend.direction == 'long'
                and trend.strength >= self._MIN_TREND_STR):
            return None

        close  = df['close']
        ema9   = compute_ema(close, 9)
        ema20  = compute_ema(close, 20)
        last   = float(close.iloc[-1])

        near_ema9  = abs(last - float(ema9.iloc[-1]))  / last <= self._EMA9_PROXIMITY
        near_ema20 = abs(last - float(ema20.iloc[-1])) / last <= self._EMA20_PROXIMITY

        if not (near_ema9 or near_ema20):
            return None

        # Bullish bar: close above open
        if float(df['close'].iloc[-1]) <= float(df['open'].iloc[-1]):
            return None

        # Price above VWAP (if available)
        vwap_sig = detector_outputs.get('vwap')
        if vwap_sig and vwap_sig.fired and not vwap_sig.metadata.get('above_vwap', True):
            return None

        return 'long'

    def generate_entry(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        direction:        str,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> float:
        return float(df['close'].iloc[-1])

    def generate_stop(
        self,
        entry_price: float,
        direction:   str,
        atr:         float,
        df:          pd.DataFrame,
        outputs:     dict = None,
    ) -> float:
        atr_stop    = entry_price - self.SL_ATR * atr
        swing_stop  = float(df['low'].tail(5).min())
        # Use the higher of the two stops (tighter stop)
        stop = max(atr_stop, swing_stop)
        # Ensure stop is below entry with minimum buffer
        stop = min(stop, entry_price - 0.01)
        return round(stop, 4)
