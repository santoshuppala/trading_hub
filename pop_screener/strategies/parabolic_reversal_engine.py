"""
pop_screener/strategies/parabolic_reversal_engine.py — Parabolic Reversal engine
=================================================================================
Detects parabolic exhaustion and enters a SHORT (sell) reversal.

Parabolic move conditions (all required)
-----------------------------------------
  - Intraday range (session_high − session_low) / session_low
      >= PARABOLIC_MOVE_THRESHOLD  (e.g. 50 %)
  - At least N_EXTENDED_BARS in the recent window where:
      bar.range > EXTENDED_RANGE_MULT * ATR
      bar closes near its high (bullish extended bar)

Exhaustion candle conditions (all required)
--------------------------------------------
  - Long upper wick: (high − max(open, close)) / (high − low) >= WICK_RATIO_MIN
  - Volume spike: volume >= EXHAUSTION_VOL_MULT * avg_volume

Entry (short / reversal after exhaustion)
------------------------------------------
  - First bar that makes a lower high AND closes below the prior bar's low
    (or below a short-period EMA)
  - entry_price = close
  - stop_loss   = high_of_exhaustion + REVERSAL_STOP_BUFFER_PCT * entry_price
  - target_1    = entry − REVERSAL_R_MULT_1 * (stop − entry)
  - target_2    = entry − REVERSAL_R_MULT_2 * (stop − entry)
    (or target = current VWAP if VWAP is below entry)
"""
from __future__ import annotations

import statistics
from typing import List, Optional, Tuple

from pop_screener import config as cfg
from pop_screener.models import (
    EngineeredFeatures, EntrySignal, ExitReason, ExitSignal,
    OHLCVBar, StrategyAssignment, StrategyType,
)


class ParabolicReversalEngine:
    """Parabolic Reversal (short-side) strategy engine."""

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

        if len(bars) < cfg.N_EXTENDED_BARS + 2:
            return entries, exits

        atr = features.atr_value
        if atr <= 0:
            return entries, exits

        avg_vol = statistics.mean(b.volume for b in bars)

        session_high = max(b.high  for b in bars)
        session_low  = min(b.low   for b in bars)

        # ── 1. Parabolic move check ────────────────────────────────────────────
        if session_low <= 0:
            return entries, exits
        intraday_move = (session_high - session_low) / session_low
        if intraday_move < cfg.PARABOLIC_MOVE_THRESHOLD:
            return entries, exits

        # ── 2. Extended candles check ──────────────────────────────────────────
        recent = bars[-(cfg.N_EXTENDED_BARS + 2):-1]   # exclude the very last bar
        n_extended = 0
        for b in recent:
            bar_range = b.high - b.low
            if bar_range <= 0:
                continue
            closes_near_high = (b.high - b.close) / bar_range < 0.25
            if bar_range > cfg.EXTENDED_RANGE_MULT * atr and closes_near_high:
                n_extended += 1

        if n_extended < cfg.N_EXTENDED_BARS:
            return entries, exits

        # ── 3. Exhaustion candle check ─────────────────────────────────────────
        exhaust_bar = bars[-2]   # the bar before the current one
        bar_range   = exhaust_bar.high - exhaust_bar.low
        if bar_range <= 0:
            return entries, exits

        upper_wick   = exhaust_bar.high - max(exhaust_bar.open, exhaust_bar.close)
        wick_ratio   = upper_wick / bar_range
        vol_spike    = exhaust_bar.volume >= cfg.EXHAUSTION_VOL_MULT * avg_vol

        if wick_ratio < cfg.WICK_RATIO_MIN or not vol_spike:
            return entries, exits

        # ── 4. Reversal entry confirmation ─────────────────────────────────────
        current_bar = bars[-1]
        lower_high  = current_bar.high < exhaust_bar.high
        close_below_prior_low = current_bar.close < exhaust_bar.low

        if not (lower_high and close_below_prior_low):
            return entries, exits

        # Entry
        entry = current_bar.close
        stop  = exhaust_bar.high + cfg.REVERSAL_STOP_BUFFER_PCT * entry
        risk  = stop - entry   # positive for a short (stop is above entry)

        # Use VWAP as target if it's below entry; otherwise use fixed multiples
        current_vwap = features.current_vwap
        if 0 < current_vwap < entry:
            t1 = current_vwap                              # target = VWAP
            t2 = entry - cfg.REVERSAL_R_MULT_2 * risk     # deeper target
        else:
            t1 = entry - cfg.REVERSAL_R_MULT_1 * risk
            t2 = entry - cfg.REVERSAL_R_MULT_2 * risk

        # Safety: targets must be below entry for a short
        t1 = min(t1, entry * 0.998)
        t2 = min(t2, t1 * 0.998)

        entries.append(EntrySignal(
            symbol=symbol,
            side='sell',   # short entry
            entry_price=round(entry, 4),
            stop_loss=round(stop, 4),
            target_1=round(t1, 4),
            target_2=round(t2, 4),
            strategy_type=StrategyType.PARABOLIC_REVERSAL,
            metadata={
                'exhaust_high':   round(exhaust_bar.high, 4),
                'wick_ratio':     round(wick_ratio, 3),
                'intraday_move':  round(intraday_move, 4),
                'n_extended':     float(n_extended),
                'atr':            round(atr, 4),
                'current_vwap':   round(current_vwap, 4),
            },
        ))

        exits += self._check_exits(
            symbol=symbol,
            bars=bars,
            exhaust_high=exhaust_bar.high,
            stop_price=stop,
            target_1=t1,
            target_2=t2,
        )

        return entries, exits

    def _check_exits(
        self,
        symbol:      str,
        bars:        List[OHLCVBar],
        exhaust_high: float,
        stop_price:  float = 0.0,
        target_1:    float = 0.0,
        target_2:    float = 0.0,
    ) -> List[ExitSignal]:
        exits: List[ExitSignal] = []
        if not bars:
            return exits

        last  = bars[-1]
        price = last.close
        meta  = {'price': round(price, 4), 'exhaust_high': round(exhaust_high, 4)}

        # Stop (for a short, stop is above price)
        if stop_price > 0 and price >= stop_price:
            exits.append(ExitSignal(
                symbol=symbol, side='buy', exit_price=round(price, 4),
                reason=ExitReason.STOP,
                strategy_type=StrategyType.PARABOLIC_REVERSAL, metadata=meta,
            ))
            return exits

        # Full target
        if target_2 > 0 and price <= target_2:
            exits.append(ExitSignal(
                symbol=symbol, side='buy', exit_price=round(price, 4),
                reason=ExitReason.TARGET_2,
                strategy_type=StrategyType.PARABOLIC_REVERSAL, metadata=meta,
            ))
            return exits

        # Partial target
        if target_1 > 0 and price <= target_1:
            exits.append(ExitSignal(
                symbol=symbol, side='buy', exit_price=round(price, 4),
                reason=ExitReason.TARGET_1,
                strategy_type=StrategyType.PARABOLIC_REVERSAL, metadata=meta,
            ))

        # Price reclaims exhaust high → reversal failed, cover
        if price >= exhaust_high:
            exits.append(ExitSignal(
                symbol=symbol, side='buy', exit_price=round(price, 4),
                reason=ExitReason.REVERSAL,
                strategy_type=StrategyType.PARABOLIC_REVERSAL, metadata=meta,
            ))

        return exits
