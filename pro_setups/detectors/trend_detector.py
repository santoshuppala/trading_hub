"""TrendDetector — Intraday EMA alignment + structure confirmation.

V10.2: Redesigned based on proven 9 EMA Pullback research.
Core trend: EMA9 > EMA20 on 5-min bars (valid by 9:50 AM).
EMA50 is an optional strength booster, NOT a gate.

Previous design required EMA9 > EMA20 > EMA50 (all three aligned),
which delayed trend detection to 10:30-11:00 AM — missing the
proven best window (9:45-11:00, 71% win rate for EMA9 pullbacks).

Reference: QuantifiedStrategies, TOS Indicators, Grokipedia 9 EMA Pullback.
"""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import compute_ema, compute_atr


class TrendDetector(BaseDetector):
    """
    Intraday trend detection using 5-min EMA alignment + structure.

    Core requirement (gate):  EMA9 > EMA20 on 5-min bars
    Strength booster:         EMA50 alignment adds +0.15 when available
    Structure confirmation:   45% higher-highs + higher-lows in lookback

    Fires from ~9:50 AM (4 five-min bars for EMA20 on 5-min).
    """
    name:          str = 'trend'
    MIN_BARS:      int = 20       # V10.2: EMA20 on 1-min fallback (was 52)
    _MIN_5M_BARS:  int = 10       # V10.2: 10 five-min bars = 50 min = 10:20 AM
                                  # EMA9 needs 9 periods to stabilize (have 10 ✓)
                                  # EMA20 starting to stabilize (10 of 20 periods)
                                  # Structural confirmation: 9 comparisons (meaningful)
                                  # Was 4 (too early, EMAs meaningless)
                                  # Was 12 (too late, misses best window)
    _LOOKBACK:     int = 20
    _MIN_STR:      float = 0.45   # 45% of bars must show trending structure

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
        precomputed: dict = None,
        **kw,
    ) -> DetectorSignal:
        # Use 5-min bars for trend detection (less noise than 1-min)
        work_df = precomputed.get('df_5min', df) if precomputed else df
        if len(work_df) < self._MIN_5M_BARS:
            return DetectorSignal.no_signal()

        # Core EMAs: 9 and 20 (required)
        ema9 = precomputed.get('ema_9_5m') if precomputed else compute_ema(work_df['close'], 9)
        ema20 = precomputed.get('ema_21_5m') if precomputed else compute_ema(work_df['close'], 20)

        if ema9 is None or ema20 is None:
            return DetectorSignal.no_signal()

        last_e9  = float(ema9.iloc[-1])
        last_e20 = float(ema20.iloc[-1])

        # V10.2: Core trend = EMA9 vs EMA20 only (not EMA50)
        uptrend   = last_e9 > last_e20
        downtrend = last_e9 < last_e20

        if not (uptrend or downtrend):
            return DetectorSignal.no_signal()

        # EMA50: optional strength booster (when available)
        ema50 = precomputed.get('ema_50_5m') if precomputed else None
        if ema50 is None and len(work_df) >= 50:
            ema50 = compute_ema(work_df['close'], 50)
        last_e50 = float(ema50.iloc[-1]) if ema50 is not None and len(ema50) > 0 else None
        has_ema50_alignment = False
        if last_e50 is not None:
            if uptrend and last_e20 > last_e50:
                has_ema50_alignment = True
            elif downtrend and last_e20 < last_e50:
                has_ema50_alignment = True

        # Structural confirmation: HH/HL (uptrend) or LH/LL (downtrend)
        n_lookback = min(self._LOOKBACK, len(work_df) - 1)
        if n_lookback <= 0:
            return DetectorSignal.no_signal()

        highs = work_df['high'].values[-n_lookback - 1:]
        lows  = work_df['low'].values[-n_lookback - 1:]
        n = len(highs) - 1

        if uptrend:
            hh = sum(1 for i in range(1, n + 1) if highs[i] > highs[i - 1])
            hl = sum(1 for i in range(1, n + 1) if lows[i]  > lows[i - 1])
            strength = (hh + hl) / (2 * n)
            direction = 'long'
        else:
            lh = sum(1 for i in range(1, n + 1) if highs[i] < highs[i - 1])
            ll = sum(1 for i in range(1, n + 1) if lows[i]  < lows[i - 1])
            strength = (lh + ll) / (2 * n)
            direction = 'short'

        if strength < self._MIN_STR:
            return DetectorSignal.no_signal()

        # EMA50 alignment boosts strength by 0.15 (optional, not required)
        if has_ema50_alignment:
            strength = min(strength + 0.15, 1.0)

        # Pullback context: ATR-based proximity (not percentage-based)
        # Research: pullback "to" EMA means within ~0.7 ATR of EMA9
        # or within ~1.2 ATR of EMA20
        last_close = float(df['close'].iloc[-1])
        atr = precomputed.get('atr') if precomputed else compute_atr(df)
        if not atr or atr <= 0:
            atr = abs(last_close * 0.01)  # fallback 1%

        dist_ema9  = abs(last_close - last_e9)
        dist_ema20 = abs(last_close - last_e20)
        near_ema9  = dist_ema9 <= atr * 0.7
        near_ema20 = dist_ema20 <= atr * 1.2

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=min(strength, 1.0),
            metadata={
                'ema9':               last_e9,
                'ema20':              last_e20,
                'ema50':              last_e50,
                'ema50_aligned':      has_ema50_alignment,
                'near_ema9':          near_ema9,
                'near_ema20':         near_ema20,
                'dist_ema9_atr':      round(dist_ema9 / atr, 3) if atr > 0 else 0,
                'dist_ema20_atr':     round(dist_ema20 / atr, 3) if atr > 0 else 0,
            },
        )
