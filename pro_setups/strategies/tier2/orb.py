"""
Tier 2 — Opening Range Breakout (ORB).

Entry: close above OR high (long) or below OR low (short) with volume ≥ 1.5×
       session average.

Stop:  1.0 ATR below entry (long) or above entry (short).

Exit:  Partial at 1.5R, Full at 3R.
Trail: EMA20 (exit if 2 closes below EMA20 for longs).
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_atr


class ORB(BaseProStrategy):
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
        orb = detector_outputs.get('orb')
        if not (orb and orb.fired and orb.strength >= 0.50):
            return None
        if not orb.metadata.get('vol_confirm', False):
            return None
        return orb.direction

    def generate_entry(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        direction:        str,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> float:
        # Entry at close of breakout bar
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
            # Also consider the ORB high as a reference
            orb_slice = df.iloc[:15]
            orb_high  = float(orb_slice['high'].max()) if len(orb_slice) >= 15 else entry_price
            struct_stop = orb_high - 0.01    # just below OR high
            stop = max(entry_price - offset, struct_stop)
            stop = min(stop, entry_price - 0.01)
        else:
            orb_slice = df.iloc[:15]
            orb_low   = float(orb_slice['low'].min()) if len(orb_slice) >= 15 else entry_price
            struct_stop = orb_low + 0.01
            stop = min(entry_price + offset, struct_stop)
        return round(stop, 4)
