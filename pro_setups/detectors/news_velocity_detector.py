"""
V8 NewsVelocityDetector — detects abnormal headline velocity for a ticker.

Uses Benzinga headlines + Yahoo earnings calendar from alt_data_cache.json.
Fires when headline count is significantly above baseline or earnings are imminent.
"""
import logging
from typing import Optional

import pandas as pd

from .base import BaseDetector, DetectorSignal

log = logging.getLogger(__name__)


class NewsVelocityDetector(BaseDetector):
    """
    Detects abnormal news activity for a ticker.
    Fires on high headline velocity or imminent earnings.
    """

    name = 'news_velocity'
    MIN_BARS = 5

    def _detect(
        self,
        ticker: str,
        df: pd.DataFrame,
        rvol_df: Optional[pd.DataFrame] = None,
    ) -> DetectorSignal:
        try:
            from data_sources.alt_data_reader import alt_data
        except ImportError:
            return DetectorSignal.no_signal()

        headline_velocity = 0.0
        earnings_imminent = False
        direction = 'neutral'
        sentiment_delta = 0.0

        # Benzinga headline velocity
        try:
            news = alt_data.news_sentiment(ticker)
            if news:
                baseline = max(news.get('headline_baseline', 2.0), 0.5)
                current = news.get('headlines_1h', 0)
                headline_velocity = current / baseline
                sentiment_delta = news.get('sentiment_delta', 0)
                if sentiment_delta > 0.1:
                    direction = 'long'
                elif sentiment_delta < -0.1:
                    direction = 'short'
        except Exception:
            pass

        # Yahoo earnings calendar
        try:
            earnings = alt_data.earnings(ticker)
            if earnings:
                days_to_earnings = earnings.get('days_to_earnings', 999)
                earnings_imminent = days_to_earnings <= 2
        except Exception:
            pass

        # Compute strength
        strength = min(headline_velocity / 8.0, 1.0)
        if earnings_imminent:
            strength = max(strength, 0.5)

        fired = headline_velocity > 1.5 or earnings_imminent

        return DetectorSignal(
            fired=fired,
            direction=direction,
            strength=strength,
            metadata={
                'headline_velocity': round(headline_velocity, 2),
                'earnings_imminent': earnings_imminent,
                'sentiment_delta': round(sentiment_delta, 4),
            },
        )
