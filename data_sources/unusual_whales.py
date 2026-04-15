"""
Unusual Whales — options flow and dark pool data.

Free tier provides limited public data. No API key for basic scraping.
Premium API ($39/mo) provides real-time flow.

For free tier: scrape public endpoints for aggregate flow data.

Provides:
  - Net options flow (bullish vs bearish premium)
  - Dark pool activity indicators
  - Most active options contracts

Usage:
    from data_sources.unusual_whales import UnusualWhalesSource
    uw = UnusualWhalesSource()
    flow = uw.get_flow_summary('AAPL')
"""
import logging
import os
import time
from typing import Dict, Optional
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

_CACHE_TTL = 600  # 10 minutes


@dataclass
class OptionsFlowSummary:
    """Aggregate options flow for a ticker."""
    ticker: str
    call_premium: float      # total call premium traded ($)
    put_premium: float       # total put premium traded ($)
    call_volume: int
    put_volume: int
    net_flow: float          # call_premium - put_premium (positive = bullish)
    put_call_ratio: float
    flow_signal: str         # 'bullish', 'bearish', 'neutral'
    unusual_count: int       # number of unusual trades flagged
    source: str


class UnusualWhalesSource:
    """Unusual Whales public data scraper."""

    def __init__(self, api_key: str = None):
        self._api_key = api_key or os.getenv('UNUSUAL_WHALES_KEY', '')
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'Mozilla/5.0 TradingHub/1.0',
            'Accept': 'application/json',
        })
        self._cache: Dict[str, tuple] = {}
        self._last_request = 0.0
        self._min_interval = 2.0  # conservative rate limiting

        if self._api_key:
            self._session.headers['Authorization'] = f'Bearer {self._api_key}'
            log.info("[UnusualWhales] API key configured")
        else:
            log.info("[UnusualWhales] No API key — using public endpoints only")

    def _rate_limit(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    def get_flow_summary(self, ticker: str) -> Optional[OptionsFlowSummary]:
        """Get aggregate options flow for a ticker.

        Without API key: returns None (public endpoints are limited).
        With API key: fetches from Unusual Whales API.
        """
        cache_key = f'flow_{ticker}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        if self._api_key:
            return self._fetch_api_flow(ticker)

        # No API key — try public endpoint (may not work)
        return self._fetch_public_flow(ticker)

    def _fetch_api_flow(self, ticker: str) -> Optional[OptionsFlowSummary]:
        """Fetch from Unusual Whales API (paid tier)."""
        try:
            self._rate_limit()
            resp = self._session.get(
                f'https://api.unusualwhales.com/api/stock/{ticker}/options-flow',
                timeout=10,
            )
            if resp.status_code != 200:
                return None

            data = resp.json().get('data', [])
            if not data:
                return None

            call_premium = 0.0
            put_premium = 0.0
            call_volume = 0
            put_volume = 0
            unusual = 0

            for trade in data:
                premium = float(trade.get('premium', 0) or 0)
                vol = int(trade.get('volume', 0) or 0)
                opt_type = trade.get('put_call', '').upper()
                is_unusual = trade.get('is_unusual', False)

                if opt_type == 'C':
                    call_premium += premium
                    call_volume += vol
                elif opt_type == 'P':
                    put_premium += premium
                    put_volume += vol

                if is_unusual:
                    unusual += 1

            net = call_premium - put_premium
            pcr = put_volume / call_volume if call_volume > 0 else 1.0

            if net > 0 and pcr < 0.7:
                signal = 'bullish'
            elif net < 0 and pcr > 1.3:
                signal = 'bearish'
            else:
                signal = 'neutral'

            result = OptionsFlowSummary(
                ticker=ticker,
                call_premium=round(call_premium, 2),
                put_premium=round(put_premium, 2),
                call_volume=call_volume,
                put_volume=put_volume,
                net_flow=round(net, 2),
                put_call_ratio=round(pcr, 3),
                flow_signal=signal,
                unusual_count=unusual,
                source='unusual_whales_api',
            )
            self._cache[f'flow_{ticker}'] = (time.time(), result)
            return result

        except Exception as exc:
            log.debug("[UnusualWhales] API fetch failed for %s: %s", ticker, exc)
            return None

    def _fetch_public_flow(self, ticker: str) -> Optional[OptionsFlowSummary]:
        """Try to get flow from public page (limited, may not work)."""
        # Public endpoints are very limited without API key
        # Return None — user needs to sign up for free tier
        log.debug("[UnusualWhales] No API key — cannot fetch flow for %s", ticker)
        return None

    def market_flow_summary(self) -> Optional[Dict]:
        """Get overall market options flow summary.

        Only works with API key.
        """
        if not self._api_key:
            return None

        cache_key = 'market_flow'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        try:
            self._rate_limit()
            resp = self._session.get(
                'https://api.unusualwhales.com/api/market/flow-summary',
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._cache[cache_key] = (time.time(), data)
                return data
        except Exception as exc:
            log.debug("[UnusualWhales] Market flow failed: %s", exc)
        return None
