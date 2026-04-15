"""
SatelliteHeartbeat — periodic status logging for satellite engines.

Emits an INFO log every 60 seconds with positions, trades, P&L.
Modeled after Core's HeartbeatEmitter pattern.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

_INTERVAL = 60.0  # seconds


class SatelliteHeartbeat:

    def __init__(self, engine_name: str, adapter):
        self._name = engine_name
        self._adapter = adapter
        self._last_tick: float = 0.0

    def tick(self) -> None:
        """Emit heartbeat if interval has elapsed."""
        now = time.monotonic()
        if now - self._last_tick < _INTERVAL:
            return
        self._last_tick = now

        try:
            stats = self._adapter.get_daily_stats()
            positions = self._adapter.get_positions()
            open_tickers = sorted(positions.keys()) if isinstance(positions, dict) else sorted(positions)

            pnl = stats.get('pnl', 0)
            trades = stats.get('trades', 0)
            wins = stats.get('wins', 0)
            win_pct = (wins / trades * 100) if trades > 0 else 0

            log.info(
                "[%s] [HEARTBEAT] positions=%d open=%s trades=%d wins=%d(%.0f%%) pnl=$%+.2f",
                self._name, len(open_tickers),
                f"({', '.join(open_tickers[:5])})" if open_tickers else "()",
                trades, wins, win_pct, pnl,
            )
        except Exception as exc:
            log.debug("[%s] Heartbeat error: %s", self._name, exc)
