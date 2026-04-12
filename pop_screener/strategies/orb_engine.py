"""
pop_screener/strategies/orb_engine.py — Opening Range Breakout engine
======================================================================
Defines the opening range (first OR_MINUTES minutes), then waits for a
breakout above or_high with volume confirmation.

Entry
-----
  After the OR window has closed:
  - current_close > or_high
  - current_volume >= OR_BREAKOUT_VOL_MULT * or_volume_avg

  entry_price = current_close
  stop_loss   = or_low − OR_STOP_BUFFER_PCT * entry_price
  target_1    = entry + OR_R_MULT_1 * (entry − or_low)
  target_2    = entry + OR_R_MULT_2 * (entry − or_low)

Exit
----
  Hard stop    : price <= stop_loss
  Target 1     : price >= target_1  → partial_exit
  Target 2     : price >= target_2  → full_exit
  Momentum fade: price closes back inside OR range AND volume dries up
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from pop_screener import config as cfg
from pop_screener.models import (
    EngineeredFeatures, EntrySignal, ExitReason, ExitSignal,
    OHLCVBar, StrategyAssignment, StrategyType,
)


class ORBEngine:
    """Opening Range Breakout strategy engine."""

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

        if len(bars) < cfg.OR_MINUTES + 1:
            return entries, exits   # OR window hasn't closed yet

        # Separate OR bars from post-OR bars
        or_bars   = bars[:cfg.OR_MINUTES]
        post_bars = bars[cfg.OR_MINUTES:]

        if not post_bars:
            return entries, exits

        or_high      = max(b.high for b in or_bars)
        or_low       = min(b.low  for b in or_bars)
        or_vol_avg   = sum(b.volume for b in or_bars) / len(or_bars)

        current_bar = post_bars[-1]
        current_vol = current_bar.volume

        # ── Entry check ────────────────────────────────────────────────────────
        breakout_up  = current_bar.close > or_high
        vol_confirm  = current_vol >= cfg.OR_BREAKOUT_VOL_MULT * or_vol_avg

        if breakout_up and vol_confirm:
            entry   = current_bar.close
            r_size  = entry - or_low
            stop    = or_low - cfg.OR_STOP_BUFFER_PCT * entry
            stop    = min(stop, entry * 0.997)   # cap stop at 0.3 % below entry
            t1      = entry + cfg.OR_R_MULT_1 * r_size
            t2      = entry + cfg.OR_R_MULT_2 * r_size

            entries.append(EntrySignal(
                symbol=symbol,
                side='buy',
                entry_price=round(entry, 4),
                stop_loss=round(stop, 4),
                target_1=round(t1, 4),
                target_2=round(t2, 4),
                strategy_type=StrategyType.ORB,
                metadata={
                    'or_high':     round(or_high, 4),
                    'or_low':      round(or_low, 4),
                    'or_vol_avg':  round(or_vol_avg, 0),
                    'current_vol': float(current_vol),
                    'r_size':      round(r_size, 4),
                    'rvol':        round(features.rvol, 2),
                },
            ))

        # ── Exit signals ───────────────────────────────────────────────────────
        exits += self._check_exits(
            symbol=symbol,
            bars=post_bars,
            or_high=or_high,
            or_low=or_low,
            or_vol_avg=or_vol_avg,
        )

        return entries, exits

    def check_exits(
        self,
        symbol:      str,
        bars:        List[OHLCVBar],
        or_high:     float,
        or_low:      float,
        or_vol_avg:  float,
        stop_price:  float = 0.0,
        target_1:    float = 0.0,
        target_2:    float = 0.0,
    ) -> List[ExitSignal]:
        return self._check_exits(
            symbol=symbol,
            bars=bars,
            or_high=or_high,
            or_low=or_low,
            or_vol_avg=or_vol_avg,
            stop_price=stop_price,
            target_1=target_1,
            target_2=target_2,
        )

    def _check_exits(
        self,
        symbol:     str,
        bars:       List[OHLCVBar],
        or_high:    float,
        or_low:     float,
        or_vol_avg: float,
        stop_price: float = 0.0,
        target_1:   float = 0.0,
        target_2:   float = 0.0,
    ) -> List[ExitSignal]:
        exits: List[ExitSignal] = []
        if not bars:
            return exits

        last  = bars[-1]
        price = last.close
        meta  = {
            'price': round(price, 4),
            'or_high': round(or_high, 4),
            'or_low': round(or_low, 4),
        }

        # Hard stop
        if stop_price > 0 and price <= stop_price:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.STOP, strategy_type=StrategyType.ORB, metadata=meta,
            ))
            return exits

        # Full target
        if target_2 > 0 and price >= target_2:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.TARGET_2, strategy_type=StrategyType.ORB, metadata=meta,
            ))
            return exits

        # Partial target
        if target_1 > 0 and price >= target_1:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.TARGET_1, strategy_type=StrategyType.ORB, metadata=meta,
            ))

        # Momentum fade — price closes back inside OR range with low volume
        price_inside_or = (or_low <= price <= or_high)
        vol_dry         = last.volume < cfg.OR_FADE_VOL_MULT * or_vol_avg
        if price_inside_or and vol_dry:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.MOMENTUM_FADE,
                strategy_type=StrategyType.ORB, metadata=meta,
            ))

        return exits
