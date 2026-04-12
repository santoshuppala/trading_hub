"""
monitor/position_registry.py — Global cross-layer position registry.

Problem
-------
RiskEngine, RiskAdapter (pro-setups), and PopExecutor each maintain independent
position tracking.  If two layers fire BUY on the same ticker in the same bar,
both pass their local max-position and duplicate checks, resulting in 2× exposure.

Solution
--------
A single ``GlobalPositionRegistry`` that all layers check before emitting
ORDER_REQ.  The registry is a thin wrapper around a thread-safe set.

Usage
-----
    from monitor.position_registry import registry

    # Before emitting ORDER_REQ:
    if not registry.try_acquire(ticker, layer="pro_setups"):
        return   # another layer already holds this ticker

    # On position close:
    registry.release(ticker)

Thread-safety: all methods use an internal lock.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, Optional, Set

log = logging.getLogger(__name__)


class GlobalPositionRegistry:
    """
    Cross-layer position deduplication.

    Each entry tracks which layer owns the position so that release()
    can be layer-aware for logging.
    """

    def __init__(self, global_max: int = 8) -> None:
        self._lock = threading.Lock()
        self._positions: Dict[str, str] = {}   # ticker → layer name
        self._global_max = global_max

    # ── Public API ────────────────────────────────────────────────────────────

    def try_acquire(self, ticker: str, layer: str) -> bool:
        """
        Attempt to reserve ``ticker`` for ``layer``.

        Returns True if the ticker is now reserved (or was already held by
        the same layer).  Returns False if another layer holds it or if the
        global limit is reached.
        """
        with self._lock:
            owner = self._positions.get(ticker)
            if owner is not None:
                if owner == layer:
                    return True   # same layer re-acquiring (idempotent)
                log.info(
                    "[PositionRegistry] BLOCKED %s for %s — already held by %s",
                    ticker, layer, owner,
                )
                return False
            if len(self._positions) >= self._global_max:
                log.info(
                    "[PositionRegistry] BLOCKED %s for %s — global max %d reached (%s)",
                    ticker, layer, self._global_max, sorted(self._positions.keys()),
                )
                return False
            self._positions[ticker] = layer
            log.debug("[PositionRegistry] Acquired %s for %s", ticker, layer)
            return True

    def release(self, ticker: str) -> None:
        """Release a ticker when the position is closed."""
        with self._lock:
            owner = self._positions.pop(ticker, None)
            if owner:
                log.debug("[PositionRegistry] Released %s (was %s)", ticker, owner)

    def is_held(self, ticker: str) -> bool:
        """Check if a ticker is held by any layer."""
        with self._lock:
            return ticker in self._positions

    def held_by(self, ticker: str) -> Optional[str]:
        """Return the layer name holding the ticker, or None."""
        with self._lock:
            return self._positions.get(ticker)

    def count(self) -> int:
        """Return total number of held positions across all layers."""
        with self._lock:
            return len(self._positions)

    def all_positions(self) -> Dict[str, str]:
        """Return a snapshot of {ticker: layer} for all held positions."""
        with self._lock:
            return dict(self._positions)

    def tickers_for_layer(self, layer: str) -> Set[str]:
        """Return set of tickers held by a specific layer."""
        with self._lock:
            return {t for t, l in self._positions.items() if l == layer}


# ── Module-level singleton ────────────────────────────────────────────────────
# Import from here:  from monitor.position_registry import registry

registry = GlobalPositionRegistry()
