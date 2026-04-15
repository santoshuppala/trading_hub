"""
Smart data source persistence — persist on first arrival + meaningful change.

Instead of flooding the DB with identical rows every 10 seconds, this module:
1. Hashes each data snapshot
2. Compares to the last persisted hash
3. Only writes to DB when the data has meaningfully changed

Change thresholds per source:
  - Fear & Greed: value changes by > 2 points
  - FRED macro: any field changes
  - Yahoo earnings: earnings date changes
  - Yahoo analyst: target price changes by > 1%
  - Finviz insider: net_insider_buys changes
  - Finviz screener: short_float changes by > 0.5%
  - SEC EDGAR: filing count changes
  - Polygon: first fetch only (prev day is static)
  - Alpha Vantage: first fetch only (daily data, 25 req/day limit)
  - Benzinga: headline count or avg sentiment changes by > 0.1
  - StockTwits: mention count changes or sentiment shifts by > 0.1

Each persisted row includes:
  - snapshot_type: source name (e.g., 'fear_greed', 'fred_macro')
  - ticker: ticker symbol or 'MARKET' for market-wide data
  - data: full JSON snapshot
  - persisted_at: timestamp
  - change_reason: why this was persisted ('first_of_day', 'value_changed', etc.)
"""
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

log = logging.getLogger(__name__)


class SmartPersistence:
    """Persist data source snapshots only on meaningful change."""

    def __init__(self, writer_fn: Callable = None):
        """
        Args:
            writer_fn: function(snapshot_type, ticker, data_dict, change_reason) -> None
                       Called when a snapshot should be persisted.
                       If None, logs only (no DB write).
        """
        self._writer_fn = writer_fn
        # {(snapshot_type, ticker): {'hash': str, 'data': dict, 'time': float}}
        self._last_persisted: Dict[tuple, dict] = {}
        self._persist_count = 0
        self._skip_count = 0

    def persist_if_changed(
        self,
        snapshot_type: str,
        ticker: str,
        data: dict,
        change_detector: Callable = None,
    ) -> bool:
        """Persist a snapshot if it's the first of the day or meaningfully changed.

        Args:
            snapshot_type: source identifier (e.g., 'fear_greed', 'yahoo_earnings')
            ticker: ticker symbol or 'MARKET' for market-wide
            data: full data dict to persist
            change_detector: optional function(old_data, new_data) -> (bool, reason)
                            Returns (should_persist, reason_string).
                            If None, uses hash comparison (any change triggers persist).

        Returns True if persisted, False if skipped.
        """
        key = (snapshot_type, ticker)
        data_hash = self._hash(data)

        last = self._last_persisted.get(key)

        # First time seeing this key today -> always persist
        if last is None:
            self._write(snapshot_type, ticker, data, 'first_of_day')
            self._last_persisted[key] = {
                'hash': data_hash, 'data': data, 'time': time.time()
            }
            return True

        # Same hash -> skip
        if last['hash'] == data_hash:
            self._skip_count += 1
            return False

        # Hash changed — check if change is meaningful
        if change_detector:
            should_persist, reason = change_detector(last['data'], data)
            if not should_persist:
                self._skip_count += 1
                return False
            self._write(snapshot_type, ticker, data, reason)
        else:
            # No detector — any hash change triggers persist
            self._write(snapshot_type, ticker, data, 'data_changed')

        self._last_persisted[key] = {
            'hash': data_hash, 'data': data, 'time': time.time()
        }
        return True

    def reset_day(self):
        """Clear persisted state for a new trading day."""
        self._last_persisted.clear()
        self._persist_count = 0
        self._skip_count = 0

    def stats(self) -> dict:
        return {
            'persisted': self._persist_count,
            'skipped': self._skip_count,
            'tracked_keys': len(self._last_persisted),
        }

    def _write(self, snapshot_type: str, ticker: str, data: dict, reason: str):
        """Write to DB via the writer function."""
        self._persist_count += 1
        if self._writer_fn:
            try:
                self._writer_fn(snapshot_type, ticker, data, reason)
            except Exception as exc:
                log.warning("[SmartPersistence] Write failed for %s/%s: %s",
                            snapshot_type, ticker, exc)
        else:
            log.debug("[SmartPersistence] %s/%s persisted (reason=%s) [no writer]",
                      snapshot_type, ticker, reason)

    @staticmethod
    def _hash(data: dict) -> str:
        """Deterministic hash of a data dict."""
        serialized = json.dumps(data, sort_keys=True, default=str)
        return hashlib.md5(serialized.encode()).hexdigest()


# -- Change detectors per source -------------------------------------------

def fear_greed_changed(old: dict, new: dict) -> tuple:
    """Persist if value changes by > 2 points."""
    old_val = old.get('value', 0)
    new_val = new.get('value', 0)
    delta = abs(new_val - old_val)
    if delta > 2.0:
        return True, f'value_changed_{old_val:.0f}_to_{new_val:.0f}'
    return False, ''


