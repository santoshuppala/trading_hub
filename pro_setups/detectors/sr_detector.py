"""SRDetector — pivot-based support/resistance levels + S/R flip detection.

V9: Two-layer S/R detection:
  Layer 1 (9:45 AM): Daily pivots from yesterday's H/L/C (institutional standard)
  Layer 2 (10:20 AM): Intraday pivots from 5-min bars (session-level structure)
  Both layers active simultaneously once Layer 2 matures.
"""
from __future__ import annotations
from typing import List, Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal
from ._compute import pivot_highs, pivot_lows


class SRDetector(BaseDetector):
    """
    Identifies support/resistance levels and checks whether the current
    price is within proximity of any level.

    V9: Two-layer approach:
      Layer 1: Daily pivots (PP, R1, R2, S1, S2) from yesterday's H/L/C.
               Available from first bar. Every institutional desk watches these.
      Layer 2: Intraday pivots from 5-min bars (structural levels forming today).
               Available after _MIN_5M_BARS (50 min).
      Both layers coexist — daily pivots remain valid all session.

    S/R flip logic: a level that was previously resistance (price was below
    it) and now price is above it acts as support → long signal.
    """
    name:           str   = 'sr'
    MIN_BARS:       int   = 15       # reduced: daily pivots need minimal bars
    _MIN_5M_BARS:   int   = 10      # 10 5-min bars = 50 min for intraday pivots
    _LOOKBACK:      int   = 40
    _PROXIMITY_PCT: float = 0.006   # within 0.6%
    _PIVOT_WINDOW:  int   = 3       # bars on each side for pivot detection

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
        precomputed: dict = None,
        **kw,
    ) -> DetectorSignal:
        last_close = float(df['close'].iloc[-1])
        all_levels: List[float] = []

        # ── Layer 1: Daily pivots from yesterday's H/L/C ─────────────────
        # Available from first bar. Standard institutional S/R levels.
        if rvol_df is not None and not rvol_df.empty:
            try:
                prev_day = rvol_df.iloc[-1]
                prev_high = float(prev_day.get('high', prev_day.get('h', 0)))
                prev_low = float(prev_day.get('low', prev_day.get('l', 0)))
                prev_close = float(prev_day.get('close', prev_day.get('c', 0)))

                if prev_high > 0 and prev_low > 0 and prev_close > 0:
                    pp = (prev_high + prev_low + prev_close) / 3
                    r1 = 2 * pp - prev_low
                    r2 = pp + (prev_high - prev_low)
                    s1 = 2 * pp - prev_high
                    s2 = pp - (prev_high - prev_low)
                    all_levels.extend([pp, r1, r2, s1, s2])
                    # Also add yesterday's high and low as levels
                    all_levels.extend([prev_high, prev_low])
            except Exception:
                pass

        # ── Layer 2: Intraday pivots from 5-min bars ─────────────────────
        # Available after 50 min. Adds session-level structure on top of daily.
        df_5min = precomputed.get('df_5min') if precomputed else None
        if df_5min is not None and len(df_5min) >= self._MIN_5M_BARS:
            window = df_5min.tail(self._LOOKBACK)
            ph = pivot_highs(window, self._PIVOT_WINDOW)
            pl = pivot_lows(window, self._PIVOT_WINDOW)
            all_levels.extend(ph + pl)

        if not all_levels:
            return DetectorSignal.no_signal()

        # Remove duplicates and invalid levels
        all_levels = [lv for lv in set(all_levels) if lv > 0]
        if not all_levels:
            return DetectorSignal.no_signal()

        # Find closest level to current price
        nearest = min(all_levels, key=lambda x: abs(x - last_close))
        dist = abs(nearest - last_close) / last_close

        if dist > self._PROXIMITY_PCT:
            return DetectorSignal.no_signal()

        # Determine context: price above level → acting as support (long)
        direction = 'long' if last_close > nearest else 'short'
        strength = 1.0 - (dist / self._PROXIMITY_PCT)

        # S/R flip bonus: check if we have intraday pivots to compare
        flip = False
        if df_5min is not None and len(df_5min) >= self._MIN_5M_BARS:
            window = df_5min.tail(self._LOOKBACK)
            ph = pivot_highs(window, self._PIVOT_WINDOW)
            pl = pivot_lows(window, self._PIVOT_WINDOW)
            flip = (nearest in ph and direction == 'long') or \
                   (nearest in pl and direction == 'short')

        if flip:
            strength = min(strength + 0.2, 1.0)

        # Determine source of the nearest level
        is_daily = nearest not in (ph + pl if df_5min is not None and len(df_5min) >= self._MIN_5M_BARS else [])

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=strength,
            metadata={
                'level':       nearest,
                'dist_pct':    dist,
                'flip':        flip,
                'source':      'daily_pivot' if is_daily else 'intraday_5m',
                'n_levels':    len(all_levels),
                'near_levels': sorted(
                    all_levels,
                    key=lambda x: abs(x - last_close)
                )[:5],
            },
        )
