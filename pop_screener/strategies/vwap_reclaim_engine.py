"""
pop_screener/strategies/vwap_reclaim_engine.py — T3.5 VWAP Reclaim Adapter.

Delegates all pattern detection to the production-hardened T4 SignalAnalyzer
in monitor/signals.py.  This adapter:
  1. Converts List[OHLCVBar] → pd.DataFrame
  2. Gates on features.rvol >= 2.0 (since rvol_cache is unavailable in T3.5)
  3. Calls SignalAnalyzer.analyze() for pattern + indicator computation
  4. Maps the result to EntrySignal / ExitSignal objects
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

from pop_screener.models import (
    EntrySignal, ExitSignal, ExitReason,
    EngineeredFeatures, OHLCVBar,
    StrategyAssignment, StrategyType,
)

import threading

log = logging.getLogger(__name__)

_RVOL_MIN = 2.0       # minimum RVOL for VWAP reclaim entry
_RSI_MIN  = 50.0
_RSI_MAX  = 70.0


def _bars_to_dataframe(bars: List[OHLCVBar]) -> pd.DataFrame:
    """Convert List[OHLCVBar] to pd.DataFrame with DatetimeIndex."""
    records = [
        {
            'open':   b.open,
            'high':   b.high,
            'low':    b.low,
            'close':  b.close,
            'volume': b.volume,
        }
        for b in bars
    ]
    index = pd.DatetimeIndex([b.timestamp for b in bars], tz=timezone.utc)
    return pd.DataFrame(records, index=index)


class VWAPReclaimEngine:
    """
    T3.5 adapter that delegates VWAP Reclaim detection to the T4 SignalAnalyzer.
    Same interface as all pop_screener strategy engines.
    """

    def __init__(self):
        self._analyzer = None   # lazy-init to avoid circular imports
        self._init_lock = threading.Lock()

    def _get_analyzer(self):
        if self._analyzer is None:
            with self._init_lock:
                if self._analyzer is None:   # double-checked locking
                    from monitor.signals import SignalAnalyzer
                    from config import STRATEGY_PARAMS
                    self._analyzer = SignalAnalyzer(STRATEGY_PARAMS, {})
        return self._analyzer

    def generate_signals(
        self,
        symbol:     str,
        bars:       List[OHLCVBar],
        vwap_series: List[float],
        features:   EngineeredFeatures,
        assignment: StrategyAssignment,
    ) -> Tuple[List[EntrySignal], List[ExitSignal]]:
        entries: List[EntrySignal] = []
        exits:   List[ExitSignal]  = []

        if len(bars) < 30:
            return entries, exits

        # ── RVOL gate (compensates for passing rvol_cache={} to T4) ──────────
        if features.rvol < _RVOL_MIN:
            return entries, exits

        # ── Convert to DataFrame and delegate to T4 ─────────────────────────
        df = _bars_to_dataframe(bars)
        analyzer = self._get_analyzer()
        result = analyzer.analyze(symbol, df, rvol_cache={})

        if result is None:
            return entries, exits

        # ── Entry check ──────────────────────────────────────────────────────
        vwap_reclaim   = result.get('_vwap_reclaim', False)
        opened_above   = result.get('_opened_above_vwap', False)
        rsi            = result.get('rsi_value', 0)
        rsi_overbought = result.get('_rsi_overbought', 70)
        atr            = result.get('atr_value', 0)
        atr_mult       = result.get('_atr_mult', 2.0)
        current_price  = result.get('current_price', 0)
        vwap           = result.get('vwap', 0)

        if vwap_reclaim and opened_above and _RSI_MIN <= rsi <= _RSI_MAX and atr > 0:
            entry = current_price
            stop  = max(
                entry - atr,                          # 1× ATR below entry
                result.get('reclaim_candle_low', entry - atr) - 0.01,
            )
            stop = min(stop, entry - 0.01)            # ensure stop < entry

            risk     = entry - stop
            target_1 = round(entry + 1.0 * risk, 4)   # 1R partial
            target_2 = round(entry + atr_mult * risk, 4)  # 2R full

            entries.append(EntrySignal(
                symbol=symbol,
                side='buy',
                entry_price=round(entry, 4),
                stop_loss=round(stop, 4),
                target_1=target_1,
                target_2=target_2,
                strategy_type=StrategyType.VWAP_RECLAIM,
                metadata={
                    'rsi':  round(rsi, 2),
                    'atr':  round(atr, 4),
                    'rvol': round(features.rvol, 2),
                    'vwap': round(vwap, 4),
                },
            ))

        # ── Exit checks (indicator-based, no position state needed) ──────────
        vwap_breakdown = result.get('_vwap_breakdown', False)
        if vwap_breakdown and current_price > 0:
            exits.append(ExitSignal(
                symbol=symbol,
                side='sell',
                exit_price=current_price,
                reason=ExitReason.VWAP_BREAK,
                strategy_type=StrategyType.VWAP_RECLAIM,
            ))

        if rsi > rsi_overbought and current_price > 0:
            exits.append(ExitSignal(
                symbol=symbol,
                side='sell',
                exit_price=current_price,
                reason=ExitReason.RSI_OVERBOUGHT,
                strategy_type=StrategyType.VWAP_RECLAIM,
            ))

        return entries, exits
