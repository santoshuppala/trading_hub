"""
ProBacktestAdapter — Wraps ProSetupEngine for backtesting.

ProSetupEngine needs no external data (no news/social sources, no options chain).
Only responsibility: sync RiskAdapter state when FillSimulator closes a position.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from pro_setups.engine import ProSetupEngine
from monitor.position_registry import registry

log = logging.getLogger(__name__)


class ProBacktestAdapter:
    """
    Backtesting adapter for ProSetupEngine (T3.6 — momentum ignition, VWAP reclaim, ORB).

    Constructor: creates ProSetupEngine with default strategy parameters.
    On position close: syncs RiskAdapter state so future entries are allowed.
    """

    def __init__(self, bus: Any, fill_simulator: Any, **kwargs):
        """
        Initialize ProSetupEngine for backtesting.

        Args:
            bus: real EventBus in SYNC mode
            fill_simulator: FillSimulator instance
            **kwargs: passed to ProSetupEngine constructor
        """
        self.engine = ProSetupEngine(bus=bus, **kwargs)
        self.fill_simulator = fill_simulator

        # Register close callback so we sync risk state when positions close
        self.fill_simulator.register_close_callback('pro', self._on_position_close)

        log.info("[ProBacktestAdapter] Initialized")

    def _on_position_close(self, ticker: str) -> None:
        """
        Called by FillSimulator when a pro_setups position closes.

        Syncs RiskAdapter state so the engine can enter again on this ticker.
        """
        try:
            # Remove ticker from engine's risk tracking
            if hasattr(self.engine, '_risk_adapter') and hasattr(self.engine._risk_adapter, '_positions'):
                self.engine._risk_adapter._positions.discard(ticker)

            # Release from global registry
            registry.release(ticker)

            log.debug(f"[ProBacktestAdapter] Synced state for {ticker} after close")
        except Exception as e:
            log.error(f"[ProBacktestAdapter] Error syncing state for {ticker}: {e}")
