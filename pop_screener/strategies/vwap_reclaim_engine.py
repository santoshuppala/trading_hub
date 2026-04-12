"""
pop_screener/strategies/vwap_reclaim_engine.py — VWAP Reclaim strategy engine
==============================================================================
Detects the dip-and-reclaim pattern on 1-min bars and generates long entry /
exit signals.  All rules are deterministic.

Entry pattern (all conditions required)
-----------------------------------------
  Preconditions:
    1. vwap_compatibility_score >= VWAP_COMPAT_MIN
    2. Price has traded ABOVE VWAP at some point earlier in the session.
    3. Current time is within the allowed trading window.

  Bar pattern:
    Bar N-1 : close < vwap  (price dipped below VWAP)
    Bar N   : close > vwap  (price reclaimed VWAP)
    Bar N   : close > open  (bullish candle on reclaim)
    Bar N   : volume >= volume of Bar N-1  (expanding volume)

  Filters:
    - RVOL at Bar N >= RVOL_VWAP_MIN
    - RSI at Bar N in [RSI_VWAP_MIN, RSI_VWAP_MAX]

Entry price  = close of Bar N
Stop loss    = min(recent swing low, vwap - VWAP_BUFFER_PCT * price)
               but never below entry - ATR_MULT_STOP * ATR
Target 1     = entry + ATR_MULT_TP1 * ATR
Target 2     = entry + ATR_MULT_TP2 * ATR
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from pop_screener import config as cfg
from pop_screener.models import (
    EngineeredFeatures, EntrySignal, ExitReason, ExitSignal,
    OHLCVBar, StrategyAssignment, StrategyType,
)

ET = ZoneInfo('America/New_York')


class VWAPReclaimEngine:
    """
    VWAP Reclaim strategy engine.

    Parameters
    ----------
    rsi_period  : lookback period for RSI (default: config.RSI_PERIOD)
    atr_period  : lookback period for ATR (default: config.ATR_PERIOD)
    """

    def __init__(
        self,
        rsi_period: int = cfg.RSI_PERIOD,
        atr_period: int = cfg.ATR_PERIOD,
    ):
        self._rsi_period = rsi_period
        self._atr_period = atr_period

    def generate_signals(
        self,
        symbol:      str,
        bars:        List[OHLCVBar],
        vwap_series: List[float],
        features:    EngineeredFeatures,
        assignment:  StrategyAssignment,
    ) -> Tuple[List[EntrySignal], List[ExitSignal]]:
        """
        Evaluate VWAP reclaim conditions and return entry/exit signals.

        Parameters
        ----------
        symbol       : ticker
        bars         : chronological 1-min OHLCV bars (at least 3 required)
        vwap_series  : one VWAP float per bar (same length as bars)
        features     : EngineeredFeatures for this symbol
        assignment   : StrategyAssignment (must have primary=VWAP_RECLAIM)

        Returns
        -------
        (entry_signals, exit_signals)
        """
        entries: List[EntrySignal] = []
        exits:   List[ExitSignal]  = []

        if not self._preconditions_met(bars, vwap_series, assignment):
            return entries, exits

        # Need at least 2 bars to check the dip-and-reclaim pattern
        if len(bars) < 3 or len(vwap_series) < 2:
            return entries, exits

        atr = features.atr_value
        if atr <= 0:
            return entries, exits

        bar_n1 = bars[-2]
        bar_n  = bars[-1]
        vwap_n1 = vwap_series[-2]
        vwap_n  = vwap_series[-1]

        # ── Entry pattern check ────────────────────────────────────────────────
        dip_below_vwap  = bar_n1.close < vwap_n1
        reclaimed_vwap  = bar_n.close  > vwap_n
        bullish_candle  = bar_n.close  > bar_n.open
        expanding_vol   = bar_n.volume >= bar_n1.volume

        if not (dip_below_vwap and reclaimed_vwap and bullish_candle and expanding_vol):
            return entries, exits

        # ── Filters ────────────────────────────────────────────────────────────
        rvol  = features.rvol
        rsi   = _compute_rsi([b.close for b in bars], period=self._rsi_period)

        if rvol < cfg.RVOL_VWAP_MIN:
            return entries, exits
        if not (cfg.RSI_VWAP_MIN <= rsi <= cfg.RSI_VWAP_MAX):
            return entries, exits

        # ── Entry price and levels ─────────────────────────────────────────────
        entry = bar_n.close

        # Stop: min of swing low and vwap buffer
        swing_low = min(b.low for b in bars[-5:])
        vwap_stop = vwap_n - cfg.VWAP_BUFFER_PCT * entry
        stop_raw  = min(swing_low, vwap_stop)
        # Floor: entry − ATR_MULT_STOP × ATR
        stop_floor = entry - cfg.ATR_MULT_STOP_VWAP * atr
        stop = max(stop_raw, stop_floor)    # don't go too tight
        stop = min(stop, entry * 0.998)     # always at least 0.2 % below entry

        target_1 = entry + cfg.ATR_MULT_TP1_VWAP * atr
        target_2 = entry + cfg.ATR_MULT_TP2_VWAP * atr

        entries.append(EntrySignal(
            symbol=symbol,
            side='buy',
            entry_price=round(entry, 4),
            stop_loss=round(stop, 4),
            target_1=round(target_1, 4),
            target_2=round(target_2, 4),
            strategy_type=StrategyType.VWAP_RECLAIM,
            metadata={
                'atr':           round(atr, 4),
                'rsi':           round(rsi, 2),
                'rvol':          round(rvol, 2),
                'vwap':          round(vwap_n, 4),
                'reclaim_candle_low': round(bar_n.low, 4),
                'vwap_distance': round(features.vwap_distance, 6),
            },
        ))

        # ── Exit signals on existing positions (check current bar) ─────────────
        exits += self._check_exits(
            symbol=symbol,
            bars=bars,
            vwap_series=vwap_series,
            features=features,
            atr=atr,
            rsi=rsi,
        )

        return entries, exits

    def check_exits(
        self,
        symbol:      str,
        bars:        List[OHLCVBar],
        vwap_series: List[float],
        features:    EngineeredFeatures,
        entry_price: float,
        stop_price:  float,
        target_1:    float,
        target_2:    float,
    ) -> List[ExitSignal]:
        """
        Check exit conditions for an already-open VWAP Reclaim position.

        Call this each bar after a position is opened to get exit signals.
        """
        atr = features.atr_value if features.atr_value > 0 else 0.01
        rsi = _compute_rsi([b.close for b in bars], period=self._rsi_period)
        return self._check_exits(
            symbol=symbol,
            bars=bars,
            vwap_series=vwap_series,
            features=features,
            atr=atr,
            rsi=rsi,
            entry_price=entry_price,
            stop_price=stop_price,
            target_1=target_1,
            target_2=target_2,
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _preconditions_met(
        self,
        bars: List[OHLCVBar],
        vwap_series: List[float],
        assignment: StrategyAssignment,
    ) -> bool:
        if assignment.vwap_compatibility_score < cfg.VWAP_COMPAT_MIN:
            return False
        if len(bars) < 3 or len(vwap_series) < 3:
            return False
        # Price must have traded above VWAP at some point today
        traded_above = any(b.close > v for b, v in zip(bars, vwap_series))
        if not traded_above:
            return False
        # Time window check
        now = datetime.now(ET)
        if now.hour < 9 or (now.hour == 9 and now.minute < 45):
            return False   # first 15 min excluded (too volatile)
        if now.hour > cfg.VWAP_EOD_HOUR or (
            now.hour == cfg.VWAP_EOD_HOUR and now.minute >= cfg.VWAP_EOD_MINUTE
        ):
            return False   # after EOD cutoff
        return True

    def _check_exits(
        self,
        symbol:      str,
        bars:        List[OHLCVBar],
        vwap_series: List[float],
        features:    EngineeredFeatures,
        atr:         float,
        rsi:         float,
        entry_price: float = 0.0,
        stop_price:  float = 0.0,
        target_1:    float = 0.0,
        target_2:    float = 0.0,
    ) -> List[ExitSignal]:
        exits: List[ExitSignal] = []
        if not bars:
            return exits

        last   = bars[-1]
        price  = last.close
        vwap   = vwap_series[-1] if vwap_series else 0.0
        meta   = {'price': round(price, 4), 'vwap': round(vwap, 4), 'rsi': round(rsi, 2)}

        # Hard stop
        if stop_price > 0 and price <= stop_price:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.STOP,
                strategy_type=StrategyType.VWAP_RECLAIM, metadata=meta,
            ))
            return exits   # stop hit — no further checks

        # Full profit target
        if target_2 > 0 and price >= target_2:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.TARGET_2,
                strategy_type=StrategyType.VWAP_RECLAIM, metadata=meta,
            ))
            return exits

        # Partial profit target
        if target_1 > 0 and price >= target_1:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.TARGET_1,
                strategy_type=StrategyType.VWAP_RECLAIM, metadata=meta,
            ))

        # VWAP breakdown — two consecutive bars closing below VWAP
        if len(bars) >= 2 and len(vwap_series) >= 2:
            below_vwap_now  = bars[-1].close < vwap_series[-1]
            below_vwap_prev = bars[-2].close < vwap_series[-2]
            if below_vwap_now and below_vwap_prev:
                exits.append(ExitSignal(
                    symbol=symbol, side='sell', exit_price=round(price, 4),
                    reason=ExitReason.VWAP_BREAK,
                    strategy_type=StrategyType.VWAP_RECLAIM, metadata=meta,
                ))

        # RSI overbought
        if rsi >= cfg.RSI_VWAP_OVERBOUGHT:
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.RSI_OVERBOUGHT,
                strategy_type=StrategyType.VWAP_RECLAIM, metadata=meta,
            ))

        # EOD force exit
        now = datetime.now(ET)
        if now.hour > cfg.VWAP_EOD_HOUR or (
            now.hour == cfg.VWAP_EOD_HOUR and now.minute >= cfg.VWAP_EOD_MINUTE
        ):
            exits.append(ExitSignal(
                symbol=symbol, side='sell', exit_price=round(price, 4),
                reason=ExitReason.EOD,
                strategy_type=StrategyType.VWAP_RECLAIM, metadata=meta,
            ))

        return exits


def _compute_rsi(closes: List[float], period: int = 14) -> float:
    """Simple RSI calculation; returns 50.0 when insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas[-period:]]
    losses = [abs(min(d, 0.0)) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)
