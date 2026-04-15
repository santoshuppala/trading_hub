"""
pop_screener/ml_classifier.py — XGBoost-based strategy classifier with rules fallback
======================================================================================
Loads a pre-trained XGBoost model (data/pop_classifier.joblib) at init time.
If the model file is missing, import fails, or prediction confidence is below
threshold, falls back transparently to the deterministic StrategyClassifier.

Train the model offline with:  python scripts/train_pop_classifier.py
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

log = logging.getLogger(__name__)


class MLStrategyClassifier:
    """ML-based strategy classifier that falls back to rules if model unavailable."""

    # Map indices to strategy types (must match training order in train_pop_classifier.py)
    _STRATEGY_NAMES: List[str] = [
        'VWAP_RECLAIM', 'ORB', 'HALT_RESUME_BREAKOUT',
        'EMA_TREND_CONTINUATION', 'BREAKOUT_PULLBACK', 'PARABOLIC_REVERSAL',
    ]

    # Feature names extracted from EngineeredFeatures, in training order
    _FEATURE_ATTRS: List[str] = [
        'atr_value', 'volatility_score', 'rvol', 'price_momentum', 'gap_size',
        'vwap_distance', 'trend_cleanliness_score',
        'sentiment_score', 'sentiment_delta', 'headline_velocity',
        'social_velocity', 'social_sentiment_skew',
        'last_price',
    ]

    def __init__(self, model_path: Optional[str] = None, min_confidence: float = 0.6):
        if model_path is None:
            model_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), 'data', 'pop_classifier.joblib'
            )
        self._model = None
        self._min_confidence = min_confidence
        self._fallback = None  # lazy import to avoid circular

        try:
            import joblib
            if os.path.exists(model_path):
                self._model = joblib.load(model_path)
                log.info("ML classifier loaded from %s", model_path)
            else:
                log.info("ML model not found at %s -- using rules fallback", model_path)
        except Exception as exc:
            log.warning("ML classifier load failed: %s -- using rules fallback", exc)

    # ── Public API ────────────────────────────────────────────────────────────

    def classify(self, candidate):
        """Classify a PopCandidate into a StrategyAssignment.

        Uses ML model if available and confidence >= threshold,
        otherwise falls back to rules-based classifier.
        """
        if self._model is None:
            return self._get_fallback().classify(candidate)

        try:
            features = self._extract_features(candidate)
            proba = self._model.predict_proba([features])[0]
            best_idx = int(proba.argmax())
            confidence = float(proba[best_idx])

            if confidence < self._min_confidence:
                log.debug(
                    "ML confidence %.2f < %.2f -- falling back to rules",
                    confidence, self._min_confidence,
                )
                return self._get_fallback().classify(candidate)

            from pop_screener.models import StrategyType, StrategyAssignment

            strategy_name = self._STRATEGY_NAMES[best_idx]
            strategy_type = StrategyType[strategy_name]

            # Build secondary strategies from next-best predictions
            secondaries = self._pick_secondaries(proba, best_idx)

            return StrategyAssignment(
                symbol=candidate.symbol,
                primary_strategy=strategy_type,
                secondary_strategies=secondaries,
                vwap_compatibility_score=round(
                    self._estimate_vwap_compat(candidate, strategy_type), 3
                ),
                strategy_confidence=round(confidence, 3),
                notes=(
                    f"ML classifier: {strategy_name} (conf={confidence:.2f})"
                ),
            )
        except Exception as exc:
            log.debug("ML classify failed: %s -- falling back to rules", exc)
            return self._get_fallback().classify(candidate)

    def classify_all(self, candidates):
        """Classify a list of PopCandidates; returns one assignment per candidate."""
        return [self.classify(c) for c in candidates]

    # ── Internals ─────────────────────────────────────────────────────────────

    def _get_fallback(self):
        if self._fallback is None:
            from pop_screener.classifier import StrategyClassifier
            self._fallback = StrategyClassifier()
        return self._fallback

    def _extract_features(self, candidate) -> List[float]:
        """Extract feature vector from PopCandidate matching training features."""
        eng = candidate.features  # EngineeredFeatures dataclass

        features: List[float] = []
        for attr in self._FEATURE_ATTRS:
            val = getattr(eng, attr, None)
            features.append(float(val if val is not None else 0))

        return features

    def _pick_secondaries(self, proba, best_idx: int) -> list:
        """Return up to 2 secondary strategies from next-best predictions."""
        from pop_screener.models import StrategyType

        # Sort indices by probability descending, skip best
        ranked = sorted(range(len(proba)), key=lambda i: -proba[i])
        secondaries = []
        for idx in ranked:
            if idx == best_idx:
                continue
            if proba[idx] < 0.10:
                break
            name = self._STRATEGY_NAMES[idx]
            secondaries.append(StrategyType[name])
            if len(secondaries) >= 2:
                break
        return secondaries

    @staticmethod
    def _estimate_vwap_compat(candidate, strategy_type) -> float:
        """Rough VWAP compatibility estimate for ML-assigned strategies."""
        from pop_screener.models import StrategyType as ST

        if strategy_type in (ST.HALT_RESUME_BREAKOUT, ST.PARABOLIC_REVERSAL):
            return 0.0  # VWAP not relevant for these

        f = candidate.features
        vwap_dist = abs(getattr(f, 'vwap_distance', 0) or 0)
        clean = getattr(f, 'trend_cleanliness_score', 0.5) or 0.5

        if strategy_type == ST.VWAP_RECLAIM:
            # Close to VWAP + clean trend = high compatibility
            dist_score = max(0.0, 1.0 - vwap_dist / 0.03)
            return min(0.4 * dist_score + 0.4 * clean + 0.2, 1.0)

        # Other strategies: moderate VWAP relevance
        dist_score = max(0.0, 1.0 - vwap_dist / 0.05)
        return min(0.3 * dist_score + 0.3 * clean + 0.1, 1.0)
