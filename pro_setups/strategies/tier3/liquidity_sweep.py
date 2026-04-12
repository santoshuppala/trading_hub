"""
Tier 3 — Liquidity Sweep (Smart Money Reversal).

Entry: after a stop-hunt sweep below a swing low (or above a swing high),
       price reverses back above (or below) the swept level.  Entry is
       confirmed on the current bar closing back inside the range.

Stop:  2.0 ATR below entry (longs) — wide stop to survive re-tests.

Exit:  Partial at 3R, Full at 6R.
Trail: Structure-based — exit when price makes lower low (long) or higher
       high (short) on a closing basis.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_atr


class LiquiditySweep(BaseProStrategy):
    TIER:      int   = 3
    SL_ATR:    float = 2.0
    PARTIAL_R: float = 3.0
    FULL_R:    float = 6.0

    _MIN_WICK_PCT: float = 0.35

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        liq = detector_outputs.get('liquidity')
        if not (liq and liq.fired and liq.strength >= 0.55):
            return None

        wick_pct = liq.metadata.get('wick_pct', 0.0)
        if wick_pct < self._MIN_WICK_PCT:
            return None

        # Volume confirmation: last bar volume above average
        avg_vol  = float(df['volume'].iloc[-21:-1].mean())
        last_vol = float(df['volume'].iloc[-1])
        if avg_vol > 0 and last_vol < avg_vol * 0.8:
            return None   # low volume reversal is weak

        return liq.direction

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
            # Stop below the sweep low (prev bar's low)
            sweep_low   = float(df['low'].iloc[-2])
            struct_stop = sweep_low - 0.01
            stop = min(entry_price - offset, struct_stop)
            stop = min(stop, entry_price - 0.01)
        else:
            sweep_high  = float(df['high'].iloc[-2])
            struct_stop = sweep_high + 0.01
            stop = max(entry_price + offset, struct_stop)
        return round(stop, 4)
