"""
pop_screener/classifier.py — Deterministic strategy classifier
===============================================================
Maps each PopCandidate to a StrategyAssignment using explicit, rule-based
logic.  No ML, no randomness — same inputs always produce the same assignment.

Classification logic per pop_reason
-------------------------------------
  MODERATE_NEWS   → primary=VWAP_RECLAIM; fallback EMA_TREND_CONTINUATION
  HIGH_IMPACT_NEWS → primary=ORB; secondary adds HALT or PARABOLIC if extreme
  SENTIMENT_POP   → primary=VWAP_RECLAIM or EMA_TREND_CONTINUATION; fallback BOPB
  UNUSUAL_VOLUME  → primary=VWAP_RECLAIM or EMA_TREND_CONTINUATION or BOPB
  LOW_FLOAT       → primary=HALT_RESUME_BREAKOUT or PARABOLIC_REVERSAL
                     VWAP_RECLAIM explicitly disallowed (vwap_compat=0)
  EARNINGS        → primary=VWAP_RECLAIM (small gap) or ORB (large gap)
                     secondary EMA_TREND if trend is clean
"""
from __future__ import annotations

from typing import List, Optional

from pop_screener import config as cfg
from pop_screener.models import (
    EngineeredFeatures, FloatCategory, PopCandidate, PopReason,
    StrategyAssignment, StrategyType,
)


