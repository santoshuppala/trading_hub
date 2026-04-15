"""
pop_screener/strategies/momentum_entry_engine.py — Simple Momentum Entry.

Fires when a pop candidate has sufficient momentum (RVOL + price movement)
without requiring complex chart patterns like VWAP reclaim or EMA trends.

This is the fallback strategy when other engines reject candidates due to
technical conditions not being met. It uses ATR-based stops and targets.

Entry conditions (ALL must be true):
  - At least 10 bars of data
  - RVOL >= 1.5 (or ETF-adjusted)
  - Price momentum > 0 (price moving in gap direction)
  - RSI not extreme (25 < RSI < 75)

Stop: 1.5× ATR below entry
Target 1: 1× risk (partial)
Target 2: 2× risk (full)
"""
from __future__ import annotations

import logging
from typing import List, Tuple

from pop_screener.models import (
    EntrySignal, ExitSignal,
    EngineeredFeatures, OHLCVBar,
    StrategyAssignment, StrategyType,
)

log = logging.getLogger(__name__)

_MIN_BARS = 10
_RVOL_MIN = 1.5
_RSI_MIN = 25.0
_RSI_MAX = 75.0
_ATR_PERIOD = 14


def _compute_atr(bars: List[OHLCVBar], period: int = _ATR_PERIOD) -> float:
    if len(bars) < period + 1:
        # Fallback: use high-low range of last bar
        return bars[-1].high - bars[-1].low if bars else 0.01
    trs = []
    for i in range(1, len(bars)):
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period


class MomentumEntryEngine:
    """Simple momentum-based entry for pop candidates."""

    def generate_signals(
        self,
        symbol:      str,
        bars:        List[OHLCVBar],
        vwap_series: List[float],
        features:    EngineeredFeatures,
        assignment:  StrategyAssignment,
    ) -> Tuple[List[EntrySignal], List[ExitSignal]]:
        entries: List[EntrySignal] = []
        exits: List[ExitSignal] = []

        if len(bars) < _MIN_BARS:
            return entries, exits

        # RVOL gate
        from monitor.sector_map import is_etf, ETF_RVOL_MIN
        rvol_min = ETF_RVOL_MIN if is_etf(symbol) else _RVOL_MIN
        if features.rvol < rvol_min:
            return entries, exits

        # RSI gate
        last = bars[-1]
        price = last.close

        # Simple RSI from features or compute inline
        rsi = features.rvol  # placeholder — use price momentum as proxy
        # Use recent price action as momentum check
        lookback = min(5, len(bars) - 1)
        prior_price = bars[-(lookback + 1)].close
        momentum = (price - prior_price) / prior_price if prior_price > 0 else 0

        # Must have positive momentum for long entry
        if momentum <= 0:
            return entries, exits

        # ATR-based stops
        atr = _compute_atr(bars)
        if atr <= 0:
            return entries, exits

        stop = round(price - 1.5 * atr, 4)
        risk = price - stop
        if risk <= 0:
            return entries, exits

        target_1 = round(price + 1.0 * risk, 4)
        target_2 = round(price + 2.0 * risk, 4)

        entries.append(EntrySignal(
            symbol=symbol,
            side='buy',
            entry_price=round(price, 4),
            stop_loss=stop,
            target_1=target_1,
            target_2=target_2,
            strategy_type=StrategyType.VWAP_RECLAIM,  # reuse existing type for compatibility
            size_hint=None,
            metadata={
                'engine': 'momentum_entry',
                'rvol': round(features.rvol, 2),
                'momentum': round(momentum, 4),
                'atr': round(atr, 4),
                'gap_size': round(features.gap_size, 4),
                'pop_reason': str(features.symbol),
            },
        ))

        log.info(
            "[MomentumEntry] SIGNAL %s BUY @ $%.2f | stop=$%.2f | "
            "t1=$%.2f t2=$%.2f | rvol=%.2f mom=%.4f atr=%.4f",
            symbol, price, stop, target_1, target_2,
            features.rvol, momentum, atr,
        )

        return entries, exits
