"""
Tier 3 — Bollinger Band Squeeze breakout.

Entry: price breaks outside bands after a compression period.  Volume
       must be above average on the breakout bar.

Stop:  1.5 ATR below the lower band (long) or above upper band (short).

Exit:  Partial at 3R, Full at 6R.
Trail: Structure-based (prior swing).
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_atr


class BollingerSqueeze(BaseProStrategy):
    TIER:      int   = 3
    SL_ATR:    float = 2.5    # V8: widened from 1.5 (post-squeeze needs room)
    PARTIAL_R: float = 3.0
    FULL_R:    float = 6.0

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        vol_sig = detector_outputs.get('volatility')
        if not (vol_sig and vol_sig.fired and vol_sig.strength >= 0.55):
            return None

        if not vol_sig.metadata.get('squeeze', False):
            return None   # require prior squeeze

        # Volume confirmation
        avg_vol  = float(df['volume'].iloc[-21:-1].mean())
        last_vol = float(df['volume'].iloc[-1])
        if avg_vol > 0 and last_vol < avg_vol * 1.2:
            return None

        return vol_sig.direction

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
        offset = self.SL_ATR * atr
        if direction == 'long':
            # Stop below lower BB
            vol_sig = None
            stop    = entry_price - offset
            stop    = min(stop, entry_price - 0.01)
        else:
            stop = entry_price + offset
        return round(stop, 4)
