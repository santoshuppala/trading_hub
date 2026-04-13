"""
Independent risk gate for options subsystem.

Enforces budget caps, position limits, and cross-layer deduplication via registry.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

from monitor.position_registry import registry

log = logging.getLogger(__name__)

LAYER_NAME = 'options'


class OptionsRiskGate:
    """
    Independent risk gate for the options subsystem.

    Enforces:
      1. options-layer max_positions (default 5)
      2. per-ticker cooldown (default 300s)
      3. per-trade budget cap (OPTIONS_TRADE_BUDGET)
      4. total deployed capital against total_budget ($10K ceiling)
      5. GlobalPositionRegistry cross-layer dedup (layer='options')

    Does NOT share state with RiskEngine, RiskAdapter (pro_setups),
    or PopExecutor. Maintains its own open-position accounting via
    OPTIONS_FILL events emitted by AlpacaOptionsBroker.

    Parameters
    ----------
    max_positions  : max concurrent options positions
    trade_budget   : per-trade dollar limit
    total_budget   : aggregate capital ceiling ($10K)
    order_cooldown : seconds between orders on the same ticker
    """

    def __init__(
        self,
        max_positions:  int   = 5,
        trade_budget:   float = 500.0,
        total_budget:   float = 10_000.0,
        order_cooldown: int   = 300,
    ) -> None:
        self._max_positions  = max_positions
        self._trade_budget   = trade_budget
        self._total_budget   = total_budget
        self._order_cooldown = order_cooldown
        self._lock           = threading.Lock()

        # Internal state — options-layer only
        self._open_positions:  Dict[str, float] = {}  # ticker → cost basis
        self._last_order:      Dict[str, float] = {}  # ticker → monotonic ts
        self._deployed_capital: float           = 0.0

    def check(
        self,
        ticker:     str,
        max_risk:   float,  # max dollar loss for this position
    ) -> Optional[str]:
        """
        Run all risk checks.

        Returns None on pass, or a human-readable rejection reason string.
        Does NOT mutate state — call acquire() after a successful check
        to reserve the position.
        """
        with self._lock:
            # Check 1: registry dedup (cross-layer)
            if not registry.try_acquire(ticker, layer=LAYER_NAME):
                owner = registry.held_by(ticker)
                return f"ticker {ticker} already held by {owner}"

            # Check 2: max positions
            if len(self._open_positions) >= self._max_positions:
                return f"max positions ({self._max_positions}) reached"

            # Check 3: cooldown
            last_order_ts = self._last_order.get(ticker, 0.0)
            elapsed = time.monotonic() - last_order_ts
            if elapsed < self._order_cooldown:
                return f"cooldown: {self._order_cooldown - elapsed:.0f}s remaining"

            # Check 4: per-trade budget
            if max_risk > self._trade_budget:
                return f"max_risk ${max_risk:.2f} > trade_budget ${self._trade_budget:.2f}"

            # Check 5: total capital
            available = self._total_budget - self._deployed_capital
            if max_risk > available:
                return f"max_risk ${max_risk:.2f} > available ${available:.2f}"

            # All checks pass
            return None

    def acquire(self, ticker: str, cost: float) -> None:
        """
        Reserve ticker in GlobalPositionRegistry and in local accounting.
        Call only after check() returns None.

        Args:
            ticker: stock symbol
            cost: net debit / credit (positive = cost, negative = credit)
        """
        with self._lock:
            # Already acquired via check(), just update accounting
            self._open_positions[ticker] = cost
            self._last_order[ticker] = time.monotonic()
            self._deployed_capital += max(cost, 0.0)  # only debit counts against budget
            log.debug(
                f"[OptionsRiskGate] acquired {ticker} | cost=${cost:.2f} | "
                f"deployed=${self._deployed_capital:.2f} / ${self._total_budget:.2f}"
            )

    def release(self, ticker: str) -> None:
        """
        Release ticker from GlobalPositionRegistry and local accounting.
        Called by OptionsEngine when position closes.

        Args:
            ticker: stock symbol
        """
        with self._lock:
            cost = self._open_positions.pop(ticker, 0.0)
            registry.release(ticker)
            self._deployed_capital = max(self._deployed_capital - max(cost, 0.0), 0.0)
            log.debug(
                f"[OptionsRiskGate] released {ticker} | cost was ${cost:.2f} | "
                f"deployed now ${self._deployed_capital:.2f} / ${self._total_budget:.2f}"
            )

    @property
    def available_capital(self) -> float:
        """Returns remaining options capital = total_budget - deployed."""
        with self._lock:
            return self._total_budget - self._deployed_capital

    @property
    def open_count(self) -> int:
        """Returns number of open positions."""
        with self._lock:
            return len(self._open_positions)
