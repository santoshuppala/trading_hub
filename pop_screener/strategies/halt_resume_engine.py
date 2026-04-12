"""
pop_screener/strategies/halt_resume_engine.py — Halt/Resume Breakout engine
============================================================================
Approximates trading halts by detecting bars with extreme single-bar moves
and high volume spikes ("halt-like" bars), then waiting for consolidation,
then entering on a breakout above the consolidation range.

Halt-like bar detection
-----------------------
  abs(close/open - 1) >= HALT_RETURN_THRESHOLD  (e.g. 10 % bar)
  volume >= HALT_VOL_MULT * avg_volume

Consolidation phase
-------------------
  bar range < CONSOLIDATION_RANGE_MAX_ATR * ATR
  at least CONSOLIDATION_MIN_BARS such bars after the halt-like bar

Entry
-----
  close > consolidation_high
  volume >= CONSOLIDATION_BREAKOUT_VOL_MULT * avg_consolidation_volume
  stop  = consolidation_low − CONSOLIDATION_STOP_BUFFER_PCT * entry
  t1    = entry + HALT_R_MULT_1 * (entry − consolidation_low)
  t2    = entry + HALT_R_MULT_2 * (entry − consolidation_low)

Exit
----
  Hard stop | targets | sharp reversal from high (> REVERSAL_RETURN_THRESHOLD)
"""
from __future__ import annotations

import statistics
from typing import List, Optional, Tuple

from pop_screener import config as cfg
from pop_screener.models import (
    EngineeredFeatures, EntrySignal, ExitReason, ExitSignal,
    OHLCVBar, StrategyAssignment, StrategyType,
)


class HaltResumeEngine:
    """Halt / Resume Breakout strategy engine."""

    def generate_signals(
        self,
        symbol:      str,
        bars:        List[OHLCVBar],
        vwap_series: List[float],
        features:    EngineeredFeatures,
        assignment:  StrategyAssignment,
    ) -> Tuple[List[EntrySignal], List[ExitSignal]]:
        entries: List[EntrySignal] = []
        exits:   List[ExitSignal]  = []

        if len(bars) < cfg.CONSOLIDATION_MIN_BARS + 2:
            return entries, exits

        atr = features.atr_value
        if atr <= 0:
            return entries, exits

        avg_vol = statistics.mean(b.volume for b in bars)

        # Find the most recent halt-like bar
        halt_idx = self._find_halt_bar(bars, avg_vol)
        if halt_idx is None:
            return entries, exits

        post_halt = bars[halt_idx + 1:]
        if len(post_halt) < cfg.CONSOLIDATION_MIN_BARS:
            return entries, exits

        # Detect consolidation phase after the halt
        consol_bars = self._find_consolidation(post_halt, atr)
        if consol_bars is None or len(consol_bars) < cfg.CONSOLIDATION_MIN_BARS:
            return entries, exits

        consol_high    = max(b.high   for b in consol_bars)
        consol_low     = min(b.low    for b in consol_bars)
        consol_vol_avg = statistics.mean(b.volume for b in consol_bars)

        # Breakout bar is the first bar AFTER consolidation
        breakout_candidate_idx = halt_idx + 1 + len(consol_bars)
        if breakout_candidate_idx >= len(bars):
            return entries, exits

        breakout_bar = bars[breakout_candidate_idx]
        if breakout_bar != bars[-1]:
            # Only signal on the most recent bar
            return entries, exits

        breakout_up  = breakout_bar.close > consol_high
        vol_confirm  = breakout_bar.volume >= (
            cfg.CONSOLIDATION_BREAKOUT_VOL_MULT * consol_vol_avg
        )

        if breakout_up and vol_confirm:
            entry  = breakout_bar.close
            r_size = entry - consol_low
            stop   = consol_low - cfg.CONSOLIDATION_STOP_BUFFER_PCT * entry
            stop   = min(stop, entry * 0.997)
            t1     = entry + cfg.HALT_R_MULT_1 * r_size
            t2     = entry + cfg.HALT_R_MULT_2 * r_size

            entries.append(EntrySignal(
                symbol=symbol,
                side='buy',
                entry_price=round(entry, 4),
                stop_loss=round(stop, 4),
                target_1=round(t1, 4),
                target_2=round(t2, 4),
                strategy_type=StrategyType.HALT_RESUME_BREAKOUT,
                metadata={
                    'consol_high':    round(consol_high, 4),
                    'consol_low':     round(consol_low, 4),
                    'consol_vol_avg': round(consol_vol_avg, 0),
                    'halt_bar_idx':   float(halt_idx),
                    'atr':            round(atr, 4),
                    'rvol':           round(features.rvol, 2),
                },
            ))

        # Exit signals
        exits += self._check_exits(
            symbol=symbol,
            bars=bars,
            breakout_bar_idx=breakout_candidate_idx,
        )

        return entries, exits

    def _find_halt_bar(
        self, bars: List[OHLCVBar], avg_vol: float
    ) -> Optional[int]:
        """Return index of the most recent halt-like bar, or None."""
        for i in range(len(bars) - 1, -1, -1):  # include bar[0] (opening halts)
            b = bars[i]
            if b.open > 0:
                bar_ret = abs(b.close / b.open - 1.0)
            else:
                continue
            if (
                bar_ret >= cfg.HALT_RETURN_THRESHOLD
                and b.volume >= cfg.HALT_VOL_MULT * avg_vol
            ):
                return i
        return None

    def _find_consolidation(
        self, bars: List[OHLCVBar], atr: float
    ) -> Optional[List[OHLCVBar]]:
        """
        Return the run of small-range bars at the start of *bars* that
        qualify as consolidation, or None if fewer than CONSOLIDATION_MIN_BARS.
        """
        max_range = cfg.CONSOLIDATION_RANGE_MAX_ATR * atr
        consol: List[OHLCVBar] = []
        for b in bars:
            if (b.high - b.low) < max_range:
                consol.append(b)
            else:
                break   # consolidation ended
        return consol if len(consol) >= cfg.CONSOLIDATION_MIN_BARS else None

    def _check_exits(
        self,
        symbol:            str,
        bars:              List[OHLCVBar],
        breakout_bar_idx:  int,
        stop_price:        float = 0.0,
        target_1:          float = 0.0,
        target_2:          float = 0.0,
    ) -> List[ExitSignal]:
        exits: List[ExitSignal] = []
        if not bars:
            return exits

        last  = bars[-1]
        price = last.close
        meta  = {'price': round(price, 4)}

        # Hard stop
        if stop_price > 0 and price <= stop_price:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.STOP,
                strategy_type=StrategyType.HALT_RESUME_BREAKOUT, metadata=meta,
            ))
            return exits

        # Targets
        if target_2 > 0 and price >= target_2:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.TARGET_2,
                strategy_type=StrategyType.HALT_RESUME_BREAKOUT, metadata=meta,
            ))
            return exits

        if target_1 > 0 and price >= target_1:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.TARGET_1,
                strategy_type=StrategyType.HALT_RESUME_BREAKOUT, metadata=meta,
            ))

        # Reversal exit: price dropped REVERSAL_RETURN_THRESHOLD from the session high
        session_high = max(b.high for b in bars)
        if session_high > 0 and (session_high - price) / session_high >= cfg.REVERSAL_RETURN_THRESHOLD:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.REVERSAL,
                strategy_type=StrategyType.HALT_RESUME_BREAKOUT,
                metadata={**meta, 'session_high': round(session_high, 4)},
            ))

        return exits
