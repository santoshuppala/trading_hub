"""
RegistryGate — centralized cross-layer position dedup at Core.

V7: Satellites no longer write to position_registry.json.
They send ORDER_REQ with a `layer` field. Core's RegistryGate acquires
the ticker in the registry before routing to the broker.

Design: RegistryGate checks every ORDER_REQ BUY. If blocked, it stores
the event_id in _blocked_ids set. SmartRouter checks this set and skips.
On POSITION CLOSED: releases the registry slot.
On ORDER_FAIL: releases if we acquired it.
"""
import logging
import threading

from monitor.event_bus import EventBus, EventType, Event
from monitor.distributed_registry import DistributedPositionRegistry

log = logging.getLogger(__name__)


class RegistryGate:
    """Centralized cross-layer dedup gate. Only Core instantiates this."""

    def __init__(self, bus: EventBus, registry: DistributedPositionRegistry):
        self._bus = bus
        self._registry = registry
        self._lock = threading.Lock()
        # V8: Track blocked event IDs with FIFO eviction (was set.pop() which
        # removes arbitrary elements → recently blocked orders could slip through).
        from collections import OrderedDict
        self._blocked_ids: OrderedDict = OrderedDict()
        # Track tickers we acquired so we can release on ORDER_FAIL
        self._acquired_tickers: set = set()

        # Subscribe — higher priority number = runs earlier in SYNC dispatch.
        # RegistryGate (2) runs BEFORE SmartRouter (1) and PortfolioRiskGate (0).
        bus.subscribe(EventType.ORDER_REQ, self._on_order_req, priority=2)
        bus.subscribe(EventType.POSITION, self._on_position, priority=2)
        bus.subscribe(EventType.ORDER_FAIL, self._on_order_fail, priority=2)

        log.info("[RegistryGate] Initialized — centralized registry at Core")

    def is_blocked(self, event_id: str) -> bool:
        """Check if an ORDER_REQ was blocked by registry. Called by SmartRouter."""
        with self._lock:
            return event_id in self._blocked_ids  # OrderedDict supports 'in'

    def _on_order_req(self, event: Event) -> None:
        """Acquire ticker in registry for BUY orders. SELL always passes."""
        p = event.payload

        # SELL orders always pass — never block exits
        side_str = str(p.side).upper()
        if 'SELL' in side_str:
            return

        # Already blocked by PortfolioRiskGate
        if getattr(event, '_portfolio_blocked', False):
            return

        ticker = p.ticker
        layer = getattr(p, 'layer', None) or self._infer_layer(p.reason)

        if not self._registry.try_acquire(ticker, layer):
            holder = self._registry.held_by(ticker)
            log.info("[RegistryGate] %s BLOCKED: held by layer '%s' "
                     "(requested by '%s')", ticker, holder, layer)

            # Store blocked event ID so SmartRouter can check
            with self._lock:
                import time as _time
                self._blocked_ids[event.event_id] = _time.monotonic()
                # V8: FIFO eviction — remove oldest entries (OrderedDict preserves insertion order)
                while len(self._blocked_ids) > 1000:
                    self._blocked_ids.popitem(last=False)  # FIFO — removes oldest

            # Emit RISK_BLOCK for observability
            try:
                from monitor.events import RiskBlockPayload
                block_payload = RiskBlockPayload(
                    ticker=ticker,
                    reason=f"registry: held by {holder} (requested by {layer})",
                    signal_action=str(getattr(p, 'reason', '')),
                )
                self._bus.emit(Event(
                    type=EventType.RISK_BLOCK,
                    payload=block_payload,
                ))
            except Exception:
                pass
            return

        # Acquired — track so we can release on ORDER_FAIL
        with self._lock:
            self._acquired_tickers.add(ticker)
        log.debug("[RegistryGate] %s acquired for layer '%s'", ticker, layer)

    def _on_position(self, event: Event) -> None:
        """Release ticker from registry when position is fully closed."""
        try:
            p = event.payload
            action = str(p.action).upper()
            if action == 'CLOSED':
                ticker = p.ticker
                self._registry.release(ticker)
                with self._lock:
                    self._acquired_tickers.discard(ticker)
                log.debug("[RegistryGate] %s released (CLOSED)", ticker)
        except Exception:
            pass

    def _on_order_fail(self, event: Event) -> None:
        """Release ticker if we acquired it but the order failed."""
        try:
            p = event.payload
            ticker = p.ticker
            side_str = str(p.side).upper()
            with self._lock:
                if 'BUY' in side_str and ticker in self._acquired_tickers:
                    self._registry.release(ticker)
                    self._acquired_tickers.discard(ticker)
                    log.debug("[RegistryGate] %s released (ORDER_FAIL)", ticker)
        except Exception:
            pass

    @staticmethod
    def _infer_layer(reason: str) -> str:
        """Infer layer from ORDER_REQ reason string if layer field is missing."""
        if not reason:
            return 'vwap'
        reason_lower = reason.lower()
        if reason_lower.startswith('pro:'):
            return 'pro'
        if reason_lower.startswith('pop:'):
            return 'pop'
        if reason_lower.startswith('options:'):
            return 'options'
        return 'vwap'
