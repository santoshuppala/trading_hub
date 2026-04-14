"""
OptionsBacktestAdapter — Wraps OptionsEngine for backtesting.

Creates a SyntheticOptionChainClient as the chain source, disables the real broker
(FillSimulator handles execution), and syncs risk/position state on close callbacks.
"""
from __future__ import annotations

import logging
from typing import Any

from options.engine import OptionsEngine
from backtests.mocks.options_chain import SyntheticOptionChainClient

log = logging.getLogger(__name__)


class OptionsBacktestAdapter:
    """
    Backtesting adapter for OptionsEngine (T3.7 — options trading subsystem).

    Constructor: creates OptionsEngine with synthetic chain client and no broker.
    update_bar: feeds ATR/spot data into the synthetic chain each bar.
    On position close: syncs OptionsRiskGate and _positions so future entries are allowed.
    """

    def __init__(self, bus: Any, fill_simulator: Any, **kwargs):
        """
        Initialize OptionsEngine for backtesting.

        Args:
            bus: real EventBus in SYNC mode (BacktestBus's inner bus)
            fill_simulator: FillSimulator instance
            **kwargs: forwarded to OptionsEngine (max_positions, trade_budget, etc.)
        """
        # Build synthetic option chain client
        self.chain = SyntheticOptionChainClient()

        # Build real OptionsEngine with no API keys (paper mode, no broker)
        self.engine = OptionsEngine(
            bus=bus,
            options_key=None,
            options_secret=None,
            paper=True,
            **kwargs,
        )

        # Replace real chain with synthetic chain
        self.engine._chain = self.chain

        # Disable broker — FillSimulator handles all execution
        self.engine._broker = None

        self.fill_simulator = fill_simulator

        # Register close callback so we sync risk + position state
        self.fill_simulator.register_close_callback('options', self._on_position_close)

        log.info("[OptionsBacktestAdapter] Initialized")

    def update_bar(self, ticker: str, atr: float, spot: float) -> None:
        """
        Called by BacktestEngine on each replayed bar to feed the synthetic chain.

        Must be called BEFORE the bar event is emitted so that OptionsEngine
        sees up-to-date chain data when _on_bar fires.
        """
        self.chain.update_bar(ticker, atr, spot)

    def _on_position_close(self, ticker: str) -> None:
        """
        Called by FillSimulator when an options position closes.

        Syncs OptionsRiskGate and _positions dict so the engine can
        re-enter on this ticker.
        """
        try:
            # Release from risk gate
            self.engine._risk.release(ticker)

            # Remove from tracked positions
            with self.engine._positions_lock:
                self.engine._positions.pop(ticker, None)

            log.debug("[OptionsBacktestAdapter] Synced state for %s after close", ticker)
        except Exception as e:
            log.error("[OptionsBacktestAdapter] Error syncing state for %s: %s", ticker, e)
