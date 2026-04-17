"""
V8 PerStrategyKillSwitch — tracks realized P&L per strategy and halts
individual strategies without stopping the entire portfolio.

Usage:
    ks = PerStrategyKillSwitch(bus, limits={'vwap': -10000, 'pro': -2000})
    # In RiskEngine/RiskAdapter:
    if ks.is_halted('vwap'):
        block_order()
"""
import logging
import time
from typing import Dict, Optional

from .event_bus import EventBus, EventType, Event
from .events import PositionPayload, PositionAction

log = logging.getLogger(__name__)


class PerStrategyKillSwitch:
    """
    Tracks realized P&L per strategy prefix and halts individual strategies
    when daily loss limit is breached.

    Config: {'vwap': -10000, 'pro': -2000}
    Each strategy is halted independently — VWAP hitting its limit doesn't
    stop Pro from trading.
    """

    def __init__(
        self,
        bus: EventBus,
        limits: Dict[str, float],
        alert_email: Optional[str] = None,
    ):
        self._limits = limits  # {strategy_prefix: max_daily_loss}
        self._pnl: Dict[str, float] = {k: 0.0 for k in limits}
        self._halted: Dict[str, bool] = {k: False for k in limits}
        self._alert_email = alert_email

        bus.subscribe(EventType.POSITION, self._on_position, priority=10)
        log.info("[KillSwitch] initialized | limits=%s", limits)

    def _on_position(self, event: Event) -> None:
        """Track realized P&L on position close."""
        try:
            p: PositionPayload = event.payload
            if p.action != PositionAction.CLOSED or p.pnl is None:
                return

            cd = getattr(p, 'close_detail', {}) or {}
            strategy = cd.get('strategy', 'vwap_reclaim')

            # Determine which prefix this strategy belongs to
            prefix = self._infer_prefix(strategy)
            if prefix not in self._pnl:
                return

            self._pnl[prefix] += float(p.pnl)

            # Check limit
            if not self._halted[prefix] and self._pnl[prefix] <= self._limits[prefix]:
                self._halted[prefix] = True
                msg = (f"KILL SWITCH: strategy '{prefix}' halted — "
                       f"daily P&L ${self._pnl[prefix]:+.2f} breached "
                       f"limit ${self._limits[prefix]:+.2f}")
                log.critical(msg)
                try:
                    from .alerts import send_alert
                    send_alert(self._alert_email, msg, severity='CRITICAL')
                except Exception:
                    pass
        except Exception as exc:
            log.debug("[KillSwitch] _on_position error: %s", exc)

    def is_halted(self, strategy_prefix: str) -> bool:
        """Check if a strategy is halted. Called by RiskEngine/RiskAdapter."""
        return self._halted.get(strategy_prefix, False)

    def status(self) -> dict:
        """Return current kill switch state for monitoring."""
        return {
            prefix: {
                'pnl': round(self._pnl.get(prefix, 0), 2),
                'limit': self._limits.get(prefix, 0),
                'halted': self._halted.get(prefix, False),
            }
            for prefix in self._limits
        }

    def reset_day(self) -> None:
        """Reset all P&L counters and un-halt all strategies."""
        for prefix in self._pnl:
            self._pnl[prefix] = 0.0
            self._halted[prefix] = False
        log.info("[KillSwitch] daily reset — all strategies un-halted")

    @staticmethod
    def _infer_prefix(strategy: str) -> str:
        """Infer strategy prefix from full strategy name."""
        if not strategy:
            return 'vwap'
        parts = strategy.split(':')
        return parts[0] if parts[0] in ('pro', 'pop', 'vwap', 'options') else 'vwap'
