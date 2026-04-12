"""
LiquidityDetector — stop-hunt sweep + smart-money reversal.

Detects when price momentarily breaches a recent swing low/high (sweeping
stop orders), then reverses back inside the range.  This indicates
institutional accumulation/distribution.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import compute_atr


class LiquidityDetector(BaseDetector):
    """
    Sweep detection algorithm:

    1. Find the ``_LOOKBACK``-bar swing low/high.
    2. Check if the previous bar's low (for bull sweep) pierced below the
       swing low by at most ``_MAX_PIERCE_ATR`` × ATR.
    3. Confirm current bar closed above the swing low (reversal confirmed).
    4. Wick size must be ≥ ``_MIN_WICK_PCT`` of the current bar's range.

    Long sweep: sweep of lows, bullish reversal.
    Short sweep: sweep of highs, bearish reversal.
    """
    name:             str   = 'liquidity'
    MIN_BARS:         int   = 22
    _LOOKBACK:        int   = 20
    _MAX_PIERCE_ATR:  float = 1.5   # sweep must not exceed 1.5× ATR beyond level
    _MIN_WICK_PCT:    float = 0.40  # wick must be ≥ 40% of bar range

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
    ) -> DetectorSignal:
        atr = compute_atr(df)

        # Swing levels (exclude last 2 bars — those are the sweep + reversal)
        lookback_slice = df.iloc[-(self._LOOKBACK + 2):-2]
        if len(lookback_slice) < 5:
            return DetectorSignal.no_signal()

        swing_lo = float(lookback_slice['low'].min())
        swing_hi = float(lookback_slice['high'].max())

        cur_bar  = df.iloc[-1]
        prev_bar = df.iloc[-2]

        cur_close   = float(cur_bar['close'])
        cur_open    = float(cur_bar['open'])
        cur_high    = float(cur_bar['high'])
        cur_low     = float(cur_bar['low'])
        cur_range   = cur_high - cur_low

        prev_low    = float(prev_bar['low'])
        prev_high   = float(prev_bar['high'])

        # ── Bull sweep: prev bar swept below swing_lo, cur bar reversed up ──
        bull_sweep = (
            prev_low < swing_lo and
            (swing_lo - prev_low) <= self._MAX_PIERCE_ATR * atr and
            cur_close > swing_lo
        )

        # ── Bear sweep: prev bar swept above swing_hi, cur bar reversed down ─
        bear_sweep = (
            prev_high > swing_hi and
            (prev_high - swing_hi) <= self._MAX_PIERCE_ATR * atr and
            cur_close < swing_hi
        )

        if not (bull_sweep or bear_sweep):
            return DetectorSignal.no_signal()

        # Wick confirmation on the reversal bar
        if bull_sweep:
            lower_wick = min(cur_open, cur_close) - cur_low
            wick_pct   = lower_wick / cur_range if cur_range > 0 else 0.0
        else:
            upper_wick = cur_high - max(cur_open, cur_close)
            wick_pct   = upper_wick / cur_range if cur_range > 0 else 0.0

        if wick_pct < self._MIN_WICK_PCT:
            return DetectorSignal.no_signal()

        direction = 'long' if bull_sweep else 'short'
        strength  = min(0.5 + wick_pct * 0.5, 1.0)

        sweep_level = swing_lo if bull_sweep else swing_hi
        pierce_depth = (
            (swing_lo - prev_low) if bull_sweep else (prev_high - swing_hi)
        )

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=strength,
            metadata={
                'sweep_level':  sweep_level,
                'pierce_depth': pierce_depth,
                'wick_pct':     wick_pct,
                'atr':          atr,
            },
        )
