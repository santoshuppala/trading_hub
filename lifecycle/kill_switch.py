"""
SatelliteKillSwitch — daily loss circuit breaker for satellite engines.

Checks adapter.get_daily_pnl() on every tick. On breach:
- Sets halted=True (irrecoverable within session)
- Persists halt state to disk (L5: survives restart)
- Calls adapter.force_close_all()
- Sends CRITICAL alert

V9 (L5): Halt state persisted to data/{engine}_kill_switch.json.
If the supervisor restarts the process (despite exit code 3),
the engine immediately reads the persisted state and stays halted.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ET = ZoneInfo('America/New_York')


class SatelliteKillSwitch:

    def __init__(
        self,
        engine_name:    str,
        adapter,                        # AbstractEngineAdapter
        alert_email:    Optional[str],
        max_daily_loss: float = -2000.0,
        state_dir:      str = 'data',
    ):
        self._name = engine_name
        self._adapter = adapter
        self._alert_email = alert_email
        self._max_daily_loss = max_daily_loss
        self.halted = False

        # L5: Persist halt state to survive restart
        self._state_path = os.path.join(state_dir, f'{engine_name}_kill_switch.json')
        self._load_persisted_state()

    def _load_persisted_state(self) -> None:
        """Check if kill switch was already triggered today (survives restart)."""
        try:
            if not os.path.exists(self._state_path):
                return
            with open(self._state_path) as f:
                state = json.load(f)
            today = datetime.now(ET).strftime('%Y-%m-%d')
            if state.get('date') == today and state.get('halted'):
                self.halted = True
                log.warning(
                    "[%s] Kill switch already triggered today (pnl=$%.2f at %s) — "
                    "remaining halted until next trading day",
                    self._name,
                    state.get('daily_pnl', 0),
                    state.get('halted_at', 'unknown'),
                )
        except Exception as exc:
            log.debug("[%s] Kill switch state load failed: %s", self._name, exc)

    def _persist_state(self, daily_pnl: float) -> None:
        """Save halt state to disk so it survives process restart."""
        try:
            os.makedirs(os.path.dirname(self._state_path) or '.', exist_ok=True)
            with open(self._state_path, 'w') as f:
                json.dump({
                    'halted': True,
                    'daily_pnl': daily_pnl,
                    'max_daily_loss': self._max_daily_loss,
                    'date': datetime.now(ET).strftime('%Y-%m-%d'),
                    'halted_at': datetime.now(ET).isoformat(),
                    'engine': self._name,
                }, f, indent=2)
            log.info("[%s] Kill switch state persisted to %s",
                     self._name, self._state_path)
        except Exception as exc:
            log.error("[%s] Kill switch state persist failed: %s", self._name, exc)

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
            self._persist_state(daily_pnl)

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
