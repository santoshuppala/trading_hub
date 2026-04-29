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

        # 6. V10: Backfill ML analytics for missed days (weekends, holidays, crashes).
        # If yesterday's ml_signal_context is empty, populate it now.
        # Ensures no trading day's ML data is permanently lost.
        try:
            self._backfill_ml_analytics()
        except Exception as bf_exc:
            log.warning("[%s] ML backfill check failed (non-fatal): %s", self._name, bf_exc)

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

        # 4. Position reconciliation every 5 min (V10: was 30 min)
        if (now_mono - self._last_reconcile) > 300:
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

        # 2. Close all positions ONLY at EOD (4:00 PM ET), NOT on mid-session restart.
        # A supervisor restart should preserve positions — broker-side stops protect them.
        # Force-close only if we're past market close.
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo('America/New_York'))
        is_eod = now_et.hour >= 16 or (now_et.hour == 15 and now_et.minute >= 50)

        if is_eod:
            positions = self._adapter.get_positions()
            if positions:
                log.info("[%s] EOD: Closing %d open positions",
                         self._name, len(positions))
                self._adapter.force_close_all('eod_shutdown')
        else:
            positions = self._adapter.get_positions()
            if positions:
                log.info("[%s] Mid-session shutdown: PRESERVING %d open positions "
                         "(broker stops protect them)", self._name, len(positions))

        # 3. EOD report — only at actual EOD (not mid-session crashes)
        if is_eod:
            try:
                self._eod.generate()
            except Exception:
                pass
        else:
            log.info("[%s] Mid-session shutdown — skipping EOD report", self._name)

        # 4. Final state save (after any closes)
        self._state.save(self._adapter.get_state())

        # 5. V10: Save market snapshot on EVERY shutdown (not just EOD).
        # Mid-day crash → snapshot has today's bars → faster restart recovery.
        # EOD → snapshot has full day → tomorrow's cold start.
        if self._name == 'core':
            try:
                from monitor.market_snapshot import save_snapshot
                engine = getattr(self._adapter, '_engine', None)
                bars_cache = getattr(engine, '_bars_cache', None) if engine else None
                if bars_cache:
                    save_snapshot(bars_cache)
                else:
                    log.info("[%s] No bars_cache for snapshot", self._name)
            except Exception as snap_exc:
                log.warning("[%s] Snapshot save failed: %s", self._name, snap_exc)

        # 6. V10: ML analytics on EVERY shutdown (crash, restart, EOD).
        # Populates ml_signal_context, ml_trade_outcomes, ml_rejection_log
        # from event_store. On crash at 2 PM → captures data up to 2 PM.
        # Next restart adds more. EOD captures the full day. No data lost.
        try:
            self._run_ml_analytics()
        except Exception as ml_exc:
            log.warning("[%s] ML analytics failed (non-fatal): %s", self._name, ml_exc)

        log.info("[%s] Lifecycle shutdown complete", self._name)

    def _run_ml_analytics(self) -> None:
        """Populate ML tables from today's event_store data.

        Runs on every shutdown (not just EOD). Idempotent — re-running
        for the same day updates counts but doesn't duplicate rows
        (INSERT ON CONFLICT DO NOTHING / upsert pattern in the jobs).
        """
        try:
            from datetime import date
            from scripts.post_session_analytics import (
                job_signal_context, job_trade_outcomes, job_rejection_log,
            )
            import psycopg2
            from config import DATABASE_URL

            conn = psycopg2.connect(DATABASE_URL)
            today = date.today()

            sc = job_signal_context(conn, today)
            to = job_trade_outcomes(conn, today)
            rl = job_rejection_log(conn, today)

            conn.close()
            log.info("[%s] ML analytics: signal_context=%d trade_outcomes=%d "
                     "rejection_log=%d", self._name, sc, to, rl)
        except ImportError:
            log.debug("[%s] ML analytics skipped (post_session_analytics not available)",
                      self._name)
        except Exception as exc:
            log.warning("[%s] ML analytics failed: %s", self._name, exc)

    def _backfill_ml_analytics(self) -> None:
        """On startup, check last 3 trading days for missing ML data. Backfill if needed.

        Catches: weekends, holidays, crashes where shutdown analytics didn't run.
        Only backfills days that have events in event_store but nothing in ml_signal_context.
        """
        try:
            from datetime import date, timedelta
            from scripts.post_session_analytics import (
                job_signal_context, job_trade_outcomes, job_rejection_log,
            )
            import psycopg2
            from config import DATABASE_URL

            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()

            # Check last 3 days (covers weekends: Fri→Mon)
            today = date.today()
            for days_ago in range(1, 4):
                check_date = today - timedelta(days=days_ago)

                # Skip if no events exist for that day (not a trading day)
                cur.execute(
                    "SELECT COUNT(*) FROM event_store "
                    "WHERE event_time::date = %s AND event_type = 'StrategySignal'",
                    (check_date,))
                event_count = cur.fetchone()[0]
                if event_count == 0:
                    continue  # no signals that day — skip

                # Check if ML data already exists
                cur.execute(
                    "SELECT COUNT(*) FROM ml_signal_context WHERE created_at::date = %s",
                    (check_date,))
                ml_count = cur.fetchone()[0]

                if ml_count < event_count * 0.5:  # less than 50% coverage → backfill
                    log.info("[%s] ML backfill: %s has %d signals but only %d in "
                             "ml_signal_context — backfilling",
                             self._name, check_date, event_count, ml_count)
                    job_signal_context(conn, check_date)
                    job_trade_outcomes(conn, check_date)
                    job_rejection_log(conn, check_date)

            conn.close()
        except ImportError:
            pass  # post_session_analytics not available
        except Exception as exc:
            log.warning("[%s] ML backfill failed: %s", self._name, exc)
