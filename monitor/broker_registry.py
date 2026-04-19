"""
BrokerRegistry — central collection of all broker instances.

Used by reconciliation, equity tracking, and PnL verification to iterate
over ALL brokers without hardcoding broker names. Adding a new broker
requires only:
    1. Create MyBroker(BaseBroker) with get_positions/get_equity
    2. registry.register('mybroker', my_broker_instance)
    3. Done — reconciliation, equity tracking, routing all work automatically.

Usage:
    registry = BrokerRegistry()
    registry.register('alpaca', alpaca_broker)
    registry.register('tradier', tradier_broker)

    for name, positions in registry.get_all_positions().items():
        ...  # positions are normalized [{symbol, qty, avg_entry, cost_basis}]

    for name, equity in registry.get_all_equity().items():
        ...  # equity is float or None
"""
from __future__ import annotations

import logging
from typing import Dict, Iterator, Optional, Tuple

log = logging.getLogger(__name__)


class BrokerRegistry:
    """Holds references to all active broker instances."""

    def __init__(self):
        self._brokers: Dict[str, object] = {}

    def register(self, name: str, broker) -> None:
        """Register a broker instance by name."""
        self._brokers[name] = broker
        log.info("[BrokerRegistry] Registered broker: %s (%s)",
                 name, type(broker).__name__)

    def get(self, name: str):
        """Get a broker by name (or None)."""
        return self._brokers.get(name)

    def brokers(self) -> Iterator[Tuple[str, object]]:
        """Iterate over (name, broker) pairs."""
        yield from self._brokers.items()

    def names(self) -> list[str]:
        """List of registered broker names."""
        return list(self._brokers.keys())

    def __len__(self) -> int:
        return len(self._brokers)

    def __contains__(self, name: str) -> bool:
        return name in self._brokers

    # ── Aggregate queries ─────────────────────────────────────────────

    def get_all_positions(self) -> Dict[str, list[dict]]:
        """Fetch normalized positions from ALL registered brokers.

        Returns {broker_name: [{symbol, qty, avg_entry, cost_basis}, ...]}.
        If a broker fails, returns empty list for that broker (logged).
        """
        result = {}
        for name, broker in self._brokers.items():
            try:
                result[name] = broker.get_positions()
            except Exception as exc:
                log.warning("[BrokerRegistry] %s get_positions failed: %s",
                            name, exc)
                result[name] = []
        return result

    def get_all_equity(self) -> Dict[str, Optional[float]]:
        """Fetch equity from ALL registered brokers.

        Returns {broker_name: equity_float_or_None}.
        """
        result = {}
        for name, broker in self._brokers.items():
            try:
                result[name] = broker.get_equity()
            except Exception as exc:
                log.warning("[BrokerRegistry] %s get_equity failed: %s",
                            name, exc)
                result[name] = None
        return result

    def supports_fractional(self, broker_name: str) -> bool:
        """Check if a broker supports fractional share orders."""
        broker = self._brokers.get(broker_name)
        if broker is None:
            return False
        return getattr(broker, 'supports_fractional', False)

    def supports_notional(self, broker_name: str) -> bool:
        """Check if a broker supports dollar-amount (notional) orders."""
        broker = self._brokers.get(broker_name)
        if broker is None:
            return False
        return getattr(broker, 'supports_notional', False)
