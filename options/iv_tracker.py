"""
IV Tracker — Historical implied volatility tracking with IV Rank and IV Percentile.

Tracks a rolling 252-day (1 trading year) window of daily IV readings per ticker
and computes IV rank / IV percentile for options strategy selection.

Usage:
    from options.iv_tracker import IVTracker

    tracker = IVTracker()
    tracker.update('AAPL', 0.32)
    rank = tracker.iv_rank('AAPL')       # 0-100
    pct  = tracker.iv_percentile('AAPL') # 0-100
"""

import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, date

logger = logging.getLogger(__name__)

_DEFAULT_LOOKBACK = 252          # 1 trading year
_MIN_HISTORY_HARD = 2            # absolute minimum (need 2 points for a range)
_MIN_HISTORY_FULL = 20           # full confidence — no blending after this
_DEFAULT_SAVE_PATH = "data/iv_history.json"
_AUTO_SAVE_INTERVAL = 3600       # seconds (1 hour)


class IVTracker:
    """Track historical IV and compute IV rank / IV percentile per ticker."""

    def __init__(self, lookback: int = _DEFAULT_LOOKBACK, save_path: str = _DEFAULT_SAVE_PATH):
        self._lookback = lookback
        self._save_path = save_path
        self._lock = threading.Lock()

        # ticker -> list of {"date": "YYYY-MM-DD", "iv": float}  (sorted by date ascending)
        self._history: dict[str, list[dict]] = defaultdict(list)

        # ticker -> latest date string already stored (fast dedup for intraday calls)
        self._last_date: dict[str, str] = {}

        # auto-save machinery
        self._last_save_ts: float = 0.0
        self._auto_save_enabled = True

    # ------------------------------------------------------------------
    # Core update
    # ------------------------------------------------------------------

    def update(self, ticker: str, current_iv: float) -> None:
        """Record an IV reading. Only one reading per calendar day is stored."""
        today_str = date.today().isoformat()

        with self._lock:
            # Deduplicate: one entry per day per ticker
            if self._last_date.get(ticker) == today_str:
                # Update the IV value for today (take the latest reading)
                if self._history[ticker] and self._history[ticker][-1]["date"] == today_str:
                    self._history[ticker][-1]["iv"] = current_iv
                return

            self._history[ticker].append({"date": today_str, "iv": current_iv})
            self._last_date[ticker] = today_str

            # Trim to rolling window
            if len(self._history[ticker]) > self._lookback:
                self._history[ticker] = self._history[ticker][-self._lookback:]

        # Auto-save check (outside lock to avoid holding it during I/O)
        self._maybe_auto_save()

    # ------------------------------------------------------------------
    # IV Rank
    # ------------------------------------------------------------------

    def iv_rank(self, ticker: str) -> float:
        """
        IV Rank (0-100), blended toward neutral (50) when history is thin.

        Formula: (current_iv - min_iv) / (max_iv - min_iv) * 100
        Confidence scales linearly from 0 at 2 days to 1.0 at 20 days.
        Returns 50.0 if < 2 data points or zero range.
        """
        with self._lock:
            entries = self._history.get(ticker)
            if not entries or len(entries) < _MIN_HISTORY_HARD:
                return 50.0

            ivs = [e["iv"] for e in entries]
            current = ivs[-1]
            min_iv = min(ivs)
            max_iv = max(ivs)
            num_days = len(entries)

        if max_iv == min_iv:
            return 50.0

        raw_rank = (current - min_iv) / (max_iv - min_iv) * 100.0
        raw_rank = max(0.0, min(100.0, raw_rank))

        # Blend toward neutral based on data confidence
        confidence = min(1.0, (num_days - _MIN_HISTORY_HARD + 1) / _MIN_HISTORY_FULL)
        blended = confidence * raw_rank + (1.0 - confidence) * 50.0
        return round(blended, 2)

    # ------------------------------------------------------------------
    # IV Percentile
    # ------------------------------------------------------------------

    def iv_percentile(self, ticker: str) -> float:
        """
        IV Percentile (0-100), blended toward neutral when history is thin.

        Formula: % of days in lookback where IV was LOWER than current IV.
        Returns 50.0 if < 2 data points.
        """
        with self._lock:
            entries = self._history.get(ticker)
            if not entries or len(entries) < _MIN_HISTORY_HARD:
                return 50.0

            ivs = [e["iv"] for e in entries]
            current = ivs[-1]
            num_days = len(entries)

        days_lower = sum(1 for iv in ivs if iv < current)
        raw_pct = days_lower / len(ivs) * 100.0
        raw_pct = max(0.0, min(100.0, raw_pct))

        confidence = min(1.0, (num_days - _MIN_HISTORY_HARD + 1) / _MIN_HISTORY_FULL)
        blended = confidence * raw_pct + (1.0 - confidence) * 50.0
        return round(blended, 2)

    # ------------------------------------------------------------------
    # Convenience flags
    # ------------------------------------------------------------------

    def history_days(self, ticker: str) -> int:
        """Number of IV data points stored for *ticker*."""
        with self._lock:
            entries = self._history.get(ticker)
            return len(entries) if entries else 0

    def is_iv_high(self, ticker: str) -> bool:
        """IV rank > 50 — favorable for selling premium."""
        return self.iv_rank(ticker) > 50.0

    def is_iv_low(self, ticker: str) -> bool:
        """IV rank < 30 — favorable for buying premium."""
        return self.iv_rank(ticker) < 30.0

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self, ticker: str) -> dict:
        """Return a summary dict of IV statistics for *ticker*."""
        with self._lock:
            entries = self._history.get(ticker)
            if not entries:
                return {
                    "current_iv": None,
                    "iv_rank": 50.0,
                    "iv_percentile": 50.0,
                    "min_iv": None,
                    "max_iv": None,
                    "mean_iv": None,
                    "lookback_days": 0,
                }

            ivs = [e["iv"] for e in entries]
            current = ivs[-1]
            min_iv = min(ivs)
            max_iv = max(ivs)
            mean_iv = sum(ivs) / len(ivs)
            lookback_days = len(ivs)

        return {
            "current_iv": round(current, 6),
            "iv_rank": self.iv_rank(ticker),
            "iv_percentile": self.iv_percentile(ticker),
            "min_iv": round(min_iv, 6),
            "max_iv": round(max_iv, 6),
            "mean_iv": round(mean_iv, 6),
            "lookback_days": lookback_days,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_to_file(self, path: str | None = None) -> None:
        """Serialize IV history to a JSON file."""
        path = path or self._save_path
        with self._lock:
            data = dict(self._history)

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, path)
            logger.info("IVTracker: saved %d tickers to %s", len(data), path)
        except OSError:
            logger.exception("IVTracker: failed to save to %s", path)

    def load_from_file(self, path: str | None = None) -> None:
        """Load IV history from a JSON file."""
        path = path or self._save_path
        if not os.path.exists(path):
            logger.info("IVTracker: no history file at %s, starting fresh", path)
            return

        try:
            with open(path, "r") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            logger.exception("IVTracker: failed to load %s", path)
            return

        with self._lock:
            for ticker, entries in raw.items():
                # Keep only the most recent `lookback` entries
                trimmed = entries[-self._lookback:]
                self._history[ticker] = trimmed
                if trimmed:
                    self._last_date[ticker] = trimmed[-1]["date"]

        logger.info("IVTracker: loaded %d tickers from %s", len(raw), path)

    # ------------------------------------------------------------------
    # Pre-seeding from option chain
    # ------------------------------------------------------------------

    def seed_from_chain(self, ticker: str, chain_client) -> None:
        """
        Bootstrap IV history from a chain client's term structure when we
        have no prior data for *ticker*.

        Expects *chain_client* to expose:
            chain_client.get_chain(ticker) -> list of dicts with at least
                {"expiration": "YYYY-MM-DD", "iv": float}

        Near-term vs far-term IV gives a rough read on the current regime.
        We store a single synthetic data point so subsequent rank/percentile
        calls return 50.0 (neutral) until real history accumulates.
        """
        with self._lock:
            if self._history.get(ticker):
                return  # already have history, skip seeding

        try:
            chain = chain_client.get_chain(ticker)
        except Exception:
            logger.exception("IVTracker: seed_from_chain failed for %s", ticker)
            return

        if not chain:
            return

        # Pull IV values from the chain, sorted by expiration
        try:
            sorted_chain = sorted(chain, key=lambda c: c["expiration"])
            ivs = [c["iv"] for c in sorted_chain if c.get("iv") is not None]
        except (KeyError, TypeError):
            logger.warning("IVTracker: chain data for %s missing expected fields", ticker)
            return

        if not ivs:
            return

        # Use the near-term ATM IV as the baseline reading
        near_term_iv = ivs[0]
        today_str = date.today().isoformat()

        with self._lock:
            if not self._history.get(ticker):
                self._history[ticker].append({"date": today_str, "iv": near_term_iv})
                self._last_date[ticker] = today_str
                logger.info(
                    "IVTracker: seeded %s with near-term IV %.4f from chain (%d expirations)",
                    ticker, near_term_iv, len(ivs),
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_auto_save(self) -> None:
        """Save to disk if auto-save is enabled and the interval has elapsed."""
        if not self._auto_save_enabled:
            return
        now = time.monotonic()
        if now - self._last_save_ts >= _AUTO_SAVE_INTERVAL:
            self._last_save_ts = now
            # Run save in a daemon thread to avoid blocking the caller
            t = threading.Thread(target=self.save_to_file, daemon=True)
            t.start()