class StrategyClassifier:
    """
    Deterministic mapper from PopCandidate → StrategyAssignment.

    Usage
    -----
        clf = StrategyClassifier()
        assignment = clf.classify(candidate)
    """

    def classify(self, candidate: PopCandidate) -> StrategyAssignment:
        """
        Map a PopCandidate to a StrategyAssignment.

        Dispatches to the appropriate rule-function by pop_reason.
        """
        dispatch = {
            PopReason.MODERATE_NEWS:    self._classify_moderate_news,
            PopReason.HIGH_IMPACT_NEWS: self._classify_high_impact_news,
            PopReason.SENTIMENT_POP:    self._classify_sentiment_pop,
            PopReason.UNUSUAL_VOLUME:   self._classify_unusual_volume,
            PopReason.LOW_FLOAT:        self._classify_low_float,
            PopReason.EARNINGS:         self._classify_earnings,
        }
        fn = dispatch[candidate.pop_reason]
        return fn(candidate)

    def classify_all(
        self, candidates: List[PopCandidate]
    ) -> List[StrategyAssignment]:
        """Classify a list of pop candidates; returns one assignment per candidate."""
        return [self.classify(c) for c in candidates]

    # ── Per-reason classifiers ─────────────────────────────────────────────────

    def _classify_moderate_news(self, c: PopCandidate) -> StrategyAssignment:
        """
        Moderate news → VWAP_RECLAIM if VWAP compatible; else EMA_TREND.

        VWAP compatibility is high when:
          - vwap_distance is small (price near VWAP)
          - trend is clean
          - RVOL is in the institutional-participation band (1.5–5×)
        """
        f = c.features
        vwap_compat = _vwap_compat_score(f)
        clean       = f.trend_cleanliness_score

        if vwap_compat >= 0.50 and clean >= cfg.TREND_CLEAN_VWAP_MIN:
            primary     = StrategyType.VWAP_RECLAIM
            secondaries = [StrategyType.EMA_TREND_CONTINUATION]
            confidence  = _blend(vwap_compat, clean, f.rvol / 5.0)
            notes = (
                f"Moderate news pop; VWAP compat={vwap_compat:.2f}, "
                f"trend cleanliness={clean:.2f} → VWAP_RECLAIM preferred"
            )
        else:
            primary     = StrategyType.EMA_TREND_CONTINUATION
            secondaries = [StrategyType.BREAKOUT_PULLBACK]
            vwap_compat = min(vwap_compat, 0.40)   # cap compat when VWAP not preferred
            confidence  = _blend(clean, f.rvol / 5.0, 0.5)
            notes = (
                f"Moderate news pop; VWAP compat={vwap_compat:.2f} or trend not clean "
                f"({clean:.2f}) → EMA_TREND_CONTINUATION"
            )

        return StrategyAssignment(
            symbol=c.symbol,
            primary_strategy=primary,
            secondary_strategies=secondaries,
            vwap_compatibility_score=round(vwap_compat, 3),
            strategy_confidence=round(min(confidence, 1.0), 3),
            notes=notes,
        )

    def _classify_high_impact_news(self, c: PopCandidate) -> StrategyAssignment:
        """
        High-impact news → ORB primary.
        Adds HALT or PARABOLIC secondaries when gap/volatility is extreme.
        """
        f = c.features
        secondaries: List[StrategyType] = []

        if (
            f.volatility_score > cfg.VOLATILITY_EXTREME_THRESHOLD
            and abs(f.gap_size) > cfg.GAP_EXTREME_THRESHOLD
        ):
            secondaries = [
                StrategyType.HALT_RESUME_BREAKOUT,
                StrategyType.PARABOLIC_REVERSAL,
            ]
            notes = (
                f"High-impact news; gap={f.gap_size:.1%}, "
                f"vol_score={f.volatility_score:.2%} → ORB + HALT/PARABOLIC"
            )
        else:
            secondaries = [StrategyType.EMA_TREND_CONTINUATION]
            notes = (
                f"High-impact news; gap={f.gap_size:.1%} → ORB primary"
            )

        confidence = _blend(
            min(abs(f.gap_size) / 0.10, 1.0),   # larger gap = higher conf
            min(f.rvol / 5.0, 1.0),
            0.6,
        )
        vwap_compat = _vwap_compat_score(f) * 0.5   # VWAP less relevant for big gaps

        return StrategyAssignment(
            symbol=c.symbol,
            primary_strategy=StrategyType.ORB,
            secondary_strategies=secondaries,
            vwap_compatibility_score=round(vwap_compat, 3),
            strategy_confidence=round(min(confidence, 1.0), 3),
            notes=notes,
        )

    def _classify_sentiment_pop(self, c: PopCandidate) -> StrategyAssignment:
        """
        Social sentiment pop → VWAP_RECLAIM or EMA_TREND, with BOPB fallback.
        """
        f = c.features
        vwap_compat = _vwap_compat_score(f)
        clean       = f.trend_cleanliness_score

        if (
            clean >= cfg.TREND_CLEAN_EMA_MIN
            and abs(f.vwap_distance) < cfg.VWAP_DISTANCE_EMA_MAX
        ):
            if vwap_compat >= 0.50:
                primary     = StrategyType.VWAP_RECLAIM
                secondaries = [StrategyType.EMA_TREND_CONTINUATION]
                notes = (
                    f"Sentiment pop; VWAP compat={vwap_compat:.2f}, "
                    f"clean trend → VWAP_RECLAIM"
                )
            else:
                primary     = StrategyType.EMA_TREND_CONTINUATION
                secondaries = [StrategyType.BREAKOUT_PULLBACK]
                notes = (
                    f"Sentiment pop; clean trend but low VWAP compat → "
                    f"EMA_TREND_CONTINUATION"
                )
        else:
            primary     = StrategyType.EMA_TREND_CONTINUATION
            secondaries = [StrategyType.BREAKOUT_PULLBACK]
            vwap_compat = min(vwap_compat, 0.30)
            notes = (
                f"Sentiment pop; trend not clean ({clean:.2f}) or price "
                f"far from VWAP ({f.vwap_distance:.2%}) → EMA_TREND + BOPB"
            )

        confidence = _blend(
            f.social_velocity / 10.0,
            (f.social_sentiment_skew + 1.0) / 2.0,
            clean,
        )
        return StrategyAssignment(
            symbol=c.symbol,
            primary_strategy=primary,
            secondary_strategies=secondaries,
            vwap_compatibility_score=round(vwap_compat, 3),
            strategy_confidence=round(min(confidence, 1.0), 3),
            notes=notes,
        )

    def _classify_unusual_volume(self, c: PopCandidate) -> StrategyAssignment:
        """
        Unusual volume → VWAP_RECLAIM when trend clean and VWAP respected;
        else EMA_TREND_CONTINUATION or BOPB.
        """
        f = c.features
        vwap_compat = _vwap_compat_score(f)
        clean       = f.trend_cleanliness_score

        if clean >= cfg.TREND_CLEAN_VWAP_MIN and vwap_compat >= 0.50:
            primary     = StrategyType.VWAP_RECLAIM
            secondaries = [StrategyType.EMA_TREND_CONTINUATION]
            notes = (
                f"Unusual volume ({f.rvol:.1f}×); clean trend + VWAP respected "
                f"→ VWAP_RECLAIM"
            )
        elif clean >= cfg.TREND_CLEAN_EMA_MIN:
            primary     = StrategyType.EMA_TREND_CONTINUATION
            secondaries = [StrategyType.BREAKOUT_PULLBACK]
            vwap_compat = min(vwap_compat, 0.40)
            notes = (
                f"Unusual volume ({f.rvol:.1f}×); moderate trend cleanliness "
                f"→ EMA_TREND_CONTINUATION"
            )
        else:
            primary     = StrategyType.BREAKOUT_PULLBACK
            secondaries = [StrategyType.EMA_TREND_CONTINUATION]
            vwap_compat = min(vwap_compat, 0.35)
            notes = (
                f"Unusual volume ({f.rvol:.1f}×); messy trend → BOPB"
            )

        confidence = _blend(
            min(f.rvol / 5.0, 1.0),
            clean,
            vwap_compat,
        )
        return StrategyAssignment(
            symbol=c.symbol,
            primary_strategy=primary,
            secondary_strategies=secondaries,
            vwap_compatibility_score=round(vwap_compat, 3),
            strategy_confidence=round(min(confidence, 1.0), 3),
            notes=notes,
        )

    def _classify_low_float(self, c: PopCandidate) -> StrategyAssignment:
        """
        Low-float pop → HALT_RESUME_BREAKOUT primary; PARABOLIC_REVERSAL secondary.
        VWAP_RECLAIM explicitly disallowed — low-float stocks have erratic VWAP
        behaviour that makes VWAP signals unreliable.
        """
        f = c.features

        # Prefer HALT if the gap is very large (halt-like move likely)
        if abs(f.gap_size) >= 0.15 or f.volatility_score > cfg.VOLATILITY_EXTREME_THRESHOLD:
            primary     = StrategyType.HALT_RESUME_BREAKOUT
            secondaries = [StrategyType.PARABOLIC_REVERSAL]
            notes = (
                f"Low-float pop; gap={f.gap_size:.1%}, "
                f"vol={f.volatility_score:.2%} → HALT_RESUME_BREAKOUT"
            )
        else:
            primary     = StrategyType.PARABOLIC_REVERSAL
            secondaries = [StrategyType.HALT_RESUME_BREAKOUT]
            notes = (
                f"Low-float pop; moderate volatility → PARABOLIC_REVERSAL"
            )

        confidence = _blend(
            min(f.rvol / 8.0, 1.0),
            min(abs(f.gap_size) / 0.20, 1.0),
            0.7,
        )
        # Explicitly zero out VWAP compatibility
        return StrategyAssignment(
            symbol=c.symbol,
            primary_strategy=primary,
            secondary_strategies=secondaries,
            vwap_compatibility_score=0.0,   # disallowed
            strategy_confidence=round(min(confidence, 1.0), 3),
            notes=notes,
        )

    def _classify_earnings(self, c: PopCandidate) -> StrategyAssignment:
        """
        Earnings pop → VWAP_RECLAIM for small gaps; ORB for large gaps.
        Secondary EMA_TREND_CONTINUATION if trend is clean.
        """
        f = c.features
        clean       = f.trend_cleanliness_score
        vwap_compat = _vwap_compat_score(f)
        secondaries: List[StrategyType] = []

        if abs(f.gap_size) < cfg.GAP_SIZE_EARNINGS_VWAP_MAX:
            primary = StrategyType.VWAP_RECLAIM
            notes = (
                f"Earnings pop; gap={f.gap_size:.1%} < "
                f"{cfg.GAP_SIZE_EARNINGS_VWAP_MAX:.0%} → VWAP_RECLAIM"
            )
        else:
            primary = StrategyType.ORB
            vwap_compat = min(vwap_compat, 0.40)
            notes = (
                f"Earnings pop; large gap={f.gap_size:.1%} → ORB"
            )

        if clean >= cfg.TREND_CLEAN_EMA_MIN:
            secondaries.append(StrategyType.EMA_TREND_CONTINUATION)

        confidence = _blend(
            min(abs(f.gap_size) / 0.08, 1.0),
            min(f.rvol / 4.0, 1.0),
            vwap_compat,
        )
        return StrategyAssignment(
            symbol=c.symbol,
            primary_strategy=primary,
            secondary_strategies=secondaries,
            vwap_compatibility_score=round(vwap_compat, 3),
            strategy_confidence=round(min(confidence, 1.0), 3),
            notes=notes,
        )


