"""
Finviz data source — insider trading, institutional holdings, screener.

No API key required. Scrapes public Finviz pages.
Rate limit: ~1 request per second (be respectful).

Provides:
  - Insider trading activity (buys/sells in last 3 months)
  - Screener data (technical + fundamental filters)
  - Short interest data

Usage:
    from data_sources.finviz_data import FinvizSource
    fv = FinvizSource()
    insider = fv.insider_activity('AAPL')
"""
import logging
import re
import time
from typing import Dict, List, Optional
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_CACHE_TTL = 3600  # 1 hour
_BASE_URL = 'https://finviz.com'


@dataclass
class InsiderActivity:
    ticker: str
    net_insider_buys: int      # buys - sells in last 3 months
    total_insider_buys: int
    total_insider_sells: int
    largest_buy: float         # dollar value
    largest_sell: float
    insider_signal: str        # 'bullish' (net buying), 'bearish' (net selling), 'neutral'


@dataclass
class ScreenerData:
    ticker: str
    price: float
    change_pct: float
    volume: int
    relative_volume: float
    short_float: float        # short interest as % of float
    analyst_rating: str       # 'Buy', 'Hold', 'Sell', etc.
    target_price: float
    pe_ratio: float
    market_cap: str           # '10.5B', '500M', etc.


class FinvizSource:
    """Finviz scraper for insider activity and screener data."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36',
        })
        self._cache: Dict[str, tuple] = {}
        self._last_request = 0.0
        self._min_interval = 1.0  # 1 second between requests

    def _rate_limit(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    def insider_activity(self, symbol: str) -> Optional[InsiderActivity]:
        """Get insider trading activity for a ticker."""
        cache_key = f'insider_{symbol}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        try:
            self._rate_limit()
            resp = self._session.get(
                f'{_BASE_URL}/quote.ashx',
                params={'t': symbol, 'ty': 'c', 'ta': '0', 'p': 'd'},
                timeout=10,
            )

            if resp.status_code != 200:
                log.debug("[Finviz] HTTP %d for %s", resp.status_code, symbol)
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')

            # Parse insider trading table
            buys = 0
            sells = 0
            largest_buy = 0.0
            largest_sell = 0.0

            insider_table = soup.find('table', class_='body-table')
            if insider_table:
                rows = insider_table.find_all('tr')[1:]  # skip header
                for row in rows[:20]:  # last 20 transactions
                    cols = row.find_all('td')
                    if len(cols) >= 5:
                        trans_type = cols[3].text.strip().lower()
                        try:
                            value_text = cols[4].text.strip().replace(',', '').replace('$', '')
                            value = float(value_text) if value_text else 0
                        except ValueError:
                            value = 0

                        if 'buy' in trans_type or 'purchase' in trans_type:
                            buys += 1
                            largest_buy = max(largest_buy, value)
                        elif 'sale' in trans_type or 'sell' in trans_type:
                            sells += 1
                            largest_sell = max(largest_sell, value)

            net = buys - sells
            if net > 0:
                signal = 'bullish'
            elif net < 0:
                signal = 'bearish'
            else:
                signal = 'neutral'

            result = InsiderActivity(
                ticker=symbol,
                net_insider_buys=net,
                total_insider_buys=buys,
                total_insider_sells=sells,
                largest_buy=largest_buy,
                largest_sell=largest_sell,
                insider_signal=signal,
            )
            self._cache[cache_key] = (time.time(), result)
            return result

        except Exception as exc:
            log.debug("[Finviz] Insider fetch failed for %s: %s", symbol, exc)
            return None

    def screener_data(self, symbol: str) -> Optional[ScreenerData]:
        """Get screener overview data for a ticker."""
        cache_key = f'screener_{symbol}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        try:
            self._rate_limit()
            resp = self._session.get(
                f'{_BASE_URL}/quote.ashx',
                params={'t': symbol},
                timeout=10,
            )

            if resp.status_code != 200:
                return None

            soup = BeautifulSoup(resp.text, 'html.parser')

            # Parse the snapshot table
            data = {}
            snapshot_table = soup.find('table', class_='snapshot-table2')
            if snapshot_table:
                cells = snapshot_table.find_all('td')
                for i in range(0, len(cells) - 1, 2):
                    key = cells[i].text.strip()
                    val = cells[i + 1].text.strip()
                    data[key] = val

            def _parse_float(s, default=0.0):
                try:
                    return float(s.replace('%', '').replace(',', ''))
                except (ValueError, AttributeError):
                    return default

            result = ScreenerData(
                ticker=symbol,
                price=_parse_float(data.get('Price', '0')),
                change_pct=_parse_float(data.get('Change', '0')),
                volume=int(_parse_float(data.get('Volume', '0'))),
                relative_volume=_parse_float(data.get('Rel Volume', '1')),
                short_float=_parse_float(data.get('Short Float', '0')),
                analyst_rating=data.get('Recom', 'N/A'),
                target_price=_parse_float(data.get('Target Price', '0')),
                pe_ratio=_parse_float(data.get('P/E', '0')),
                market_cap=data.get('Market Cap', 'N/A'),
            )
            self._cache[cache_key] = (time.time(), result)
            return result

        except Exception as exc:
            log.debug("[Finviz] Screener fetch failed for %s: %s", symbol, exc)
            return None

    def batch_insider(self, symbols: list, max_per_call: int = 10) -> Dict[str, InsiderActivity]:
        """Batch fetch insider data (rate-limited)."""
        results = {}
        for symbol in symbols[:max_per_call]:
            data = self.insider_activity(symbol)
            if data:
                results[symbol] = data
        return results
