import logging
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from vwap_utils import compute_vwap

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)

TRADIER_BASE_URL = 'https://api.tradier.com/v1'
_MAX_WORKERS = 20   # concurrent per-ticker fetches


class TradierDataClient:
    """
    Fetches market data from the Tradier API.
    Order execution remains on Alpaca — only the data source changes here.
    """

    def __init__(self, token):
        self._token = token
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json',
        })

    # ------------------------------------------------------------------
    # Low-level Tradier REST helpers
    # ------------------------------------------------------------------

    def _get(self, path, params=None):
        resp = self._session.get(
            f'{TRADIER_BASE_URL}{path}', params=params, timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    def _fetch_quotes(self, symbols):
        """
        Batch quote fetch (one API call for up to ~400 symbols).

        Returns
        -------
        dict : {symbol -> quote_dict}
            Keys of interest: last, bid, ask, volume, change_percentage
        """
        if not symbols:
            return {}
        data = self._get(
            '/markets/quotes',
            {'symbols': ','.join(symbols), 'greeks': 'false'},
        )
        raw = data.get('quotes', {}).get('quote', [])
        if isinstance(raw, dict):   # single symbol → Tradier returns dict not list
            raw = [raw]
        return {q['symbol']: q for q in raw if isinstance(q, dict)}

    def _fetch_timesales(self, symbol, start, end, interval='1min'):
        """
        Intraday bars for one symbol.

        Returns
        -------
        pd.DataFrame  columns: open, high, low, close, volume  (empty on error)
        """
        try:
            data = self._get('/markets/timesales', {
                'symbol':         symbol,
                'interval':       interval,
                'start':          start.strftime('%Y-%m-%dT%H:%M:%S'),
                'end':            end.strftime('%Y-%m-%dT%H:%M:%S'),
                'session_filter': 'open',
            })
            series = data.get('series')
            if not series:
                return pd.DataFrame()
            rows = series.get('data', [])
            if isinstance(rows, dict):  # single bar
                rows = [rows]
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            df['time'] = pd.to_datetime(df['time']).dt.tz_localize(ET)
            return df.set_index('time')[['open', 'high', 'low', 'close', 'volume']].astype(float)
        except Exception as e:
            log.debug(f"Timesales error for {symbol}: {e}")
            return pd.DataFrame()

    def _fetch_daily_history(self, symbol, start, end):
        """
        Daily OHLCV bars for a symbol (used for RVOL baseline).

        Returns
        -------
        pd.DataFrame  columns: open, high, low, close, volume  (empty on error)
        """
        try:
            data = self._get('/markets/history', {
                'symbol':   symbol,
                'interval': 'daily',
                'start':    start.strftime('%Y-%m-%d'),
                'end':      end.strftime('%Y-%m-%d'),
            })
            history = data.get('history')
            if not history:
                return pd.DataFrame()
            days = history.get('day', [])
            if isinstance(days, dict):
                days = [days]
            if not days:
                return pd.DataFrame()
            df = pd.DataFrame(days)
            df['date'] = pd.to_datetime(df['date']).dt.tz_localize(ET)
            return df.set_index('date')[['open', 'high', 'low', 'close', 'volume']].astype(float)
        except Exception as e:
            log.debug(f"History error for {symbol}: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # Public interface (same as the old AlpacaDataClient)
    # ------------------------------------------------------------------

    def fetch_batch_bars(self, tickers):
        """
        Fetch today's 1-minute bars and RVOL daily history for all tickers.

        Returns
        -------
        (bars_cache, rvol_cache)
          bars_cache : {ticker -> DataFrame of today's 1-min bars}
          rvol_cache : {ticker -> DataFrame of last-10-day daily bars}
        """
        now = datetime.now(ET)
        today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)

        if now < today_open:
            log.info(
                f"[{now.strftime('%H:%M:%S')} ET] Pre-market — "
                "waiting for 9:30 AM ET."
            )
            return {}, {}

        rvol_start = (now - timedelta(days=14)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        bars_cache = {}
        rvol_cache = {}

        def _fetch_one(ticker):
            bars = self._fetch_timesales(ticker, today_open, now)
            hist  = self._fetch_daily_history(ticker, rvol_start, now)
            return ticker, bars, hist

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            futures = {ex.submit(_fetch_one, t): t for t in tickers}
            for f in as_completed(futures):
                try:
                    ticker, bars, hist = f.result()
                    if not bars.empty:
                        bars_cache[ticker] = bars
                    if not hist.empty:
                        rvol_cache[ticker] = hist
                except Exception as e:
                    log.error(f"Fetch error for {futures[f]}: {e}")

        log.info(
            f"Tradier: fetched bars for {len(bars_cache)}/{len(tickers)} tickers."
        )
        return bars_cache, rvol_cache

    def get_bars(self, ticker, bars_cache, rvol_cache, calendar_days=2):
        """Return cached bars, falling back to an individual fetch."""
        cache = rvol_cache if calendar_days > 2 else bars_cache
        cached = cache.get(ticker) if cache else None
        if cached is not None and not cached.empty:
            return cached

        now = datetime.now(ET)
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        return self._fetch_timesales(ticker, start, now)

    def check_spread(self, ticker):
        """
        Return (spread_pct, ask_price) via a real-time Level 1 quote.
        Returns (None, None) on error or missing data.
        """
        try:
            quotes = self._fetch_quotes([ticker])
            q = quotes.get(ticker, {})
            bid = float(q.get('bid') or 0)
            ask = float(q.get('ask') or 0)
            if bid <= 0 or ask <= 0:
                return None, None
            mid = (bid + ask) / 2
            return (ask - bid) / mid, ask
        except Exception as e:
            log.error(f"Quote error for {ticker}: {e}")
            return None, None

    def get_spy_vwap_bias(self, bars_cache):
        """Return True if SPY is above its cumulative intraday VWAP."""
        try:
            spy = bars_cache.get('SPY')
            if spy is None or spy.empty:
                now = datetime.now(ET)
                today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
                spy = self._fetch_timesales('SPY', today_open, now)
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
