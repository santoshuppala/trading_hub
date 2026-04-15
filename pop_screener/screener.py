"""
pop_screener/screener.py — Rule-based pop-stock detection
==========================================================
Applies six deterministic rule-sets to an EngineeredFeatures object and
returns a PopCandidate when any rule fires, or None when no rule fires.

Rules are evaluated in priority order:
  1. high_impact_news_pop  (strongest catalyst — checked first)
  2. earnings_pop
  3. low_float_pop
  4. moderate_news_pop
  5. sentiment_pop
  6. unusual_volume_pop

Only the *first* matching rule is used to avoid duplicate candidates for the
same symbol.  If a symbol would match multiple rules the most specific /
highest-priority rule wins.
"""
from __future__ import annotations

from typing import List, Optional

from pop_screener import config as cfg
from pop_screener.models import (
    EngineeredFeatures, FloatCategory, PopCandidate, PopReason,
)


class PopScreener:
    """
    Rule-based screener that promotes EngineeredFeatures → PopCandidate.

    Usage
    -----
        screener = PopScreener()
        candidate = screener.screen(features)
        if candidate:
            ...
    """

    def screen(self, features: EngineeredFeatures) -> Optional[PopCandidate]:
        """
        Evaluate all pop-detection rules for one symbol.

        Returns the first matching PopCandidate, or None if no rule fires.
        Rules are evaluated in priority order (highest-impact first).
        """
        for rule_fn in (
            self._high_impact_news_pop,
            self._earnings_pop,
            self._low_float_pop,
            self._moderate_news_pop,
            self._sentiment_pop,
            self._unusual_volume_pop,
        ):
            candidate = rule_fn(features)
            if candidate is not None:
                return candidate
        return None

    def screen_universe(
        self, features_list: List[EngineeredFeatures]
    ) -> List[PopCandidate]:
        """
        Screen a list of EngineeredFeatures objects and return all candidates.

        Parameters
        ----------
        features_list : one EngineeredFeatures per symbol in the universe

        Returns
        -------
        list of PopCandidate, one per symbol that passed at least one rule
        """
        candidates = []
        for f in features_list:
            c = self.screen(f)
            if c is not None:
                candidates.append(c)
        return candidates

    # ── Individual rule implementations ───────────────────────────────────────

    def _high_impact_news_pop(self, f: EngineeredFeatures) -> Optional[PopCandidate]:
        """
        HIGH_IMPACT_NEWS: major catalyst with high headline velocity and large gap.

        Conditions (ALL must be true):
          - sentiment_delta  > SENTIMENT_DELTA_HIGH
          - headline_velocity > HEADLINE_VELOCITY_HIGH
          - abs(gap_size)   >= GAP_SIZE_HIGH_IMPACT_MIN
        """
        if (
            f.sentiment_delta    > cfg.SENTIMENT_DELTA_HIGH
            and f.headline_velocity > cfg.HEADLINE_VELOCITY_HIGH
            and abs(f.gap_size)  >= cfg.GAP_SIZE_HIGH_IMPACT_MIN
        ):
            return PopCandidate(
                symbol=f.symbol,
                features=f,
                pop_reason=PopReason.HIGH_IMPACT_NEWS,
                raw_scores={
                    'sentiment_delta':    f.sentiment_delta,
                    'headline_velocity':  f.headline_velocity,
                    'gap_size':           f.gap_size,
                    'rvol':               f.rvol,
                },
            )
        return None

    def _earnings_pop(self, f: EngineeredFeatures) -> Optional[PopCandidate]:
        """
        EARNINGS: earnings-driven gap move.

        Conditions (ALL must be true):
          - earnings_flag == True  (now propagated from MarketDataSlice through
            EngineeredFeatures)
          - abs(gap_size) > GAP_SIZE_EARNINGS_MIN
          - rvol > threshold (2.0 for stocks, ETF_RVOL_MIN for ETFs)
        """
        from monitor.sector_map import is_etf, ETF_RVOL_MIN
        rvol_min = ETF_RVOL_MIN if is_etf(f.symbol) else 2.0
        if (
            f.earnings_flag
            and abs(f.gap_size) > cfg.GAP_SIZE_EARNINGS_MIN
            and f.rvol > rvol_min
        ):
            return PopCandidate(
                symbol=f.symbol,
                features=f,
                pop_reason=PopReason.EARNINGS,
                raw_scores={
                    'gap_size':        f.gap_size,
                    'sentiment_delta': f.sentiment_delta,
                    'rvol':            f.rvol,
                },
            )
        return None

    def _low_float_pop(self, f: EngineeredFeatures) -> Optional[PopCandidate]:
        """
        LOW_FLOAT: low-float stock with abnormal volume and a meaningful gap.

        Conditions (ALL must be true):
          - float_category  == LOW_FLOAT
          - rvol            > RVOL_LOW_FLOAT_THRESHOLD
          - abs(gap_size)   > GAP_SIZE_LOW_FLOAT_MIN
        """
        if (
            f.float_category == FloatCategory.LOW_FLOAT
            and f.rvol       > cfg.RVOL_LOW_FLOAT_THRESHOLD
            and abs(f.gap_size) > cfg.GAP_SIZE_LOW_FLOAT_MIN
        ):
            return PopCandidate(
                symbol=f.symbol,
                features=f,
                pop_reason=PopReason.LOW_FLOAT,
                raw_scores={
                    'float_category': 1.0,
                    'rvol':           f.rvol,
                    'gap_size':       f.gap_size,
                },
            )
        return None

    def _moderate_news_pop(self, f: EngineeredFeatures) -> Optional[PopCandidate]:
        """
        MODERATE_NEWS: news-driven move without an extreme gap.

        Conditions (ALL must be true):
          - sentiment_delta    > SENTIMENT_DELTA_MODERATE
          - headline_velocity  in [HEADLINE_VELOCITY_MIN, HEADLINE_VELOCITY_MAX]
          - abs(gap_size)      < GAP_SIZE_MODERATE_MAX
        """
        if (
            f.sentiment_delta    > cfg.SENTIMENT_DELTA_MODERATE
            and cfg.HEADLINE_VELOCITY_MIN <= f.headline_velocity <= cfg.HEADLINE_VELOCITY_MAX
            and abs(f.gap_size)  < cfg.GAP_SIZE_MODERATE_MAX
        ):
            return PopCandidate(
                symbol=f.symbol,
                features=f,
                pop_reason=PopReason.MODERATE_NEWS,
                raw_scores={
                    'sentiment_delta':   f.sentiment_delta,
                    'headline_velocity': f.headline_velocity,
                    'gap_size':          f.gap_size,
                },
            )
        return None

    def _sentiment_pop(self, f: EngineeredFeatures) -> Optional[PopCandidate]:
        """
        SENTIMENT_POP: social-media-driven retail momentum.

        Conditions (ALL must be true):
          - social_velocity       > SOCIAL_VELOCITY_THRESHOLD
          - social_sentiment_skew > SOCIAL_SKEW_THRESHOLD
        """
        if (
            f.social_velocity       > cfg.SOCIAL_VELOCITY_THRESHOLD
            and f.social_sentiment_skew > cfg.SOCIAL_SKEW_THRESHOLD
        ):
            return PopCandidate(
                symbol=f.symbol,
                features=f,
                pop_reason=PopReason.SENTIMENT_POP,
                raw_scores={
                    'social_velocity':        f.social_velocity,
                    'social_sentiment_skew':  f.social_sentiment_skew,
                    'rvol':                   f.rvol,
                },
            )
        return None

    def _unusual_volume_pop(self, f: EngineeredFeatures) -> Optional[PopCandidate]:
        """
        UNUSUAL_VOLUME: abnormal volume with positive price momentum.

        Conditions (ALL must be true):
          - rvol            > threshold (RVOL_UNUSUAL_THRESHOLD for stocks, lower for ETFs)
          - price_momentum  > MOMENTUM_MIN
        """
        from monitor.sector_map import is_etf, ETF_RVOL_MIN
        rvol_threshold = max(ETF_RVOL_MIN, 1.0) if is_etf(f.symbol) else cfg.RVOL_UNUSUAL_THRESHOLD
        if (
            f.rvol           > rvol_threshold
            and f.price_momentum > cfg.MOMENTUM_MIN
        ):
            return PopCandidate(
                symbol=f.symbol,
                features=f,
                pop_reason=PopReason.UNUSUAL_VOLUME,
                raw_scores={
                    'rvol':            f.rvol,
                    'price_momentum':  f.price_momentum,
                },
            )
        return None
