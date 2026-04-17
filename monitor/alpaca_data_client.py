import logging
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from vwap_utils import compute_vwap
from .data_client import BaseDataClient

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)

_MAX_WORKERS = 10


class AlpacaDataClient(BaseDataClient):
    """
    Market data via the Alpaca Data API (alpaca-py SDK).
    Uses the same API key/secret as the trading client — no separate credentials needed.
    rvol_cache contains daily bars, consistent with TradierDataClient.
    """

    def __init__(self, api_key, api_secret):
        from alpaca.data.historical import StockHistoricalDataClient
        self._client = StockHistoricalDataClient(api_key, api_secret)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _bars_to_df(self, bar_list):
        """Convert a list of alpaca Bar objects to a OHLCV DataFrame in ET."""
        if not bar_list:
            return pd.DataFrame()
        rows = [
            {
                'time':   b.timestamp,
                'open':   float(b.open),
                'high':   float(b.high),
                'low':    float(b.low),
                'close':  float(b.close),
                'volume': float(b.volume),
            }
            for b in bar_list
        ]
        df = pd.DataFrame(rows).set_index('time')
        df.index = df.index.tz_convert(ET)
        return df

    def _fetch_minute_bars(self, tickers, start, end):
        """Batch minute bars for multiple tickers → {ticker: DataFrame}.

        V8: Splits into chunks of 200 symbols (Alpaca limit) and retries
        each chunk up to 3 times with exponential backoff.
        """
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        import time as _time

        _CHUNK_SIZE = 200
        ticker_list = list(tickers)
        out = {}

        for i in range(0, len(ticker_list), _CHUNK_SIZE):
            chunk = ticker_list[i:i + _CHUNK_SIZE]
            for attempt in range(3):
                try:
                    req = StockBarsRequest(
                        symbol_or_symbols=chunk,
                        timeframe=TimeFrame.Minute,
                        start=start,
                        end=end,
                        feed='iex',
                    )
                    result = self._client.get_stock_bars(req)
                    for sym, bars in (result.data or {}).items():
                        df = self._bars_to_df(bars)
                        if not df.empty:
                            out[sym] = df
                    break  # success
                except Exception as e:
                    if attempt < 2:
                        wait = 2 ** attempt
                        log.warning("Alpaca minute bars chunk %d/%d error (attempt %d/3): %s — retrying in %ds",
                                    i // _CHUNK_SIZE + 1, (len(ticker_list) + _CHUNK_SIZE - 1) // _CHUNK_SIZE,
                                    attempt + 1, e, wait)
                        _time.sleep(wait)
                    else:
                        log.error("Alpaca minute bars chunk failed after 3 attempts: %s", e)

        return out

    def _fetch_daily_bars_batch(self, tickers, start, end):
        """Batch daily bars for multiple tickers → {ticker: DataFrame}.

        V8: Splits into chunks of 200 symbols and retries each chunk
        up to 3 times with exponential backoff.
        """
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        import time as _time

        _CHUNK_SIZE = 200
        ticker_list = list(tickers)
        out = {}

        for i in range(0, len(ticker_list), _CHUNK_SIZE):
            chunk = ticker_list[i:i + _CHUNK_SIZE]
            for attempt in range(3):
                try:
                    req = StockBarsRequest(
                        symbol_or_symbols=chunk,
                        timeframe=TimeFrame.Day,
                        start=start,
                        end=end,
                    )
                    result = self._client.get_stock_bars(req)
                    for sym, bars in (result.data or {}).items():
                        df = self._bars_to_df(bars)
                        if not df.empty:
                            out[sym] = df
                    break  # success
                except Exception as e:
                    if attempt < 2:
                        wait = 2 ** attempt
                        log.warning("Alpaca daily bars chunk %d error (attempt %d/3): %s — retrying in %ds",
                                    i // _CHUNK_SIZE + 1, attempt + 1, e, wait)
                        _time.sleep(wait)
                    else:
                        log.error("Alpaca daily bars chunk failed after 3 attempts: %s", e)

        return out

    # ------------------------------------------------------------------
    # BaseDataClient public interface
    # ------------------------------------------------------------------

    def get_daily_history(self, symbol, start, end):
        """Daily OHLCV bars for one symbol (used for RVOL baseline and RS filter)."""
        result = self._fetch_daily_bars_batch([symbol], start, end)
        return result.get(symbol, pd.DataFrame())

    def get_quotes(self, symbols):
        """
        Batch snapshot — one API call for all symbols.
        Returns {symbol -> {'volume', 'change_percentage', 'last', 'bid', 'ask'}}
        """
        from alpaca.data.requests import StockSnapshotRequest
        if not symbols:
            return {}
        try:
            req = StockSnapshotRequest(symbol_or_symbols=list(symbols))
            snapshots = self._client.get_stock_snapshot(req)
            out = {}
            for sym, snap in (snapshots or {}).items():
                db = snap.daily_bar
                lq = snap.latest_quote
                if db is None:
                    continue
                vol = float(db.volume or 0)
                chg = ((db.close - db.open) / db.open * 100) if db.open else 0.0
                out[sym] = {
                    'volume':            vol,
                    'change_percentage': chg,
                    'last':              float(db.close or 0),
                    'bid':               float(lq.bid_price or 0) if lq else 0.0,
                    'ask':               float(lq.ask_price or 0) if lq else 0.0,
                }
            return out
        except Exception as e:
            log.error(f"Alpaca snapshot error: {e}")
            return {}

    def fetch_batch_bars(self, tickers):
        """
        Single batch call each for minute bars (today) and daily bars (14d).
        Alpaca supports multi-symbol requests natively — no per-ticker threading needed.
        """
        now = datetime.now(ET)
        today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)

        if now < today_open:
            log.info(f"[{now.strftime('%H:%M:%S')} ET] Pre-market — waiting for 9:30 AM ET.")
            return {}, {}

        rvol_start = now - timedelta(days=14)

        log.info(f"Alpaca: fetching bars for {len(tickers)} tickers...")
        bars_cache = self._fetch_minute_bars(tickers, today_open, now)
        rvol_cache = self._fetch_daily_bars_batch(tickers, rvol_start, now)

        log.info(f"Alpaca: fetched bars for {len(bars_cache)}/{len(tickers)} tickers.")
        return bars_cache, rvol_cache

    def get_bars(self, ticker, bars_cache, rvol_cache, calendar_days=2):
        """Return cached bars, falling back to a live fetch."""
        cache = rvol_cache if calendar_days > 2 else bars_cache
        cached = cache.get(ticker) if cache else None
        if cached is not None and not cached.empty:
            return cached
        now = datetime.now(ET)
        today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        result = self._fetch_minute_bars([ticker], today_open, now)
        return result.get(ticker, pd.DataFrame())

    def check_spread(self, ticker):
        """Return (spread_pct, ask_price) via a real-time Level 1 quote."""
        from alpaca.data.requests import StockLatestQuoteRequest
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=[ticker])
            quotes = self._client.get_stock_latest_quote(req)
            q = quotes.get(ticker)
            if q is None:
                return None, None
            bid = float(q.bid_price or 0)
            ask = float(q.ask_price or 0)
            if bid <= 0 or ask <= 0:
                return None, None
            mid = (bid + ask) / 2
            return (ask - bid) / mid, ask
        except Exception as e:
            log.error(f"Alpaca quote error for {ticker}: {e}")
            return None, None

    def get_spy_vwap_bias(self, bars_cache):
        """Return True if SPY is above its cumulative intraday VWAP."""
        try:
            spy = bars_cache.get('SPY')
            if spy is None or spy.empty:
                now = datetime.now(ET)
                today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
                result = self._fetch_minute_bars(['SPY'], today_open, now)
                spy = result.get('SPY', pd.DataFrame())
            if spy is None or spy.empty:
                return True
            today = datetime.now(ET).date()
            spy = spy[spy.index.date == today]
            if spy.empty:
                return True
            vwap = compute_vwap(spy['high'], spy['low'], spy['close'], spy['volume'])
            return float(spy['close'].iloc[-1]) > float(vwap.iloc[-1])
        except Exception:
            return True
