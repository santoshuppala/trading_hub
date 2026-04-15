"""
Tier 1 — S/R Flip strategy.

Entry: price is at a former resistance level now acting as support
       (or former support acting as resistance).  Confirmation: bar closes
       in the direction of the flip with volume.

Stop:  0.3 ATR below the S/R level (or 0.3 ATR above for shorts).

Exit:  Partial at 1R, Full at 2R.
Trail: Higher lows (long) / lower highs (short).
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_ema


class SRFlip(BaseProStrategy):
    TIER:      int   = 1
    SL_ATR:    float = 0.3
    PARTIAL_R: float = 1.0
    FULL_R:    float = 2.0

    _MIN_FLIP_STR: float = 0.60   # require strong S/R signal

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        sr_sig = detector_outputs.get('sr')
        if not (sr_sig and sr_sig.fired and sr_sig.strength >= self._MIN_FLIP_STR):
            return None
        if not sr_sig.metadata.get('flip', False):
            return None

        direction = sr_sig.direction
        if direction not in ('long', 'short'):
            return None

        # Confirm bar closes in the flip direction
        close = float(df['close'].iloc[-1])
        open_ = float(df['open'].iloc[-1])
        if direction == 'long' and close <= open_:
            return None
        if direction == 'short' and close >= open_:
            return None

        # Trend alignment bonus (not required, but checked)
        trend = detector_outputs.get('trend')
        if trend and trend.fired and trend.direction != direction and trend.strength > 0.7:
            return None   # strong counter-trend — skip

        return direction

    def generate_entry(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        direction:        str,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> float:
        return float(df['close'].iloc[-1])

    # Uses BaseProStrategy.generate_stop() — structure-aware with SR levels
