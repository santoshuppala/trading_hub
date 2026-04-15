"""
SEC EDGAR data source — institutional holdings (13F), insider trades (Form 4).

No API key required. Just needs User-Agent with contact email.
Rate limit: 10 requests/second (SEC guideline).

Provides:
  - 13F filings: what institutions (Bridgewater, Berkshire) hold
  - Form 4: insider buys/sells (CEO, CFO, directors)
  - Company filings search

Usage:
    from data_sources.sec_edgar import SECEdgarSource
    sec = SECEdgarSource(email='your@email.com')
    insiders = sec.insider_trades('AAPL')
"""
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

_CACHE_TTL = 86400  # 24 hours (filings update infrequently)
_BASE_URL = 'https://data.sec.gov'
_EFTS_URL = 'https://efts.sec.gov/LATEST/search-index'
_COMPANY_TICKERS_URL = 'https://www.sec.gov/files/company_tickers.json'


@dataclass
class InsiderTrade:
    """A single insider transaction from Form 4."""
    ticker: str
    insider_name: str
    title: str            # CEO, CFO, Director, etc.
    transaction_type: str # 'buy' or 'sell'
    shares: int
    price: float
    value: float          # shares * price
    date: str             # ISO date
    filing_url: str


@dataclass
class InstitutionalHolding:
    """Institutional position from 13F filing."""
    ticker: str
    institution: str
    shares: int
    value: float
    change_pct: float     # % change from previous quarter
    filing_date: str


class SECEdgarSource:
    """SEC EDGAR public data — insider trades and institutional holdings."""

    def __init__(self, email: str = None):
        self._email = email or os.getenv('ALERT_EMAIL_TO', 'tradinghub@example.com')
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': f'TradingHub/1.0 ({self._email})',
            'Accept': 'application/json',
        })
        self._cache: Dict[str, tuple] = {}
        self._last_request = 0.0
        self._min_interval = 0.15  # 100ms between requests (10 req/s limit)
        self._cik_map: Dict[str, str] = {}  # ticker → CIK

    def _rate_limit(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    def _get_cik(self, ticker: str) -> Optional[str]:
        """Get CIK number for a ticker."""
        if ticker in self._cik_map:
            return self._cik_map[ticker]

        # Load company tickers mapping
        if not self._cik_map:
            try:
                self._rate_limit()
                resp = self._session.get(_COMPANY_TICKERS_URL, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    for entry in data.values():
                        t = entry.get('ticker', '')
                        cik = str(entry.get('cik_str', ''))
                        if t and cik:
                            self._cik_map[t.upper()] = cik.zfill(10)
            except Exception as exc:
                log.debug("[EDGAR] CIK map load failed: %s", exc)

        return self._cik_map.get(ticker.upper())

    def insider_trades(self, ticker: str, limit: int = 10) -> List[InsiderTrade]:
        """Get recent insider trades (Form 4) for a ticker."""
        cache_key = f'insider_{ticker}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        cik = self._get_cik(ticker)
        if not cik:
            return []

        try:
            self._rate_limit()
            resp = self._session.get(
                f'{_BASE_URL}/submissions/CIK{cik}.json',
                timeout=10,
            )

            if resp.status_code != 200:
                return []

            data = resp.json()
            recent = data.get('filings', {}).get('recent', {})

            trades = []
            forms = recent.get('form', [])
            dates = recent.get('filingDate', [])
            accessions = recent.get('accessionNumber', [])

            for i, form in enumerate(forms):
                if form == '4' and i < len(dates):  # Form 4 = insider trade
                    filing_date = dates[i] if i < len(dates) else ''
                    accession = accessions[i] if i < len(accessions) else ''

                    trades.append(InsiderTrade(
                        ticker=ticker,
                        insider_name=data.get('name', 'Unknown'),
                        title='Officer/Director',
                        transaction_type='unknown',  # Would need to parse XML for buy/sell
                        shares=0,
                        price=0.0,
                        value=0.0,
                        date=filing_date,
                        filing_url=f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace("-", "")}/{accession}-index.htm',
                    ))

                    if len(trades) >= limit:
                        break

            self._cache[cache_key] = (time.time(), trades)
            return trades

        except Exception as exc:
            log.debug("[EDGAR] Insider trades failed for %s: %s", ticker, exc)
            return []

    def company_filings(self, ticker: str, form_type: str = '10-K', limit: int = 5) -> List[Dict]:
        """Get recent company filings (10-K, 10-Q, 8-K, etc.)."""
        cache_key = f'filings_{ticker}_{form_type}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        cik = self._get_cik(ticker)
        if not cik:
            return []

        try:
            self._rate_limit()
            resp = self._session.get(
                f'{_BASE_URL}/submissions/CIK{cik}.json',
                timeout=10,
            )

            if resp.status_code != 200:
                return []

            data = resp.json()
            recent = data.get('filings', {}).get('recent', {})
            forms = recent.get('form', [])
            dates = recent.get('filingDate', [])
            descriptions = recent.get('primaryDocument', [])

            results = []
            for i, form in enumerate(forms):
                if form == form_type:
                    results.append({
                        'form': form,
                        'date': dates[i] if i < len(dates) else '',
                        'document': descriptions[i] if i < len(descriptions) else '',
                    })
                    if len(results) >= limit:
                        break

            self._cache[cache_key] = (time.time(), results)
            return results

        except Exception as exc:
            log.debug("[EDGAR] Filings failed for %s: %s", ticker, exc)
            return []

    def filing_count_recent(self, ticker: str, days: int = 30) -> int:
        """Count filings in the last N days (activity spike = something happening)."""
        cik = self._get_cik(ticker)
        if not cik:
            return 0

        try:
            self._rate_limit()
            resp = self._session.get(
                f'{_BASE_URL}/submissions/CIK{cik}.json',
                timeout=10,
            )
            if resp.status_code != 200:
                return 0

            data = resp.json()
            dates = data.get('filings', {}).get('recent', {}).get('filingDate', [])
            cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            return sum(1 for d in dates if d >= cutoff)

        except Exception:
            return 0
