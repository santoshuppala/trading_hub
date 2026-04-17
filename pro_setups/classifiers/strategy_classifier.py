"""
StrategyClassifier — maps detector outputs to (strategy_name, tier).

Priority order: Tier 3 (highest R:R, most specific) → Tier 2 → Tier 1.
Each rule checks required detectors and minimum strength thresholds.
First matching rule wins.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

from ..detectors.base import DetectorSignal

log = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    """Output of StrategyClassifier.classify()."""
    strategy_name: str
    tier:          int     # 1, 2, or 3
    direction:     str     # 'long' or 'short'
    confidence:    float   # [0, 1]


class StrategyClassifier:
    """
    Deterministic, rule-based classifier.  Same detector outputs always
    produce the same classification result.

    Rules are evaluated in priority order.  The first rule whose required
    detectors have all fired (at or above threshold) wins.

    Tier 3 rules are checked first — they represent the most selective,
    highest R:R setups with the most stringent conditions.
    """

    # ── Classification rules (priority-ordered, Tier 3 first) ─────────────
    # Each entry: (strategy_name, tier, required_detectors, min_strength)
    # required_detectors: list of (detector_name, min_strength) pairs
    # All must fire; additional fired detectors boost confidence.

    _RULES = [
        # ── Tier 3 ───────────────────────────────────────────────────────
        ('momentum_ignition', 3, [('momentum', 0.70)]),
        ('fib_confluence',    3, [('fib',      0.60)]),
        ('bollinger_squeeze', 3, [('volatility', 0.60)]),
        ('liquidity_sweep',   3, [('liquidity', 0.60)]),
        # ── Tier 2 ───────────────────────────────────────────────────────
        ('flag_pennant',      2, [('flag',       0.50)]),
        ('gap_and_go',        2, [('gap',        0.50)]),
        ('inside_bar',        2, [('inside_bar', 0.50)]),
        ('orb',               2, [('orb',        0.50)]),
        # ── Tier 1 ───────────────────────────────────────────────────────
        ('sr_flip',           1, [('sr',         0.60)]),
        ('vwap_reclaim',      1, [('vwap',       0.50)]),
        ('trend_pullback',    1, [('trend',      0.50), ('vwap', 0.30)]),
    ]

    # Detectors whose direction vote also influences the final direction
    _DIRECTION_WEIGHT = {
        'trend':    0.40,
        'vwap':     0.25,
        'momentum': 0.20,
        'sr':       0.15,
    }

    def classify(
        self,
        ticker:           str,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[ClassificationResult]:
        """
        V8: Evaluate ALL rules and return the HIGHEST CONFIDENCE match.
        Tie-break by tier (higher tier = higher R:R = better).

        V8: Sentiment boost — if sentiment detector fired, lower required
        detector strengths by up to 25%. Formula:
          adjusted_threshold = base_threshold × (1.0 - sentiment_strength × 0.25)
        """
        # V8: Get sentiment strength for threshold reduction
        sentiment_strength = 0.0
        sentiment_sig = detector_outputs.get('sentiment')
        if sentiment_sig and sentiment_sig.fired:
            sentiment_strength = sentiment_sig.strength

        candidates = []
        for strategy_name, tier, required in self._RULES:
            result = self._evaluate_rule(
                strategy_name, tier, required, detector_outputs,
                sentiment_strength=sentiment_strength,
            )
            if result is not None:
                candidates.append(result)

        if not candidates:
            return None

        # Pick highest confidence; break ties by tier (higher tier = better R:R)
        best = max(candidates, key=lambda r: (r.confidence, r.tier))
        log.debug(
            "[classifier][%s] matched=%s tier=%d dir=%s conf=%.2f "
            "(from %d candidates, sentiment=%.2f)",
            ticker, best.strategy_name, best.tier, best.direction,
            best.confidence, len(candidates), sentiment_strength,
        )
        return best

    # ── Private helpers ───────────────────────────────────────────────────

    def _evaluate_rule(
        self,
        strategy_name:    str,
        tier:             int,
        required:         list,
        outputs:          Dict[str, DetectorSignal],
        sentiment_strength: float = 0.0,
    ) -> Optional[ClassificationResult]:
        """
        Check all required detector conditions.  Returns None on any miss.
        Confidence = arithmetic mean of required strengths × optional bonus.

        V8: Sentiment reduces required detector strength thresholds by up to 25%.
        """
        strengths = []
        for det_name, min_str in required:
            # V8: Sentiment lowers thresholds (max 25% reduction)
            adjusted_min = min_str * (1.0 - sentiment_strength * 0.25)
            sig = outputs.get(det_name)
            if sig is None or not sig.fired or sig.strength < adjusted_min:
                return None
            strengths.append(sig.strength)

        if not strengths:
            return None

        base_conf = sum(strengths) / len(strengths)

        # V8: Optional detector bonus weighted by strength (not fixed +0.05)
        optional_bonus = 0.0
        req_names = {r[0] for r in required}
        for name, sig in outputs.items():
            if name not in req_names and sig.fired:
                optional_bonus += min(sig.strength * 0.05, 0.05)
        optional_bonus = min(optional_bonus, 0.15)  # cap total bonus

        confidence = min(base_conf + optional_bonus, 1.0)

        # Direction: weighted vote from all required detectors.
        # If detectors disagree, reject the signal (return None).
        directions = set()
        for det_name, _ in required:
            sig = outputs[det_name]
            if sig.direction in ('long', 'short'):
                directions.add(sig.direction)

        if len(directions) != 1:
            # Detectors disagree on direction or none provided a valid one
            return None
        direction = directions.pop()

        return ClassificationResult(
            strategy_name=strategy_name,
            tier=tier,
            direction=direction,
            confidence=confidence,
        )
