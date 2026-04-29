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
import threading
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

    # V10: Per-broker loss limit (prevents asymmetric loss when one broker fails)
    _DEFAULT_BROKER_LOSS_LIMIT = -10_000.0

    def __init__(
        self,
        bus: EventBus,
        limits: Dict[str, float],
        alert_email: Optional[str] = None,
        broker_loss_limit: float = _DEFAULT_BROKER_LOSS_LIMIT,
    ):
        self._limits = limits  # {strategy_prefix: max_daily_loss}
        self._pnl: Dict[str, float] = {k: 0.0 for k in limits}
        self._halted: Dict[str, bool] = {k: False for k in limits}
        self._lock = threading.Lock()  # protects _pnl + _halted atomically
        self._alert_email = alert_email

        # V10: Per-broker P&L tracking
        self._broker_loss_limit = broker_loss_limit
        self._broker_pnl: Dict[str, float] = {}  # {broker_name: cumulative_pnl}
        self._broker_halted: Dict[str, bool] = {}

        bus.subscribe(EventType.POSITION, self._on_position, priority=10)
        log.info("[KillSwitch] initialized | limits=%s | broker_limit=$%.0f",
                 limits, broker_loss_limit)

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

            # Atomic: update P&L + check limits under single lock
            broker = cd.get('broker', getattr(event, '_routed_broker', None) or 'unknown')
            alerts = []
            with self._lock:
                # Per-strategy P&L
                self._pnl[prefix] += float(p.pnl)
                if not self._halted[prefix] and self._pnl[prefix] <= self._limits[prefix]:
                    self._halted[prefix] = True
                    alerts.append(
                        f"KILL SWITCH: strategy '{prefix}' halted — "
                        f"daily P&L ${self._pnl[prefix]:+.2f} breached "
                        f"limit ${self._limits[prefix]:+.2f}")

                # V10: Per-broker P&L
                if broker and broker != 'unknown':
                    self._broker_pnl.setdefault(broker, 0.0)
                    self._broker_pnl[broker] += float(p.pnl)
                    if (not self._broker_halted.get(broker, False)
                            and self._broker_pnl[broker] <= self._broker_loss_limit):
                        self._broker_halted[broker] = True
                        alerts.append(
                            f"KILL SWITCH: broker '{broker}' halted — "
                            f"daily P&L ${self._broker_pnl[broker]:+.2f} breached "
                            f"limit ${self._broker_loss_limit:+.2f}")

            for msg in alerts:
                log.critical(msg)
                try:
                    from .alerts import send_alert
                    send_alert(self._alert_email, msg, severity='CRITICAL')
                except Exception:
                    pass
        except Exception as exc:
            log.warning("[KillSwitch] _on_position error: %s", exc)

    def is_halted(self, strategy_prefix: str, broker: str = '') -> bool:
        """Check if a strategy or broker is halted. Called by RiskEngine/RiskAdapter."""
        with self._lock:
            if self._halted.get(strategy_prefix, False):
                return True
            if broker and self._broker_halted.get(broker, False):
                return True
            return False

    def status(self) -> dict:
        """Return current kill switch state for monitoring."""
        with self._lock:
            strategies = {
                prefix: {
                    'pnl': round(self._pnl.get(prefix, 0), 2),
                    'limit': self._limits.get(prefix, 0),
                    'halted': self._halted.get(prefix, False),
                }
                for prefix in self._limits
            }
            brokers = {
                broker: {
                    'pnl': round(pnl, 2),
                    'limit': self._broker_loss_limit,
                    'halted': self._broker_halted.get(broker, False),
                }
                for broker, pnl in self._broker_pnl.items()
            }
            return {'strategies': strategies, 'brokers': brokers}

    def reset_day(self) -> None:
        """Reset all P&L counters and un-halt all strategies + brokers."""
        with self._lock:
            for prefix in self._pnl:
                self._pnl[prefix] = 0.0
                self._halted[prefix] = False
            self._broker_pnl.clear()
            self._broker_halted.clear()
        log.info("[KillSwitch] daily reset — all strategies + brokers un-halted")

    def seed_pnl(self, strategy_prefix: str, pnl: float) -> None:
        """Seed P&L from FillLedger on startup (crash recovery).

        Without this, a crash at -$8K + restart resets P&L to $0 and the
        system can lose another full daily limit before kill switch fires.
        FillLedger persists realized P&L across restarts — seed from it.
        """
        with self._lock:
            if strategy_prefix in self._pnl:
                self._pnl[strategy_prefix] = pnl
                # Check if already breached
                if pnl <= self._limits.get(strategy_prefix, 0):
                    self._halted[strategy_prefix] = True
                    log.critical("[KillSwitch] SEEDED '%s' at $%.2f — ALREADY BREACHED "
                                 "limit $%.2f — halted immediately",
                                 strategy_prefix, pnl,
                                 self._limits[strategy_prefix])
                else:
                    log.info("[KillSwitch] seeded '%s' P&L=$%.2f (limit=$%.2f, "
                             "headroom=$%.2f)",
                             strategy_prefix, pnl,
                             self._limits[strategy_prefix],
                             pnl - self._limits[strategy_prefix])

    @staticmethod
    def _infer_prefix(strategy: str) -> str:
        """Infer strategy prefix from full strategy name."""
        if not strategy:
            return 'vwap'
        parts = strategy.split(':')
        return parts[0] if parts[0] in ('pro', 'pop', 'vwap', 'options') else 'vwap'
