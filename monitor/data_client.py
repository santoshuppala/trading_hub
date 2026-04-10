import logging
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from vwap_utils import compute_vwap

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)


class AlpacaDataClient:
    """Wraps Alpaca StockHistoricalDataClient with convenience methods."""

    def __init__(self, api_key, api_secret):
        self._client = StockHistoricalDataClient(api_key, api_secret)

    def fetch_batch_bars(self, tickers):
        """
        Fetch today's 1-minute bars and historical RVOL bars for all tickers
        in a single Alpaca API call each.

        Returns:
            (bars_cache, rvol_cache) — dicts mapping ticker -> DataFrame.
            Both dicts are empty if now < 9:30 AM ET (pre-market guard).
        """
        now = datetime.now(ET)
        today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)

        if now < today_open:
            log.info(f"[{now.strftime('%H:%M:%S')} ET] Pre-market — bars not yet available, waiting for 9:30 AM ET.")
            return {}, {}

        rvol_start = (now - timedelta(days=10)).replace(hour=9, minute=30, second=0, microsecond=0)

        def _parse_multi(raw_df, tickers_list):
            result = {}
            if raw_df.empty:
                return result
            for tkr in tickers_list:
                try:
                    df = (
                        raw_df.xs(tkr, level='symbol').copy()
                        if isinstance(raw_df.index, pd.MultiIndex)
                        else raw_df.copy()
                    )
                    df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
                    result[tkr] = df
                except Exception:
                    pass
            return result

        bars_cache = {}
        rvol_cache = {}

        # Today's bars (for analysis)
        try:
            req = StockBarsRequest(
                symbol_or_symbols=tickers,
                timeframe=TimeFrame.Minute,
                start=today_open,
                feed='iex',
            )
            bars_cache = _parse_multi(self._client.get_stock_bars(req).df, tickers)
        except Exception as e:
            log.error(f"Batch bars error: {e}")

        # Historical bars for RVOL (last 10 calendar days)
        try:
            req = StockBarsRequest(
                symbol_or_symbols=tickers,
                timeframe=TimeFrame.Minute,
                start=rvol_start,
                feed='iex',
            )
            rvol_cache = _parse_multi(self._client.get_stock_bars(req).df, tickers)
        except Exception as e:
            log.error(f"RVOL batch bars error: {e}")

        return bars_cache, rvol_cache

    def get_bars(self, ticker, bars_cache, rvol_cache, calendar_days=2):
        """
        Return cached bars for ticker, falling back to an individual fetch if not cached.

        Uses rvol_cache for calendar_days > 2, bars_cache otherwise.
        """
        cache = rvol_cache if calendar_days > 2 else bars_cache
        cached = cache.get(ticker) if cache else None
        if cached is not None and not cached.empty:
            return cached

        # Fallback: individual fetch
        try:
            now = datetime.now(ET)
            start = (now - timedelta(days=calendar_days)).replace(
                hour=9, minute=30, second=0, microsecond=0
            )
            req = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=TimeFrame.Minute,
                start=start,
                feed='iex',
            )
            bars = self._client.get_stock_bars(req)
            df = bars.df
            if df.empty:
                return pd.DataFrame()
            if isinstance(df.index, pd.MultiIndex):
                df = (
                    df.xs(ticker, level='symbol')
                    if ticker in df.index.get_level_values('symbol')
                    else pd.DataFrame()
                )
            df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
            return df
        except Exception as e:
            log.error(f"Alpaca data error for {ticker}: {e}")
            return pd.DataFrame()

    def check_spread(self, ticker):
        """
        Fetch Level 1 quote and return (spread_pct, ask_price).
        Returns (None, None) on error or invalid quote.
        """
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
            quote = self._client.get_stock_latest_quote(req)[ticker]
            bid = float(quote.bid_price)
            ask = float(quote.ask_price)
            if bid <= 0 or ask <= 0:
                return None, None
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid
            return spread_pct, ask
        except Exception as e:
            log.error(f"Quote error for {ticker}: {e}")
            return None, None

    def get_spy_vwap_bias(self, bars_cache):
        """
        Returns True if SPY is above its cumulative VWAP (market is bullish).
        Falls back to True if data is unavailable.
        """
        try:
            spy = bars_cache.get('SPY')
            if spy is None or spy.empty:
                # Attempt individual fetch
                now = datetime.now(ET)
                today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
                req = StockBarsRequest(
                    symbol_or_symbols='SPY',
                    timeframe=TimeFrame.Minute,
                    start=today_open,
                    feed='iex',
                )
                raw = self._client.get_stock_bars(req).df
                if raw.empty:
                    return True
                if isinstance(raw.index, pd.MultiIndex):
                    raw = raw.xs('SPY', level='symbol')
                raw.index = pd.to_datetime(raw.index, utc=True).tz_convert(ET)
                spy = raw

            today = datetime.now(ET).date()
            spy = spy[spy.index.date == today]
            if spy.empty:
                return True
            vwap = compute_vwap(spy['high'], spy['low'], spy['close'], spy['volume'])
            return float(spy['close'].iloc[-1]) > float(vwap.iloc[-1])
        except Exception:
            return True
