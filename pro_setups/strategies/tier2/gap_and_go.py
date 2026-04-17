"""
Tier 2 — Gap and Go.

Entry: Gap up ≥ 0.5%, price has not filled the gap (no full retrace to
       previous close), current bar is a continuation bar (close near
       session high, volume elevated).

Stop:  1.0 ATR below entry.

Exit:  Partial at 1.5R, Full at 3R.
Trail: VWAP (exit on 2 closes below VWAP for longs).
"""
from __future__ import annotations

from typing import Dict, Optional

import pandas as pd

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal
from ...detectors._compute import compute_vwap


class GapAndGo(BaseProStrategy):
    TIER:      int   = 2
    SL_ATR:    float = 1.5    # V8: widened from 1.0 (survives overnight gap)
    PARTIAL_R: float = 1.5
    FULL_R:    float = 3.0

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        gap_sig = detector_outputs.get('gap')
        if not (gap_sig and gap_sig.fired and gap_sig.strength >= 0.40):
            return None

        direction = gap_sig.direction

        # Continuation bar: close in top/bottom 40% of session range
        session_high = float(df['high'].max())
        session_low  = float(df['low'].min())
        last_close   = float(df['close'].iloc[-1])
        session_range = session_high - session_low

        if session_range <= 0:
            return None

        position = (last_close - session_low) / session_range

        if direction == 'long'  and position < 0.60:
            return None   # not near session high — not a go
        if direction == 'short' and position > 0.40:
            return None   # not near session low

        # VWAP alignment (optional but preferred)
        vwap_sig = detector_outputs.get('vwap')
        if vwap_sig and vwap_sig.fired:
            above_vwap = vwap_sig.metadata.get('above_vwap', direction == 'long')
            if direction == 'long'  and not above_vwap:
                return None
            if direction == 'short' and above_vwap:
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
        outputs:     dict = None,
    ) -> float:
        offset = self.SL_ATR * atr
        if direction == 'long':
            # Stop below VWAP or ATR stop, whichever is higher
            vwap_val = float(compute_vwap(df).iloc[-1])
            stops    = [entry_price - offset]
            if vwap_val > 0:
                stops.append(vwap_val - 0.01)
            stop = max(stops)
            stop = min(stop, entry_price - 0.01)
        else:
            vwap_val = float(compute_vwap(df).iloc[-1])
            stops    = [entry_price + offset]
            if vwap_val > 0:
                stops.append(vwap_val + 0.01)
            stop = min(stops)
        return round(stop, 4)
