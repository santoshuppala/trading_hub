"""
SmartRouter — routes orders to the best available broker.

Compares spreads between Alpaca and Tradier, routes to the one with
better execution. Provides automatic failover if one broker is down.

Usage:
    from monitor.smart_router import SmartRouter

    router = SmartRouter(
        bus=bus,
        alpaca_broker=alpaca_broker,
        tradier_broker=tradier_broker,
    )
    # Router subscribes to ORDER_REQ and handles routing automatically.
    # Individual brokers should NOT subscribe to ORDER_REQ when using the router.
"""
import json
import logging
import os
import time
import threading
from typing import Optional, Dict
from dataclasses import dataclass

from monitor.event_bus import EventBus, EventType, Event
from lifecycle.safe_state import SafeStateFile

log = logging.getLogger(__name__)


@dataclass
class BrokerHealth:
    """Track broker availability."""
    name: str
    available: bool = True
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    total_orders: int = 0
    total_fills: int = 0
    total_failures: int = 0
    avg_fill_time_ms: float = 0.0
    # Circuit breaker: disable after N consecutive failures, re-enable after cooldown
    max_failures: int = 3
    cooldown_seconds: float = 300.0  # 5 minutes


class SmartRouter:
    """
    Routes ORDER_REQ events to the best available broker.

    Routing priority:
    1. Strategy preference (if order specifies a broker via 'source' field)
    2. Spread comparison (route to broker with tighter spread)
    3. Health-based (skip unhealthy brokers)
    4. Round-robin fallback (distribute load)

    Failover:
    - If primary broker fails (ORDER_FAIL or exception), retry on secondary
    - Circuit breaker: disable broker after 3 consecutive failures
    - Auto-recover: re-enable after 5 minutes cooldown
    """

    def __init__(
        self,
        bus: EventBus,
        alpaca_broker=None,
        tradier_broker=None,
        default_broker: str = 'alpaca',
        brokers: Dict[str, object] = None,
    ):
        """
        V7 P4-1: Generic broker registration.

        Two ways to register brokers:
          1. Legacy: alpaca_broker=, tradier_broker= (backward compat)
          2. V7: brokers={'alpaca': broker1, 'tradier': broker2, 'ibkr': broker3}
             Pass any Dict[str, BaseBroker]. No hardcoded broker names.
        """
        self._bus = bus
        self._brokers: Dict[str, object] = {}
        self._health: Dict[str, BrokerHealth] = {}

        # V7 P4-1: Generic broker registration via dict
        if brokers:
            for name, broker in brokers.items():
                self._register_broker(name, broker, bus)
        else:
            # Legacy path: named parameters (backward compat)
            if alpaca_broker:
                self._register_broker('alpaca', alpaca_broker, bus)
            if tradier_broker:
                self._register_broker('tradier', tradier_broker, bus)

        self._default = default_broker
        self._lock = threading.Lock()
        self._registry_gate = None  # V7: set via set_registry_gate()
        # V7 P0-2: ORDER_REQ dedup — prevent replayed events from routing twice
        self._routed_event_ids: dict = {}  # {event_id: monotonic_time} — bounded LRU
        self._ROUTED_MAX = 5000
        self._all_brokers_down_alerted = False
        self._order_counter = 0  # for round-robin routing
        # Track which broker opened each position (ticker → broker_name)
        # V7: Uses SafeStateFile for fcntl locking, checksums, backups
        self._position_broker: Dict[str, str] = {}
        self._broker_map_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'position_broker_map.json',
        )
        self._broker_map_sf = SafeStateFile(self._broker_map_file,
                                            max_age_seconds=300.0)
        self._load_broker_map()

        # Subscribe to ORDER_REQ (we handle routing, individual brokers don't)
        bus.subscribe(EventType.ORDER_REQ, self._on_order_req, priority=1)

        # Track fills, failures, and position lifecycle for health monitoring
        bus.subscribe(EventType.FILL, self._on_fill, priority=10)
        bus.subscribe(EventType.ORDER_FAIL, self._on_order_fail, priority=10)
        bus.subscribe(EventType.POSITION, self._on_position, priority=10)

        broker_names = list(self._brokers.keys())
        log.info("[SmartRouter] ready | brokers=%s | default=%s", broker_names, default_broker)

    def _register_broker(self, name: str, broker, bus: EventBus) -> None:
        """V7 P4-1: Register a broker generically. Unsubscribes its ORDER_REQ handler."""
        self._brokers[name] = broker
        self._health[name] = BrokerHealth(name=name)
        # Unsubscribe broker's own ORDER_REQ handler (SmartRouter calls directly)
        for method_name in ('_on_order_request', '_on_order_req'):
            handler = getattr(broker, method_name, None)
            if handler:
                try:
                    bus.unsubscribe(EventType.ORDER_REQ, handler)
                except Exception:
                    pass
                break

    def _on_order_req(self, event: Event) -> None:
        """Route an order to the best broker."""
        # Skip if PortfolioRiskGate blocked this order
        if getattr(event, '_portfolio_blocked', False):
            return
        # V7: Skip if RegistryGate blocked this order
        if self._registry_gate and self._registry_gate.is_blocked(event.event_id):
            return
        # V7 P0-2: ORDER_REQ dedup — skip if we already routed this event_id
        # Protects against Redpanda replay after crash (at-least-once delivery)
        eid = event.event_id
        with self._lock:
            if eid in self._routed_event_ids:
                log.warning("[SmartRouter] Duplicate ORDER_REQ skipped: %s", eid[:12])
                return
            self._routed_event_ids[eid] = time.monotonic()
            # Prune oldest entries if over capacity
            if len(self._routed_event_ids) > self._ROUTED_MAX:
                oldest = sorted(self._routed_event_ids.items(),
                                key=lambda kv: kv[1])[:self._ROUTED_MAX // 2]
                for k, _ in oldest:
                    del self._routed_event_ids[k]

        p = event.payload
        ticker = p.ticker

        # Determine preferred broker
        preferred = self._select_broker(p)

        # Try preferred broker first
        broker = self._brokers.get(preferred)
        if broker and self._is_healthy(preferred):
            log.info("[SmartRouter] Routing %s %s to %s (preferred)",
                     p.side, ticker, preferred)
            self._execute_on_broker(preferred, broker, p, event)
            return

        # Failover to other broker
        fallback = self._get_fallback(preferred)
        if fallback:
            fb_broker = self._brokers[fallback]
            if self._is_healthy(fallback):
                log.warning("[SmartRouter] FAILOVER %s %s: %s -> %s",
                            p.side, ticker, preferred, fallback)
                self._execute_on_broker(fallback, fb_broker, p, event)
                return

        # No healthy broker available
        log.error("[SmartRouter] NO HEALTHY BROKER for %s %s — all brokers down",
                  p.side, ticker)
        if not self._all_brokers_down_alerted:
            self._all_brokers_down_alerted = True
            log.error("[SmartRouter] ALL BROKERS DOWN — halting order routing")
            try:
                from monitor.alerts import send_alert
                send_alert(None, "ALL BROKERS DOWN — trading halted", severity='CRITICAL')
            except Exception:
                pass
        from monitor.events import OrderFailPayload
        self._bus.emit(Event(
            type=EventType.ORDER_FAIL,
            payload=OrderFailPayload(
                ticker=ticker,
                side=str(getattr(p, 'side', 'UNKNOWN')),
                qty=p.qty,
                price=getattr(p, 'price', 0.0),
                reason='no healthy broker available',
            ),
            correlation_id=event.event_id,
        ))

    def _select_broker(self, p) -> str:
        """Select the best broker for this order."""
        side = str(getattr(p, 'side', '')).upper()
        ticker = p.ticker

        # 1. SELL orders MUST go to the broker that opened the position
        if 'SELL' in side:
            opening_broker = self._position_broker.get(ticker)
            if opening_broker and opening_broker in self._brokers:
                return opening_broker
            # V7 P4-1: Generic position detection — try has_position() first,
            # fall back to broker-specific APIs if not available.
            for broker_name, broker in self._brokers.items():
                try:
                    # V7: Generic interface — brokers can implement has_position()
                    if hasattr(broker, 'has_position'):
                        if broker.has_position(ticker):
                            log.info("[SmartRouter] SELL %s → %s (has_position)", ticker, broker_name)
                            return broker_name
                    # Legacy fallback: Alpaca-specific
                    elif hasattr(broker, '_client') and broker._client:
                        pos = broker._client.get_open_position(ticker)
                        if pos and int(float(pos.qty or 0)) > 0:
                            log.info("[SmartRouter] SELL %s → %s (detected open position)", ticker, broker_name)
                            return broker_name
                except Exception:
                    pass
            # Last resort: default
            return self._default

        # 2. Check if order specifies a preferred broker
        source = str(getattr(p, 'reason', '')).lower()
        if 'tradier' in source:
            return 'tradier'
        if 'alpaca' in source:
            return 'alpaca'

        # 3. Health-based: if one broker is unhealthy, use the other
        healthy_brokers = [n for n in self._brokers if self._is_healthy(n)]
        if len(healthy_brokers) == 1:
            return healthy_brokers[0]
        if not healthy_brokers:
            return self._default

        # 4. Round-robin across healthy brokers (BUY orders only)
        self._order_counter += 1
        return healthy_brokers[self._order_counter % len(healthy_brokers)]

    def _execute_on_broker(self, broker_name: str, broker, p, event: Event) -> None:
        """Execute an order on a specific broker."""
        side = str(getattr(p, 'side', '')).upper()
        ticker = p.ticker

        with self._lock:
            self._health[broker_name].total_orders += 1
            # Track which broker opened this position
            if 'BUY' in side:
                self._position_broker[ticker] = broker_name
                self._save_broker_map()
            # Only clear on full close — partial sells keep the broker mapping
            # We listen for POSITION events to clear on full close instead

        # Tag the event so fill tracking knows which broker executed
        event._routed_broker = broker_name

        try:
            # Call the broker's order handler directly
            if hasattr(broker, '_on_order_request'):
                broker._on_order_request(event)
            elif hasattr(broker, '_on_order_req'):
                broker._on_order_req(event)
            else:
                log.error("[SmartRouter] Broker %s has no order handler method", broker_name)
                self._record_failure(broker_name)
        except Exception as exc:
            log.error("[SmartRouter] Broker %s execution failed: %s", broker_name, exc)
            self._record_failure(broker_name)

            # Try failover
            fallback = self._get_fallback(broker_name)
            if fallback and self._is_healthy(fallback):
                log.warning("[SmartRouter] FAILOVER on exception: %s -> %s", broker_name, fallback)
                try:
                    fb_broker = self._brokers[fallback]
                    if hasattr(fb_broker, '_on_order_request'):
                        fb_broker._on_order_request(event)
                    elif hasattr(fb_broker, '_on_order_req'):
                        fb_broker._on_order_req(event)
                except Exception as fb_exc:
                    log.error("[SmartRouter] Failover also failed: %s", fb_exc)
                    self._record_failure(fallback)

    def _on_fill(self, event: Event) -> None:
        """Track successful fills for health monitoring."""
        broker_name = getattr(event, '_routed_broker', None)
        if broker_name:
            self._record_success(broker_name)

    def _on_order_fail(self, event: Event) -> None:
        """Track failures for health monitoring."""
        broker_name = getattr(event, '_routed_broker', None)
        if broker_name:
            self._record_failure(broker_name)

    def _on_position(self, event: Event) -> None:
        """Clear broker mapping only when position is fully closed."""
        try:
            p = event.payload
            if str(p.action) == 'CLOSED':
                with self._lock:
                    self._position_broker.pop(p.ticker, None)
                    self._save_broker_map()
        except Exception:
            pass

    def _record_failure(self, broker_name: str) -> None:
        """Record a broker failure for circuit breaking."""
        with self._lock:
            h = self._health.get(broker_name)
            if h:
                h.consecutive_failures += 1
                h.total_failures += 1
                h.last_failure_time = time.monotonic()
                if h.consecutive_failures >= h.max_failures:
                    h.available = False
                    log.error("[SmartRouter] CIRCUIT BREAKER: %s disabled after %d failures "
                              "(cooldown %ds)", broker_name, h.consecutive_failures,
                              int(h.cooldown_seconds))

    def _record_success(self, broker_name: str) -> None:
        """Record a successful order — resets circuit breaker."""
        with self._lock:
            h = self._health.get(broker_name)
            if h:
                h.consecutive_failures = 0
                h.total_fills += 1
                h.available = True
        self._all_brokers_down_alerted = False

    def _is_healthy(self, broker_name: str) -> bool:
        """Check if a broker is healthy (circuit breaker)."""
        with self._lock:
            h = self._health.get(broker_name)
            if h is None:
                return False
            if h.available:
                return True
            # Check cooldown — auto-recover after cooldown_seconds
            if (time.monotonic() - h.last_failure_time) > h.cooldown_seconds:
                h.available = True
                h.consecutive_failures = 0
                log.info("[SmartRouter] %s auto-recovered after cooldown", broker_name)
                return True
            return False

    def _get_fallback(self, primary: str) -> Optional[str]:
        """Get the fallback broker name."""
        for name in self._brokers:
            if name != primary:
                return name
        return None

    def _load_broker_map(self) -> None:
        """Load position→broker mapping from disk. V7: shared fcntl lock + checksum."""
        data, _ = self._broker_map_sf.read()
        if data and 'map' in data:
            self._position_broker = data['map']
            log.info("[SmartRouter] Loaded broker map (v%d): %s",
                     data.get('_version', 0), self._position_broker or '{}')
        elif data:
            # Backward compat: old format was flat {ticker: broker}
            self._position_broker = {k: v for k, v in data.items()
                                     if not k.startswith('_')}

    def _save_broker_map(self) -> None:
        """Persist position→broker mapping. V7: exclusive fcntl lock + checksum + backup."""
        self._broker_map_sf.write({'map': self._position_broker})

    def seed_position_broker(self, alpaca_tickers: set, tradier_tickers: set) -> None:
        """Seed position→broker mapping from broker open positions on startup.
        Merges with any existing mapping loaded from disk.

        V7.2: Detects dual-broker overlap and logs error. For overlapping
        tickers, Tradier wins (processed second) since reconciliation picks
        the larger-qty broker as authoritative.
        """
        overlap = alpaca_tickers & tradier_tickers
        if overlap:
            log.error(
                "[SmartRouter] DUAL-BROKER OVERLAP detected: %s at both brokers. "
                "Tradier mapping will take precedence. Manual close recommended.",
                overlap)

        with self._lock:
            for t in alpaca_tickers:
                self._position_broker[t] = 'alpaca'
            for t in tradier_tickers:
                self._position_broker[t] = 'tradier'
            self._save_broker_map()
        if alpaca_tickers or tradier_tickers:
            log.info("[SmartRouter] Seeded position_broker: alpaca=%s tradier=%s",
                     alpaca_tickers or '{}', tradier_tickers or '{}')

    def set_registry_gate(self, gate) -> None:
        """V7: Connect RegistryGate so SmartRouter can check blocked orders."""
        self._registry_gate = gate

    def status(self) -> dict:
        """Return router status for monitoring."""
        with self._lock:
            return {
                name: {
                    'available': h.available,
                    'consecutive_failures': h.consecutive_failures,
                    'total_orders': h.total_orders,
                    'total_fills': h.total_fills,
                    'total_failures': h.total_failures,
                }
                for name, h in self._health.items()
            }
