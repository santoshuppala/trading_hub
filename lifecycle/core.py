"""
EngineLifecycle — orchestrates all lifecycle features for satellite engines.

Provides three hooks for run scripts:
    lifecycle.startup()   — before main loop
    lifecycle.tick()      — every loop iteration (10s)
    lifecycle.shutdown()  — in finally block
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .adapters.base import AbstractEngineAdapter
from .state_persistence import AtomicStateFile
from .kill_switch import SatelliteKillSwitch
from .heartbeat import SatelliteHeartbeat
from .eod_report import SatelliteEODReport
from .reconciler import PositionReconciler

log = logging.getLogger(__name__)

ET = ZoneInfo('America/New_York')


class EngineLifecycle:
    """
    Production lifecycle manager for satellite trading engines.

    Replicates Core engine's lifecycle features:
    - State persistence (crash recovery)
    - Kill switch (daily loss halt)
    - Heartbeat (60s status log)
    - EOD report (structured summary + email)
    - Position reconciliation (broker sync)
    - Preflight checks (cancel stale orders, verify connectivity)

    Usage:
        lifecycle = EngineLifecycle('options', adapter, bus, email, max_loss)
        lifecycle.startup()
        while running:
            if not lifecycle.tick():
                break
        lifecycle.shutdown()
    """

    def __init__(
        self,
        engine_name:    str,
        adapter:        AbstractEngineAdapter,
        bus,                                    # EventBus
        alert_email:    Optional[str] = None,
        max_daily_loss: float = -2000.0,
        state_dir:      str = 'data',
    ):
        self._name = engine_name
        self._adapter = adapter
        self._bus = bus
        self._alert_email = alert_email

        # Components
        self._state = AtomicStateFile(engine_name, state_dir)
        self._kill_switch = SatelliteKillSwitch(
            engine_name, adapter, alert_email, max_daily_loss,
        )
        self._heartbeat = SatelliteHeartbeat(engine_name, adapter)
        self._eod = SatelliteEODReport(engine_name, adapter, alert_email)
        self._reconciler = PositionReconciler(engine_name, adapter)

        # Hourly tracking
        self._last_hourly_pnl: float = 0.0
        self._last_reconcile: float = 0.0

        log.info("[%s] EngineLifecycle initialized | max_daily_loss=$%.0f",
                 engine_name, max_daily_loss)

    # ── startup() — called once before main loop ───────────────────────

    def startup(self) -> None:
        """Preflight checks, restore state, reconcile with broker."""
        log.info("[%s] ============================================================",
                 self._name)
        log.info("[%s] LIFECYCLE STARTUP", self._name)
        log.info("[%s] ============================================================",
                 self._name)

        # 1. Verify broker connectivity
        if self._adapter.verify_connectivity():
            log.info("[%s] Broker connectivity: OK", self._name)
        else:
            log.warning("[%s] Broker connectivity: FAILED — trading may be impacted",
                        self._name)

        # 2. Cancel stale orders from previous session
        cancelled = self._adapter.cancel_stale_orders()
        if cancelled:
            log.info("[%s] Cancelled %d stale orders from previous session",
                     self._name, cancelled)

        # 3. Restore state from disk
        saved = self._state.load()
        if saved:
            self._adapter.restore_state(saved)
            positions = self._adapter.get_positions()
            log.info("[%s] Restored %d positions from state file",
                     self._name, len(positions))

        # 4. Reconcile with broker (broker is source of truth)
        self._reconciler.sync_startup()

        # 5. Save reconciled state
        self._state.save(self._adapter.get_state())

        log.info("[%s] Lifecycle startup complete", self._name)

    # ── tick() — called every loop iteration (10s) ─────────────────────

    def tick(self) -> bool:
        """Run periodic lifecycle checks. Returns False if engine should stop.

        Checks (in order):
        1. Kill switch (every tick)
        2. Heartbeat (every 60s)
        3. Hourly P&L summary (at minute == 0)
        4. Position reconciliation (at minute == 30)
        5. State persistence (every tick, skips if unchanged)
        """
        # 1. Kill switch — most critical, check first
        if self._kill_switch.check():
            log.error("[%s] KILL SWITCH TRIGGERED — stopping engine", self._name)
            self._adapter.force_close_all('kill_switch_daily_loss')
            self._state.save(self._adapter.get_state())
            return False

        now = datetime.now(ET)
        now_mono = time.monotonic()

        # 2. Heartbeat (every 60s)
        self._heartbeat.tick()

        # 3. Hourly P&L summary (at minute == 0, max once per hour)
        if now.minute == 0 and (now_mono - self._last_hourly_pnl) > 3500:
            self._last_hourly_pnl = now_mono
            stats = self._adapter.get_daily_stats()
            log.info(
                "[%s] HOURLY | trades=%d wins=%d pnl=$%.2f positions=%d",
                self._name,
                stats.get('trades', 0),
                stats.get('wins', 0),
                stats.get('pnl', 0),
                stats.get('open_positions', 0),
            )

        # 4. Position reconciliation (at minute == 30, max once per hour)
        if now.minute == 30 and (now_mono - self._last_reconcile) > 3500:
            self._last_reconcile = now_mono
            self._reconciler.sync_periodic()

        # 5. State persistence (every tick, skips if unchanged)
        self._state.save(self._adapter.get_state())

        return True

    # ── shutdown() — called in finally block ───────────────────────────

    def shutdown(self) -> None:
        """EOD report, final state save, close positions if needed."""
        log.info("[%s] ============================================================",
                 self._name)
        log.info("[%s] LIFECYCLE SHUTDOWN", self._name)
        log.info("[%s] ============================================================",
                 self._name)

        # 1. Final state save
        self._state.save(self._adapter.get_state())

        # 2. Close all positions (options must be closed at EOD)
        positions = self._adapter.get_positions()
        if positions:
            log.info("[%s] Closing %d open positions at shutdown",
                     self._name, len(positions))
            self._adapter.force_close_all('eod_shutdown')

        # 3. EOD report
        self._eod.generate()

        # 4. Final state save (after closes)
        self._state.save(self._adapter.get_state())

        log.info("[%s] Lifecycle shutdown complete", self._name)
