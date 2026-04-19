"""
pop_screener/stocktwits_social.py — StockTwits Social Sentiment adapter.

Production replacement for the mock SocialSentimentSource.
Uses the StockTwits API v2 (FREE — no API key required for public endpoints).

API docs: https://api.stocktwits.com/developers/docs/api

Endpoint: GET https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json
    - No authentication needed for public streams
    - Rate limit: 200 requests/hour (unauthenticated)
    - Returns latest 30 messages per call
    - Each message has sentiment: {basic: {sentiment: 'Bullish'|'Bearish'|null}}

Usage
-----
    from pop_screener.stocktwits_social import StockTwitsSocialSource

    source = StockTwitsSocialSource()  # no key needed
    social = source.get_social('AAPL', window_hours=1.0)

Rate limit strategy
-------------------
    200 req/hr = ~3.3 req/min. With 200 tickers checked every minute,
    we MUST cache aggressively. Cache TTL = 5 minutes per symbol.
    At 200 tickers / 5-min cycle = 40 unique requests/5 min = 8/min = 480/hr.
    Still over limit. Solution: rotate through tickers — only fetch the
    ones with recent BAR activity (typically 20-50 active at any time).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

import requests

from pop_screener.models import SocialData

log = logging.getLogger(__name__)

_CACHE_TTL = 900  # 15 minutes — stay well within 200 req/hr limit


class StockTwitsSocialSource:
    """
    Production social sentiment adapter using StockTwits public API.

    Implements the same interface as the mock SocialSentimentSource:
        get_social(symbol: str, window_hours: float) -> SocialData
    """

    _BASE_URL = 'https://api.stocktwits.com/api/2/streams/symbol'

    def __init__(
        self,
        access_token: Optional[str] = None,
        timeout: float = 5.0,
    ):
        """
        Parameters
        ----------
        access_token : optional StockTwits OAuth token (increases rate limit to 400/hr)
        timeout : HTTP request timeout in seconds
        """
        self._access_token = access_token
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            'Accept': 'application/json',
            'User-Agent': 'TradingHub/1.0',
        })
        # Cache: {symbol: (monotonic_time, SocialData)}
        self._cache: Dict[str, Tuple[float, SocialData]] = {}
        # Rate limit tracking
        self._last_request_time = 0.0
        self._min_interval = 20.0  # 20s between requests → max 180/hr (under 200 limit)
        # Hourly request counter for rate limit monitoring
        self._request_count = 0
        self._request_window_start = time.time()
        self._rate_limit_warnings = 0

    def get_social(self, symbol: str, window_hours: float = 1.0) -> SocialData:
        """
        Fetch social sentiment for *symbol*.

        Returns SocialData with mention counts and bullish/bearish percentages.
        On API failure, returns neutral SocialData (never raises).
        """
        # Cache check
        cached = self._cache.get(symbol)
        if cached and (time.monotonic() - cached[0]) < _CACHE_TTL:
            return cached[1]

        # --- Rate limit: non-blocking throttle ---
        # If we made a request too recently, return cached/default instead of sleeping
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._min_interval:
            return self._get_cached_or_default(symbol)

        # --- Hourly rate limit monitoring ---
        # Reset counter every hour
        if time.time() - self._request_window_start > 3600:
            self._request_count = 0
            self._request_window_start = time.time()
        # Hard stop at limit
        if self._request_count >= 180:
            if self._request_count == 180:
                log.warning("[StockTwits] Rate limit reached (180/200) — returning cached data until next hour")
            self._request_count += 1
            return self._get_cached_or_default(symbol)
        self._request_count += 1

        try:
            result = self._fetch(symbol)
            self._cache[symbol] = (time.monotonic(), result)
            return result
        except Exception as exc:
            log.warning("[StockTwits] API error for %s: %s", symbol, exc)
            # Return neutral data on failure
            return SocialData(
                symbol=symbol,
                mention_count=0,
                mention_velocity=0.0,
                bullish_pct=0.50,
                bearish_pct=0.50,
            )

    def _get_cached_or_default(self, symbol: str) -> SocialData:
        """Return last cached SocialData for *symbol*, or a neutral default."""
        cached = self._cache.get(symbol)
        if cached:
            return cached[1]
        return SocialData(
            symbol=symbol,
            mention_count=0,
            mention_velocity=0.0,
            bullish_pct=0.50,
            bearish_pct=0.50,
        )

    def _fetch(self, symbol: str) -> SocialData:
        """Call StockTwits API and parse response."""
        url = f"{self._BASE_URL}/{symbol}.json"
        params = {}
        if self._access_token:
            params['access_token'] = self._access_token

        self._last_request_time = time.monotonic()
        resp = self._session.get(url, params=params, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()

        # Parse messages
        messages = data.get('messages', [])
        total = len(messages)
        if total == 0:
            return SocialData(
                symbol=symbol,
                mention_count=0,
                mention_velocity=0.0,
                bullish_pct=0.50,
                bearish_pct=0.50,
            )

        bullish = 0
        bearish = 0
        with_sentiment = 0

        for msg in messages:
            entities = msg.get('entities', {})
            sentiment = entities.get('sentiment')
            if not sentiment or not isinstance(sentiment, dict):
                continue

            # StockTwits format: {'basic': 'Bullish'} or {'basic': 'Bearish'}
            sent_label = sentiment.get('basic', '')

            if sent_label == 'Bullish':
                bullish += 1
                with_sentiment += 1
            elif sent_label == 'Bearish':
                bearish += 1
                with_sentiment += 1

        # Calculate percentages from messages WITH sentiment tags
        if with_sentiment > 0:
            bullish_pct = round(bullish / with_sentiment, 4)
            bearish_pct = round(bearish / with_sentiment, 4)
        else:
            bullish_pct = 0.50
            bearish_pct = 0.50

        # StockTwits returns latest 30 messages — estimate velocity
        # by looking at the time span of returned messages
        newest_ts_str = ''
        oldest_ts_str = ''
        if len(messages) >= 2:
            try:
                newest = _parse_ts(messages[0].get('created_at', ''))
                oldest = _parse_ts(messages[-1].get('created_at', ''))
                newest_ts_str = newest.isoformat()
                oldest_ts_str = oldest.isoformat()
                span_hours = max((newest - oldest).total_seconds() / 3600, 0.01)
                mention_velocity = round(total / span_hours, 1)
            except Exception:
                mention_velocity = float(total)
        elif len(messages) == 1:
            try:
                ts = _parse_ts(messages[0].get('created_at', ''))
                newest_ts_str = ts.isoformat()
                oldest_ts_str = newest_ts_str
            except Exception:
                pass
            mention_velocity = float(total)
        else:
            mention_velocity = float(total)

        return SocialData(
            symbol=symbol,
            mention_count=total,
            mention_velocity=mention_velocity,
            bullish_pct=bullish_pct,
            bearish_pct=bearish_pct,
            newest_message_time=newest_ts_str,
            oldest_message_time=oldest_ts_str,
        )


    def discover_trending(self) -> List[str]:
        """V8: Fetch StockTwits trending tickers. 1 API call = top 30 tickers.
        No per-ticker cost. Returns list of trending ticker symbols.
        """
        # Cache for 15 min
        cached = self._cache.get('__trending__')
        if cached and (time.monotonic() - cached[0]) < 900:
            return cached[1]

        try:
            url = 'https://api.stocktwits.com/api/2/trending/symbols.json'
            params = {}
            if self._access_token:
                params['access_token'] = self._access_token
            resp = self._session.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                symbols = data.get('symbols', [])
                tickers = [s.get('symbol', '') for s in symbols
                          if isinstance(s, dict) and s.get('symbol', '').isalpha()
                          and len(s.get('symbol', '')) <= 5]
                self._cache['__trending__'] = (time.monotonic(), tickers)
                log.info("[StockTwits] Trending: %d tickers", len(tickers))
                return tickers
        except Exception as exc:
            log.debug("[StockTwits] Trending fetch failed: %s", exc)
        return []


def _parse_ts(ts_str: str) -> datetime:
    """Parse StockTwits timestamp format: '2026-04-12T10:30:00Z'."""
    for fmt in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S%z'):
        try:
            dt = datetime.strptime(ts_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return datetime.now(timezone.utc)
