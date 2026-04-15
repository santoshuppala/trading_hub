"""
Alpha Vantage data source — technicals, fundamentals, economic indicators.

Requires: ALPHA_VANTAGE_KEY (free at https://www.alphavantage.co/support/#api-key)
Rate limit: 25 requests/day (free tier) — cache aggressively.

Provides:
  - Technical indicators (SMA, EMA, RSI from a second source)
  - Company fundamentals (earnings, overview)
  - Economic indicators (as backup to FRED)
  - Cross-validation of our internal calculations

Usage:
    from data_sources.alpha_vantage import AlphaVantageSource
    av = AlphaVantageSource(api_key='your_key')
    sma = av.get_sma('AAPL', period=20)
"""
import logging
import os
import time
from typing import Dict, List, Optional
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

_CACHE_TTL = 86400  # 24 hours (25 req/day limit — cache everything for a day)
_BASE_URL = 'https://www.alphavantage.co/query'


@dataclass
class TechnicalSnapshot:
    """Technical indicator values from Alpha Vantage."""
    ticker: str
    sma_20: float
    sma_50: float
    sma_200: float
    ema_9: float
    ema_21: float
    rsi_14: float
    price_vs_sma200: str   # 'above', 'below' — long-term trend


@dataclass
class CompanyOverview:
    """Company fundamental data."""
    ticker: str
    name: str
    sector: str
    industry: str
    market_cap: float
    pe_ratio: float
    forward_pe: float
    peg_ratio: float
    dividend_yield: float
    eps: float
    revenue_growth_yoy: float
    profit_margin: float
    beta: float
    analyst_target: float
    week_52_high: float
    week_52_low: float


class AlphaVantageSource:
    """Alpha Vantage API data source."""

    def __init__(self, api_key: str = None):
        self._api_key = api_key or os.getenv('ALPHA_VANTAGE_KEY', '')
        self._session = requests.Session()
        self._cache: Dict[str, tuple] = {}
        self._daily_requests = 0
        self._daily_limit = 25

        if not self._api_key:
            log.info("[AlphaVantage] No API key — data unavailable. "
                     "Get free key: https://www.alphavantage.co/support/#api-key")

    def _fetch(self, params: dict) -> Optional[dict]:
        """Make an API call with rate limiting and caching."""
        if not self._api_key:
            return None

        if self._daily_requests >= self._daily_limit:
            log.warning("[AlphaVantage] Daily limit reached (%d/%d)",
                        self._daily_requests, self._daily_limit)
            return None

        cache_key = str(sorted(params.items()))
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        params['apikey'] = self._api_key

        try:
            resp = self._session.get(_BASE_URL, params=params, timeout=15)
            self._daily_requests += 1

            if resp.status_code != 200:
                log.warning("[AlphaVantage] HTTP %d", resp.status_code)
                return None

            data = resp.json()

            # Check for error messages
            if 'Error Message' in data:
                log.warning("[AlphaVantage] Error: %s", data['Error Message'])
                return None
            if 'Note' in data:
                log.warning("[AlphaVantage] Rate limited: %s", data['Note'][:100])
                return None

            self._cache[cache_key] = (time.time(), data)
            return data

        except Exception as exc:
            log.warning("[AlphaVantage] Fetch failed: %s", exc)
            return None

    def get_sma(self, symbol: str, period: int = 20) -> Optional[float]:
        """Get current Simple Moving Average."""
        data = self._fetch({
            'function': 'SMA',
            'symbol': symbol,
            'interval': 'daily',
            'time_period': str(period),
            'series_type': 'close',
        })
        if data:
            analysis = data.get('Technical Analysis: SMA', {})
            if analysis:
                latest = next(iter(analysis.values()), {})
                return float(latest.get('SMA', 0))
        return None

    def get_ema(self, symbol: str, period: int = 9) -> Optional[float]:
        """Get current Exponential Moving Average."""
        data = self._fetch({
            'function': 'EMA',
            'symbol': symbol,
            'interval': 'daily',
            'time_period': str(period),
            'series_type': 'close',
        })
        if data:
            analysis = data.get('Technical Analysis: EMA', {})
            if analysis:
                latest = next(iter(analysis.values()), {})
                return float(latest.get('EMA', 0))
        return None

    def get_rsi(self, symbol: str, period: int = 14) -> Optional[float]:
        """Get current RSI."""
        data = self._fetch({
            'function': 'RSI',
            'symbol': symbol,
            'interval': 'daily',
            'time_period': str(period),
            'series_type': 'close',
        })
        if data:
            analysis = data.get('Technical Analysis: RSI', {})
            if analysis:
                latest = next(iter(analysis.values()), {})
                return float(latest.get('RSI', 0))
        return None

    def company_overview(self, symbol: str) -> Optional[CompanyOverview]:
        """Get company fundamental overview."""
        data = self._fetch({'function': 'OVERVIEW', 'symbol': symbol})
        if not data or 'Symbol' not in data:
            return None

        def _f(key, default=0.0):
            val = data.get(key, default)
            try:
                return float(val) if val and val != 'None' and val != '-' else default
            except (ValueError, TypeError):
                return default

        return CompanyOverview(
            ticker=symbol,
            name=data.get('Name', symbol),
            sector=data.get('Sector', 'Unknown'),
            industry=data.get('Industry', 'Unknown'),
            market_cap=_f('MarketCapitalization'),
            pe_ratio=_f('TrailingPE'),
            forward_pe=_f('ForwardPE'),
            peg_ratio=_f('PEGRatio'),
            dividend_yield=_f('DividendYield'),
            eps=_f('EPS'),
            revenue_growth_yoy=_f('QuarterlyRevenueGrowthYOY'),
            profit_margin=_f('ProfitMargin'),
            beta=_f('Beta', 1.0),
            analyst_target=_f('AnalystTargetPrice'),
            week_52_high=_f('52WeekHigh'),
            week_52_low=_f('52WeekLow'),
        )

    def technical_snapshot(self, symbol: str) -> Optional[TechnicalSnapshot]:
        """Get a snapshot of key technical indicators.

        WARNING: Uses 6 API calls (SMA20, SMA50, SMA200, EMA9, EMA21, RSI14).
        With 25/day limit, only call this for ~4 tickers per day.
        """
        sma_20 = self.get_sma(symbol, 20) or 0
        sma_50 = self.get_sma(symbol, 50) or 0
        sma_200 = self.get_sma(symbol, 200) or 0
        ema_9 = self.get_ema(symbol, 9) or 0
        ema_21 = self.get_ema(symbol, 21) or 0
        rsi_14 = self.get_rsi(symbol, 14) or 50

        return TechnicalSnapshot(
            ticker=symbol,
            sma_20=round(sma_20, 2),
            sma_50=round(sma_50, 2),
            sma_200=round(sma_200, 2),
            ema_9=round(ema_9, 2),
            ema_21=round(ema_21, 2),
            rsi_14=round(rsi_14, 2),
            price_vs_sma200='above' if sma_200 > 0 and ema_9 > sma_200 else 'below',
        )

    def requests_remaining(self) -> int:
        """How many API calls left today."""
        return max(0, self._daily_limit - self._daily_requests)
