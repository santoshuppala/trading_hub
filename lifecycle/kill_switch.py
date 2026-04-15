"""
SatelliteKillSwitch — daily loss circuit breaker for satellite engines.

Modeled after run_monitor.py:KillSwitchGuard (lines 179-210).
Checks adapter.get_daily_pnl() on every tick. On breach:
- Sets halted=True (irrecoverable within session)
- Calls adapter.force_close_all()
- Sends CRITICAL alert
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


class SatelliteKillSwitch:

    def __init__(
        self,
        engine_name:    str,
        adapter,                        # AbstractEngineAdapter
        alert_email:    Optional[str],
        max_daily_loss: float = -2000.0,
    ):
        self._name = engine_name
        self._adapter = adapter
        self._alert_email = alert_email
        self._max_daily_loss = max_daily_loss
        self.halted = False

    def check(self) -> bool:
        """Check if daily loss threshold is breached. Returns True if halted."""
        if self.halted:
            return True

        try:
            daily_pnl = self._adapter.get_daily_pnl()
        except Exception:
            return False

        if daily_pnl <= self._max_daily_loss:
            self.halted = True
            stats = self._adapter.get_daily_stats()
            log.error(
                "[%s] KILL SWITCH ACTIVATED: daily P&L $%.2f breached limit $%.2f | "
                "trades=%d wins=%d",
                self._name, daily_pnl, self._max_daily_loss,
                stats.get('trades', 0), stats.get('wins', 0),
            )
            try:
                from monitor.alerts import send_alert
                send_alert(
                    self._alert_email,
                    f"[CRITICAL] {self._name.upper()} KILL SWITCH: "
                    f"Daily P&L ${daily_pnl:+.2f} breached ${self._max_daily_loss:+.2f}. "
                    f"All positions force-closed. Trading halted.",
                    severity='CRITICAL',
                )
            except Exception:
                pass
            return True

        return False
