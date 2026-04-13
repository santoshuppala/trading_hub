"""
Tier 2 — Flag / Pennant breakout.

Entry: break above flag channel high (bull flag) or below flag channel low
       (bear flag).  The FlagDetector has already confirmed the pole and
       consolidation structure.

Stop:  0.8 ATR below the flag low (long) or above flag high (short).

Exit:  Partial at 1.5R (≈ pole height midpoint), Full at 3R (pole height).
Trail: EMA20.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_atr


class FlagPennant(BaseProStrategy):
    TIER:      int   = 2
    SL_ATR:    float = 0.8
    PARTIAL_R: float = 1.5
    FULL_R:    float = 3.0

    def __init__(self):
        self._last_flag_low:  Optional[float] = None
        self._last_flag_high: Optional[float] = None

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        flag = detector_outputs.get('flag')
        if not (flag and flag.fired and flag.strength >= 0.45):
            return None
        # Cache flag boundaries from detector metadata for generate_stop()
        self._last_flag_low  = flag.metadata.get('flag_low')
        self._last_flag_high = flag.metadata.get('flag_high')
        return flag.direction

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
        flag_sig   = None
        offset     = self.SL_ATR * atr

        # Use flag channel boundaries from detector metadata when available
        if direction == 'long':
            flag_low = float(self._last_flag_low) if self._last_flag_low else float(df.tail(10)['low'].min())
            stop     = max(flag_low - 0.01, entry_price - offset)
            stop     = min(stop, entry_price - 0.01)
        else:
            flag_high = float(self._last_flag_high) if self._last_flag_high else float(df.tail(10)['high'].max())
            stop      = min(flag_high + 0.01, entry_price + offset)
        return round(stop, 4)
