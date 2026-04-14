"""
pop_screener/ticker_discovery.py — Dynamic Ticker Discovery from News & Social

Discovers trending/hyped stocks that aren't in the static watchlist by scanning:
  1. Benzinga news headlines — extracts ticker symbols from recent articles
  2. StockTwits trending — fetches trending symbols from StockTwits API
  3. Benzinga movers — top gainers/losers/most-active from Benzinga

This ensures that stocks like CRWV (trending on news/social but not in the
static TICKERS list) get picked up by the PopStrategyEngine automatically.

Called by MomentumScreener.refresh() every 30 minutes during market hours.

Usage:
    discovery = TickerDiscovery(benzinga_key='...', stocktwits_token=None)
    trending = discovery.discover()  # Returns set of ticker symbols
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

import requests

log = logging.getLogger(__name__)

# Cache TTL — don't hammer APIs
_CACHE_TTL = 600  # 10 minutes between discovery refreshes

# Minimum mentions to qualify as "trending"
_MIN_BENZINGA_MENTIONS = 2
_MIN_STOCKTWITS_WATCHERS = 1000

# Ticker validation pattern: 1-5 uppercase letters
_TICKER_RE = re.compile(r'^[A-Z]{1,5}$')

# Common words that look like tickers but aren't
_FALSE_TICKERS = frozenset({
    'A', 'I', 'AM', 'AN', 'AS', 'AT', 'BE', 'BY', 'DO', 'GO', 'HE',
    'IF', 'IN', 'IS', 'IT', 'ME', 'MY', 'NO', 'OF', 'ON', 'OR', 'SO',
    'TO', 'UP', 'US', 'WE', 'AI', 'CEO', 'CFO', 'COO', 'CTO', 'FDA',
    'SEC', 'IPO', 'ETF', 'NYSE', 'API', 'GDP', 'CPI', 'FED', 'FOMC',
    'EPS', 'PE', 'ATH', 'ATL', 'OTC', 'THE', 'AND', 'FOR', 'ARE',
    'BUT', 'NOT', 'YOU', 'ALL', 'CAN', 'HER', 'WAS', 'ONE', 'OUR',
    'OUT', 'DAY', 'HAD', 'HAS', 'HIS', 'HOW', 'ITS', 'MAY', 'NEW',
    'NOW', 'OLD', 'SEE', 'WAY', 'WHO', 'DID', 'GET', 'HIM', 'LET',
    'SAY', 'SHE', 'TOO', 'USE', 'DD', 'PM', 'EST', 'PST', 'EOD',
    'PRE', 'POST', 'YTD', 'QOQ', 'MOM', 'WOW', 'LOL', 'IMO', 'ICYMI',
    'TLDR', 'HODL', 'YOLO', 'FOMO', 'BTFD', 'MOASS',
})

# ETFs are included in discovery — no exclusion list


class TickerDiscovery:
    """
    Discovers trending tickers from news and social media.

    Sources (tried in order, failures don't block):
      1. Benzinga headlines — extract mentioned symbols
      2. StockTwits trending — API trending endpoint
      3. Benzinga movers — top gainers/losers
    """

    def __init__(
        self,
        benzinga_key: Optional[str] = None,
        stocktwits_token: Optional[str] = None,
        timeout: float = 5.0,
    ):
        self._benzinga_key = benzinga_key or os.getenv('BENZINGA_API_KEY') or os.getenv('BENZENGA_API_KEY', '')
        self._stocktwits_token = stocktwits_token or os.getenv('STOCKTWITS_TOKEN', '')
        self._timeout = timeout
        self._session = requests.Session()

        # Cache: (timestamp, Set[str])
        self._cache: Optional[Tuple[float, Set[str]]] = None

    def discover(self, exclude_tickers: Set[str] = None) -> Set[str]:
        """
        Discover trending tickers from all sources.

        Args:
            exclude_tickers: symbols already in the watchlist (skip these)

        Returns:
            Set of new ticker symbols to add to the scan universe
        """
        # Rate limit
        if self._cache is not None:
            ts, cached = self._cache
            if time.time() - ts < _CACHE_TTL:
                return cached

        exclude = exclude_tickers or set()
        discovered: Set[str] = set()

        # Source 1: Benzinga headlines
        try:
            from_news = self._discover_from_benzinga_news()
            discovered |= from_news
            if from_news:
                log.info("[TickerDiscovery] Benzinga news: %d tickers — %s",
                         len(from_news), ', '.join(sorted(from_news)[:10]))
        except Exception as e:
            log.warning("[TickerDiscovery] Benzinga news scan failed: %s", e)

        # Source 2: StockTwits trending
        try:
            from_social = self._discover_from_stocktwits_trending()
            discovered |= from_social
            if from_social:
                log.info("[TickerDiscovery] StockTwits trending: %d tickers — %s",
                         len(from_social), ', '.join(sorted(from_social)[:10]))
        except Exception as e:
            log.warning("[TickerDiscovery] StockTwits trending scan failed: %s", e)

        # Source 3: Benzinga movers
        try:
            from_movers = self._discover_from_benzinga_movers()
            discovered |= from_movers
            if from_movers:
                log.info("[TickerDiscovery] Benzinga movers: %d tickers — %s",
                         len(from_movers), ', '.join(sorted(from_movers)[:10]))
        except Exception as e:
            log.warning("[TickerDiscovery] Benzinga movers scan failed: %s", e)

        # Filter
        result = {
            t for t in discovered
            if self._is_valid_ticker(t) and t not in exclude
        }

        self._cache = (time.time(), result)

        if result:
            log.info("[TickerDiscovery] Total discovered: %d new tickers — %s",
                     len(result), ', '.join(sorted(result)))

        return result

    # ── Benzinga News Headlines ─────────────────────────────────────────────

    def _discover_from_benzinga_news(self) -> Set[str]:
        """
        Scan recent Benzinga headlines and extract ticker symbols.
        Uses the tickers field from the Benzinga API response.
        """
        if not self._benzinga_key:
            return set()

        url = 'https://api.benzinga.com/api/v2/news'
        since = datetime.now(timezone.utc) - timedelta(hours=4)

        params = {
            'token': self._benzinga_key,
            'pageSize': 50,
            'sort': 'created:desc',
            'dateFrom': since.strftime('%Y-%m-%dT%H:%M:%S'),
        }

        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            articles = resp.json()
        except Exception as e:
            log.debug("[TickerDiscovery] Benzinga API error: %s", e)
            return set()

        if not isinstance(articles, list):
            return set()

        # Count mentions per ticker
        mention_counts: Dict[str, int] = {}
        for article in articles:
            # Benzinga API returns stocks field with ticker objects
            stocks = article.get('stocks', [])
            if isinstance(stocks, list):
                for stock in stocks:
                    name = stock.get('name', '') if isinstance(stock, dict) else str(stock)
                    if name and _TICKER_RE.match(name):
                        mention_counts[name] = mention_counts.get(name, 0) + 1

            # Also extract from title as fallback
            title = article.get('title', '')
            for word in title.split():
                clean = word.strip('.,;:!?()[]"\'$#@')
                if _TICKER_RE.match(clean) and clean not in _FALSE_TICKERS:
                    mention_counts[clean] = mention_counts.get(clean, 0) + 1

        # Return tickers with enough mentions
        return {
            ticker for ticker, count in mention_counts.items()
            if count >= _MIN_BENZINGA_MENTIONS
        }

    # ── StockTwits Trending ─────────────────────────────────────────────────

    def _discover_from_stocktwits_trending(self) -> Set[str]:
        """
        Fetch trending symbols from StockTwits.
        Uses the trending/symbols endpoint (public, no auth required).
        """
        url = 'https://api.stocktwits.com/api/2/trending/symbols.json'
        params = {}
        if self._stocktwits_token:
            params['access_token'] = self._stocktwits_token

        try:
            resp = self._session.get(url, params=params, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.debug("[TickerDiscovery] StockTwits trending error: %s", e)
            return set()

        symbols = data.get('symbols', [])
        if not isinstance(symbols, list):
            return set()

        result = set()
        for sym_data in symbols:
            symbol = sym_data.get('symbol', '') if isinstance(sym_data, dict) else ''
            if symbol and _TICKER_RE.match(symbol):
                # Optional: filter by watcher count
                watchers = sym_data.get('watchlist_count', 0) if isinstance(sym_data, dict) else 0
                if watchers >= _MIN_STOCKTWITS_WATCHERS or watchers == 0:
                    result.add(symbol)

        return result

    # ── Benzinga Movers ─────────────────────────────────────────────────────

    def _discover_from_benzinga_movers(self) -> Set[str]:
        """
        Fetch top movers (gainers/losers/most-active) from Benzinga.
        """
        if not self._benzinga_key:
            return set()

        result = set()
        for screener_type in ['gainers', 'losers', 'most_active']:
            try:
                url = f'https://api.benzinga.com/api/v1/market/movers'
                params = {
                    'token': self._benzinga_key,
                    'session': 'REGULAR',
                    'from': screener_type,
                    'limit': 20,
                }
                resp = self._session.get(url, params=params, timeout=self._timeout)
                if resp.status_code != 200:
                    continue
                data = resp.json()

                movers = data if isinstance(data, list) else data.get(screener_type, [])
                if not isinstance(movers, list):
                    continue

                for mover in movers:
                    symbol = mover.get('ticker', mover.get('symbol', ''))
                    if symbol and _TICKER_RE.match(symbol):
                        result.add(symbol)
            except Exception:
                continue

        return result

    # ── Validation ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_valid_ticker(symbol: str) -> bool:
        """Validate a discovered ticker symbol."""
        if not symbol or not _TICKER_RE.match(symbol):
            return False
        if symbol in _FALSE_TICKERS:
            return False
        if len(symbol) < 2:
            return False
        return True
