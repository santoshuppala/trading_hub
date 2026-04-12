"""
Tier 1 — VWAP Reclaim strategy (pro_setups version).

Entry: price dipped below VWAP on the previous bar and closed above
       VWAP on the current bar (reclaim confirmed).  Uptrend context
       preferred (EMA20 slope positive).

Stop:  0.4 ATR below entry OR below the reclaim candle low, whichever
       is tighter (higher stop value for longs).

Exit:  Partial at 1R, Full at 2R.
Trail: Higher lows — handled at PositionManager level.

Note: This is the pro_setups version.  The existing T4 StrategyEngine
(monitor/strategy_engine.py) runs its own VWAP Reclaim with 9 filters.
These are additive, not overlapping.
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_ema


class VWAPReclaim(BaseProStrategy):
    TIER:      int   = 1
    SL_ATR:    float = 0.4
    PARTIAL_R: float = 1.0
    FULL_R:    float = 2.0

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        vwap_sig = detector_outputs.get('vwap')
        if not (vwap_sig and vwap_sig.fired):
            return None
        if not vwap_sig.metadata.get('reclaim', False):
            return None
        # Must be closing above VWAP (not just touching)
        if not vwap_sig.metadata.get('above_vwap', False):
            return None

        # EMA20 slope check: prefer positive (uptrend context)
        ema20 = compute_ema(df['close'], 20)
        slope = float(ema20.iloc[-1]) - float(ema20.iloc[-6])
        if slope < 0:
            return None   # avoid counter-trend reclaims

        # Bullish close bar
        if float(df['close'].iloc[-1]) <= float(df['open'].iloc[-1]):
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
    ) -> float:
        atr_stop       = entry_price - self.SL_ATR * atr
        reclaim_candle = float(df['low'].iloc[-1])      # low of reclaim bar
        stop = max(atr_stop, reclaim_candle - 0.01)
        stop = min(stop, entry_price - 0.01)
        return round(stop, 4)
