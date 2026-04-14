"""
Earnings calendar — fetch, cache, and safety-check earnings dates.

Selling premium through earnings is one of the fastest ways to blow up
an account.  This module provides the guardrails:

  * NEVER sell premium (credit strategies) within 7 days of earnings.
  * Debit strategies OK within 3 days (buying gamma before earnings).
  * Post-earnings IV crush: flag tickers where earnings just happened
    so the options engine can opportunistically sell premium.

Data sources (tried in order):
  1. yfinance  — free, no auth
  2. Alpaca corporate-actions endpoint — fallback
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import yfinance as yf

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_CACHE_TTL_SECS = 24 * 60 * 60  # 24 hours


class EarningsCalendar:
    """Thread-safe earnings-date cache with safety helpers for the options engine.

    Usage::

        cal = EarningsCalendar()
        cal.refresh(['AAPL', 'NVDA', 'TSLA'])
        if cal.is_earnings_safe('AAPL', min_days=7):
            # OK to sell premium
            ...
    """

    def __init__(self, cache_ttl: int = _CACHE_TTL_SECS) -> None:
        self._cache_ttl = cache_ttl
        self._lock = threading.Lock()

        # ticker -> (fetch_timestamp, next_earnings_date, last_earnings_date)
        self._cache: Dict[str, Tuple[float, Optional[date], Optional[date]]] = {}

        # Alpaca base URL (paper or live) — only used as fallback
        self._alpaca_base = os.getenv(
            "APCA_OPTIONS_BASE_URL",
            os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets"),
        )
        self._alpaca_key = os.getenv("APCA_OPTIONS_KEY", os.getenv("APCA_API_KEY_ID"))
        self._alpaca_secret = os.getenv(
            "APCA_OPTIONS_SECRET", os.getenv("APCA_API_SECRET_KEY")
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def days_to_earnings(self, ticker: str) -> Optional[int]:
        """Calendar days until next earnings, or *None* if unknown."""
        self._ensure_cached(ticker)
        entry = self._cache.get(ticker)
        if entry is None:
            return None
        _, next_dt, _ = entry
        if next_dt is None:
            return None
        delta = (next_dt - date.today()).days
        return delta if delta >= 0 else None

    def is_earnings_safe(self, ticker: str, min_days: int = 7) -> bool:
        """True when earnings are more than *min_days* away **or** unknown.

        Unknown → safe (we don't block trades just because data is
        unavailable; the risk gate already caps exposure).
        """
        days = self.days_to_earnings(ticker)
        if days is None:
            return True
        return days > min_days

    def is_post_earnings(self, ticker: str, within_days: int = 3) -> bool:
        """True if earnings happened within the last *within_days* calendar days.

        Useful for spotting IV-crush opportunities — selling premium
        right after the event when implied vol collapses.
        """
        self._ensure_cached(ticker)
        entry = self._cache.get(ticker)
        if entry is None:
            return False
        _, _, last_dt = entry
        if last_dt is None:
            return False
        days_since = (date.today() - last_dt).days
        return 0 <= days_since <= within_days

    def get_earnings_date(self, ticker: str) -> Optional[str]:
        """ISO-8601 date string of next earnings, or *None*."""
        self._ensure_cached(ticker)
        entry = self._cache.get(ticker)
        if entry is None:
            return None
        _, next_dt, _ = entry
        return next_dt.isoformat() if next_dt else None

    def refresh(self, tickers: List[str]) -> None:
        """Batch-fetch earnings dates for *tickers* (always hits the source)."""
        for t in tickers:
            self.refresh_single(t)

    def refresh_single(self, ticker: str) -> None:
        """Fetch earnings dates for a single ticker and update the cache."""
        ticker = ticker.upper()
        next_dt, last_dt = self._fetch(ticker)
        with self._lock:
            self._cache[ticker] = (time.time(), next_dt, last_dt)
        if next_dt:
            log.info("earnings | %s next=%s", ticker, next_dt.isoformat())
        else:
            log.debug("earnings | %s next=unknown", ticker)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_cached(self, ticker: str) -> None:
        """Populate cache entry if missing or stale."""
        ticker = ticker.upper()
        with self._lock:
            entry = self._cache.get(ticker)
        if entry is not None:
            ts, _, _ = entry
            if time.time() - ts < self._cache_ttl:
                return  # still fresh
        self.refresh_single(ticker)

    def _fetch(self, ticker: str) -> Tuple[Optional[date], Optional[date]]:
        """Try yfinance first, then Alpaca.  Returns (next_date, last_date)."""
        next_dt, last_dt = self._fetch_earnings_yfinance(ticker)
        if next_dt is not None:
            return next_dt, last_dt

        # Fallback to Alpaca
        next_dt_alpaca = self._fetch_earnings_alpaca(ticker)
        if next_dt_alpaca is not None:
            return next_dt_alpaca, last_dt  # keep yfinance last_dt if we got one

        return None, last_dt

    # ---- yfinance ----

    def _fetch_earnings_yfinance(
        self, ticker: str
    ) -> Tuple[Optional[date], Optional[date]]:
        """Return (next_earnings, last_earnings) via yfinance."""
        try:
            t = yf.Ticker(ticker)
            cal = t.calendar
            if cal is None:
                return None, None

            # yfinance may return a DataFrame or a dict depending on version.
            # Normalise to a list of dates.
            earnings_dates = self._extract_dates_from_calendar(cal)
            if not earnings_dates:
                return None, None

            today = date.today()
            next_dt: Optional[date] = None
            last_dt: Optional[date] = None

            for d in sorted(earnings_dates):
                d_date = d if isinstance(d, date) else d.date()
                if d_date >= today:
                    if next_dt is None:
                        next_dt = d_date
                else:
                    last_dt = d_date  # keep overwriting → most recent past

            return next_dt, last_dt

        except Exception as exc:
            log.debug("yfinance earnings fetch failed for %s: %s", ticker, exc)
            return None, None

    @staticmethod
    def _extract_dates_from_calendar(cal) -> list:
        """Handle the multiple formats yfinance returns for .calendar."""
        import pandas as pd

        # DataFrame with 'Earnings Date' column (list of Timestamps)
        if isinstance(cal, pd.DataFrame):
            if cal.empty:
                return []
            earnings = cal.get("Earnings Date")
            if earnings is not None:
                return list(earnings)
            # Some yfinance versions put dates as index with row labels
            if "Earnings Date" in cal.index:
                val = cal.loc["Earnings Date"]
                if isinstance(val, pd.Series):
                    return list(val)
                return [val]
            return []

        # dict {'Earnings Date': [...], ...}
        if isinstance(cal, dict):
            raw = cal.get("Earnings Date") or cal.get("Earnings Dates") or []
            return list(raw) if not isinstance(raw, list) else raw

        return []

    # ---- Alpaca fallback ----

    def _fetch_earnings_alpaca(self, ticker: str) -> Optional[date]:
        """Best-effort earnings lookup via Alpaca corporate-actions API."""
        if not self._alpaca_key or not self._alpaca_secret:
            return None
        try:
            import requests

            url = f"{self._alpaca_base}/v2/corporate_actions/announcements"
            today_str = date.today().isoformat()
            future_str = (date.today() + timedelta(days=90)).isoformat()
            params = {
                "ca_types": "Earnings",
                "since": today_str,
                "until": future_str,
                "symbol": ticker,
            }
            headers = {
                "APCA-API-KEY-ID": self._alpaca_key,
                "APCA-API-SECRET-KEY": self._alpaca_secret,
            }
            resp = requests.get(url, params=params, headers=headers, timeout=5)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data:
                return None
            # Take the earliest future date
            dates = []
            for item in data:
                raw = item.get("date") or item.get("ex_date")
                if raw:
                    dates.append(date.fromisoformat(raw))
            return min(dates) if dates else None
        except Exception as exc:
            log.debug("Alpaca earnings fetch failed for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    # Convenience for the options engine
    # ------------------------------------------------------------------

    def credit_strategy_allowed(self, ticker: str, min_days: int = 7) -> bool:
        """Should we allow **selling premium** on this ticker right now?

        Returns False if earnings are within *min_days* (default 7).
        """
        return self.is_earnings_safe(ticker, min_days=min_days)

    def debit_strategy_allowed(self, ticker: str, min_days: int = 3) -> bool:
        """Debit (long gamma) plays can get closer to earnings."""
        return self.is_earnings_safe(ticker, min_days=min_days)

    def iv_crush_opportunity(self, ticker: str, within_days: int = 3) -> bool:
        """True when earnings just passed — premium selling window."""
        return self.is_post_earnings(ticker, within_days=within_days)
