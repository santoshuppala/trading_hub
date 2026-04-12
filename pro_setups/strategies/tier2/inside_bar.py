"""
Tier 2 — Inside Bar breakout.

Entry: break above mother bar high (long) or below mother bar low (short).
       Entry is at the current close, which must have breached the mother bar.

Stop:  1.0 ATR below entry.  Structure stop = below mother bar low (long).

Exit:  Partial at 1.5R, Full at 3R.
Trail: EMA20.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_ema


class InsideBar(BaseProStrategy):
    TIER:      int   = 2
    SL_ATR:    float = 1.0
    PARTIAL_R: float = 1.5
    FULL_R:    float = 3.0

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        ib = detector_outputs.get('inside_bar')
        if not (ib and ib.fired):
            return None

        direction = ib.direction

        # Confirm breakout: current close must breach mother bar boundary
        mother_high = float(ib.metadata.get('mother_high', 0))
        mother_low  = float(ib.metadata.get('mother_low',  0))
        last_close  = float(df['close'].iloc[-1])

        if direction == 'long'  and last_close <= mother_high:
            return None
        if direction == 'short' and last_close >= mother_low:
            return None

        return direction

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
    ) -> float:
        offset = self.SL_ATR * atr
        if direction == 'long':
            # Structure stop: below mother bar low
            mother_low = float(df['low'].iloc[-2])
            struct_stop = mother_low - 0.01
            stop = max(entry_price - offset, struct_stop)
            stop = min(stop, entry_price - 0.01)
        else:
            mother_high = float(df['high'].iloc[-2])
            struct_stop = mother_high + 0.01
            stop = min(entry_price + offset, struct_stop)
        return round(stop, 4)
