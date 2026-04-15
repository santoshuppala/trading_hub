"""
AbstractEngineAdapter — interface for engine-specific lifecycle operations.

Each satellite engine (Pro, Pop, Options) implements this interface so that
EngineLifecycle can manage state persistence, kill switch, heartbeat,
EOD reports, and broker reconciliation generically.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class AbstractEngineAdapter(ABC):
    """Engine-specific adapter injected into EngineLifecycle."""

    @abstractmethod
    def get_state(self) -> dict:
        """Return serializable state dict for persistence to disk.

        Must include all mutable state that would be lost on crash:
        positions, cooldowns, daily counters, trade log.
        """

    @abstractmethod
    def restore_state(self, state: dict) -> None:
        """Restore engine state from a previously saved dict.

        Called on startup after loading from disk. The adapter must
        validate and apply the state to the underlying engine.
        """

    @abstractmethod
    def get_positions(self) -> Dict[str, Any]:
        """Return current open positions for heartbeat/reconciliation.

        Returns dict: {ticker: position_info} where position_info
        varies by engine (Set[str] for Pro/Pop, Dict for Options).
        """

    @abstractmethod
    def get_trade_log(self) -> List[dict]:
        """Return today's completed trades for EOD report.

        Each trade dict should have at minimum:
        ticker, entry_price, exit_price, qty, pnl, reason, strategy
        """

    @abstractmethod
    def get_daily_pnl(self) -> float:
        """Return today's realized P&L for kill switch check."""

    @abstractmethod
    def get_daily_stats(self) -> dict:
        """Return daily statistics for heartbeat/EOD.

        Should include: trades, wins, pnl, open_positions count,
        plus any engine-specific metrics.
        """

    @abstractmethod
    def get_broker_positions(self) -> Dict[str, int]:
        """Fetch open positions from the actual broker.

        Returns {ticker: qty}. Used for reconciliation.
        Must handle API errors gracefully (return {} on failure).
        """

    @abstractmethod
    def force_close_all(self, reason: str) -> None:
        """Emergency close all open positions (kill switch).

        Must be non-blocking and best-effort. Log failures but
        never raise. Called when daily loss threshold is breached.
        """

    @abstractmethod
    def cancel_stale_orders(self) -> int:
        """Cancel any stale/orphaned orders at broker.

        Called during startup preflight. Returns count of cancelled orders.
        """

    @abstractmethod
    def verify_connectivity(self) -> bool:
        """Check that broker API is reachable.

        Called during startup preflight. Returns True if connected.
        """

    @abstractmethod
    def add_position(self, ticker: str, info: Any) -> None:
        """Add a position to local tracking (crash recovery from broker)."""

    @abstractmethod
    def remove_position(self, ticker: str) -> None:
        """Remove a position from local tracking (closed externally)."""
