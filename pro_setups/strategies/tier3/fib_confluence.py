"""
Tier 3 — Fibonacci Confluence.

Entry: price at 38.2% or 61.8% retracement with at least one additional
       confirming factor (S/R level, VWAP, or trend alignment).

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

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        fib_sig = detector_outputs.get('fib')
        if not (fib_sig and fib_sig.fired and fib_sig.strength >= 0.55):
            return None

        label = fib_sig.metadata.get('fib_label', '')
        if label not in self._PRIMARY_FIBS:
            return None   # require primary Fib level

        direction = fib_sig.direction

        # Confluence: at least one additional confirming signal
        confirms = 0
        sr_sig   = detector_outputs.get('sr')
        vwap_sig = detector_outputs.get('vwap')
        trend    = detector_outputs.get('trend')

        if sr_sig   and sr_sig.fired   and sr_sig.direction   == direction: confirms += 1
        if vwap_sig and vwap_sig.fired and vwap_sig.direction == direction: confirms += 1
        if trend    and trend.fired    and trend.direction    == direction: confirms += 1

        if confirms < 1:
            return None   # no confluence

        # Confirming candle: bar closes in the expected direction
        close = float(df['close'].iloc[-1])
        open_ = float(df['open'].iloc[-1])
        if direction == 'long'  and close <= open_:
            return None
        if direction == 'short' and close >= open_:
            return None

        return direction

    def generate_entry(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        direction:        str,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> float:
        # Entry at the Fib level itself if close is very near; otherwise close
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
    ) -> float:
        offset = self.SL_ATR * atr
        if direction == 'long':
            stop = entry_price - offset
            stop = min(stop, entry_price - 0.01)
        else:
            stop = entry_price + offset
        return round(stop, 4)