def fred_macro_changed(old: dict, new: dict) -> tuple:
    """Persist if any macro field changes."""
    for key in ['regime', 'fed_trend', 'yield_status', 'vix_regime']:
        if old.get(key) != new.get(key):
            return True, f'{key}_changed'
    # VIX changed by > 1
    old_vix = old.get('vix', 0)
    new_vix = new.get('vix', 0)
    if abs(new_vix - old_vix) > 1.0:
        return True, f'vix_changed_{old_vix:.1f}_to_{new_vix:.1f}'
    return False, ''


def yahoo_earnings_changed(old: dict, new: dict) -> tuple:
    """Persist if earnings date changes."""
    if old.get('next_earnings_date') != new.get('next_earnings_date'):
        return True, 'earnings_date_changed'
    return False, ''


def yahoo_analyst_changed(old: dict, new: dict) -> tuple:
    """Persist if target price changes by > 1%."""
    old_target = old.get('target_mean', 0) or 0
    new_target = new.get('target_mean', 0) or 0
    if old_target > 0:
        change_pct = abs(new_target - old_target) / old_target
        if change_pct > 0.01:
            return True, f'target_changed_{old_target:.0f}_to_{new_target:.0f}'
    elif new_target > 0:
        return True, 'target_first_value'
    return False, ''


def finviz_insider_changed(old: dict, new: dict) -> tuple:
    """Persist if net insider buys changes."""
    if old.get('net_insider_buys') != new.get('net_insider_buys'):
        return True, f"insider_changed_{old.get('net_insider_buys')}_to_{new.get('net_insider_buys')}"
    return False, ''


def finviz_screener_changed(old: dict, new: dict) -> tuple:
    """Persist if short_float changes by > 0.5%."""
    old_short = old.get('short_float', 0)
    new_short = new.get('short_float', 0)
    if abs(new_short - old_short) > 0.5:
        return True, f'short_float_changed_{old_short:.1f}_to_{new_short:.1f}'
    # Price changed by > 2%
    old_price = old.get('price', 0)
    new_price = new.get('price', 0)
    if old_price > 0 and abs(new_price - old_price) / old_price > 0.02:
        return True, f'price_changed_{old_price:.2f}_to_{new_price:.2f}'
    return False, ''


def edgar_filing_changed(old: dict, new: dict) -> tuple:
    """Persist if filing count increased."""
    old_count = old.get('filing_count', 0)
    new_count = new.get('filing_count', 0)
    if new_count > old_count:
        return True, f'new_filings_{new_count - old_count}'
    return False, ''


def polygon_changed(old: dict, new: dict) -> tuple:
    """Polygon prev close: persist only if date changed (new trading day)."""
    if old.get('date') != new.get('date'):
        return True, 'new_trading_day'
    return False, ''


def alpha_vantage_changed(old: dict, new: dict) -> tuple:
    """Alpha Vantage: persist only once per day (25 req/day limit)."""
    # Already cached for 24 hours — any hash change means new data
    return True, 'daily_update'


def benzinga_changed(old: dict, new: dict) -> tuple:
    """Persist if headline count changes or sentiment shifts by > 0.1."""
    if old.get('headlines_1h', 0) != new.get('headlines_1h', 0):
        return True, f"headlines_changed_{old.get('headlines_1h', 0)}_to_{new.get('headlines_1h', 0)}"
    old_sent = old.get('avg_sentiment_1h', 0)
    new_sent = new.get('avg_sentiment_1h', 0)
    if abs(new_sent - old_sent) > 0.1:
        return True, f'sentiment_shifted_{old_sent:.2f}_to_{new_sent:.2f}'
    return False, ''


def stocktwits_changed(old: dict, new: dict) -> tuple:
    """Persist if mention count changes or sentiment shifts by > 0.1."""
    old_mentions = old.get('mention_count', 0)
    new_mentions = new.get('mention_count', 0)
    if old_mentions != new_mentions:
        return True, f'mentions_changed_{old_mentions}_to_{new_mentions}'
    old_bull = old.get('bullish_pct', 0.5)
    new_bull = new.get('bullish_pct', 0.5)
    if abs(new_bull - old_bull) > 0.1:
        return True, f'sentiment_shifted_{old_bull:.2f}_to_{new_bull:.2f}'
    return False, ''


# Mapping of snapshot_type -> change detector
CHANGE_DETECTORS = {
    'fear_greed': fear_greed_changed,
    'fred_macro': fred_macro_changed,
    'yahoo_earnings': yahoo_earnings_changed,
    'yahoo_analyst': yahoo_analyst_changed,
    'yahoo_fundamentals': None,  # any change -> persist (rare)
    'finviz_insider': finviz_insider_changed,
    'finviz_screener': finviz_screener_changed,
    'sec_edgar_filings': edgar_filing_changed,
    'polygon_prev_close': polygon_changed,
    'alpha_vantage': alpha_vantage_changed,
    'benzinga_news': benzinga_changed,
    'stocktwits_social': stocktwits_changed,
}
