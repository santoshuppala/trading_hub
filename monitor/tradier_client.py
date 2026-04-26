import logging
import threading
import time
import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from vwap_utils import compute_vwap
from .data_client import BaseDataClient

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)

TRADIER_BASE_URL = 'https://api.tradier.com/v1'
_MAX_WORKERS = 40


class _TokenBucket:
    """Simple thread-safe token bucket rate limiter.

    Tradier limit: 120 requests/minute. We use 100 to leave headroom.
    """

    def __init__(self, rate: float = 100.0, capacity: float = 100.0):
        self._rate = rate          # tokens per second
        self._capacity = capacity  # max burst
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 10.0) -> bool:
        """Block until a token is available. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity,
                                   self._tokens + (now - self._last) * (self._rate / 60.0))
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)


_tradier_bucket = _TokenBucket(rate=100, capacity=100)


class TradierDataClient(BaseDataClient):
    """
    Market data via the Tradier REST API.
    Bearer-token auth; no SDK dependency.
    """

    def __init__(self, token):
        self._token = token
        self._session = requests.Session()
        # V8: Raise connection pool size to match _MAX_WORKERS (40) so parallel
        # fetch_batch_bars threads don't discard connections under load.
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=_MAX_WORKERS,
            pool_maxsize=_MAX_WORKERS,
        )
        self._session.mount('https://', adapter)
        self._session.headers.update({
            'Authorization': f'Bearer {token}',
            'Accept': 'application/json',
        })
        # V8: Reuse ThreadPoolExecutor across calls (avoids thread creation overhead)
        self._executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS)

    # ------------------------------------------------------------------
    # Low-level REST helpers (private)
    # ------------------------------------------------------------------

    def _get(self, path, params=None):
        # V10: Proactive rate limiting — block before sending, not after 429
        _tradier_bucket.acquire(timeout=10.0)
        # V8: Retry on 5xx errors (had 26 x 502 today) + 429 rate limit + 401 circuit breaker
        for attempt in range(3):
            try:
                resp = self._session.get(
                    f'{TRADIER_BASE_URL}{path}', params=params, timeout=15
                )
            except Exception as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise

            # V10: 401 circuit breaker — stop all fetches + alert on token expiry
            if resp.status_code == 401:
                self._consecutive_401s = getattr(self, '_consecutive_401s', 0) + 1
                if not getattr(self, '_auth_alert_sent', False):
                    log.critical("Tradier API 401 — token expired! All data fetches will fail.")
                    self._auth_alert_sent = True
                    try:
                        from monitor.alerts import send_alert
                        send_alert(None,
                                   f"TRADIER TOKEN EXPIRED: API returning 401. "
                                   f"Data pipeline and order execution are DOWN. "
                                   f"Rotate token and restart immediately.",
                                   severity='CRITICAL')
                    except Exception:
                        pass
                resp.raise_for_status()

            # Retryable status codes (429 rate limit + 5xx server errors)
            if resp.status_code in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt, 8)
                log.warning(f"Tradier {resp.status_code} on {path}; retrying in {wait}s "
                            f"(attempt {attempt + 1}/3)")
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def _fetch_timesales(self, symbol, start, end, interval='1min'):
        """Intraday 1-min bars for one symbol."""
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
            if isinstance(rows, dict):
                rows = [rows]
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows)
            # V8: Validate required columns exist before selecting
            required_cols = ['time', 'open', 'high', 'low', 'close', 'volume']
            missing = [c for c in required_cols if c not in df.columns]
            if missing:
                log.warning("Timesales for %s missing columns: %s (have: %s)",
                            symbol, missing, list(df.columns))
                return pd.DataFrame()
            df['time'] = pd.to_datetime(df['time']).dt.tz_localize(ET)
            return df.set_index('time')[['open', 'high', 'low', 'close', 'volume']].astype(float)
        except Exception as e:
            log.debug(f"Timesales error for {symbol}: {e}")
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # BaseDataClient public interface
    # ------------------------------------------------------------------

    def get_daily_history(self, symbol, start, end):
        """Daily OHLCV bars (used for RVOL baseline and RS filter)."""
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

    def get_quotes(self, symbols):
        """
        Batch quote fetch — one API call for up to ~400 symbols.

        Returns {symbol -> {'volume', 'change_percentage', 'last', 'bid', 'ask'}}
        """
        if not symbols:
            return {}
        try:
            data = self._get(
                '/markets/quotes',
                {'symbols': ','.join(symbols), 'greeks': 'false'},
            )
            raw = data.get('quotes', {}).get('quote', [])
            if isinstance(raw, dict):
                raw = [raw]
            return {q['symbol']: q for q in raw if isinstance(q, dict)}
        except Exception as e:
            log.error(f"Quotes error: {e}")
            return {}

    def fetch_batch_bars(self, tickers):
        """
        Parallel fetch: today's 1-min bars + 14-day daily history for all tickers.
        rvol_cache contains daily bars (time-fraction RVOL in signals.py).
        """
        now = datetime.now(ET)
        today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)

        if now < today_open:
            log.info(f"[{now.strftime('%H:%M:%S')} ET] Pre-market — waiting for 9:30 AM ET.")
            return {}, {}

        rvol_start = (now - timedelta(days=14)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

        bars_cache = {}
        rvol_cache = {}

        def _fetch_one(ticker):
            bars = self._fetch_timesales(ticker, today_open, now)
            hist = self.get_daily_history(ticker, rvol_start, now)
            return ticker, bars, hist

        # V8: Reuse persistent executor instead of creating a new one per call
        # V9 (R5): Hard 20s timeout on entire batch to prevent pool deadlock
        import concurrent.futures as _cf
        futures = {self._executor.submit(_fetch_one, t): t for t in tickers}
        done, not_done = _cf.wait(futures, timeout=20)
        if not_done:
            for f in not_done:
                f.cancel()
            log.warning("[TradierClient] %d/%d tickers timed out in fetch_batch_bars "
                        "(20s hard limit)", len(not_done), len(tickers))
        for f in done:
            try:
                ticker, bars, hist = f.result(timeout=1)
                if not bars.empty:
                    bars_cache[ticker] = bars
                if not hist.empty:
                    rvol_cache[ticker] = hist
            except Exception as e:
                log.error(f"Fetch error for {futures[f]}: {e}")

        log.info(f"Tradier: fetched bars for {len(bars_cache)}/{len(tickers)} tickers.")
        return bars_cache, rvol_cache

    def get_bars(self, ticker, bars_cache, rvol_cache, calendar_days=2):
        """Return cached bars, falling back to a live fetch."""
        cache = rvol_cache if calendar_days > 2 else bars_cache
        cached = cache.get(ticker) if cache else None
        if cached is not None and not cached.empty:
            return cached
        now = datetime.now(ET)
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        return self._fetch_timesales(ticker, start, now)

    def check_spread(self, ticker):
        """Return (spread_pct, ask_price) via a real-time Level 1 quote."""
        try:
            quotes = self.get_quotes([ticker])
            q = quotes.get(ticker, {})
            bid = float(q.get('bid') or 0)
            ask = float(q.get('ask') or 0)
            if bid <= 0 or ask <= 0:
                return None, None
            mid = (bid + ask) / 2
            return (ask - bid) / mid, ask
        except Exception as e:
            log.error(f"Spread check error for {ticker}: {e}")
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
