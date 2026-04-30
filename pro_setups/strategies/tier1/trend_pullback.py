"""
Tier 1 — Trend Pullback strategy (V10.2 — research-validated).

Based on proven 9 EMA Pullback pattern (71% win rate, 9:45-11:00 AM window).

Entry: EMA9 > EMA20 on 5-min + price pulls back to within 0.7 ATR of EMA9
       or 1.2 ATR of EMA20 + pullback on declining volume + bullish close.

Stop:  Below swing low of last 5 bars (structural) or 0.5 ATR (whichever
       is wider — gives room for EMA retest).

Exit:  Partial 33% at 1.5R, Full at 3R.
Trail: Higher lows (structural).

References:
  - QuantifiedStrategies: EMA pullback backtest results
  - TOS Indicators: QQQ 5-min pullback optimal parameters
  - Grokipedia: 9 EMA Pullback institutional rules
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_ema, compute_atr


class TrendPullback(BaseProStrategy):
    TIER:      int   = 1
    SL_ATR:    float = 0.5
    PARTIAL_R: float = 1.5    # V10.2: was 1.0 — research says 1.5R minimum
    FULL_R:    float = 3.0    # V10.2: was 2.0 — let trend trades run

    # Thresholds
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

        # V10.2: Use ATR-based proximity from detector metadata (not percentage)
        near_ema9  = trend.metadata.get('near_ema9', False)
        near_ema20 = trend.metadata.get('near_ema20', False)

        if not (near_ema9 or near_ema20):
            return None

        # Bullish bar: close above open
        last_close = float(df['close'].iloc[-1])
        last_open  = float(df['open'].iloc[-1])
        if last_close <= last_open:
            return None

        # V10.2: Candle quality — close in top 40% of bar range
        # Research: weak doji-like candles are not valid bounce confirmation
        bar_range = float(df['high'].iloc[-1]) - float(df['low'].iloc[-1])
        if bar_range > 0:
            close_position = (last_close - float(df['low'].iloc[-1])) / bar_range
            if close_position < 0.40:
                return None

        # V10.2: Pullback volume filter — pullback should be on declining volume
        # Research: high-volume pullback = distribution, low-volume = healthy retracement
        if len(df) >= 10:
            recent_vol = float(df['volume'].iloc[-1])
            avg_vol = float(df['volume'].iloc[-10:-1].mean())
            if avg_vol > 0 and recent_vol > avg_vol * 1.5:
                return None  # pullback on high volume = selling, not retracement

        # Price above VWAP (mandatory for longs)
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
        # V10.2: Thesis-based stop — below the EMA that was tested.
        # Research: "stop just below EMA9 or pullback candle low"
        # If price bounced off EMA9, stop below EMA9 (thesis = EMA9 is support)
        # If price bounced off EMA20, stop below EMA20 (thesis = EMA20 is support)
        # Buffer: 0.2 ATR below the EMA (room for wick noise)
        # Floor: never tighter than 0.3 ATR from entry
        trend_meta = {}
        if outputs and 'trend' in outputs:
            trend_meta = outputs['trend'].metadata or {}

        ema9  = trend_meta.get('ema9', 0)
        ema20 = trend_meta.get('ema20', 0)
        near_ema9  = trend_meta.get('near_ema9', False)
        near_ema20 = trend_meta.get('near_ema20', False)

        buffer = atr * 0.2  # room below EMA for wick noise

        if near_ema9 and ema9 > 0:
            # Thesis: EMA9 is support → stop just below EMA9
            thesis_stop = ema9 - buffer
        elif near_ema20 and ema20 > 0:
            # Thesis: EMA20 is support → stop just below EMA20
            thesis_stop = ema20 - buffer
        else:
            # Fallback: swing low of last 5 bars
            thesis_stop = float(df['low'].tail(5).min()) - 0.01

        # Floor: never tighter than 0.3 ATR from entry
        min_stop = entry_price - atr * 0.3
        stop = min(thesis_stop, min_stop)

        # Safety: ensure stop is below entry
        stop = min(stop, entry_price - 0.01)
        return round(stop, 4)
