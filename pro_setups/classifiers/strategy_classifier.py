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
        ('vwap_reclaim',      1, [('vwap',       0.40)]),
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
        Returns a ``ClassificationResult`` for the first matching rule,
        or ``None`` if no rule matches.
        """
        for strategy_name, tier, required in self._RULES:
            result = self._evaluate_rule(
                strategy_name, tier, required, detector_outputs
            )
            if result is not None:
                log.debug(
                    "[classifier][%s] matched=%s tier=%d dir=%s conf=%.2f",
                    ticker, strategy_name, tier, result.direction, result.confidence,
                )
                return result
        return None

    # ── Private helpers ───────────────────────────────────────────────────

    def _evaluate_rule(
        self,
        strategy_name:    str,
        tier:             int,
        required:         list,
        outputs:          Dict[str, DetectorSignal],
    ) -> Optional[ClassificationResult]:
        """
        Check all required detector conditions.  Returns None on any miss.
        Confidence = geometric mean of required strengths × optional bonus.
        """
        strengths = []
        for det_name, min_str in required:
            sig = outputs.get(det_name)
            if sig is None or not sig.fired or sig.strength < min_str:
                return None
            strengths.append(sig.strength)

        if not strengths:
            return None

        base_conf = sum(strengths) / len(strengths)

        # Optional detector bonus (+0.05 per additional fired detector)
        optional_bonus = 0.0
        req_names = {r[0] for r in required}
        for name, sig in outputs.items():
            if name not in req_names and sig.fired:
                optional_bonus = min(optional_bonus + 0.05, 0.15)

        confidence = min(base_conf + optional_bonus, 1.0)

        # Direction: primary from the first required detector
        primary_det  = required[0][0]
        primary_sig  = outputs[primary_det]
        direction    = primary_sig.direction

        # Validate direction is actionable
        if direction not in ('long', 'short'):
            return None

        return ClassificationResult(
            strategy_name=strategy_name,
            tier=tier,
            direction=direction,
            confidence=confidence,
        )
