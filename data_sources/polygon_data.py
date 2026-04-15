"""
Polygon.io data source — delayed quotes, historical bars, reference data.

Requires: POLYGON_API_KEY (free at https://polygon.io/dashboard/signup)
Rate limit: 5 requests/minute (free tier)

Provides:
  - Previous day OHLCV (cross-reference with Tradier)
  - Ticker details (market cap, SIC code, locale)
  - Stock splits and dividends
  - Grouped daily bars (all tickers in one call — efficient)

Usage:
    from data_sources.polygon_data import PolygonSource
    poly = PolygonSource(api_key='your_key')
    prev = poly.previous_close('AAPL')
"""
import logging
import os
import time
from typing import Dict, Optional, List
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

_CACHE_TTL = 3600  # 1 hour
_BASE_URL = 'https://api.polygon.io'


@dataclass
class PreviousClose:
    ticker: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float
    change_pct: float
    date: str


@dataclass
class TickerDetails:
    ticker: str
    name: str
    market_cap: float
    sic_code: str
    sic_description: str
    total_employees: int
    list_date: str
    share_class_shares_outstanding: int


class PolygonSource:
    """Polygon.io REST API data source."""

    def __init__(self, api_key: str = None):
        self._api_key = api_key or os.getenv('POLYGON_API_KEY', '')
        self._session = requests.Session()
        self._cache: Dict[str, tuple] = {}
        self._request_times: List[float] = []
        self._rate_limit_per_min = 5

        if not self._api_key:
            log.info("[Polygon] No API key — data unavailable. "
                     "Get free key: https://polygon.io/dashboard/signup")

    def _rate_limit(self):
        """Enforce 5 req/min for free tier."""
        now = time.monotonic()
        self._request_times = [t for t in self._request_times if now - t < 60]
        if len(self._request_times) >= self._rate_limit_per_min:
            wait = 60 - (now - self._request_times[0])
            if wait > 0:
                log.debug("[Polygon] Rate limited, waiting %.1fs", wait)
                time.sleep(wait)
        self._request_times.append(time.monotonic())

    def _fetch(self, path: str, params: dict = None) -> Optional[dict]:
        if not self._api_key:
            return None

        cache_key = f'{path}_{params}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        self._rate_limit()
        try:
            p = params or {}
            p['apiKey'] = self._api_key
            resp = self._session.get(f'{_BASE_URL}{path}', params=p, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                self._cache[cache_key] = (time.time(), data)
                return data
            elif resp.status_code == 429:
                log.warning("[Polygon] Rate limited (429)")
            else:
                log.debug("[Polygon] HTTP %d for %s", resp.status_code, path)
        except Exception as exc:
            log.debug("[Polygon] Fetch failed: %s", exc)
        return None

    def previous_close(self, ticker: str) -> Optional[PreviousClose]:
        """Get previous day's OHLCV."""
        data = self._fetch(f'/v2/aggs/ticker/{ticker}/prev')
        if not data:
            return None

        results = data.get('results', [])
        if not results:
            return None

        r = results[0]
        o = float(r.get('o', 0))
        c = float(r.get('c', 0))
        change = ((c - o) / o * 100) if o > 0 else 0

        return PreviousClose(
            ticker=ticker,
            open=o,
            high=float(r.get('h', 0)),
            low=float(r.get('l', 0)),
            close=c,
            volume=int(r.get('v', 0)),
            vwap=float(r.get('vw', 0)),
            change_pct=round(change, 2),
            date=str(r.get('t', '')),
        )

    def ticker_details(self, ticker: str) -> Optional[TickerDetails]:
        """Get company details (market cap, employees, SIC code)."""
        data = self._fetch(f'/v3/reference/tickers/{ticker}')
        if not data:
            return None

        r = data.get('results', {})
        return TickerDetails(
            ticker=ticker,
            name=r.get('name', ticker),
            market_cap=float(r.get('market_cap', 0) or 0),
            sic_code=r.get('sic_code', ''),
            sic_description=r.get('sic_description', ''),
            total_employees=int(r.get('total_employees', 0) or 0),
            list_date=r.get('list_date', ''),
            share_class_shares_outstanding=int(r.get('share_class_shares_outstanding', 0) or 0),
        )

    def market_snapshot(self) -> List[Dict]:
        """Get grouped daily bars for all tickers (one API call).

        Useful for finding movers without per-ticker calls.
        Returns list of {ticker, change_pct, volume, vwap}.
        """
        data = self._fetch('/v2/snapshot/locale/us/markets/stocks/tickers',
                           params={'include_otc': 'false'})
        if not data:
            return []

        results = []
        for t in data.get('tickers', []):
            ticker_name = t.get('ticker', '')
            day = t.get('day', {})
            prev = t.get('prevDay', {})

            prev_close = float(prev.get('c', 0))
            curr_close = float(day.get('c', 0))
            change = ((curr_close - prev_close) / prev_close * 100) if prev_close > 0 else 0

            results.append({
                'ticker': ticker_name,
                'price': curr_close,
                'change_pct': round(change, 2),
                'volume': int(day.get('v', 0)),
                'vwap': float(day.get('vw', 0)),
            })

        return results
