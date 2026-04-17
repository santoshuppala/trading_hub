"""
BaseProStrategy — contract for all 11 pro setup strategy modules.

Every strategy implements the four-method contract required by the
existing StrategyEngine interface, adapted for pro_setups:

    detect_signal(ticker, df, detector_outputs) → 'long' | 'short' | None
    generate_entry(ticker, df, direction, detector_outputs) → float
    generate_stop(entry_price, direction, atr, df) → float
    generate_exit(entry_price, stop_price, direction) → (target_1, target_2)

Tier constants (SL_ATR, PARTIAL_R, FULL_R) are defined at class level.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

import pandas as pd

from ..detectors.base import DetectorSignal


class BaseProStrategy(ABC):
    """
    Abstract base strategy.  Tier-specific subclasses override the class-level
    constants; strategies override ``detect_signal`` and ``generate_entry``.
    ``generate_stop`` and ``generate_exit`` use the tier constants and are
    rarely overridden.

    Tier constants
    --------------
    Tier 1: SL=0.4 ATR, partial=1R, full=2R   (high win-rate)
    Tier 2: SL=1.0 ATR, partial=1.5R, full=3R (moderate, higher R:R)
    Tier 3: SL=2.0 ATR, partial=3R,   full=6R  (low win-rate, high expectancy)
    """
    TIER:       int   = 0      # must be set by concrete class
    SL_ATR:     float = 1.0    # stop-loss ATR multiplier
    PARTIAL_R:  float = 1.5    # partial-exit at this multiple of 1R
    FULL_R:     float = 3.0    # full-exit at this multiple of 1R
    MIN_STOP_PCT: float = 0.003  # absolute minimum stop: 0.3% (edge case floor)
    DAILY_ATR_MULT: float = 1.5  # fallback stop = 1.5× daily ATR (when no SR found)

    @abstractmethod
    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        """
        Final signal confirmation.  Returns 'long', 'short', or None.
        The classifier has already selected this strategy; this method
        verifies the specific conditions required for an entry.
        """
        ...

    @abstractmethod
    def generate_entry(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        direction:        str,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> float:
        """
        Returns the proposed entry price.  Typically the current close
        or a specific level derived from detector metadata.
        """
        ...

    def generate_stop(
        self,
        entry_price: float,
        direction:   str,
        atr:         float,
        df:          pd.DataFrame,
        outputs:     dict = None,
    ) -> float:
        """
        Dynamic structure-aware stop loss using full-day S/R analysis.

        Approach:
        1. Scan ALL bars for the day to find pivot support/resistance levels
        2. Score each level by how many times price touched/bounced off it
           (more touches = stronger level)
        3. Pick the strongest support below entry (for longs) as the stop
        4. If no strong level found, fall back to daily ATR-based stop
        5. Absolute floor: MIN_STOP_PCT (0.2%) from entry

        This gives each stock a stop at a level that has actually held
        during today's trading — not an arbitrary distance.
        """
        outputs = outputs or {}
        abs_min_offset = entry_price * self.MIN_STOP_PCT

        # ── 1. Find daily S/R levels scored by touch count ───────────
        sr_stop = self._find_strongest_sr_level(entry_price, direction, df, abs_min_offset)

        # ── 2. Daily ATR stop (fallback) ─────────────────────────────
        # Use ATR from all available bars (full day)
        daily_atr_offset = abs_min_offset
        n_bars = len(df)
        if n_bars >= 10:
            highs = df['high'].values
            lows = df['low'].values
            closes = df['close'].values
            tr_vals = []
            for i in range(1, n_bars):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
                tr_vals.append(tr)
            if tr_vals:
                daily_atr = sum(tr_vals) / len(tr_vals)
                daily_atr_offset = self.DAILY_ATR_MULT * daily_atr

        if direction == 'long':
            atr_stop = entry_price - daily_atr_offset
        else:
            atr_stop = entry_price + daily_atr_offset

        # ── 3. Pick the best stop ────────────────────────────────────
        # Prefer the structural SR level — it's a real price level that held.
        # Only fall back to ATR if no strong SR level exists.
        # Cap SR-based stop at max 3% from entry to avoid huge risk.
        max_offset = entry_price * 0.03  # 3% max stop distance

        if direction == 'long':
            if sr_stop is not None:
                stop = sr_stop
                stop = max(stop, entry_price - max_offset)  # cap at 3%
                # V8: T2/T3 use WIDER of ATR and structural (survive overnight gap)
                # T1 uses TIGHTER (intraday, close at EOD)
                if self.TIER >= 2:
                    stop = min(stop, atr_stop)  # wider = further from entry
                # else: keep sr_stop (tighter) for T1
            else:
                stop = atr_stop
            stop = min(stop, entry_price - abs_min_offset)
            return round(max(stop, 0.01), 4)
        else:
            if sr_stop is not None:
                stop = sr_stop
                stop = min(stop, entry_price + max_offset)
                if self.TIER >= 2:
                    stop = max(stop, atr_stop)  # wider for shorts = further from entry
            else:
                stop = atr_stop
            stop = max(stop, entry_price + abs_min_offset)
            return round(stop, 4)

    @staticmethod
    def _find_strongest_sr_level(
        entry_price: float,
        direction: str,
        df: 'pd.DataFrame',
        min_offset: float,
        proximity_pct: float = 0.002,  # 0.2% band for clustering touches
        min_touches: int = 2,           # level must be tested at least 2×
    ) -> float | None:
        """
        Scan the full day's bars for support/resistance levels, scored by
        how many times price touched and bounced off each level.

        Returns the strongest level below entry (longs) or above entry (shorts),
        or None if no level qualifies.
        """
        if len(df) < 20:
            return None

        lows = df['low'].values
        highs = df['high'].values
        closes = df['close'].values

        # ── Build candidate levels from pivot lows (support) and highs (resistance)
        window = 3  # bars on each side
        pivot_levels = []

        for i in range(window, len(lows) - window):
            # Pivot low (potential support)
            if all(lows[i] < lows[i - j] for j in range(1, window + 1)) and \
               all(lows[i] < lows[i + j] for j in range(1, window + 1)):
                pivot_levels.append(('support', float(lows[i])))

            # Pivot high (potential resistance)
            if all(highs[i] > highs[i - j] for j in range(1, window + 1)) and \
               all(highs[i] > highs[i + j] for j in range(1, window + 1)):
                pivot_levels.append(('resistance', float(highs[i])))

        if not pivot_levels:
            return None

        # ── Cluster nearby levels and count touches ──────────────────
        # Group levels within proximity_pct of each other
        levels_with_score = []
        used = set()

        for idx, (kind, level) in enumerate(pivot_levels):
            if idx in used:
                continue

            band = level * proximity_pct
            cluster = [level]
            used.add(idx)

            for j, (_, other_level) in enumerate(pivot_levels):
                if j not in used and abs(other_level - level) <= band:
                    cluster.append(other_level)
                    used.add(j)

            avg_level = sum(cluster) / len(cluster)

            # Count how many bars touched this level (low within band for support)
            touches = 0
            bounces = 0
            for i in range(len(lows)):
                if abs(lows[i] - avg_level) <= band:
                    touches += 1
                    # Bounce = price touched level then closed above it
                    if closes[i] > avg_level + band:
                        bounces += 1
                if abs(highs[i] - avg_level) <= band:
                    touches += 1
                    if closes[i] < avg_level - band:
                        bounces += 1

            score = len(cluster) + touches + bounces * 2  # weight bounces higher
            levels_with_score.append((avg_level, score, touches, kind))

        # ── Pick the strongest valid level ────────────────────────────
        if direction == 'long':
            valid = [
                (lv, sc) for lv, sc, tc, kind in levels_with_score
                if lv < entry_price - min_offset and tc >= min_touches
            ]
            if not valid:
                return None
            # Pick the closest strong level below entry
            valid.sort(key=lambda x: (-x[1], -x[0]))  # best score, then closest
            return valid[0][0] - 0.01  # just below the level
        else:
            valid = [
                (lv, sc) for lv, sc, tc, kind in levels_with_score
                if lv > entry_price + min_offset and tc >= min_touches
            ]
            if not valid:
                return None
            valid.sort(key=lambda x: (-x[1], x[0]))
            return valid[0][0] + 0.01

    def generate_exit(
        self,
        entry_price: float,
        stop_price:  float,
        direction:   str,
    ) -> Tuple[float, float]:
        """
        Tier-based exit targets.  Returns (target_1, target_2).
        Risk R = abs(entry_price - stop_price).
        """
        risk = abs(entry_price - stop_price)
        if direction == 'long':
            t1 = round(entry_price + self.PARTIAL_R * risk, 4)
            t2 = round(entry_price + self.FULL_R   * risk, 4)
        else:
            t1 = round(entry_price - self.PARTIAL_R * risk, 4)
            t2 = round(entry_price - self.FULL_R   * risk, 4)
        return t1, t2
