"""
pop_screener/strategies/ema_trend_engine.py — EMA Trend Continuation engine
============================================================================
Enters long on a pullback to a short-term EMA in the context of an uptrend.

Uptrend definition (all conditions)
-------------------------------------
  - last_close > EMA20
  - EMA20 > EMA50
  - At least N consecutive higher highs and higher lows in the lookback window

Entry pattern
-------------
  Pullback: bar.low <= EMA9 (or EMA20) touches or breaches the fast EMA
  Confirmation candle (same bar or next bar):
    - close > open  (bullish body)
    - close > EMA20 (recovered above mid EMA)
    - volume >= EMA_PULLBACK_VOL_MULT * avg_volume

  entry_price = close of confirmation candle
  stop_loss   = min(recent swing low, EMA20 − EMA_STOP_BUFFER_PCT * price)
  target_1    = entry + EMA_R_MULT_1 * (entry − stop)
  target_2    = entry + EMA_R_MULT_2 * (entry − stop)

Exit
----
  Hard stop
  N consecutive bars closing below EMA20
  Profit targets
  trend_cleanliness_score drops below TREND_CLEAN_EXIT_MIN
"""
from __future__ import annotations

import statistics
from typing import List, Tuple

from pop_screener import config as cfg
from pop_screener.models import (
    EngineeredFeatures, EntrySignal, ExitReason, ExitSignal,
    OHLCVBar, StrategyAssignment, StrategyType,
)


class EMATrendEngine:
    """EMA Trend Continuation strategy engine."""

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

        min_bars = max(cfg.EMA_SLOW_PERIOD, 5) + 2
        if len(bars) < min_bars:
            return entries, exits

        closes   = [b.close for b in bars]
        ema9     = _ema(closes, cfg.EMA_FAST_PERIOD)
        ema20    = _ema(closes, cfg.EMA_MID_PERIOD)
        ema50    = _ema(closes, cfg.EMA_SLOW_PERIOD)
        avg_vol  = statistics.mean(b.volume for b in bars)

        last_close = bars[-1].close

        # ── Uptrend check ──────────────────────────────────────────────────────
        if not (last_close > ema20 and ema20 > ema50):
            return entries, exits

        uptrend = _check_higher_highs_lows(bars, lookback=cfg.TREND_CLEAN_LOOKBACK_BARS)
        if not uptrend:
            return entries, exits

        # ── Pullback detection ─────────────────────────────────────────────────
        bar_n1 = bars[-2]
        bar_n  = bars[-1]

        # Pullback: prior bar touched or breached fast EMA, current bar recovers
        pullback_touch   = bar_n1.low <= ema9
        bullish_recovery = (
            bar_n.close > bar_n.open       # bullish candle
            and bar_n.close > ema20        # back above mid EMA
            and bar_n.volume >= cfg.EMA_PULLBACK_VOL_MULT * avg_vol
        )

        if pullback_touch and bullish_recovery:
            entry = bar_n.close
            swing_low  = min(b.low for b in bars[-5:])
            stop_ema   = ema20 - cfg.EMA_STOP_BUFFER_PCT * entry
            stop       = min(swing_low, stop_ema)
            stop       = min(stop, entry * 0.997)

            risk = entry - stop
            t1   = entry + cfg.EMA_R_MULT_1 * risk
            t2   = entry + cfg.EMA_R_MULT_2 * risk

            entries.append(EntrySignal(
                symbol=symbol,
                side='buy',
                entry_price=round(entry, 4),
                stop_loss=round(stop, 4),
                target_1=round(t1, 4),
                target_2=round(t2, 4),
                strategy_type=StrategyType.EMA_TREND_CONTINUATION,
                metadata={
                    'ema9':                      round(ema9, 4),
                    'ema20':                     round(ema20, 4),
                    'ema50':                     round(ema50, 4),
                    'trend_cleanliness_score':   round(features.trend_cleanliness_score, 4),
                    'atr':                       round(features.atr_value, 4),
                    'rvol':                      round(features.rvol, 2),
                    'avg_vol':                   round(avg_vol, 0),
                },
            ))

        # ── Exit signals ───────────────────────────────────────────────────────
        exits += self._check_exits(
            symbol=symbol,
            bars=bars,
            ema20=ema20,
            features=features,
        )

        return entries, exits

    def _check_exits(
        self,
        symbol:     str,
        bars:       List[OHLCVBar],
        ema20:      float,
        features:   EngineeredFeatures,
        stop_price: float = 0.0,
        target_1:   float = 0.0,
        target_2:   float = 0.0,
    ) -> List[ExitSignal]:
        exits: List[ExitSignal] = []
        if not bars:
            return exits

        last  = bars[-1]
        price = last.close
        meta  = {'price': round(price, 4), 'ema20': round(ema20, 4)}

        # Hard stop
        if stop_price > 0 and price <= stop_price:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.STOP,
                strategy_type=StrategyType.EMA_TREND_CONTINUATION, metadata=meta,
            ))
            return exits

        # Full target
        if target_2 > 0 and price >= target_2:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.TARGET_2,
                strategy_type=StrategyType.EMA_TREND_CONTINUATION, metadata=meta,
            ))
            return exits

        # Partial target
        if target_1 > 0 and price >= target_1:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.TARGET_1,
                strategy_type=StrategyType.EMA_TREND_CONTINUATION, metadata=meta,
            ))

        # N consecutive closes below EMA20
        n_consec = cfg.EMA_CONSECUTIVE_BELOW
        if len(bars) >= n_consec:
            below_ema = all(b.close < ema20 for b in bars[-n_consec:])
            if below_ema:
                exits.append(ExitSignal(
                    symbol=symbol, side='sell', exit_price=round(price, 4),
                    reason=ExitReason.BELOW_EMA,
                    strategy_type=StrategyType.EMA_TREND_CONTINUATION, metadata=meta,
                ))

        # Trend cleanliness degraded
        if features.trend_cleanliness_score < cfg.TREND_CLEAN_EXIT_MIN:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.TREND_BREAK,
                strategy_type=StrategyType.EMA_TREND_CONTINUATION,
                metadata={**meta, 'trend_score': round(features.trend_cleanliness_score, 4)},
            ))

        return exits


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ema(closes: List[float], period: int) -> float:
    if not closes:
        return 0.0
    n = min(period, len(closes))
    if n < 2:
        return closes[-1]
    alpha = 2.0 / (n + 1)
    val = closes[0]
    for c in closes[1:]:
        val = alpha * c + (1 - alpha) * val
    return val


def _check_higher_highs_lows(
    bars: List[OHLCVBar], lookback: int = 10
) -> bool:
    """
    Return True if a majority of consecutive bar pairs show HH + HL
    in the last *lookback* bars.  Requires at least 60 % of pairs to qualify.
    """
    window = bars[-lookback:] if len(bars) >= lookback else bars
    if len(window) < 5:
        return False   # insufficient data for reliable trend — require at least 5 bars

    hh_count = sum(
        1 for i in range(1, len(window)) if window[i].high > window[i - 1].high
    )
    hl_count = sum(
        1 for i in range(1, len(window)) if window[i].low > window[i - 1].low
    )
    pairs = len(window) - 1
    return (hh_count / pairs >= 0.55) and (hl_count / pairs >= 0.45)
