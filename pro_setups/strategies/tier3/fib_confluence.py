"""
Tier 3 — Fibonacci Confluence.

Entry: price at 38.2% or 61.8% retracement with at least TWO additional
       confirming factors (S/R level, trend alignment, volume).
       VWAP alone does not count (fires every bar).

Stop:  2.0 ATR below the Fib level (long) to allow normal noise.

Exit:  Partial at 3R, Full at 6R (measured from Fib level, not entry).
Trail: Structure-based (swing low/high).
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_atr


class FibConfluence(BaseProStrategy):
    TIER:      int   = 3
    SL_ATR:    float = 2.0
    PARTIAL_R: float = 3.0
    FULL_R:    float = 6.0

    _PRIMARY_FIBS: tuple = ('61.8', '38.2')
    _MIN_STRENGTH: float = 0.70   # was 0.55 — require stronger fib proximity
    _MIN_CONFIRMS: int   = 2      # was 1 — require 2 confirming signals (not just VWAP)

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        fib_sig = detector_outputs.get('fib')
        if not (fib_sig and fib_sig.fired and fib_sig.strength >= self._MIN_STRENGTH):
            return None

        label = fib_sig.metadata.get('fib_label', '')
        if label not in self._PRIMARY_FIBS:
            return None

        direction = fib_sig.direction

        # Confluence: require at least 2 confirming signals
        # VWAP alone doesn't count (it fires every bar — no signal value)
        confirms = 0
        sr_sig   = detector_outputs.get('sr')
        vwap_sig = detector_outputs.get('vwap')
        trend    = detector_outputs.get('trend')

        # S/R and trend are real confirmations
        if sr_sig and sr_sig.fired and sr_sig.direction == direction:
            confirms += 1
        if trend and trend.fired and trend.direction == direction:
            confirms += 1

        # VWAP only counts if we already have 1 real confirmation
        # (prevents VWAP-only "confluence" which fires every bar)
        if confirms >= 1 and vwap_sig and vwap_sig.fired and vwap_sig.direction == direction:
            confirms += 1

        if confirms < self._MIN_CONFIRMS:
            return None

        # Volume confirmation: require above-average volume on signal bar
        try:
            vol = df['volume']
            if len(vol) >= 20:
                avg_vol = float(vol.iloc[-20:].mean())
                last_vol = float(vol.iloc[-1])
                if avg_vol > 0 and last_vol < avg_vol * 1.1:
                    return None  # volume not confirming — no conviction
        except (KeyError, IndexError):
            pass

        # Candle quality: close must be in the strong portion of the bar
        close = float(df['close'].iloc[-1])
        open_ = float(df['open'].iloc[-1])
        high  = float(df['high'].iloc[-1])
        low   = float(df['low'].iloc[-1])

        bar_range = high - low
        if bar_range <= 0:
            return None

        if direction == 'long':
            if close <= open_:
                return None  # bearish candle
            # Close must be in top 30% of bar range
            close_position = (close - low) / bar_range
            if close_position < 0.70:
                return None
        elif direction == 'short':
            if close >= open_:
                return None  # bullish candle
            # Close must be in bottom 30% of bar range
            close_position = (close - low) / bar_range
            if close_position > 0.30:
                return None

        return direction

    def generate_entry(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        direction:        str,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> float:
        fib_sig = detector_outputs.get('fib')
        if fib_sig and fib_sig.fired:
            fib_price = float(fib_sig.metadata.get('fib_price', 0))
            last      = float(df['close'].iloc[-1])
            if 0 < fib_price and abs(last - fib_price) / last < 0.003:
                return round(fib_price, 4)
        return float(df['close'].iloc[-1])

    def generate_stop(
        self,
        entry_price: float,
        direction:   str,
        atr:         float,
        df:          pd.DataFrame,
        outputs:     dict = None,
    ) -> float:
        offset = self.SL_ATR * atr
        if direction == 'long':
            stop = entry_price - offset
            stop = min(stop, entry_price - 0.01)
        else:
            stop = entry_price + offset
        return round(stop, 4)
