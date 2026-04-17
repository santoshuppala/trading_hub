"""
Tier 3 — Momentum Ignition.

Entry: 3× volume thrust + 1.5 ATR price expansion in 3 bars + close in
       top/bottom 25% of bar range.  Trend context preferred.

Stop:  1.5 ATR below entry.  Wide enough to survive an initial retest.

Exit:  Partial at 3R, Full at 8R — momentum setups can run far.
Trail: Structure-based: trailing 3-bar low (long) or high (short).
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_atr


class MomentumIgnition(BaseProStrategy):
    TIER:      int   = 3
    SL_ATR:    float = 2.0    # V8: widened from 1.5 (multi-day holding)
    PARTIAL_R: float = 3.0
    FULL_R:    float = 8.0     # high expectancy — let winners run

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        mom = detector_outputs.get('momentum')
        if not (mom and mom.fired and mom.strength >= 0.65):
            return None

        vol_ratio = mom.metadata.get('vol_ratio', 0.0)
        if vol_ratio < 2.5:
            return None   # absolute volume minimum

        direction = mom.direction

        # Trend alignment preferred (not required for ignition)
        trend = detector_outputs.get('trend')
        if trend and trend.fired and trend.direction != direction and trend.strength > 0.75:
            return None   # strong opposing trend — skip

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
        outputs:     dict = None,
    ) -> float:
        offset = self.SL_ATR * atr
        if direction == 'long':
            # Stop below 3-bar low for structure reference
            three_bar_low = float(df['low'].tail(3).min())
            stop = min(entry_price - offset, three_bar_low - 0.01)
            stop = min(stop, entry_price - 0.01)
        else:
            three_bar_high = float(df['high'].tail(3).max())
            stop = max(entry_price + offset, three_bar_high + 0.01)
        return round(stop, 4)
