"""
monitor/ticker_scanner.py — Tiered scanning with quick_filter + cold rotation.

Option D: Classify tickers into HOT / WARM / COLD tiers before emit_batch.
  - HOT:  open positions + recently signaled → scanned every cycle
  - WARM: high RVOL or recent momentum → scanned every cycle
  - COLD: everything else → rotated in batches (1/3 per cycle)

This reduces per-cycle API calls while ensuring active tickers are always fresh.

Usage:
    from monitor.ticker_scanner import TickerScanner
    scanner = TickerScanner(positions=positions)
    active_tickers = scanner.classify(all_tickers, bars_cache)
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Set

log = logging.getLogger(__name__)

# Tiers
TIER_HOT  = 'hot'
TIER_WARM = 'warm'
TIER_COLD = 'cold'

# RVOL threshold for warm classification
_WARM_RVOL_THRESHOLD = 1.5

# How many cold rotation groups (scan 1/N per cycle)
_COLD_ROTATION_GROUPS = 3


class TickerScanner:
    """
    Tiered ticker classifier.  Call classify() before each emit_batch
    to get the subset of tickers that should be scanned this cycle.
    """

    def __init__(self, positions: dict) -> None:
        self._positions = positions
        self._signal_times: Dict[str, float] = {}   # ticker -> monotonic time of last signal
        self._cold_rotation_idx = 0

    def record_signal(self, ticker: str) -> None:
        """Mark a ticker as recently signaled (promotes to WARM for 10 min)."""
        self._signal_times[ticker] = time.monotonic()

    def quick_filter(self, tickers: List[str], bars_cache: dict) -> Dict[str, str]:
        """
        Classify all tickers into HOT / WARM / COLD.

        Returns {ticker: tier} for every ticker.
        """
        now = time.monotonic()
        classification: Dict[str, str] = {}

        for ticker in tickers:
            # HOT: currently holding a position
            if ticker in self._positions:
                classification[ticker] = TIER_HOT
                continue

            # WARM: recently signaled (within 10 min)
            last_sig = self._signal_times.get(ticker, 0)
            if now - last_sig < 600:
                classification[ticker] = TIER_WARM
                continue

            # WARM: high RVOL in last bars_cache
            df = bars_cache.get(ticker)
            if df is not None and not getattr(df, 'empty', True):
                try:
                    recent_vol = float(df['volume'].iloc[-5:].mean()) if len(df) >= 5 else 0
                    avg_vol = float(df['volume'].mean()) if len(df) > 0 else 1
                    if avg_vol > 0 and recent_vol / avg_vol > _WARM_RVOL_THRESHOLD:
                        classification[ticker] = TIER_WARM
                        continue
                except Exception:
                    pass

            # COLD: everything else
            classification[ticker] = TIER_COLD

        return classification

    def classify(self, tickers: List[str], bars_cache: dict) -> List[str]:
        """
        Return the list of tickers to scan this cycle.

        HOT + WARM tickers are always included.
        COLD tickers are rotated in groups of 1/_COLD_ROTATION_GROUPS per cycle.
        """
        tier_map = self.quick_filter(tickers, bars_cache)

        hot_warm = [t for t in tickers if tier_map.get(t) in (TIER_HOT, TIER_WARM)]
        cold = [t for t in tickers if tier_map.get(t) == TIER_COLD]

        # Rotate cold tickers
        if cold:
            group_size = max(1, len(cold) // _COLD_ROTATION_GROUPS)
            start = self._cold_rotation_idx * group_size
            end = start + group_size
            # Last group gets any remainder
            if self._cold_rotation_idx >= _COLD_ROTATION_GROUPS - 1:
                end = len(cold)
            cold_batch = cold[start:end]
            self._cold_rotation_idx = (self._cold_rotation_idx + 1) % _COLD_ROTATION_GROUPS
        else:
            cold_batch = []

        result = hot_warm + cold_batch

        # Prune stale signal times (>30 min old)
        cutoff = time.monotonic() - 1800
        stale = [t for t, ts in self._signal_times.items() if ts < cutoff]
        for t in stale:
            del self._signal_times[t]

        log.debug(
            "[TickerScanner] hot=%d warm=%d cold=%d(batch=%d) total=%d",
            sum(1 for v in tier_map.values() if v == TIER_HOT),
            sum(1 for v in tier_map.values() if v == TIER_WARM),
            len(cold), len(cold_batch), len(result),
        )
        return result