# ── Score helpers ──────────────────────────────────────────────────────────────

def _vwap_compat_score(f: EngineeredFeatures) -> float:
    """
    Compute a [0, 1] VWAP compatibility score using three components:
      - VWAP distance (closer = better; linear decay from 0 to VWAP_DISTANCE_MAX)
      - Trend cleanliness
      - RVOL in the institutional band (1.5–5×)

    Weights match _WEIGHT_TREND / _WEIGHT_RVOL / _WEIGHT_VWAP from config.
    """
    # VWAP distance component — peaks at 0 distance, 0 at ≥ 3 % away
    max_dist = 0.03
    dist_score = max(0.0, 1.0 - abs(f.vwap_distance) / max_dist)

    # RVOL in-band component [1.5, 5] normalised to [0, 1]
    rvol_lo, rvol_hi = cfg.RVOL_VWAP_RECLAIM_MIN, cfg.RVOL_VWAP_RECLAIM_MAX
    if f.rvol < rvol_lo:
        rvol_score = f.rvol / rvol_lo   # ramp up to threshold
    elif f.rvol <= rvol_hi:
        rvol_score = 1.0
    else:
        rvol_score = max(0.0, 1.0 - (f.rvol - rvol_hi) / 5.0)

    score = (
        cfg._WEIGHT_VWAP  * dist_score
        + cfg._WEIGHT_TREND * f.trend_cleanliness_score
        + cfg._WEIGHT_RVOL  * rvol_score
    )
    return round(min(max(score, 0.0), 1.0), 4)


def _blend(*values: float) -> float:
    """Equal-weight average of all provided values, clamped to [0, 1]."""
    if not values:
        return 0.5
    return min(max(sum(values) / len(values), 0.0), 1.0)
