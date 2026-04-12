"""
pop_screener/strategies/bopb_engine.py — Breakout-Pullback (BOPB) engine
=========================================================================
Identifies a prior resistance level (prior_high), waits for a breakout
above it, then enters on a pullback-and-confirmation to that level.

Breakout detection
------------------
  prior_high = max(high) over the last BOPB_LOOKBACK_BARS bars (excluding today)
  Breakout bar:
    close > prior_high
    volume >= BOPB_BREAKOUT_VOL_MULT * avg_volume

Pullback detection
------------------
  After the breakout bar:
    low <= prior_high + BOPB_PULLBACK_TOLERANCE * prior_high
    (price "tested" the breakout level from above)

Confirmation
------------
  Candle that reclaims prior_high from the pullback:
    close > open  (bullish candle)
    close > prior_high
    volume >= BOPB_CONFIRM_VOL_MULT * avg_volume

Entry
-----
  entry_price = close of confirmation candle
  stop_loss   = prior_high − BOPB_STOP_BUFFER_PCT * entry_price
  target_1    = entry + BOPB_R_MULT_1 * (entry − stop)
  target_2    = entry + BOPB_R_MULT_2 * (entry − stop)

Exit
----
  Hard stop
  Targets
  Price closes back below prior_high for BOPB_BELOW_PRIOR_HIGH_BARS bars
"""
from __future__ import annotations

import statistics
from typing import List, Optional, Tuple

from pop_screener import config as cfg
from pop_screener.models import (
    EngineeredFeatures, EntrySignal, ExitReason, ExitSignal,
    OHLCVBar, StrategyAssignment, StrategyType,
)


class BOPBEngine:
    """Breakout-Pullback strategy engine."""

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

        lookback = cfg.BOPB_LOOKBACK_BARS
        # Extend lookback up to 3× default if data is available, to catch
        # resistance levels outside the fixed window
        max_lookback = min(lookback * 3, len(bars) - 3)
        effective_lookback = max(lookback, max_lookback)

        # Need breakout history + at least one pullback bar + confirmation
        if len(bars) < lookback + 3:
            return entries, exits

        avg_vol     = statistics.mean(b.volume for b in bars)
        prior_high  = max(b.high for b in bars[:effective_lookback])

        # Find the most recent breakout bar
        breakout_idx = self._find_breakout(bars, prior_high, avg_vol, lookback)
        if breakout_idx is None:
            return entries, exits

        post_breakout = bars[breakout_idx + 1:]
        if len(post_breakout) < 2:
            return entries, exits

        # Detect pullback phase
        pullback_idx = self._find_pullback(post_breakout, prior_high)
        if pullback_idx is None:
            return entries, exits

        # Confirmation bar follows the pullback
        confirm_idx = pullback_idx + 1
        if confirm_idx >= len(post_breakout):
            return entries, exits

        confirm_bar = post_breakout[confirm_idx]
        if confirm_bar != bars[-1]:
            # Only signal on the current bar
            return entries, exits

        # Confirmation conditions
        bullish_candle  = confirm_bar.close > confirm_bar.open
        above_prior_high = confirm_bar.close > prior_high
        vol_confirm     = confirm_bar.volume >= cfg.BOPB_CONFIRM_VOL_MULT * avg_vol

        if bullish_candle and above_prior_high and vol_confirm:
            entry = confirm_bar.close
            stop  = prior_high - cfg.BOPB_STOP_BUFFER_PCT * entry
            stop  = min(stop, entry * 0.997)
            risk  = entry - stop
            t1    = entry + cfg.BOPB_R_MULT_1 * risk
            t2    = entry + cfg.BOPB_R_MULT_2 * risk

            entries.append(EntrySignal(
                symbol=symbol,
                side='buy',
                entry_price=round(entry, 4),
                stop_loss=round(stop, 4),
                target_1=round(t1, 4),
                target_2=round(t2, 4),
                strategy_type=StrategyType.BREAKOUT_PULLBACK,
                metadata={
                    'prior_high':   round(prior_high, 4),
                    'breakout_idx': float(breakout_idx),
                    'risk':         round(risk, 4),
                    'atr':          round(features.atr_value, 4),
                    'rvol':         round(features.rvol, 2),
                    'avg_vol':      round(avg_vol, 0),
                },
            ))

        exits += self._check_exits(
            symbol=symbol,
            bars=bars[breakout_idx:],   # only post-breakout bars
            prior_high=prior_high,
        )

        return entries, exits

    def _find_breakout(
        self,
        bars:       List[OHLCVBar],
        prior_high: float,
        avg_vol:    float,
        lookback:   int,
    ) -> Optional[int]:
        """Return the index of the most recent valid breakout bar."""
        for i in range(len(bars) - 1, lookback - 1, -1):
            b = bars[i]
            if (
                b.close > prior_high
                and b.volume >= cfg.BOPB_BREAKOUT_VOL_MULT * avg_vol
            ):
                return i
        return None

    def _find_pullback(
        self, post_bars: List[OHLCVBar], prior_high: float
    ) -> Optional[int]:
        """Return index of the first bar that tests the prior_high from above."""
        tolerance = cfg.BOPB_PULLBACK_TOLERANCE * prior_high
        for i, b in enumerate(post_bars):
            if b.low <= prior_high + tolerance:
                return i
        return None

    def _check_exits(
        self,
        symbol:     str,
        bars:       List[OHLCVBar],
        prior_high: float,
        stop_price: float = 0.0,
        target_1:   float = 0.0,
        target_2:   float = 0.0,
    ) -> List[ExitSignal]:
        exits: List[ExitSignal] = []
        if not bars:
            return exits

        last  = bars[-1]
        price = last.close
        meta  = {'price': round(price, 4), 'prior_high': round(prior_high, 4)}

        # Hard stop
        if stop_price > 0 and price <= stop_price:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.STOP,
                strategy_type=StrategyType.BREAKOUT_PULLBACK, metadata=meta,
            ))
            return exits

        # Full target
        if target_2 > 0 and price >= target_2:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.TARGET_2,
                strategy_type=StrategyType.BREAKOUT_PULLBACK, metadata=meta,
            ))
            return exits

        # Partial target
        if target_1 > 0 and price >= target_1:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.TARGET_1,
                strategy_type=StrategyType.BREAKOUT_PULLBACK, metadata=meta,
            ))

        # N consecutive closes back below prior_high → failed breakout
        n = cfg.BOPB_BELOW_PRIOR_HIGH_BARS
        if len(bars) >= n:
            all_below = all(b.close < prior_high for b in bars[-n:])
            if all_below:
                exits.append(ExitSignal(
                    symbol=symbol, side='sell', exit_price=round(price, 4),
                    reason=ExitReason.BELOW_PRIOR_HIGH,
                    strategy_type=StrategyType.BREAKOUT_PULLBACK, metadata=meta,
                ))

        return exits
