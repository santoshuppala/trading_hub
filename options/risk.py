"""
Independent risk gate for options subsystem.

Enforces budget caps, position limits, and per-ticker cooldowns.
Tracks capital at risk (max_risk), NOT just net debit — critical for credit strategies.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

log = logging.getLogger(__name__)


class OptionsRiskGate:
    """
    Independent risk gate for the options subsystem.

    Enforces:
      1. options-layer max_positions (default 5)
      2. per-ticker cooldown (default 300s)
      3. per-trade max_risk cap (OPTIONS_TRADE_BUDGET)
      4. total capital at risk against total_budget ($10K ceiling)

    Capital tracking:
      - _deployed_capital tracks MAX RISK (not net debit)
      - A credit spread with $500 max risk counts as $500 deployed
      - This prevents over-leveraging credit strategies
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

        # ticker → max_risk (NOT net debit)
        self._open_positions:  Dict[str, float] = {}
        self._last_order:      Dict[str, float] = {}
        self._deployed_capital: float           = 0.0

    def check(
        self,
        ticker:     str,
        max_risk:   float,
    ) -> Optional[str]:
        """
        Run all risk checks. Returns None on pass, or rejection reason.
        Does NOT mutate state.
        """
        with self._lock:
            if ticker in self._open_positions:
                return f"options position already open for {ticker}"

            if len(self._open_positions) >= self._max_positions:
                return f"max positions ({self._max_positions}) reached"

            last_order_ts = self._last_order.get(ticker, 0.0)
            elapsed = time.monotonic() - last_order_ts
            if elapsed < self._order_cooldown:
                return f"cooldown: {self._order_cooldown - elapsed:.0f}s remaining"

            if max_risk > self._trade_budget:
                return f"max_risk ${max_risk:.2f} > trade_budget ${self._trade_budget:.2f}"

            available = self._total_budget - self._deployed_capital
            if max_risk > available:
                return f"max_risk ${max_risk:.2f} > available capital ${available:.2f}"

            return None

    def acquire(self, ticker: str, cost: float, max_risk: float = 0.0) -> None:
        """
        Reserve position. Tracks max_risk (not cost) against budget.

        Args:
            ticker: stock symbol
            cost: net debit/credit (positive=paid, negative=credit received)
            max_risk: maximum dollar loss for this position (always positive)
        """
        with self._lock:
            risk_amount = max_risk if max_risk > 0 else max(cost, 0.0)
            self._open_positions[ticker] = risk_amount
            self._last_order[ticker] = time.monotonic()
            self._deployed_capital += risk_amount
            log.info(
                "[OptionsRiskGate] acquired %s | cost=$%.2f | max_risk=$%.2f | "
                "deployed=$%.2f / $%.2f",
                ticker, cost, risk_amount,
                self._deployed_capital, self._total_budget,
            )

    def release(self, ticker: str) -> None:
        """Release position and free capital."""
        with self._lock:
            risk_amount = self._open_positions.pop(ticker, 0.0)
            self._deployed_capital = max(self._deployed_capital - risk_amount, 0.0)
            log.info(
                "[OptionsRiskGate] released %s | freed=$%.2f | "
                "deployed=$%.2f / $%.2f",
                ticker, risk_amount,
                self._deployed_capital, self._total_budget,
            )

    @property
    def available_capital(self) -> float:
        with self._lock:
            return self._total_budget - self._deployed_capital

    @property
    def open_count(self) -> int:
        with self._lock:
            return len(self._open_positions)
