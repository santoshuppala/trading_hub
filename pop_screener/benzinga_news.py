"""
pop_screener/benzinga_news.py — Benzinga News API adapter.

Production replacement for the mock NewsSentimentSource.
Uses the Benzinga Content API v2 to fetch real headlines with sentiment.

API docs: https://docs.benzinga.io/benzinga/newsfeed-v2.html

Usage
-----
    from pop_screener.benzinga_news import BenzingaNewsSentimentSource

    source = BenzingaNewsSentimentSource(api_key=os.getenv('BENZINGA_API_KEY'))
    headlines = source.get_news('AAPL', window_hours=1.0)

Rate limits
-----------
    Benzinga Pro: 500 requests/min.
    We cache results per (symbol, window_hours) for 60 seconds to avoid
    hammering the API on every BAR event (200 tickers x 2 calls = 400/min).
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests

from pop_screener.models import NewsData

log = logging.getLogger(__name__)

# Simple keyword-based sentiment when Benzinga doesn't provide scores
_BULLISH_KEYWORDS = frozenset({
    'upgrade', 'buy', 'outperform', 'beat', 'raises', 'guidance',
    'record', 'breakout', 'surge', 'soar', 'jump', 'rally', 'gain',
    'approval', 'fda', 'breakthrough', 'buyback', 'dividend',
    'partnership', 'contract', 'awarded', 'strong', 'exceeds',
})
_BEARISH_KEYWORDS = frozenset({
    'downgrade', 'sell', 'underperform', 'miss', 'cuts', 'lower',
    'decline', 'plunge', 'crash', 'drop', 'fall', 'slump', 'loss',
    'investigation', 'lawsuit', 'fraud', 'recall', 'warning',
    'resign', 'layoff', 'restructuring', 'weak', 'disappointing',
})

_CACHE_TTL = 60  # seconds


class BenzingaNewsSentimentSource:
    """
    Production news adapter using Benzinga Content API.

    Implements the same interface as the mock NewsSentimentSource:
        get_news(symbol: str, window_hours: float) -> List[NewsData]
    """

    _BASE_URL = 'https://api.benzinga.com/api/v2/news'

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 5.0,
        max_results: int = 20,
    ):
        self._api_key = api_key or os.getenv('BENZINGA_API_KEY') or os.getenv('BENZENGA_API_KEY', '')
        self._timeout = timeout
        self._max_results = max_results
        self._session = requests.Session()
        self._session.headers.update({
            'Accept': 'application/json',
        })
        # Cache: {(symbol, window_key): (timestamp, List[NewsData])}
        self._cache: Dict[Tuple[str, str], Tuple[float, List[NewsData]]] = {}

        if not self._api_key:
            log.warning("[BenzingaNews] No API key found — get_news() will return empty lists")

    def get_news(self, symbol: str, window_hours: float = 24.0) -> List[NewsData]:
        """
        Fetch recent news headlines for *symbol* within the last *window_hours*.

        Returns List[NewsData] sorted oldest -> newest.
        On API failure, returns empty list (never raises).
        """
        if not self._api_key:
            return []

        # Cache check
        cache_key = (symbol, f"{window_hours:.1f}")
        cached = self._cache.get(cache_key)
        if cached and (time.monotonic() - cached[0]) < _CACHE_TTL:
            return cached[1]

        try:
            results = self._fetch(symbol, window_hours)
            self._cache[cache_key] = (time.monotonic(), results)
            return results
        except Exception as exc:
            log.warning("[BenzingaNews] API error for %s: %s", symbol, exc)
            return []

    def _fetch(self, symbol: str, window_hours: float) -> List[NewsData]:
        """Call Benzinga API and parse response."""
        since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        # Benzinga v2 uses 'updatedSince' with format YYYY-MM-DD or epoch
        date_from = since.strftime('%Y-%m-%d')

        params = {
            'token': self._api_key,
            'tickers': symbol,
            'pageSize': self._max_results,
        }

        resp = self._session.get(
            self._BASE_URL,
            params=params,
            headers={'Accept': 'application/json'},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()

        results: List[NewsData] = []
        articles = data if isinstance(data, list) else data.get('data', data.get('articles', []))

        for article in articles:
            try:
                # Parse timestamp
                created = article.get('created', article.get('updated', ''))
                if created:
                    # Benzinga format: "Sun, 12 Apr 2026 08:30:00 -0400"
                    ts = _parse_benzinga_ts(created)
                else:
                    ts = datetime.now(timezone.utc)

                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                # Extract headline
                headline = article.get('title', article.get('headline', ''))
                if not headline:
                    continue

                # Sentiment: Benzinga Pro provides sentiment_score in analytics
                # For basic tier, compute from keywords
                raw_sentiment = article.get('sentiment', None)
                if raw_sentiment and isinstance(raw_sentiment, (int, float)):
                    sentiment = max(-1.0, min(1.0, float(raw_sentiment)))
                else:
                    sentiment = self._compute_sentiment(headline)

                results.append(NewsData(
                    timestamp=ts,
                    headline=headline[:500],  # cap length
                    sentiment_score=round(sentiment, 4),
                    source='benzinga',
                ))
            except Exception as exc:
                log.debug("[BenzingaNews] Skipping article: %s", exc)
                continue

        # Filter by time window (API may return older articles)
        results = [n for n in results if n.timestamp >= since]
        results.sort(key=lambda n: n.timestamp)
        return results

    @staticmethod
    def _compute_sentiment(headline: str) -> float:
        """
        Simple keyword-based sentiment when API doesn't provide scores.
        Returns float in [-1.0, +1.0].
        """
        words = set(headline.lower().split())
        bull = len(words & _BULLISH_KEYWORDS)
        bear = len(words & _BEARISH_KEYWORDS)
        total = bull + bear
        if total == 0:
            return 0.0
        return round((bull - bear) / total, 2)


def _parse_benzinga_ts(ts_str: str) -> datetime:
    """Parse Benzinga timestamp: 'Sun, 12 Apr 2026 08:30:00 -0400'."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(ts_str)
    except Exception:
        try:
            return datetime.fromisoformat(ts_str)
        except Exception:
            return datetime.now(timezone.utc)
