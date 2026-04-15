"""
BacktestBus + SignalCapture — Event system for backtesting.

BacktestBus wraps the real EventBus in SYNC mode (handlers run inline,
no async worker threads). SignalCapture intercepts strategy signals before
broker/executor acts.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from monitor.event_bus import EventBus, EventType, DispatchMode
from monitor.position_registry import registry

log = logging.getLogger(__name__)


@dataclass
class ProSignalPayload:
    """Captured PRO_STRATEGY_SIGNAL event payload."""
    ticker: str
    signal: str  # 'BUY' | 'SELL'
    setup_name: str
    confidence: float
    entry_price: float
    stop_price: float
    target_1: float
    target_2: float
    qty: int
    timestamp: Any  # BarPayload.timestamp


@dataclass
class PopSignalPayload:
    """Captured POP_SIGNAL event payload."""
    ticker: str
    signal: str  # 'BUY' | 'SELL'
    strategy_name: str
    confidence: float
    entry_price: float
    stop_price: float
    target_1: float
    target_2: float
    qty: int
    timestamp: Any


@dataclass
class OptionsSignalPayload:
    """Captured OPTIONS_SIGNAL event payload."""
    ticker: str
    option_type: str  # 'CALL' | 'PUT'
    net_debit: float
    max_risk: float
    max_reward: float
    timestamp: Any


class SignalCapture:
    """
    Intercepts strategy signals before broker/executor acts.

    Priority=999 ensures this runs before any engine subscriber (default ≤ 3).
    Stores all signals in audit trail and routes to FillSimulator via callbacks.
    """

    def __init__(self):
        self.all_signals: deque = deque(maxlen=50_000)  # bounded audit trail: ('pro' | 'pop' | 'options', payload)
        self.fill_simulator: Optional[Any] = None  # set by BacktestEngine after construction

    def on_pro(self, event: Any) -> None:
        """Handle PRO_STRATEGY_SIGNAL."""
        payload = event.payload
        self.all_signals.append(('pro', payload))
        if self.fill_simulator:
            self.fill_simulator.queue_from_pro(payload)

    def on_pop(self, event: Any) -> None:
        """Handle POP_SIGNAL."""
        payload = event.payload
        self.all_signals.append(('pop', payload))
        if self.fill_simulator:
            self.fill_simulator.queue_from_pop(payload)

    def on_options(self, event: Any) -> None:
        """Handle OPTIONS_SIGNAL."""
        payload = event.payload
        self.all_signals.append(('options', payload))
        if self.fill_simulator:
            self.fill_simulator.queue_from_options(payload)


class BacktestBus:
    """
    Real EventBus in SYNC mode for backtesting.

    SYNC mode: emit() calls all handlers inline (no async workers).
    Guarantees bar N's signals are fully processed before bar N+1 starts.

    SignalCapture intercepts strategy signals at priority=999 before
    broker/executor can execute them.

    ORDER_REQ and FILL are swallowed at priority=999 so broker never sees them.
    """

    def __init__(self):
        # Reset global position registry before each backtest run
        registry._positions.clear()

        # Real EventBus in SYNC mode
        self.bus = EventBus(mode=DispatchMode.SYNC)
        self.capture = SignalCapture()

        # Swallow ORDER_REQ and FILL at priority=999 (before any engine subscriber)
        # This prevents AlpacaBroker.execute() from being called
        self.bus.subscribe(
            EventType.ORDER_REQ,
            lambda e: None,
            priority=999,
        )
        self.bus.subscribe(
            EventType.FILL,
            lambda e: None,
            priority=999,
        )

        # Capture signals at priority=999 (before router/executor)
        self.bus.subscribe(
            EventType.PRO_STRATEGY_SIGNAL,
            self.capture.on_pro,
            priority=999,
        )
        self.bus.subscribe(
            EventType.POP_SIGNAL,
            self.capture.on_pop,
            priority=999,
        )
        self.bus.subscribe(
            EventType.OPTIONS_SIGNAL,
            self.capture.on_options,
            priority=999,
        )

        log.info("[BacktestBus] Initialized with SYNC dispatch, signal capture at priority=999")

    def reset_for_session(self) -> None:
        """Reset position registry and signal capture for a new trading session."""
        registry._positions.clear()
        self.capture.all_signals.clear()
        log.debug("[BacktestBus] Reset for new session")
