"""
ProStrategyRouter — subscribes to PRO_STRATEGY_SIGNAL and forwards to
RiskAdapter for final risk validation and ORDER_REQ emission.

The entry/stop/exit levels are already computed by ProSetupEngine and
encoded in the ProStrategySignalPayload.  The router's job is purely to
hand off to the RiskAdapter, which is the pro-setup risk gate.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..risk.risk_adapter import RiskAdapter

from monitor.event_bus import EventBus, EventType, Event

log = logging.getLogger(__name__)


class ProStrategyRouter:
    """
    Thin subscriber that routes PRO_STRATEGY_SIGNAL events to RiskAdapter.

    Registered at ``priority=1`` (same as StrategyEngine subscribers) so it
    runs before lower-priority audit handlers.

    Lifecycle
    ---------
    ProSetupEngine creates both ProStrategyRouter and RiskAdapter and wires
    them together.  Subscriptions are registered here (not in the engine)
    so the router object is self-contained and testable.
    """

    def __init__(self, bus: EventBus, risk_adapter: 'RiskAdapter') -> None:
        self._bus          = bus
        self._risk_adapter = risk_adapter
        bus.subscribe(EventType.PRO_STRATEGY_SIGNAL, self._on_pro_signal, priority=1)
        log.info("[ProStrategyRouter] subscribed to PRO_STRATEGY_SIGNAL")

    def _on_pro_signal(self, event: Event) -> None:
        p = event.payload   # ProStrategySignalPayload

        log.info(
            "[ProStrategyRouter][%s] routing  strategy=%s  tier=%d  dir=%s  "
            "entry=%.4f  stop=%.4f  t1=%.4f  t2=%.4f  conf=%.0f%%",
            p.ticker, p.strategy_name, p.tier, p.direction,
            p.entry_price, p.stop_price, p.target_1, p.target_2,
            p.confidence * 100,
        )

        self._risk_adapter.validate_and_emit(
            ticker        = p.ticker,
            direction     = p.direction,
            entry_price   = p.entry_price,
            stop_price    = p.stop_price,
            target_1      = p.target_1,
            target_2      = p.target_2,
            atr_value     = p.atr_value,
            tier          = p.tier,
            strategy_name = p.strategy_name,
            confidence    = p.confidence,
            source_event  = event,
        )
