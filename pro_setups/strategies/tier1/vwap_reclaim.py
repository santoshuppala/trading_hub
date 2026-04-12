"""
Tier 1 — VWAP Reclaim (T4 Adapter).

Delegates pattern detection to the production-hardened T4 SignalAnalyzer
in monitor/signals.py.  Zero logic duplication — this is a thin adapter
that maps SignalAnalyzer output to the BaseProStrategy interface.

Entry:  T4's 2-bar VWAP reclaim + opened-above-VWAP + RSI 50-70.
Stop:   1.0 ATR below entry (BaseProStrategy default).
Exit:   Partial at 1R, Full at 2R.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional

import pandas as pd

import threading

from ..base import BaseProStrategy
from ...detectors.base import DetectorSignal

log = logging.getLogger(__name__)


class VWAPReclaim(BaseProStrategy):
    TIER:      int   = 1
    SL_ATR:    float = 1.0     # 1× ATR stop (matches T4)
    PARTIAL_R: float = 1.0     # partial at 1R
    FULL_R:    float = 2.0     # full exit at 2R

    def __init__(self):
        self._analyzer = None   # lazy-init to avoid circular imports at module load
        self._init_lock = threading.Lock()

    def _get_analyzer(self):
        if self._analyzer is None:
            with self._init_lock:
                if self._analyzer is None:   # double-checked locking
                    from monitor.signals import SignalAnalyzer
                    from config import STRATEGY_PARAMS
                    self._analyzer = SignalAnalyzer(STRATEGY_PARAMS, {})
        return self._analyzer

    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        analyzer = self._get_analyzer()
        result = analyzer.analyze(ticker, df, rvol_cache={})
        if result is None:
            return None

        # Delegate to T4 pattern detection
        vwap_reclaim    = result.get('_vwap_reclaim', False)
        opened_above    = result.get('_opened_above_vwap', False)
        rsi             = result.get('rsi_value', 0)
        rsi_overbought  = result.get('_rsi_overbought', 70)

        if not (vwap_reclaim and opened_above):
            return None
        if not (50 <= rsi <= rsi_overbought):
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
        # Use T4's reclaim-candle-low as a structural reference
        reclaim_low = float(df['low'].iloc[-1])
        atr_stop    = entry_price - self.SL_ATR * atr
        # Take the tighter (higher) stop — protects capital better
        stop = max(atr_stop, reclaim_low - 0.01)
        return round(min(stop, entry_price - 0.01), 4)
