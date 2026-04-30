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

        # 6. V10: Persist today's bars to market_bars DB (for fast ML queries).
        # bars_cache in memory → market_bars table. Same data as snapshot but
        # in DB format for SQL analytics. ON CONFLICT DO NOTHING = safe on restart.
        if self._name == 'core':
            try:
                engine = getattr(self._adapter, '_engine', None)
                bars_cache = getattr(engine, '_bars_cache', None) if engine else None
                if bars_cache:
                    self._persist_market_bars(bars_cache)
            except Exception as mb_exc:
                log.warning("[%s] market_bars persist failed (non-fatal): %s",
                            self._name, mb_exc)

        # 7. V10: Generate daily trade analysis CSV (for Streamlit dashboard)
        if is_eod and self._name == 'core':
            try:
                self._generate_daily_report()
            except Exception as rpt_exc:
                log.warning("[%s] Daily report generation failed (non-fatal): %s",
                            self._name, rpt_exc)

        # 8. V10: DB retention — delete QuoteReceived older than 3 days.
        # Ticks are 99% of event_store (16GB). After 3 days, ticks are useless
        # (1-min bars in market_bars replace them for ML/backtest).
        # Signals, fills, positions kept forever (tiny).
        # Only runs at EOD (not on mid-session crash — don't slow restart).
        if is_eod and self._name == 'core':
            try:
                self._run_db_retention()
            except Exception as ret_exc:
                log.warning("[%s] DB retention failed (non-fatal): %s",
                            self._name, ret_exc)

        # 9. V10: ML analytics on EVERY shutdown (crash, restart, EOD).
        # Populates ml_signal_context, ml_trade_outcomes, ml_rejection_log
        # from event_store. On crash at 2 PM → captures data up to 2 PM.
        # Next restart adds more. EOD captures the full day. No data lost.
        try:
            self._run_ml_analytics()
        except Exception as ml_exc:
            log.warning("[%s] ML analytics failed (non-fatal): %s", self._name, ml_exc)

        log.info("[%s] Lifecycle shutdown complete", self._name)

    def _persist_market_bars(self, bars_cache: dict) -> None:
        """Write today's bar data from bars_cache to market_bars DB table.

        Uses INSERT ... ON CONFLICT DO NOTHING (safe on crash + restart).
        Each shutdown writes whatever bars exist — next restart adds more.
        Full day captured by EOD shutdown.
        """
        try:
            import psycopg2
            from config import DATABASE_URL
            from datetime import date

            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            today = date.today()
            rows_written = 0

            for ticker, df in bars_cache.items():
                if df is None or df.empty:
                    continue
                try:
                    for idx, row in df.iterrows():
                        # Extract bar time from index or compute from position
                        bar_time = idx if hasattr(idx, 'strftime') else None
                        if bar_time is None:
                            continue

                        cur.execute(
                            "INSERT INTO market_bars (ticker, bar_time, open, high, low, close, volume, bar_interval) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT DO NOTHING",
                            (ticker, bar_time,
                             float(row.get('open', row.get('o', 0))),
                             float(row.get('high', row.get('h', 0))),
                             float(row.get('low', row.get('l', 0))),
                             float(row.get('close', row.get('c', 0))),
                             int(row.get('volume', row.get('v', 0))),
                             '1min')
                        )
                        rows_written += 1
                except Exception:
                    continue

            conn.commit()
            conn.close()
            log.info("[%s] market_bars: persisted %d bars for %d tickers",
                     self._name, rows_written, len(bars_cache))
        except ImportError:
            pass
        except Exception as exc:
            log.warning("[%s] market_bars persist failed: %s", self._name, exc)

    def _generate_daily_report(self) -> None:
        """Generate daily trade analysis CSV at EOD.

        Format matches reports/daily_analysis/trade_analysis_YYYYMMDD.csv
        consumed by Streamlit dashboard (dashboards/trade_analysis_dashboard.py).
        """
        import csv
        import os
        from datetime import date

        engine = getattr(self._adapter, '_engine', None)
        trade_log = getattr(engine, 'trade_log', []) if engine else []

        # Fallback: if in-memory trade_log is empty (lost on restart),
        # read from completed_trades DB (always has the data).
        if not trade_log:
            try:
                import psycopg2
                from config import DATABASE_URL
                conn = psycopg2.connect(DATABASE_URL)
                cur = conn.cursor()
                cur.execute("""
                    SELECT ticker, qty, entry_time, entry_price, exit_price,
                           pnl, strategy, lifecycle_data
                    FROM completed_trades
                    WHERE exit_time::date = CURRENT_DATE
                    ORDER BY exit_time
                """)
                for r in cur.fetchall():
                    import json as _j
                    _lc = {}
                    if r[7]:
                        try:
                            _lc = _j.loads(r[7]) if isinstance(r[7], str) else r[7]
                        except Exception:
                            pass
                    trade_log.append({
                        'ticker': r[0], 'qty': r[1], 'entry_time': str(r[2] or ''),
                        'entry_price': float(r[3] or 0), 'exit_price': float(r[4] or 0),
                        'pnl': float(r[5] or 0), 'strategy': r[6] or '',
                        'reason': _lc.get('exit_reason', ''),
                        'lifecycle': _lc,
                    })
                conn.close()
                log.info("[%s] Loaded %d trades from DB for daily report",
                         self._name, len(trade_log))
            except Exception as db_exc:
                log.warning("[%s] DB trade fetch failed: %s", self._name, db_exc)

        if not trade_log:
            log.info("[%s] No trades today — skipping daily report", self._name)
            return

        today_str = date.today().strftime('%Y%m%d')
        report_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'reports', 'daily_analysis')
        os.makedirs(report_dir, exist_ok=True)
        path = os.path.join(report_dir, f'trade_analysis_{today_str}.csv')

        # CSV columns matching existing format + V10 attribution
        headers = [
            'date', 'ticker', 'qty', 'entry_time', 'entry_price', 'exit_price',
            'pnl', 'entry_reason', 'strategy', 'exit_reason', 'exit_category',
            'exit_phase', 'exit_phase_label', 'max_phase_reached', 'phase0_passed',
            'partial_done', 'r_multiple_at_exit', 'trail_stop', 'bars_held',
            'is_win', 'is_loss', 'is_breakeven',
            # V10: Alpha/Beta attribution
            'spy_return', 'intraday_beta', 'beta_pnl', 'alpha_pnl',
            'net_alpha_pnl', 'slippage_cost', 'session_phase', 'regime_trend',
            'alpha_significant',
        ]

        phase_labels = {0: 'P0 Validation', 1: 'P1 Protection', 2: 'P2 Breakeven',
                        3: 'P3 Harvest', 4: 'P4 Runner', -1: 'No Lifecycle'}

        # Categorize exit reasons
        def _exit_category(reason):
            r = str(reason or '').upper()
            if 'TARGET' in r or 'PARTIAL' in r:
                return 'Target Hit'
            elif 'STOP' in r:
                return 'Stop Loss'
            elif 'VWAP' in r:
                return 'VWAP Exit'
            elif 'RSI' in r:
                return 'RSI Exit'
            elif 'PHASE0' in r:
                return 'Phase 0 Fail'
            elif 'EOD' in r or 'FORCE' in r:
                return 'EOD Close'
            elif 'PHANTOM' in r or 'RECONCIL' in r:
                return 'Phantom/Reconcile'
            else:
                return 'Other'

        today_date = date.today().strftime('%Y-%m-%d')
        rows = []
        for t in trade_log:
            pnl = float(t.get('pnl', 0) or 0)
            lc = t.get('lifecycle', {})
            reason = t.get('reason', '')

            phase = lc.get('final_phase', -1) if lc else -1
            max_phase = lc.get('max_phase', phase) if lc else phase
            bars = lc.get('bars_held', 0) if lc else 0
            trail = lc.get('trail_stop', 0) if lc else 0
            partial = lc.get('partial_done', False) if lc else False
            p0_passed = (max_phase >= 1) if max_phase >= 0 else False

            # R-multiple at exit
            entry = float(t.get('entry_price', 0) or 0)
            exit_p = float(t.get('exit_price', 0) or 0)
            r_val = lc.get('R', 0) if lc else 0
            r_mult = round(pnl / r_val, 2) if r_val and r_val > 0 else 0

            rows.append({
                'date': today_date,
                'ticker': t.get('ticker', ''),
                'qty': t.get('qty', 0),
                'entry_time': t.get('entry_time', ''),
                'entry_price': entry,
                'exit_price': exit_p,
                'pnl': round(pnl, 2),
                'entry_reason': t.get('strategy', ''),
                'strategy': t.get('strategy', ''),
                'exit_reason': reason,
                'exit_category': _exit_category(reason),
                'exit_phase': phase,
                'exit_phase_label': phase_labels.get(phase, 'Unknown'),
                'max_phase_reached': max_phase,
                'phase0_passed': p0_passed,
                'partial_done': partial,
                'r_multiple_at_exit': r_mult,
                'trail_stop': round(trail, 4) if trail else 0,
                'bars_held': bars,
                'is_win': pnl > 0,
                'is_loss': pnl < 0,
                'is_breakeven': pnl == 0,
            })

        # V10: Merge attribution data from ml_pnl_attribution (if available)
        try:
            import psycopg2
            import psycopg2.extras
            from config import DATABASE_URL
            _aconn = psycopg2.connect(DATABASE_URL,
                                      cursor_factory=psycopg2.extras.RealDictCursor)
            _acur = _aconn.cursor()
            _acur.execute("""
                SELECT trade_id, ticker, spy_return, intraday_beta, beta_pnl, alpha_pnl,
                       net_alpha_pnl, slippage_cost, session_phase, regime_trend,
                       alpha_p_value
                FROM trading.ml_pnl_attribution
                WHERE session_date = CURRENT_DATE
            """)
            _attr_rows = _acur.fetchall()
            _aconn.close()

            # Build lookup by ticker (may have multiple trades per ticker)
            from collections import defaultdict
            _attr_by_ticker = defaultdict(list)
            for r in _attr_rows:
                _attr_by_ticker[r['ticker']].append(r)

            for row in rows:
                ticker = row.get('ticker', '')
                candidates = _attr_by_ticker.get(ticker, [])
                attr = None
                if len(candidates) == 1:
                    attr = candidates[0]
                elif len(candidates) > 1:
                    # Multiple trades for same ticker — match by closest entry_time
                    row_entry = row.get('entry_time', '')
                    for c in candidates:
                        if c.get('entry_time') and row_entry and str(row_entry)[:8] in str(c['entry_time']):
                            attr = c
                            break
                    if not attr:
                        attr = candidates[0]  # fallback to first
                if attr:
                    row['spy_return'] = round(float(attr['spy_return'] or 0), 6)
                    row['intraday_beta'] = round(float(attr['intraday_beta'] or 0), 4)
                    row['beta_pnl'] = round(float(attr['beta_pnl'] or 0), 2)
                    row['alpha_pnl'] = round(float(attr['alpha_pnl'] or 0), 2)
                    row['net_alpha_pnl'] = round(float(attr['net_alpha_pnl'] or 0), 2)
                    row['slippage_cost'] = round(float(attr['slippage_cost'] or 0), 2)
                    row['session_phase'] = attr['session_phase'] or ''
                    row['regime_trend'] = round(float(attr['regime_trend'] or 0), 4)
                    p = attr.get('alpha_p_value')
                    if p is not None:
                        row['alpha_significant'] = 'YES' if float(p) < 0.05 else 'NO'
                    else:
                        row['alpha_significant'] = 'ACCUMULATING'
                else:
                    for h in ['spy_return', 'intraday_beta', 'beta_pnl', 'alpha_pnl',
                              'net_alpha_pnl', 'slippage_cost', 'session_phase',
                              'regime_trend', 'alpha_significant']:
                        row.setdefault(h, '')
        except Exception as attr_exc:
            log.debug("[%s] Attribution merge skipped: %s", self._name, attr_exc)
            for row in rows:
                for h in ['spy_return', 'intraday_beta', 'beta_pnl', 'alpha_pnl',
                          'net_alpha_pnl', 'slippage_cost', 'session_phase',
                          'regime_trend', 'alpha_significant']:
                    row.setdefault(h, '')

        with open(path, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)

        log.info("[%s] Daily report: %d trades → %s", self._name, len(rows), path)

    def _run_db_retention(self) -> None:
        """Delete QuoteReceived events older than 3 days.

        QuoteReceived = 2.7M rows/day = ~2-3 GB/day = 99% of event_store.
        After 3 days, raw ticks are useless (market_bars has 1-min OHLCV).
        Signals, fills, positions, risk blocks kept forever (tiny).

        Also cleans old log files (> 7 days).
        """
        try:
            import psycopg2
            from config import DATABASE_URL

            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()

            # Delete QuoteReceived older than 3 days
            cur.execute(
                "DELETE FROM event_store "
                "WHERE event_type = 'QuoteReceived' "
                "AND event_time < CURRENT_DATE - INTERVAL '3 days'"
            )
            deleted_quotes = cur.rowcount

            # Also clean BarReceived older than 7 days (market_bars replaces these)
            cur.execute(
                "DELETE FROM event_store "
                "WHERE event_type = 'BarReceived' "
                "AND event_time < CURRENT_DATE - INTERVAL '7 days'"
            )
            deleted_bars = cur.rowcount

            # Also clean HeartbeatEmitted older than 3 days
            cur.execute(
                "DELETE FROM event_store "
                "WHERE event_type = 'HeartbeatEmitted' "
                "AND event_time < CURRENT_DATE - INTERVAL '3 days'"
            )
            deleted_hb = cur.rowcount

            conn.commit()
            conn.close()

            log.info("[%s] DB retention: deleted %d QuoteReceived, %d BarReceived, "
                     "%d HeartbeatEmitted (older than 3/7/3 days)",
                     self._name, deleted_quotes, deleted_bars, deleted_hb)
        except ImportError:
            pass
        except Exception as exc:
            log.warning("[%s] DB retention failed: %s", self._name, exc)

        # Clean old log files (> 7 days)
        try:
            import shutil
            from datetime import date, timedelta
            log_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
            cutoff = (date.today() - timedelta(days=7)).strftime('%Y%m%d')
            for d in os.listdir(log_dir):
                if d.isdigit() and len(d) == 8 and d < cutoff:
                    path = os.path.join(log_dir, d)
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                        log.info("[%s] Cleaned old log dir: %s", self._name, d)
        except Exception as exc:
            log.warning("[%s] Log cleanup failed: %s", self._name, exc)

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
                job_pnl_attribution,
            )
            import psycopg2
            import psycopg2.extras
            from config import DATABASE_URL

            conn = psycopg2.connect(DATABASE_URL,
                                    cursor_factory=psycopg2.extras.RealDictCursor)
            today = date.today()

            sc = job_signal_context(conn, today)
            to = job_trade_outcomes(conn, today)
            rl = job_rejection_log(conn, today)
            pa = job_pnl_attribution(conn, today)

            conn.close()
            log.info("[%s] ML analytics: signal_context=%d trade_outcomes=%d "
                     "rejection_log=%d pnl_attribution=%d",
                     self._name, sc, to, rl, pa)
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
                job_pnl_attribution,
            )
            import psycopg2
            import psycopg2.extras
            from config import DATABASE_URL

            conn = psycopg2.connect(DATABASE_URL,
                                    cursor_factory=psycopg2.extras.RealDictCursor)
            cur = conn.cursor()

            # Check last 3 days (covers weekends: Fri→Mon)
            today = date.today()
            for days_ago in range(1, 4):
                check_date = today - timedelta(days=days_ago)

                # Skip if no events exist for that day (not a trading day)
                cur.execute(
                    "SELECT COUNT(*) as cnt FROM event_store "
                    "WHERE event_time::date = %s AND event_type = 'StrategySignal'",
                    (check_date,))
                event_count = cur.fetchone()['cnt']
                if event_count == 0:
                    continue  # no signals that day — skip

                # Check if ML data already exists
                cur.execute(
                    "SELECT COUNT(*) as cnt FROM ml_signal_context WHERE created_at::date = %s",
                    (check_date,))
                ml_count = cur.fetchone()['cnt']

                if ml_count < event_count * 0.5:  # less than 50% coverage → backfill
                    log.info("[%s] ML backfill: %s has %d signals but only %d in "
                             "ml_signal_context — backfilling",
                             self._name, check_date, event_count, ml_count)
                    job_signal_context(conn, check_date)
                    job_trade_outcomes(conn, check_date)
                    job_rejection_log(conn, check_date)
                    job_pnl_attribution(conn, check_date)

            conn.close()
        except ImportError:
            pass  # post_session_analytics not available
        except Exception as exc:
            log.warning("[%s] ML backfill failed: %s", self._name, exc)
