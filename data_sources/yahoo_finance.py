"""
Yahoo Finance data source — earnings dates, analyst ratings, fundamentals.

No API key required. Uses yfinance library.

Provides:
  - Earnings calendar (next earnings date for any ticker)
  - Analyst consensus (buy/hold/sell counts, target price)
  - Key fundamentals (P/E, market cap, short interest, float)
  - Ex-dividend dates

Usage:
    from data_sources.yahoo_finance import YahooFinanceSource
    yf_source = YahooFinanceSource()
    earnings = yf_source.next_earnings('AAPL')
    consensus = yf_source.analyst_consensus('AAPL')
"""
import logging
import time
from datetime import datetime, date
from typing import Dict, Optional
from dataclasses import dataclass

log = logging.getLogger(__name__)

_CACHE_TTL = 3600  # 1 hour cache (fundamentals don't change intraday)


@dataclass
class EarningsInfo:
    ticker: str
    next_earnings_date: Optional[str]  # ISO date or None
    days_until_earnings: Optional[int]
    earnings_before_open: Optional[bool]  # True=BMO, False=AMC, None=unknown


@dataclass
class AnalystConsensus:
    ticker: str
    target_mean: float      # mean price target
    target_low: float
    target_high: float
    buy_count: int
    hold_count: int
    sell_count: int
    recommendation: str     # 'buy', 'hold', 'sell', 'strong_buy'
    upside_pct: float       # (target_mean - current) / current


@dataclass
class Fundamentals:
    ticker: str
    market_cap: float
    pe_ratio: float
    forward_pe: float
    short_pct_float: float  # short interest as % of float
    float_shares: float
    avg_volume_10d: int
    beta: float
    dividend_yield: float
    sector: str
    industry: str


class YahooFinanceSource:
    """Yahoo Finance data source — no API key required."""

    def __init__(self):
        self._cache: Dict[str, tuple] = {}  # {key: (timestamp, data)}

    def _get_ticker(self, symbol: str):
        """Get yfinance Ticker object with caching."""
        cache_key = f'ticker_{symbol}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            self._cache[cache_key] = (time.time(), ticker)
            return ticker
        except ImportError:
            log.warning("[Yahoo] yfinance not installed. Run: pip install yfinance")
            return None
        except Exception as exc:
            log.warning("[Yahoo] Failed to get ticker %s: %s", symbol, exc)
            return None

    def next_earnings(self, symbol: str) -> Optional[EarningsInfo]:
        """Get next earnings date for a ticker."""
        cache_key = f'earnings_{symbol}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        try:
            ticker = self._get_ticker(symbol)
            if ticker is None:
                return None

            cal = ticker.calendar
            if cal is None or (hasattr(cal, 'empty') and cal.empty):
                result = EarningsInfo(ticker=symbol, next_earnings_date=None,
                                     days_until_earnings=None, earnings_before_open=None)
                self._cache[cache_key] = (time.time(), result)
                return result

            # Extract earnings date
            earnings_date = None
            if isinstance(cal, dict):
                ed = cal.get('Earnings Date')
                if ed and isinstance(ed, list) and len(ed) > 0:
                    earnings_date = ed[0]
                elif ed:
                    earnings_date = ed
            elif hasattr(cal, 'iloc'):
                try:
                    earnings_date = cal.iloc[0, 0] if len(cal) > 0 else None
                except Exception:
                    pass

            if earnings_date is not None:
                if hasattr(earnings_date, 'date'):
                    ed_date = earnings_date.date() if callable(getattr(earnings_date, 'date', None)) else earnings_date
                elif isinstance(earnings_date, str):
                    ed_date = datetime.fromisoformat(earnings_date).date()
                else:
                    ed_date = None

                if ed_date:
                    days = (ed_date - date.today()).days
                    result = EarningsInfo(
                        ticker=symbol,
                        next_earnings_date=str(ed_date),
                        days_until_earnings=days,
                        earnings_before_open=None,
                    )
                    self._cache[cache_key] = (time.time(), result)
                    return result

            result = EarningsInfo(ticker=symbol, next_earnings_date=None,
                                 days_until_earnings=None, earnings_before_open=None)
            self._cache[cache_key] = (time.time(), result)
            return result

        except Exception as exc:
            log.debug("[Yahoo] Earnings fetch failed for %s: %s", symbol, exc)
            return None

    def analyst_consensus(self, symbol: str) -> Optional[AnalystConsensus]:
        """Get analyst ratings and price targets."""
        cache_key = f'analyst_{symbol}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        try:
            ticker = self._get_ticker(symbol)
            if ticker is None:
                return None

            info = ticker.info or {}
            current_price = info.get('currentPrice') or info.get('regularMarketPrice', 0)
            target_mean = info.get('targetMeanPrice', 0) or 0
            target_low = info.get('targetLowPrice', 0) or 0
            target_high = info.get('targetHighPrice', 0) or 0

            rec = info.get('recommendationKey', 'none') or 'none'
            buy = info.get('numberOfAnalystOpinions', 0) or 0

            upside = ((target_mean - current_price) / current_price * 100) if current_price > 0 and target_mean > 0 else 0

            result = AnalystConsensus(
                ticker=symbol,
                target_mean=round(target_mean, 2),
                target_low=round(target_low, 2),
                target_high=round(target_high, 2),
                buy_count=buy,
                hold_count=0,
                sell_count=0,
                recommendation=rec,
                upside_pct=round(upside, 2),
            )
            self._cache[cache_key] = (time.time(), result)
            return result

        except Exception as exc:
            log.debug("[Yahoo] Analyst fetch failed for %s: %s", symbol, exc)
            return None

    def fundamentals(self, symbol: str) -> Optional[Fundamentals]:
        """Get key fundamental data."""
        cache_key = f'fund_{symbol}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        try:
            ticker = self._get_ticker(symbol)
            if ticker is None:
                return None

            info = ticker.info or {}

            result = Fundamentals(
                ticker=symbol,
                market_cap=float(info.get('marketCap', 0) or 0),
                pe_ratio=float(info.get('trailingPE', 0) or 0),
                forward_pe=float(info.get('forwardPE', 0) or 0),
                short_pct_float=float(info.get('shortPercentOfFloat', 0) or 0),
                float_shares=float(info.get('floatShares', 0) or 0),
                avg_volume_10d=int(info.get('averageDailyVolume10Day', 0) or 0),
                beta=float(info.get('beta', 1.0) or 1.0),
                dividend_yield=float(info.get('dividendYield', 0) or 0),
                sector=info.get('sector', 'Unknown') or 'Unknown',
                industry=info.get('industry', 'Unknown') or 'Unknown',
            )
            self._cache[cache_key] = (time.time(), result)
            return result

        except Exception as exc:
            log.debug("[Yahoo] Fundamentals fetch failed for %s: %s", symbol, exc)
            return None

    def batch_earnings(self, symbols: list) -> Dict[str, EarningsInfo]:
        """Fetch earnings dates for multiple tickers."""
        results = {}
        for symbol in symbols:
            info = self.next_earnings(symbol)
            if info:
                results[symbol] = info
        return results
