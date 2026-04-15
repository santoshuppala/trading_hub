"""
PositionReconciler — sync local position state with broker reality.

Rules:
1. Broker is ALWAYS source of truth
2. Position at broker but not local → add to local (crash recovery)
3. Position in local but not at broker → remove from local (closed externally)
4. NEVER auto-sell — only adjust tracking, log for human review

Called at:
- startup: full sync (sync_startup)
- every 30 min: lightweight check (sync_periodic)
"""
from __future__ import annotations

import logging
from typing import Set

log = logging.getLogger(__name__)


class PositionReconciler:

    def __init__(self, engine_name: str, adapter):
        self._name = engine_name
        self._adapter = adapter

    def sync_startup(self) -> None:
        """Full reconciliation at startup. Adjusts local state to match broker."""
        log.info("[%s] Running startup position reconciliation...", self._name)
        self._reconcile(context='startup')

    def sync_periodic(self) -> None:
        """Lightweight periodic reconciliation (every 30 min)."""
        self._reconcile(context='periodic')

    def _reconcile(self, context: str = 'periodic') -> None:
        try:
            broker_positions = self._adapter.get_broker_positions()
        except Exception as exc:
            log.warning("[%s] Reconciliation failed — could not fetch broker positions: %s",
                        self._name, exc)
            return

        local_positions = self._adapter.get_positions()

        # Normalize to sets of tickers
        broker_tickers: Set[str] = set(broker_positions.keys())
        if isinstance(local_positions, set):
            local_tickers = local_positions
        elif isinstance(local_positions, dict):
            local_tickers = set(local_positions.keys())
        else:
            local_tickers = set()

        # Positions at broker but not local → add (crash recovery)
        orphaned = broker_tickers - local_tickers
        for ticker in orphaned:
            qty = broker_positions[ticker]
            log.warning(
                "[%s] [reconcile] ORPHANED position at broker: %s qty=%d — "
                "adding to local tracking (crash recovery)",
                self._name, ticker, qty,
            )
            self._adapter.add_position(ticker, qty)

        # Positions in local but not at broker → remove (closed externally)
        stale = local_tickers - broker_tickers
        for ticker in stale:
            log.warning(
                "[%s] [reconcile] STALE position in local state: %s — "
                "not found at broker, removing from tracking",
                self._name, ticker,
            )
            self._adapter.remove_position(ticker)

        # Summary
        if orphaned or stale:
            log.warning(
                "[%s] [reconcile] %s: %d orphaned recovered, %d stale removed | "
                "local=%d broker=%d",
                self._name, context,
                len(orphaned), len(stale),
                len(local_tickers) + len(orphaned) - len(stale),
                len(broker_tickers),
            )
        else:
            log.info("[%s] [reconcile] %s: positions in sync (%d)",
                     self._name, context, len(local_tickers))
