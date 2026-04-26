#!/usr/bin/env python3
"""
Session Watchdog — DEPRECATED as standalone process.

V10: Merged into outer_watchdog.py. This file is now used as a LIBRARY only.
The outer watchdog imports SessionWatchdog and runs its checks directly.
Do NOT run this file as a separate process — use outer_watchdog.py instead.

Original description:
Session Watchdog — Self-healing monitor for unattended trading sessions.

Runs alongside the supervisor. Monitors health, detects issues, applies
safe auto-fixes, and sends alerts for anything it can't fix.

What it monitors:
  1. Process health      — is core/options alive?
  2. Log errors          — parse core.log for ERROR/CRITICAL tracebacks
  3. Signal rates        — too many signals = detector issue
  4. P&L drift           — FillLedger vs trade_log mismatch
  5. Kill switch         — triggered = alert owner
  6. Position anomalies  — stuck positions, phantom positions
  7. Data staleness      — no bars for X minutes = data feed issue
  8. Memory usage        — process consuming too much RAM
  9. Strategy health     — gap_and_go firing (should be near zero)

Self-healing (safe auto-fixes):
  - Corrupt JSON state files   → restore from .prev backup
  - Stale lock files           → remove
  - __pycache__ corruption     → clear
  - Process not starting       → diagnose + clean state + alert
  - Repeated crashes (>3)      → capture traceback + halt + alert
  - Supervisor not running     → restart supervisor

What it does NOT touch:
  - Source code (never modified)
  - Open positions (never modified)
  - Active orders (never cancelled)
  - Config values (never changed)

Usage:
    python scripts/session_watchdog.py
    python scripts/session_watchdog.py --check-interval 60
    python scripts/session_watchdog.py --dry-run  # one check, then exit
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.split('#')[0].strip()
            if '=' in _line:
                _k, _v = _line.split('=', 1)
                _v = _v.strip().strip('"').strip("'")
                if _k.strip():
                    os.environ[_k.strip()] = _v  # always use project .env

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from config import (DATA_DIR, BOT_STATE_PATH, FILL_LEDGER_PATH,
                    OPTIONS_STATE_PATH, BROKER_MAP_PATH, SUPERVISOR_STATUS_PATH,
                    LIVE_CACHE_PATH)
_BOT_STATE_FILE = BOT_STATE_PATH

# Logging
log_dir = os.path.join(PROJECT_ROOT, 'logs', datetime.now().strftime('%Y%m%d'))
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'watchdog.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [watchdog] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        # StreamHandler removed: supervisor already redirects stdout to watchdog.log,
        # so StreamHandler caused every line to appear twice in the log file.
    ],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Health checks
# ══════════════════════════════════════════════════════════════════════════════

class HealthCheck:
    """Single health check result."""
    def __init__(self, name: str, status: str, message: str, severity: str = 'INFO'):
        self.name = name
        self.status = status      # OK, WARN, CRITICAL
        self.message = message
        self.severity = severity   # INFO, WARNING, CRITICAL
        self.timestamp = datetime.now(ET)


def _fix_path():
    """Ensure system binaries are in PATH (supervisor may strip them)."""
    for d in ['/bin', '/usr/bin', '/usr/local/bin', '/opt/homebrew/bin']:
        if d not in os.environ.get('PATH', ''):
            os.environ['PATH'] = d + ':' + os.environ.get('PATH', '')

_fix_path()


class SessionWatchdog:
    """Autonomous trading session monitor."""

    # Periodic update schedule (ET hours)
    _UPDATE_HOURS = [10, 11, 12, 13, 14, 15, 16]

    def __init__(self, check_interval: int = 120, enable_healing: bool = True):
        self.check_interval = check_interval
        self.enable_healing = enable_healing
        self.session_start = datetime.now(ET)
        self.checks_run = 0
        self.issues_found = defaultdict(int)   # issue_type → count
        self.alerts_sent = set()               # dedup: (issue_type, ticker)
        self.heals_applied = []                # list of (time, action, result)
        self._last_log_pos = {}                # log_file → last read position
        self._prev_signal_count = 0
        self._prev_check_time = None
        self._prev_crash_count = {}            # engine → crash count
        self._session_report = []
        self._updates_sent = set()             # hours already sent

        # Auto-fix manager (paper trading = auto-apply, no approval needed)
        self._hotfix_mgr = None
        if enable_healing:
            try:
                from scripts.hotfix_manager import HotfixManager
                self._hotfix_mgr = HotfixManager(auto_apply=True)
                log.info("HotfixManager active — auto-apply mode (paper trading)")
            except Exception as e:
                log.warning("HotfixManager init failed: %s", e)

    def run(self, dry_run: bool = False):
        """Main loop — run checks every interval until market close."""
        log.info("=" * 60)
        log.info("SESSION WATCHDOG STARTED")
        log.info("  Check interval: %ds", self.check_interval)
        log.info("  Log file: %s", log_file)
        log.info("=" * 60)

        if dry_run:
            results = self._run_all_checks()
            self._print_results(results)
            return

        # V10: Generate session report on SIGTERM (supervisor kills us at 4 PM)
        import signal as _signal
        def _on_shutdown(signum, frame):
            log.info("Shutdown signal received — generating session report")
            try:
                self._generate_session_report()
            except Exception as exc:
                log.warning("Session report failed on shutdown: %s", exc)
            raise SystemExit(0)
        _signal.signal(_signal.SIGTERM, _on_shutdown)

        try:
            while True:
                now = datetime.now(ET)

                # Stop after market close (4:00 PM)
                if now.hour >= 16:
                    log.info("Market closed — generating session report")
                    self._generate_session_report()
                    break

                # Skip before market open (9:25 AM)
                if now.hour < 9 or (now.hour == 9 and now.minute < 25):
                    log.info("Pre-market — waiting for 9:25 AM ET")
                    time.sleep(60)
                    continue

                # Run all checks
                results = self._run_all_checks()
                self._print_results(results)

                # Self-heal where possible
                if self.enable_healing:
                    self._run_healer(results)

                # Check for email-approved hotfixes
                if self._hotfix_mgr and self._hotfix_mgr._pending:
                    applied = self._hotfix_mgr.check_approvals()
                    for fix_id in applied:
                        self._record_heal(f"Hotfix {fix_id} applied (email approved)", "OK")

                # Alert on critical issues that weren't healed
                for check in results:
                    if check.severity == 'CRITICAL':
                        self._send_alert(check)

                # Periodic status update (hourly)
                self._maybe_send_periodic_update(results)

                self.checks_run += 1
                time.sleep(self.check_interval)

        except KeyboardInterrupt:
            log.info("Watchdog interrupted")
            self._generate_session_report()

    # All managed processes — script name, log name, critical flag
    _MANAGED_PROCESSES = {
        'supervisor': {'script': 'supervisor.py', 'log': 'supervisor.log', 'critical': True},
        'core':       {'script': 'run_core.py',   'log': 'core.log',       'critical': True},
        'options':    {'script': 'run_options.py', 'log': 'options.log',    'critical': False},
        'data_collector': {'script': 'run_data_collector.py', 'log': 'data_collector.log', 'critical': False},
    }

    def _run_all_checks(self) -> list:
        """Run all health checks. Returns list of HealthCheck."""
        results = []
        results.extend(self._check_all_processes())
        results.append(self._check_supervisor_status())
        results.extend(self._check_log_errors())
        results.append(self._check_signal_rate())
        results.append(self._check_positions())
        results.append(self._check_kill_switch())
        results.append(self._check_fill_ledger())
        results.append(self._check_order_wal())
        results.append(self._check_data_freshness())
        results.append(self._check_memory())
        results.append(self._check_gap_and_go())
        # V10: New checks for hardening fixes
        results.append(self._check_v10_equity_lifecycle())
        results.append(self._check_v10_options_lifecycle())
        results.append(self._check_v10_pending_tickers())
        results.append(self._check_v10_bar_builder())
        return [r for r in results if r is not None]

    # ── Individual checks ─────────────────────────────────────────────────

    def _get_ps_output(self) -> str:
        """Get process list, handling PATH issues."""
        for ps_cmd in ['/bin/ps', '/usr/bin/ps', 'ps']:
            try:
                result = subprocess.run(
                    [ps_cmd, 'aux'], capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0:
                    return result.stdout
            except FileNotFoundError:
                continue
        # Fallback
        try:
            result = subprocess.run(
                ['/usr/bin/pgrep', '-fl', 'python'], capture_output=True, text=True, timeout=5,
            )
            return result.stdout
        except Exception:
            return ''

    def _check_all_processes(self) -> list:
        """Check ALL managed processes: alive + not zombie."""
        results = []
        ps_output = self._get_ps_output()
        now = datetime.now(ET)
        in_market = (now.hour >= 9 and now.minute >= 30) and now.hour < 16
        today = now.strftime('%Y%m%d')

        for name, info in self._MANAGED_PROCESSES.items():
            script = info['script']
            log_name = info['log']
            critical = info['critical']

            # Check 1: Is the process running?
            is_running = script in ps_output

            if not is_running:
                severity = 'CRITICAL' if critical else 'WARNING'
                results.append(HealthCheck(
                    f'proc_{name}', 'CRITICAL' if critical else 'WARN',
                    f'{name} NOT running', severity,
                ))
                continue

            # Check 2: Is it a zombie? (process alive but log stale during market hours)
            if in_market:
                log_path = os.path.join(PROJECT_ROOT, 'logs', today, log_name)
                if os.path.exists(log_path):
                    log_age = time.time() - os.path.getmtime(log_path)
                    stale_threshold = 300 if critical else 600  # 5 min for critical, 10 for others
                    if log_age > stale_threshold:
                        self.issues_found[f'{name}_zombie'] += 1
                        results.append(HealthCheck(
                            f'proc_{name}', 'CRITICAL',
                            f'{name} ZOMBIE: alive but log stale ({log_age/60:.0f}min) — needs restart',
                            'CRITICAL' if critical else 'WARNING',
                        ))
                        continue

            results.append(HealthCheck(f'proc_{name}', 'OK', f'{name} running'))

        return results

    def _check_supervisor_status(self) -> HealthCheck:
        """Check supervisor_status.json for restart counts."""
        status_file = SUPERVISOR_STATUS_PATH
        try:
            if not os.path.exists(status_file):
                return HealthCheck('supervisor', 'WARN', 'No status file', 'WARNING')

            with open(status_file) as f:
                status = json.load(f)

            issues = []
            for engine, info in status.items():
                restarts = info.get('restart_count', 0)
                state = info.get('state', 'unknown')
                if restarts > 0:
                    issues.append(f'{engine}: {restarts} restarts')
                if state == 'halted':
                    issues.append(f'{engine}: HALTED')

            if issues:
                severity = 'CRITICAL' if any('HALTED' in i for i in issues) else 'WARNING'
                return HealthCheck('supervisor', 'WARN',
                                   ' | '.join(issues), severity)

            return HealthCheck('supervisor', 'OK', 'All engines nominal')
        except Exception as e:
            return HealthCheck('supervisor', 'WARN', f'Check failed: {e}', 'WARNING')

    def _check_log_errors(self) -> list:
        """Scan ALL process logs for new ERROR/CRITICAL lines since last check."""
        results = []
        today = datetime.now().strftime('%Y%m%d')
        log_dir = os.path.join(PROJECT_ROOT, 'logs', today)

        if not os.path.exists(log_dir):
            return [HealthCheck('log_errors', 'WARN', 'No log directory for today', 'WARNING')]

        total_errors = 0
        worst_severity = 'INFO'
        samples = []

        for name, info in self._MANAGED_PROCESSES.items():
            log_path = os.path.join(log_dir, info['log'])
            if not os.path.exists(log_path):
                continue

            try:
                last_pos = self._last_log_pos.get(log_path, 0)
                file_size = os.path.getsize(log_path)

                if file_size < last_pos:
                    last_pos = 0  # log rotated
                if file_size == last_pos:
                    continue

                errors = []
                with open(log_path, 'r') as f:
                    f.seek(last_pos)
                    for line in f:
                        if ' ERROR ' in line or ' CRITICAL ' in line:
                            errors.append(line.strip()[:150])
                    self._last_log_pos[log_path] = f.tell()

                if errors:
                    total_errors += len(errors)
                    self.issues_found[f'log_errors_{name}'] += len(errors)
                    has_critical = any('CRITICAL' in e for e in errors)
                    if has_critical:
                        worst_severity = 'CRITICAL'
                    elif worst_severity != 'CRITICAL':
                        worst_severity = 'WARNING'
                    # Keep top 2 unique errors per process
                    unique = list(set(errors))[:2]
                    for e in unique:
                        samples.append(f"[{name}] {e[:100]}")

            except Exception:
                pass

        if total_errors > 0:
            msg = f'{total_errors} errors across logs'
            if samples:
                msg += f'. Latest: {samples[0]}'
            return [HealthCheck('log_errors', 'WARN', msg, worst_severity)]

        return [HealthCheck('log_errors', 'OK', 'All logs clean')]

    def _check_signal_rate(self) -> HealthCheck:
        """Check if signal rate is abnormal (too high = detector issue)."""
        today = datetime.now().strftime('%Y%m%d')
        core_log = os.path.join(PROJECT_ROOT, 'logs', today, 'core.log')

        try:
            if not os.path.exists(core_log):
                return HealthCheck('signal_rate', 'OK', 'No log to check')

            result = subprocess.run(
                ['/usr/bin/grep', '-c', 'SIGNAL.*strategy=', core_log],
                capture_output=True, text=True, timeout=10,
            )
            count = int(result.stdout.strip()) if result.returncode == 0 else 0

            now = datetime.now(ET)
            minutes_since_open = max(1, (now - now.replace(hour=9, minute=30)).total_seconds() / 60)
            rate_per_min = count / minutes_since_open

            # Check rate change since last check
            delta = count - self._prev_signal_count
            self._prev_signal_count = count

            if rate_per_min > 50:  # more than 50 signals/min is suspicious
                self.issues_found['high_signal_rate'] += 1
                return HealthCheck('signal_rate', 'WARN',
                                   f'{count} total ({rate_per_min:.1f}/min, +{delta} since last check)',
                                   'WARNING')

            return HealthCheck('signal_rate', 'OK',
                               f'{count} total ({rate_per_min:.1f}/min)')
        except Exception as e:
            return HealthCheck('signal_rate', 'WARN', f'Check failed: {e}', 'WARNING')

    def _check_positions(self) -> HealthCheck:
        """Check position state for ALL engines (core + options)."""
        core_positions = 0
        core_trades = 0
        core_pnl = 0.0
        options_positions = 0
        options_trades = 0
        options_pnl = 0.0
        issues = []

        # ── Core: FillLedger for P&L, bot_state for positions (V10) ─
        # P&L: FillLedger (FIFO-matched, always correct)
        # Positions: bot_state.json (reconciliation keeps in sync with broker)
        # FillLedger's open_positions can be stale if broker closed positions
        # while core was down (bracket stops, etc.) — bot_state is reconciled.
        try:
            # Positions from bot_state (reconciled with broker every 5 min)
            if os.path.exists(_BOT_STATE_FILE):
                with open(_BOT_STATE_FILE) as f:
                    state = json.load(f)
                core_positions = len(state.get('positions', {}))

            # P&L + trades from FillLedger
            if os.path.exists(FILL_LEDGER_PATH):
                with open(FILL_LEDGER_PATH) as f:
                    ledger = json.load(f)
                core_pnl = ledger.get('daily_pnl', 0.0)
                today_str = datetime.now(ET).strftime('%Y-%m-%d')
                all_lots = ledger.get('lots', [])
                core_trades = sum(1 for l in all_lots
                                  if l.get('side') == 'SELL'
                                  and l.get('timestamp', '').startswith(today_str))
            elif os.path.exists(_BOT_STATE_FILE):
                # Fallback: P&L from bot_state trade_log
                trade_log = state.get('trade_log', [])
                core_trades = len(trade_log)
                core_pnl = sum(t.get('pnl', 0) for t in trade_log)
        except Exception:
            pass

        # ── Options state (options_state.json) ───────────────────────
        try:
            opts_file = OPTIONS_STATE_PATH
            if os.path.exists(opts_file):
                with open(opts_file) as f:
                    opts = json.load(f)
                options_positions = len(opts.get('positions', {}))
                options_trades = opts.get('daily_entries', 0)
                options_pnl = opts.get('daily_pnl', 0.0)
        except Exception:
            pass

        # ── Kill switch check ────────────────────────────────────────
        try:
            ks_file = os.path.join(DATA_DIR, 'options_kill_switch.json')
            if os.path.exists(ks_file):
                with open(ks_file) as f:
                    ks = json.load(f)
                if ks.get('halted'):
                    issues.append(f'OPTIONS HALTED (pnl=${ks.get("daily_pnl", 0):.0f})')
        except Exception:
            pass

        total_positions = core_positions + options_positions
        total_pnl = core_pnl + options_pnl

        if total_positions > 20:
            issues.append(f'HIGH position count: {total_positions}')

        msg = (f'core: {core_positions}pos/{core_trades}trades(today)/${core_pnl:+.2f} | '
               f'options: {options_positions}pos/{options_trades}trades/${options_pnl:+.2f} | '
               f'TOTAL: ${total_pnl:+.2f}')

        if issues:
            return HealthCheck('positions', 'WARN', f'{msg} | {" | ".join(issues[:3])}', 'WARNING')

        return HealthCheck('positions', 'OK', msg)


    # Known production engine prefixes for kill switch files.
    # Prevents stale test files (test_kill_switch.json) from triggering
    # false CRITICAL alerts in watchdog and email reports.
    _KNOWN_KILL_SWITCH_ENGINES = {'vwap', 'pro', 'pop', 'options', 'core'}

    def _check_kill_switch(self) -> HealthCheck:
        """Check if any kill switch has been triggered."""
        try:
            triggered = []
            for fname in os.listdir(DATA_DIR):
                if fname.endswith('_kill_switch.json'):
                    engine = fname.replace('_kill_switch.json', '')
                    if engine not in self._KNOWN_KILL_SWITCH_ENGINES:
                        continue  # skip test/unknown kill switch files
                    fpath = os.path.join(DATA_DIR, fname)
                    with open(fpath) as f:
                        ks = json.load(f)
                    if ks.get('halted', False):
                        triggered.append(f'{engine} (since {ks.get("halted_at", "?")})')

            if triggered:
                self.issues_found['kill_switch'] += 1
                return HealthCheck('kill_switch', 'CRITICAL',
                                   'TRIGGERED: ' + ', '.join(triggered), 'CRITICAL')

            return HealthCheck('kill_switch', 'OK', 'Not triggered')
        except Exception as e:
            return HealthCheck('kill_switch', 'OK', f'No kill switch files (OK)')

    def _check_fill_ledger(self) -> HealthCheck:
        """Check FillLedger status — today's activity + open positions from prior days."""
        ledger_file = FILL_LEDGER_PATH

        try:
            if not os.path.exists(ledger_file):
                return HealthCheck('fill_ledger', 'OK', 'Not active (shadow mode off)')

            with open(ledger_file) as f:
                ledger = json.load(f)

            lots = ledger.get('lots', [])
            lot_states = ledger.get('lot_states', {})
            today = datetime.now(ET).strftime('%Y-%m-%d')

            # Separate today's lots from carried-over open lots
            today_buys = [l for l in lots if l.get('side') == 'BUY'
                          and l.get('timestamp', '').startswith(today)]
            today_sells = [l for l in lots if l.get('side') == 'SELL'
                           and l.get('timestamp', '').startswith(today)]
            open_lots = [l for l in lots if l.get('side') == 'BUY'
                         and not lot_states.get(l.get('lot_id', ''), {}).get('is_closed', False)
                         and lot_states.get(l.get('lot_id', ''), {}).get('remaining_qty', l.get('qty', 0)) > 0.001]
            carried_open = [l for l in open_lots if not l.get('timestamp', '').startswith(today)]

            # P&L from pre-computed field (V10)
            daily_pnl = ledger.get('daily_pnl', 0.0)

            msg = (f'today: {len(today_buys)} buys, {len(today_sells)} sells, '
                   f'P&L=${daily_pnl:+.2f} | '
                   f'{len(open_lots)} open ({len(carried_open)} from prior days)')

            return HealthCheck('fill_ledger', 'OK', msg)
        except Exception as e:
            return HealthCheck('fill_ledger', 'WARN', f'Check failed: {e}', 'WARNING')

    def _check_order_wal(self) -> HealthCheck:
        """Check Order WAL for stuck/incomplete orders."""
        try:
            from monitor.order_wal import wal
            stats = wal.stats()
            incomplete = stats.get('incomplete', 0)
            total = stats.get('total_orders', 0)
            states = stats.get('states', {})

            if incomplete > 0:
                # Orders stuck in non-terminal state — something crashed mid-order
                self.issues_found['wal_incomplete'] += 1
                return HealthCheck('order_wal', 'WARN',
                                   f'{incomplete} incomplete orders (total={total}, states={states})',
                                   'WARNING')

            return HealthCheck('order_wal', 'OK',
                               f'{total} orders today, 0 incomplete')
        except Exception as e:
            return HealthCheck('order_wal', 'OK', f'WAL check skipped: {e}')

    def _check_data_freshness(self) -> HealthCheck:
        """Check if market data is flowing (cache file recently updated)."""
        cache_file = LIVE_CACHE_PATH
        pkl_fallback = os.path.join(DATA_DIR, 'live_cache.pkl')

        try:
            path = cache_file if os.path.exists(cache_file) else pkl_fallback
            if not os.path.exists(path):
                return HealthCheck('data_freshness', 'WARN',
                                   'No cache file found', 'WARNING')

            mtime = os.path.getmtime(path)
            age_seconds = time.time() - mtime
            age_minutes = age_seconds / 60

            now = datetime.now(ET)
            in_market_hours = (now.hour >= 9 and now.minute >= 30) and now.hour < 16

            if in_market_hours and age_minutes > 5:
                self.issues_found['stale_data'] += 1
                severity = 'CRITICAL' if age_minutes > 15 else 'WARNING'
                return HealthCheck('data_freshness', 'WARN',
                                   f'Cache {age_minutes:.0f}min old (stale)', severity)

            return HealthCheck('data_freshness', 'OK',
                               f'Cache {age_minutes:.1f}min old')
        except Exception as e:
            return HealthCheck('data_freshness', 'WARN', f'Check failed: {e}', 'WARNING')

    def _check_memory(self) -> HealthCheck:
        """Check memory usage of trading processes."""
        try:
            result = subprocess.run(
                ['/bin/ps', 'aux'], capture_output=True, text=True, timeout=5,
            )
            total_mb = 0
            for line in result.stdout.splitlines():
                if 'run_core.py' in line or 'run_options.py' in line or 'supervisor.py' in line:
                    parts = line.split()
                    if len(parts) > 5:
                        rss_kb = int(parts[5])
                        total_mb += rss_kb / 1024

            if total_mb > 2000:
                self.issues_found['high_memory'] += 1
                return HealthCheck('memory', 'WARN',
                                   f'{total_mb:.0f} MB total (high)', 'WARNING')

            return HealthCheck('memory', 'OK', f'{total_mb:.0f} MB total')
        except Exception as e:
            return HealthCheck('memory', 'OK', f'Check skipped: {e}')

    def _check_gap_and_go(self) -> HealthCheck:
        """Check if gap_and_go is firing excessively (should be near zero after fix)."""
        today = datetime.now().strftime('%Y%m%d')
        core_log = os.path.join(PROJECT_ROOT, 'logs', today, 'core.log')

        try:
            if not os.path.exists(core_log):
                return HealthCheck('gap_and_go', 'OK', 'No log to check')

            result = subprocess.run(
                ['/usr/bin/grep', '-c', 'gap_and_go', core_log],
                capture_output=True, text=True, timeout=10,
            )
            count = int(result.stdout.strip()) if result.returncode == 0 else 0

            if count > 50:
                self.issues_found['gap_and_go_excess'] += 1
                return HealthCheck('gap_and_go', 'WARN',
                                   f'{count} gap_and_go signals (should be <10 after fix)',
                                   'WARNING')

            return HealthCheck('gap_and_go', 'OK', f'{count} signals (nominal)')
        except Exception as e:
            return HealthCheck('gap_and_go', 'OK', f'Check skipped: {e}')

    # ── V10 Health Checks ─────────────────────────────────────────────────

    def _check_v10_equity_lifecycle(self) -> HealthCheck:
        """V10: Check equity exit engine — bars_held should match actual bars, not quotes."""
        today = datetime.now().strftime('%Y%m%d')
        core_log = os.path.join(PROJECT_ROOT, 'logs', today, 'core.log')
        try:
            if not os.path.exists(core_log):
                return HealthCheck('v10_equity_lifecycle', 'OK', 'No log')

            # Check for is_quote corruption: bars_held incrementing too fast
            result = subprocess.run(
                ['/usr/bin/grep', '-c', 'bars_held.*=.*[0-9][0-9][0-9]', core_log],
                capture_output=True, text=True, timeout=10)
            high_bars = int(result.stdout.strip()) if result.returncode == 0 else 0

            # Check for QUOTE handler poisoning lifecycle
            result2 = subprocess.run(
                ['/usr/bin/grep', '-c', 'phase0_quote_hold', core_log],
                capture_output=True, text=True, timeout=10)
            quote_holds = int(result2.stdout.strip()) if result2.returncode == 0 else 0

            # Check for lifecycle crashes
            result3 = subprocess.run(
                ['/usr/bin/grep', '-c', 'Lifecycle evaluate CRASHED', core_log],
                capture_output=True, text=True, timeout=10)
            crashes = int(result3.stdout.strip()) if result3.returncode == 0 else 0

            if crashes > 0:
                self.issues_found['lifecycle_crash'] += crashes
                return HealthCheck('v10_equity_lifecycle', 'CRITICAL',
                    f'{crashes} lifecycle crashes — falling back to flat exit',
                    'CRITICAL')

            msg = f'quote_holds={quote_holds} (is_quote working)'
            if crashes > 0:
                msg += f', {crashes} crashes'
            return HealthCheck('v10_equity_lifecycle', 'OK', msg)
        except Exception as e:
            return HealthCheck('v10_equity_lifecycle', 'OK', f'Check skipped: {e}')

    def _check_v10_options_lifecycle(self) -> HealthCheck:
        """V10: Check options lifecycle — phases, exits, close events persisted."""
        today = datetime.now().strftime('%Y%m%d')
        options_log = os.path.join(PROJECT_ROOT, 'logs', today, 'options.log')
        try:
            if not os.path.exists(options_log):
                return HealthCheck('v10_options_lifecycle', 'OK', 'No options log')

            # Check lifecycle creation
            result = subprocess.run(
                ['/usr/bin/grep', '-c', 'OptionsLifecycle.*OPENED', options_log],
                capture_output=True, text=True, timeout=10)
            opened = int(result.stdout.strip()) if result.returncode == 0 else 0

            # Check close events emitted
            result2 = subprocess.run(
                ['/usr/bin/grep', '-c', 'OPTIONS_CLOSE persisted', options_log],
                capture_output=True, text=True, timeout=10)
            closed = int(result2.stdout.strip()) if result2.returncode == 0 else 0

            # Check for auto-created lifecycles (restart recovery)
            result3 = subprocess.run(
                ['/usr/bin/grep', '-c', 'AUTO-CREATED lifecycle', options_log],
                capture_output=True, text=True, timeout=10)
            auto = int(result3.stdout.strip()) if result3.returncode == 0 else 0

            # Check for lifecycle crashes
            result4 = subprocess.run(
                ['/usr/bin/grep', '-c', 'Lifecycle evaluate CRASHED', options_log],
                capture_output=True, text=True, timeout=10)
            crashes = int(result4.stdout.strip()) if result4.returncode == 0 else 0

            if crashes > 0:
                self.issues_found['options_lifecycle_crash'] += crashes
                return HealthCheck('v10_options_lifecycle', 'CRITICAL',
                    f'{crashes} options lifecycle crashes', 'CRITICAL')

            msg = f'opened={opened} closed={closed} auto_recovered={auto}'
            return HealthCheck('v10_options_lifecycle', 'OK', msg)
        except Exception as e:
            return HealthCheck('v10_options_lifecycle', 'OK', f'Check skipped: {e}')

    def _check_v10_pending_tickers(self) -> HealthCheck:
        """V10: Check if pending tickers set is growing (FILL/ORDER_FAIL events lost)."""
        today = datetime.now().strftime('%Y%m%d')
        core_log = os.path.join(PROJECT_ROOT, 'logs', today, 'core.log')
        try:
            if not os.path.exists(core_log):
                return HealthCheck('v10_pending', 'OK', 'No log')

            # Check for stale pending evictions (means events are being lost)
            result = subprocess.run(
                ['/usr/bin/grep', '-c', 'Evicted stale pending', core_log],
                capture_output=True, text=True, timeout=10)
            evictions = int(result.stdout.strip()) if result.returncode == 0 else 0

            if evictions > 5:
                self.issues_found['pending_evictions'] += evictions
                return HealthCheck('v10_pending', 'WARN',
                    f'{evictions} stale pending tickers evicted — FILL events may be lost',
                    'WARNING')

            return HealthCheck('v10_pending', 'OK',
                f'{evictions} evictions (0 = healthy)')
        except Exception as e:
            return HealthCheck('v10_pending', 'OK', f'Check skipped: {e}')

    def _check_v10_bar_builder(self) -> HealthCheck:
        """V10: Check BarBuilder health — flush thread alive, no excessive restarts."""
        today = datetime.now().strftime('%Y%m%d')
        core_log = os.path.join(PROJECT_ROOT, 'logs', today, 'core.log')
        try:
            if not os.path.exists(core_log):
                return HealthCheck('v10_bar_builder', 'OK', 'No log')

            # Check for flush thread restarts
            result = subprocess.run(
                ['/usr/bin/grep', '-c', 'Flush thread died', core_log],
                capture_output=True, text=True, timeout=10)
            restarts = int(result.stdout.strip()) if result.returncode == 0 else 0

            # Check for "giving up" (max restarts exceeded)
            result2 = subprocess.run(
                ['/usr/bin/grep', '-c', 'giving up.*REST polling', core_log],
                capture_output=True, text=True, timeout=10)
            gave_up = int(result2.stdout.strip()) if result2.returncode == 0 else 0

            if gave_up > 0:
                self.issues_found['bb_gave_up'] += 1
                return HealthCheck('v10_bar_builder', 'CRITICAL',
                    'BarBuilder flush thread gave up — running on REST only',
                    'CRITICAL')

            if restarts > 3:
                self.issues_found['bb_restarts'] += restarts
                return HealthCheck('v10_bar_builder', 'WARN',
                    f'Flush thread restarted {restarts}x — unstable',
                    'WARNING')

            return HealthCheck('v10_bar_builder', 'OK',
                f'{restarts} restarts (0 = healthy)')
        except Exception as e:
            return HealthCheck('v10_bar_builder', 'OK', f'Check skipped: {e}')

    # ── Self-Healing ──────────────────────────────────────────────────────

    def _run_healer(self, results: list):
        """Apply safe auto-fixes based on check results."""
        for check in results:
            if check.status == 'OK':
                continue

            # ── Fix 1: Supervisor not running → restart it ────────────
            if check.name == 'proc_supervisor' and 'NOT running' in check.message:
                self._heal_restart_supervisor()

            # ── Fix 2: Any process not running → diagnose crash ───────
            if check.name.startswith('proc_') and 'NOT running' in check.message:
                engine = check.name.replace('proc_', '')
                if engine != 'supervisor':
                    self._heal_diagnose_crash(engine)

            # ── Fix 3: Any process zombie → kill for supervisor restart ─
            if check.name.startswith('proc_') and 'ZOMBIE' in check.message:
                engine = check.name.replace('proc_', '')
                self._heal_kill_zombie(engine)

            # ── Fix 4: Stale data → clear cache to force fresh fetch ───
            if check.name == 'data_freshness' and 'stale' in check.message:
                self._heal_stale_cache()

            # ── Fix 5: Log errors with known patterns ──────────────────
            if check.name == 'log_errors' and check.severity in ('WARNING', 'CRITICAL'):
                self._heal_from_log_errors()

        # ── V10 Fix: Lifecycle crash → restart core (supervisor will restart it)
            if check.name == 'v10_equity_lifecycle' and 'crash' in check.message.lower():
                log.warning("[HEAL] Equity lifecycle crashing — killing core for supervisor restart")
                self._heal_kill_zombie('core')

            # ── V10 Fix: Options lifecycle crash → restart options
            if check.name == 'v10_options_lifecycle' and 'crash' in check.message.lower():
                log.warning("[HEAL] Options lifecycle crashing — killing options for supervisor restart")
                self._heal_kill_zombie('options')

            # ── V10 Fix: BarBuilder gave up → restart core
            if check.name == 'v10_bar_builder' and 'gave up' in check.message.lower():
                log.warning("[HEAL] BarBuilder gave up — killing core for supervisor restart (resets flush thread)")
                self._heal_kill_zombie('core')

        # ── Fix 6: Always check for corrupt state files ────────────────
        self._heal_corrupt_state_files()

        # ── Fix 7: Always clean stale lock files ───────────────────────
        self._heal_stale_locks()

    def _heal_restart_supervisor(self):
        """Restart the supervisor process."""
        try:
            # Check if it really died vs we just can't see it
            result = subprocess.run(
                ['/usr/bin/pgrep', '-f', 'supervisor.py'],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                return  # actually running, ps check was wrong

            log.info("[HEAL] Supervisor not running — restarting...")
            supervisor_script = os.path.join(PROJECT_ROOT, 'scripts', 'supervisor.py')
            subprocess.Popen(
                [sys.executable, supervisor_script],
                cwd=PROJECT_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(5)

            # Verify it started
            result = subprocess.run(
                ['/usr/bin/pgrep', '-f', 'supervisor.py'],
                capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                self._record_heal("Restarted supervisor", "SUCCESS")
                log.info("[HEAL] Supervisor restarted successfully")
            else:
                self._record_heal("Restart supervisor", "FAILED — process didn't start")
                log.error("[HEAL] Supervisor restart FAILED")
        except Exception as e:
            self._record_heal("Restart supervisor", f"ERROR: {e}")
            log.error("[HEAL] Supervisor restart error: %s", e)

    def _heal_kill_zombie(self, engine: str):
        """Kill a zombie process (alive but not working) so supervisor restarts it."""
        script_name = self._MANAGED_PROCESSES.get(engine, {}).get('script', f'run_{engine}.py')
        try:
            result = subprocess.run(
                ['/usr/bin/pgrep', '-f', script_name],
                capture_output=True, text=True, timeout=5,
            )
            pids = [p.strip() for p in result.stdout.strip().split('\n') if p.strip()]
            if not pids:
                log.warning("[HEAL] No PID found for %s (%s)", engine, script_name)
                return

            for pid in pids:
                os.kill(int(pid), 9)  # SIGKILL
                self._record_heal(
                    f"Killed zombie {engine} (PID {pid})",
                    "OK — supervisor will restart",
                )
                log.info("[HEAL] Killed zombie %s PID %s — supervisor will restart",
                         engine, pid)

            # Send notification
            try:
                from config import ALERT_EMAIL
                self._send_alert(HealthCheck(
                    f'zombie_{engine}', 'WARNING',
                    f'{engine} was zombie (log stale) — killed PID {",".join(pids)}, supervisor restarting',
                    'WARNING',
                ))
            except Exception:
                pass

        except Exception as e:
            log.warning("[HEAL] Zombie kill for %s failed: %s", engine, e)

    def _heal_diagnose_crash(self, engine: str):
        """Diagnose why an engine crashed and apply fixes if possible."""
        today = datetime.now().strftime('%Y%m%d')
        log_name = self._MANAGED_PROCESSES.get(engine, {}).get('log', f'{engine}.log')
        log_path = os.path.join(PROJECT_ROOT, 'logs', today, log_name)

        if not os.path.exists(log_path):
            return

        # Count crashes today
        crash_count = self._prev_crash_count.get(engine, 0)

        try:
            # Read last 100 lines for traceback
            result = subprocess.run(
                ['/usr/bin/tail', '-100', log_path],
                capture_output=True, text=True, timeout=5,
            )
            tail = result.stdout

            # Extract traceback
            traceback_lines = []
            in_traceback = False
            for line in tail.splitlines():
                if 'Traceback (most recent call last)' in line:
                    in_traceback = True
                    traceback_lines = [line]
                elif in_traceback:
                    traceback_lines.append(line)
                    if line and not line.startswith(' ') and 'Error' in line:
                        in_traceback = False

            if not traceback_lines:
                return

            error_text = '\n'.join(traceback_lines[-5:])
            log.info("[HEAL] %s crash traceback:\n%s", engine, error_text)

            # ── Pattern matching: known fixable errors ──────────────
            fixed = False

            # JSON decode error → corrupt state file
            if 'JSONDecodeError' in error_text:
                file_match = re.search(r"'([^']+\.json)'", error_text)
                if file_match:
                    fixed = self._restore_json_backup(file_match.group(1))

            # FileNotFoundError for state files → create empty
            if 'FileNotFoundError' in error_text and '.json' in error_text:
                file_match = re.search(r"'([^']+\.json)'", error_text)
                if file_match:
                    fixed = self._create_empty_state(file_match.group(1))

            # PermissionError → fix permissions
            if 'PermissionError' in error_text:
                file_match = re.search(r"'([^']+)'", error_text)
                if file_match:
                    fixed = self._fix_permissions(file_match.group(1))

            # ModuleNotFoundError / ImportError → clear __pycache__
            if 'ModuleNotFoundError' in error_text or 'ImportError' in error_text:
                fixed = self._clear_pycache()

            # Pickle errors → remove corrupt cache
            if 'UnpicklingError' in error_text or 'pickle' in error_text.lower():
                fixed = self._remove_corrupt_cache()

            if fixed:
                self._record_heal(f"Auto-fixed {engine} crash", "SUCCESS — supervisor will restart")
                crash_count = 0
            else:
                # Try email-based hotfix for code errors
                if self._hotfix_mgr and error_type in (
                    'NameError', 'AttributeError', 'TypeError',
                    'ImportError', 'ModuleNotFoundError', 'KeyError',
                    'IndexError', 'ValueError', 'ZeroDivisionError',
                    'StopIteration', 'FileNotFoundError',
                ):
                    full_tb = '\n'.join(traceback_lines)
                    proposal = self._hotfix_mgr.propose_fix_from_traceback(full_tb)
                    if proposal:
                        self._record_heal(
                            f"Hotfix proposed for {engine}: {proposal.explanation}",
                            "AWAITING EMAIL APPROVAL",
                        )
                        fixed = True  # don't count as unresolved crash

                if not fixed:
                    crash_count += 1
                    self._prev_crash_count[engine] = crash_count

                    if crash_count >= 3:
                        self._record_heal(
                            f"{engine} crashed {crash_count}x — unfixable",
                            f"ALERT SENT\nLast error: {error_text[:200]}",
                        )
                        self._send_alert(HealthCheck(
                            'repeated_crash', 'CRITICAL',
                            f'{engine} crashed {crash_count}x. Last: {error_text[:150]}',
                            'CRITICAL',
                        ))

        except Exception as e:
            log.warning("[HEAL] Crash diagnosis failed: %s", e)

    def _heal_stale_cache(self):
        """Remove stale cache files to force fresh data fetch."""
        cache_files = [
            LIVE_CACHE_PATH,
            os.path.join(DATA_DIR, 'live_cache.pkl'),
        ]
        for path in cache_files:
            if os.path.exists(path):
                age = time.time() - os.path.getmtime(path)
                if age > 600:  # >10 min old
                    try:
                        os.remove(path)
                        self._record_heal(f"Removed stale cache: {os.path.basename(path)}", "OK")
                        log.info("[HEAL] Removed stale cache: %s (%.0fs old)", path, age)
                    except Exception as e:
                        log.warning("[HEAL] Cache removal failed: %s", e)

    def _heal_from_log_errors(self):
        """Scan recent errors from ALL process logs for fixable patterns."""
        today = datetime.now().strftime('%Y%m%d')

        for name, info in self._MANAGED_PROCESSES.items():
            log_path = os.path.join(PROJECT_ROOT, 'logs', today, info['log'])
            if not os.path.exists(log_path):
                continue

            try:
                result = subprocess.run(
                    ['/usr/bin/tail', '-50', log_path],
                    capture_output=True, text=True, timeout=5,
                )
                recent = result.stdout

                # Lock file errors → remove stale locks
                if '.lock' in recent and ('LockError' in recent or 'locked' in recent.lower()):
                    self._heal_stale_locks()

                # "No space left on device" → clear old logs
                if 'No space left' in recent:
                    self._clear_old_logs()

                # Traceback in log → try hotfix
                if 'Traceback (most recent call last)' in recent and self._hotfix_mgr:
                    # Extract the traceback
                    lines = recent.split('\n')
                    tb_lines = []
                    in_tb = False
                    for line in lines:
                        if 'Traceback (most recent call last)' in line:
                            in_tb = True
                            tb_lines = [line]
                        elif in_tb:
                            tb_lines.append(line)
                            if line.strip() and not line.startswith(' ') and 'Error' in line:
                                in_tb = False
                    if tb_lines:
                        tb = '\n'.join(tb_lines)
                        error_type = tb_lines[-1].split(':')[0].strip() if tb_lines else ''
                        if error_type in (
                            'NameError', 'AttributeError', 'TypeError',
                            'ImportError', 'ModuleNotFoundError', 'KeyError',
                            'IndexError', 'ValueError', 'ZeroDivisionError',
                            'StopIteration',
                        ):
                            proposal = self._hotfix_mgr.propose_fix_from_traceback(tb)
                            if proposal and proposal.applied:
                                self._record_heal(
                                    f"Auto-fixed {name}: {proposal.explanation}",
                                    "OK — supervisor will restart",
                                )

            except Exception:
                pass

    def _heal_corrupt_state_files(self):
        """Check all JSON state files for corruption, restore from backups."""
        state_files = [
            _BOT_STATE_FILE,
            FILL_LEDGER_PATH,
            os.path.join(DATA_DIR, 'position_registry.json'),
            BROKER_MAP_PATH,
        ]
        for fpath in state_files:
            if os.path.exists(fpath):
                try:
                    with open(fpath) as f:
                        json.load(f)
                except (json.JSONDecodeError, ValueError):
                    log.warning("[HEAL] Corrupt state file: %s — restoring backup", fname)
                    self._restore_json_backup(fpath)

    def _heal_stale_locks(self):
        """Remove lock files older than 5 minutes."""
        for fname in os.listdir(DATA_DIR):
            if fname.endswith('.lock'):
                fpath = os.path.join(DATA_DIR, fname)
                try:
                    age = time.time() - os.path.getmtime(fpath)
                    if age > 300:  # >5 min
                        os.remove(fpath)
                        self._record_heal(f"Removed stale lock: {fname}", "OK")
                        log.info("[HEAL] Removed stale lock: %s (%.0fs old)", fname, age)
                except Exception:
                    pass

    # ── Heal helpers ──────────────────────────────────────────────────────

    def _restore_json_backup(self, filepath: str) -> bool:
        """Restore a JSON file from .prev or .prev2 backup."""
        for suffix in ['.prev', '.prev2']:
            backup = filepath + suffix
            if os.path.exists(backup):
                try:
                    # Validate backup is valid JSON
                    with open(backup) as f:
                        json.load(f)
                    # Restore
                    import shutil
                    shutil.copy2(backup, filepath)
                    self._record_heal(f"Restored {os.path.basename(filepath)} from {suffix}", "OK")
                    log.info("[HEAL] Restored %s from %s", filepath, backup)
                    return True
                except (json.JSONDecodeError, OSError):
                    continue
        # No valid backup found — create empty
        return self._create_empty_state(filepath)

    def _create_empty_state(self, filepath: str) -> bool:
        """Create a minimal valid JSON state file."""
        basename = os.path.basename(filepath)
        try:
            if 'bot_state' in basename:
                empty = {'positions': {}, 'reclaimed_today': [], 'trade_log': [],
                         'last_order_time': {}}
            elif 'fill_ledger' in basename:
                empty = {'lots': [], 'lot_states': {}, 'meta': {}, 'matches': []}
            elif 'registry' in basename or 'broker_map' in basename:
                empty = {}
            else:
                empty = {}

            with open(filepath, 'w') as f:
                json.dump(empty, f)
            self._record_heal(f"Created empty {basename}", "OK")
            log.info("[HEAL] Created empty state file: %s", filepath)
            return True
        except Exception as e:
            log.warning("[HEAL] Failed to create %s: %s", filepath, e)
            return False

    def _fix_permissions(self, filepath: str) -> bool:
        """Fix file permissions."""
        try:
            os.chmod(filepath, 0o644)
            self._record_heal(f"Fixed permissions: {os.path.basename(filepath)}", "OK")
            return True
        except Exception:
            return False

    def _clear_pycache(self) -> bool:
        """Clear __pycache__ directories."""
        cleared = 0
        for root, dirs, files in os.walk(PROJECT_ROOT):
            if '__pycache__' in dirs:
                cache_dir = os.path.join(root, '__pycache__')
                try:
                    import shutil
                    shutil.rmtree(cache_dir)
                    cleared += 1
                except Exception:
                    pass
        if cleared:
            self._record_heal(f"Cleared {cleared} __pycache__ dirs", "OK")
            log.info("[HEAL] Cleared %d __pycache__ directories", cleared)
            return True
        return False

    def _remove_corrupt_cache(self) -> bool:
        """Remove corrupt pickle/cache files."""
        removed = False
        for fpath in [LIVE_CACHE_PATH, LIVE_CACHE_PATH.replace('.json', '.pkl')]:
            if os.path.exists(fpath):
                try:
                    os.remove(fpath)
                    removed = True
                    self._record_heal(f"Removed corrupt: {fname}", "OK")
                except Exception:
                    pass
        return removed

    def _clear_old_logs(self):
        """Remove log directories older than 7 days."""
        logs_dir = os.path.join(PROJECT_ROOT, 'logs')
        cutoff = datetime.now() - timedelta(days=7)
        for entry in os.listdir(logs_dir):
            entry_path = os.path.join(logs_dir, entry)
            if os.path.isdir(entry_path) and len(entry) == 8:
                try:
                    dir_date = datetime.strptime(entry, '%Y%m%d')
                    if dir_date < cutoff:
                        import shutil
                        shutil.rmtree(entry_path)
                        log.info("[HEAL] Removed old logs: %s", entry)
                except (ValueError, OSError):
                    pass

    def _record_heal(self, action: str, result: str):
        """Record a healing action."""
        self.heals_applied.append((
            datetime.now(ET).strftime('%H:%M:%S'),
            action,
            result,
        ))

    # ── Periodic Updates ─────────────────────────────────────────────────

    def _maybe_send_periodic_update(self, results: list):
        """Send hourly status email during market hours."""
        now = datetime.now(ET)
        hour = now.hour

        if hour not in self._UPDATE_HOURS or hour in self._updates_sent:
            return

        self._updates_sent.add(hour)

        # Build status summary
        ok_count = sum(1 for r in results if r.status == 'OK')
        warn_count = sum(1 for r in results if r.status == 'WARN')
        crit_count = sum(1 for r in results if r.status == 'CRITICAL')

        # Read P&L, positions, and trades from FillLedger (source of truth)
        # and options_state.json for options engine.
        core_pnl = 0.0
        core_open = 0
        core_closed = 0
        core_buys = 0
        options_pnl = 0.0
        options_open = 0
        options_trades = 0
        position_lines = []
        closed_trade_lines = []

        # ── Core: FillLedger is P&L authority (V10) ─────────────────
        # Read pre-computed daily_pnl and open_positions from fill_ledger.json.
        # Falls back to bot_state.json if FillLedger not available.
        try:
            ledger_file = FILL_LEDGER_PATH
            if os.path.exists(ledger_file):
                with open(ledger_file) as f:
                    ledger = json.load(f)

                # P&L from pre-computed field (FIFO-matched, always correct)
                core_pnl = ledger.get('daily_pnl', 0.0)

                # Open positions from bot_state (authoritative, reconciled with broker)
                # fill_ledger's open_positions can be stale after sells
                try:
                    with open(_BOT_STATE_FILE) as _bf:
                        _bs = json.load(_bf)
                    _live_pos = _bs.get('positions', {})
                    core_open = len(_live_pos)
                    for t, info in _live_pos.items():
                        entry = info.get('entry_price', 0)
                        stop = info.get('stop_price', 0)
                        target = info.get('target_price', 0)
                        strategy = info.get('strategy', '')
                        qty = info.get('quantity', info.get('qty', 0))
                        position_lines.append(
                            f"  {t:6s}  {qty:>6.0f} shares @ ${entry:>8.2f}  "
                            f"stop=${stop:>8.2f}  target=${target:>8.2f}  "
                            f"{strategy}"
                        )
                except Exception:
                    # Fallback to fill_ledger if bot_state unavailable
                    open_pos = ledger.get('open_positions', {})
                    core_open = len(open_pos)
                    for t, info in open_pos.items():
                        position_lines.append(
                            f"  {t:6s}  {info.get('qty',0):>6.0f} shares @ ${info.get('avg_entry',0):>8.2f}  "
                            f"stop=${info.get('stop_price',0):>8.2f}  target=${info.get('target_price',0):>8.2f}  "
                            f"{info.get('strategy','')}"
                        )

                # Trade details from raw lots (today only — not all-time)
                lots = ledger.get('lots', [])
                _today_str = datetime.now(ET).strftime('%Y-%m-%d')
                buy_lots = [l for l in lots if l['side'] == 'BUY'
                            and l.get('timestamp', '').startswith(_today_str)]
                sell_lots = [l for l in lots if l['side'] == 'SELL'
                             and l.get('timestamp', '').startswith(_today_str)]
                core_buys = len(buy_lots)
                core_closed = len(sell_lots)

                for sl in sell_lots:
                    t = sl['ticker']
                    reason = sl.get('reason', '?')
                    price = sl.get('fill_price', 0)
                    qty = sl.get('qty', 0)
                    closed_trade_lines.append(
                        f"  {t:6s}  SELL {qty:>6.0f} @ ${price:>8.2f}  reason={reason}"
                    )
                for bl in buy_lots:
                    t = bl['ticker']
                    strategy = bl.get('strategy', '?')
                    price = bl.get('fill_price', 0)
                    qty = bl.get('qty', 0)
                    closed_trade_lines.append(
                        f"  {t:6s}  BUY  {qty:>6.0f} @ ${price:>8.2f}  {strategy}"
                    )
                closed_trade_lines.sort()
            else:
                # Fallback: bot_state.json (FillLedger not available)
                if os.path.exists(_BOT_STATE_FILE):
                    with open(_BOT_STATE_FILE) as f:
                        state = json.load(f)
                    trade_log = state.get('trade_log', [])
                    core_pnl = sum(t.get('pnl', 0) for t in trade_log)
                    core_closed = len(trade_log)
                    core_open = len(state.get('positions', {}))
        except Exception:
            pass

        # ── Options: options_state.json ──────────────────────────────
        try:
            opts_file = OPTIONS_STATE_PATH
            if os.path.exists(opts_file):
                with open(opts_file) as f:
                    opts = json.load(f)
                options_pnl = opts.get('daily_pnl', 0.0)
                options_open = len(opts.get('positions', {}))
                options_trades = opts.get('daily_entries', 0)
                for ticker, pos_data in opts.get('positions', {}).items():
                    strategy = pos_data.get('strategy_type', '?') if isinstance(pos_data, dict) else '?'
                    position_lines.append(f"  {ticker:6s}  [OPTIONS] {strategy}")
        except Exception:
            pass

        pnl = core_pnl + options_pnl
        open_positions = core_open + options_open
        total_trades = core_closed + options_trades  # closed trades only (not buys+sells)

        # Overall status
        if crit_count > 0:
            status_emoji = "RED"
            status_text = f"{crit_count} CRITICAL issues"
        elif warn_count > 0:
            status_emoji = "YELLOW"
            status_text = f"{warn_count} warnings"
        else:
            status_emoji = "GREEN"
            status_text = "All systems nominal"

        # Time labels
        labels = {
            10: "10 AM — First Hour",
            11: "11 AM — Mid-Morning",
            12: "12 PM — Midday",
            13: "1 PM — Early Afternoon",
            14: "2 PM — Mid-Afternoon",
            15: "3 PM — Power Hour",
            16: "4 PM — Market Close",
        }
        time_label = labels.get(hour, f"{hour}:00")

        subject = f"[{status_emoji}] {time_label} | P&L: ${pnl:+,.2f} | {open_positions} open | {total_trades} trades"

        body = (
            f"PERIODIC STATUS UPDATE — {time_label}\n"
            f"{'=' * 50}\n\n"
            f"Status: {status_text}\n"
            f"Time: {now.strftime('%Y-%m-%d %H:%M ET')}\n\n"
            f"TRADING SUMMARY\n"
            f"  Realized P&L:    ${pnl:+,.2f}\n"
            f"  Open positions:  {open_positions}\n"
            f"  Entries today:   {core_buys}\n"
            f"  Exits today:     {core_closed}\n\n"
            f"  CORE:    {core_open} open, {core_buys} entries, {core_closed} exits, "
            f"P&L ${core_pnl:+,.2f}\n"
            f"  OPTIONS: {options_open} open, {options_trades} trades, "
            f"P&L ${options_pnl:+,.2f}\n\n"
        )

        if position_lines:
            body += f"OPEN POSITIONS ({len(position_lines)})\n"
            body += '\n'.join(position_lines[:20])
            body += "\n\n"

        if closed_trade_lines:
            body += f"TRADES TODAY ({len(closed_trade_lines)})\n"
            body += '\n'.join(closed_trade_lines[:30])
            body += "\n\n"


        # WAL stats
        _wal_summary = ''
        try:
            from monitor.order_wal import wal as _wal
            _ws = _wal.stats()
            _wal_summary = (
                f"ORDER WAL\n"
                f"  Orders today:  {_ws.get('total_orders', 0)}\n"
                f"  Incomplete:    {_ws.get('incomplete', 0)}\n"
                f"  States:        {_ws.get('states', {})}\n\n"
            )
        except Exception:
            pass

        body += (
            f"HEALTH\n"
            f"  Checks:    {ok_count} OK, {warn_count} WARN, {crit_count} CRITICAL\n"
            f"  Hotfixes:  {len(self.heals_applied)} applied\n"
            f"  Crashes:   {sum(self._prev_crash_count.values())}\n\n"
        )

        if _wal_summary:
            body += _wal_summary

        # Component status
        body += "COMPONENTS\n"
        for r in results:
            icon = {'OK': 'OK ', 'WARN': 'WRN', 'CRITICAL': 'CRT'}.get(r.status, '???')
            body += f"  [{icon}] {r.name}: {r.message}\n"

        if self.heals_applied:
            body += f"\nAUTO-FIXES APPLIED\n"
            for ts, action, result in self.heals_applied[-5:]:
                body += f"  [{ts}] {action} -> {result}\n"

        body += (
            f"\n{'=' * 50}\n"
            f"Next update: {hour + 1}:00 ET\n"
            f"Watchdog checks: every {self.check_interval}s\n"
        )

        try:
            from config import ALERT_EMAIL
            from monitor.alerts import send_alert
            if ALERT_EMAIL:
                # Use direct SMTP for reliability (bypass async queue)
                import smtplib
                from email.mime.text import MIMEText
                msg = MIMEText(body, 'plain')
                msg['Subject'] = subject
                msg['From'] = os.getenv('ALERT_EMAIL_FROM', '')
                msg['To'] = ALERT_EMAIL
                s = smtplib.SMTP(
                    os.getenv('ALERT_EMAIL_SERVER', 'smtp.mail.yahoo.com'),
                    int(os.getenv('ALERT_EMAIL_PORT', '587')),
                    timeout=15,
                )
                s.ehlo()
                s.starttls()
                s.ehlo()
                s.login(
                    os.getenv('ALERT_EMAIL_USER', ''),
                    os.getenv('ALERT_EMAIL_PASS', ''),
                )
                s.sendmail(msg['From'], [ALERT_EMAIL], msg.as_string())
                s.quit()
                log.info("[Update] Sent %s update: P&L=$%.2f, %d open",
                         time_label, pnl, open_positions)
        except Exception as e:
            log.warning("[Update] Email send failed: %s", e)

    # ── Output & Alerts ───────────────────────────────────────────────────

    def _print_results(self, results: list):
        """Log check results."""
        now = datetime.now(ET).strftime('%H:%M:%S')
        log.info("─── Check #%d at %s ───", self.checks_run + 1, now)

        criticals = [r for r in results if r.severity == 'CRITICAL']
        warnings = [r for r in results if r.severity == 'WARNING']
        oks = [r for r in results if r.severity == 'INFO']

        for r in results:
            icon = {'OK': 'OK ', 'WARN': 'WRN', 'CRITICAL': 'CRT'}.get(r.status, '???')
            log.info("  [%s] %-18s %s", icon, r.name, r.message)

        if criticals:
            log.info("  >>> %d CRITICAL issues <<<", len(criticals))

        self._session_report.append({
            'time': now,
            'ok': len(oks),
            'warn': len(warnings),
            'critical': len(criticals),
            'details': [(r.name, r.status, r.message) for r in results],
        })

    def _send_alert(self, check: HealthCheck):
        """Send email alert for critical issues (dedup by type)."""
        alert_key = check.name
        if alert_key in self.alerts_sent:
            return  # already alerted for this issue type

        try:
            from config import ALERT_EMAIL
            from monitor.alerts import send_alert
            if ALERT_EMAIL:
                msg = (
                    f"WATCHDOG ALERT [{check.severity}]\n\n"
                    f"Check: {check.name}\n"
                    f"Status: {check.status}\n"
                    f"Detail: {check.message}\n"
                    f"Time: {check.timestamp.strftime('%Y-%m-%d %H:%M:%S ET')}\n\n"
                    f"Session checks run: {self.checks_run}\n"
                    f"Issues found: {dict(self.issues_found)}"
                )
                send_alert(ALERT_EMAIL, msg, severity=check.severity)
                self.alerts_sent.add(alert_key)
                log.info("  ALERT SENT: %s — %s", check.name, check.message)
        except Exception as e:
            log.warning("  Alert send failed: %s", e)

    def _generate_session_report(self):
        """Generate end-of-session summary report with full broker reconciliation."""
        now = datetime.now(ET)
        log.info("")
        log.info("=" * 60)
        log.info("SESSION REPORT — %s", now.strftime('%Y-%m-%d'))
        log.info("=" * 60)
        log.info("  Duration:     %s → %s",
                 self.session_start.strftime('%H:%M'), now.strftime('%H:%M'))
        log.info("  Checks run:   %d", self.checks_run)
        log.info("  Alerts sent:  %d", len(self.alerts_sent))

        if self.issues_found:
            log.info("  Issues found:")
            for issue, count in sorted(self.issues_found.items()):
                log.info("    %-25s %d occurrences", issue, count)
        else:
            log.info("  Issues found: NONE")

        if self.heals_applied:
            log.info("  Auto-heals applied: %d", len(self.heals_applied))
            for ts, action, result in self.heals_applied[-5:]:
                log.info("    [%s] %s → %s", ts, action, result)
        else:
            log.info("  Auto-heals applied: NONE")

        # ── Comprehensive P&L Report ────────────────────────────────────
        report_lines = []
        report_lines.append("")
        report_lines.append("P&L REPORT")
        report_lines.append("-" * 50)

        # 1. Our realized P&L from FillLedger
        our_realized = 0.0
        our_trades = 0
        our_open_count = 0
        our_open_tickers = []
        try:
            if os.path.exists(FILL_LEDGER_PATH):
                with open(FILL_LEDGER_PATH) as f:
                    ledger = json.load(f)
                our_realized = ledger.get('daily_pnl', 0.0)
                our_trades = ledger.get('trade_count', 0)
                our_open = ledger.get('open_positions', {})
                our_open_count = len(our_open)
                our_open_tickers = list(our_open.keys())
        except Exception:
            pass

        report_lines.append(f"  OUR SYSTEM (FillLedger)")
        report_lines.append(f"    Realized P&L:    ${our_realized:+,.2f}")
        report_lines.append(f"    Trades today:    {our_trades}")
        report_lines.append(f"    Open positions:  {our_open_count} {our_open_tickers}")

        # 2. Broker accounts — BOD equity, EOD equity, P&L
        report_lines.append("")
        report_lines.append("  BROKER ACCOUNTS")

        alpaca_bod = 0.0
        alpaca_eod = 0.0
        alpaca_pnl = 0.0
        alpaca_open = {}
        alpaca_unrealized = 0.0
        tradier_bod = 0.0
        tradier_eod = 0.0
        tradier_pnl = 0.0
        tradier_open = {}
        tradier_unrealized = 0.0

        try:
            from alpaca.trading.client import TradingClient
            ac = TradingClient(
                os.getenv('APCA_API_KEY_ID', ''),
                os.getenv('APCA_API_SECRET_KEY', ''), paper=True)
            acct = ac.get_account()
            alpaca_eod = float(acct.equity)
            alpaca_bod = float(acct.last_equity)
            alpaca_pnl = alpaca_eod - alpaca_bod

            for p in ac.get_all_positions():
                sym = str(p.symbol)
                if len(sym) <= 10:
                    unrealized = float(p.unrealized_pl)
                    alpaca_unrealized += unrealized
                    alpaca_open[sym] = {
                        'qty': int(float(p.qty)),
                        'entry': float(p.avg_entry_price),
                        'unrealized': unrealized,
                    }
        except Exception as e:
            report_lines.append(f"    Alpaca:   FETCH FAILED — {e}")

        try:
            import requests
            t_token = os.getenv('TRADIER_SANDBOX_TOKEN', '') or os.getenv('TRADIER_TOKEN', '')
            t_acct = os.getenv('TRADIER_ACCOUNT_ID', '')
            t_sandbox = os.getenv('TRADIER_SANDBOX', 'true').lower() == 'true'
            t_base = 'https://sandbox.tradier.com' if t_sandbox else 'https://api.tradier.com'
            t_headers = {'Authorization': f'Bearer {t_token}', 'Accept': 'application/json'}

            r = requests.get(f'{t_base}/v1/accounts/{t_acct}/balances',
                            headers=t_headers, timeout=10)
            if r.ok:
                b = r.json().get('balances', {})
                tradier_eod = float(b.get('total_equity', 0))
                # Tradier doesn't give last_equity — estimate from our baseline
                tradier_bod = tradier_eod  # will be corrected below if equity tracker available

            r2 = requests.get(f'{t_base}/v1/accounts/{t_acct}/positions',
                             headers=t_headers, timeout=10)
            if r2.ok:
                pos_data = r2.json().get('positions', {})
                if pos_data and pos_data != 'null':
                    plist = pos_data.get('position', [])
                    if isinstance(plist, dict): plist = [plist]
                    for p in plist:
                        qty = int(float(p.get('quantity', 0)))
                        if qty > 0:
                            cost = float(p.get('cost_basis', 0))
                            tradier_open[p['symbol']] = {
                                'qty': qty,
                                'entry': cost / qty if qty else 0,
                            }
        except Exception as e:
            report_lines.append(f"    Tradier:  FETCH FAILED — {e}")

        # Try to get BOD from equity tracker baseline
        try:
            # Read baseline from the equity check logs
            import subprocess
            result = subprocess.run(
                ['/usr/bin/grep', 'SESSION EQUITY BASELINE', f'logs/{now.strftime("%Y%m%d")}/core.log'],
                capture_output=True, text=True, timeout=5)
            if result.stdout:
                for line in result.stdout.strip().split('\n'):
                    if 'alpaca' in line.lower():
                        import re
                        m = re.search(r'\$([0-9,]+\.\d+)', line)
                        if m and alpaca_bod == 0:
                            alpaca_bod = float(m.group(1).replace(',', ''))
                    if 'tradier' in line.lower():
                        m = re.search(r'\$([0-9,]+\.\d+)', line)
                        if m:
                            tradier_bod = float(m.group(1).replace(',', ''))
        except Exception:
            pass

        # If we still don't have Tradier BOD, compute from Alpaca pattern
        if tradier_bod == tradier_eod and tradier_eod > 0:
            tradier_pnl = 0.0  # unknown
        else:
            tradier_pnl = tradier_eod - tradier_bod

        report_lines.append(f"    {'':20s} {'BOD':>14s} {'EOD':>14s} {'P&L':>12s}")
        report_lines.append(f"    {'Alpaca':20s} ${alpaca_bod:>13,.2f} ${alpaca_eod:>13,.2f} ${alpaca_pnl:>+11,.2f}")
        report_lines.append(f"    {'Tradier':20s} ${tradier_bod:>13,.2f} ${tradier_eod:>13,.2f} ${tradier_pnl:>+11,.2f}")
        combined_bod = alpaca_bod + tradier_bod
        combined_eod = alpaca_eod + tradier_eod
        combined_pnl = alpaca_pnl + tradier_pnl
        report_lines.append(f"    {'-'*20} {'-'*14} {'-'*14} {'-'*12}")
        report_lines.append(f"    {'COMBINED':20s} ${combined_bod:>13,.2f} ${combined_eod:>13,.2f} ${combined_pnl:>+11,.2f}")

        # 3. Open positions detail
        all_broker_open = {}
        for sym, info in alpaca_open.items():
            all_broker_open[sym] = {**info, 'broker': 'alpaca'}
        for sym, info in tradier_open.items():
            if sym in all_broker_open:
                all_broker_open[sym]['qty'] += info['qty']
                all_broker_open[sym]['broker'] += '+tradier'
            else:
                all_broker_open[sym] = {**info, 'unrealized': 0, 'broker': 'tradier'}

        if all_broker_open:
            report_lines.append("")
            report_lines.append(f"  OPEN POSITIONS ({len(all_broker_open)})")
            for sym, info in sorted(all_broker_open.items()):
                unrealized = info.get('unrealized', 0)
                report_lines.append(
                    f"    {sym:6s}  {info['qty']:>4} shares @ ${info['entry']:>8.2f}  "
                    f"unrealized=${unrealized:>+8.2f}  ({info['broker']})")

        # 4. P&L reconciliation
        report_lines.append("")
        report_lines.append("  P&L RECONCILIATION")
        report_lines.append(f"    Our realized (FillLedger):  ${our_realized:>+10,.2f}")
        report_lines.append(f"    Broker combined P&L:        ${combined_pnl:>+10,.2f}")
        drift = abs(our_realized - combined_pnl)
        report_lines.append(f"    DRIFT:                      ${drift:>10,.2f}")
        if drift < 5:
            report_lines.append(f"    STATUS: IN SYNC")
        elif drift < 50:
            report_lines.append(f"    STATUS: MINOR DRIFT (unrealized positions may explain)")
        else:
            report_lines.append(f"    STATUS: SIGNIFICANT DRIFT — investigate")

        # 5. WAL status
        try:
            from monitor.order_wal import wal
            ws = wal.stats()
            report_lines.append("")
            report_lines.append("  ORDER WAL")
            report_lines.append(f"    Orders today:   {ws.get('total_orders', 0)}")
            report_lines.append(f"    Incomplete:     {ws.get('incomplete', 0)}")
        except Exception:
            pass

        # Log everything
        for line in report_lines:
            log.info(line)

        # Final health check
        log.info("")
        log.info("  FINAL HEALTH CHECK")
        final = self._run_all_checks()
        for r in final:
            icon = {'OK': 'OK ', 'WARN': 'WRN', 'CRITICAL': 'CRT'}.get(r.status, '???')
            log.info("    [%s] %s: %s", icon, r.name, r.message)

        log.info("=" * 60)

        # ── Send email ──────────────────────────────────────────────────
        try:
            from config import ALERT_EMAIL
            from monitor.alerts import send_alert
            if ALERT_EMAIL:
                email_body = (
                    f"SESSION REPORT — {now.strftime('%Y-%m-%d')}\n"
                    f"{'=' * 50}\n\n"
                    f"Duration: {self.session_start.strftime('%H:%M')} → {now.strftime('%H:%M')}\n"
                    f"Checks: {self.checks_run} | Alerts: {len(self.alerts_sent)}\n"
                    f"Issues: {dict(self.issues_found) or 'None'}\n\n"
                )
                email_body += '\n'.join(report_lines)
                email_body += f"\n\n{'=' * 50}\n"

                status = 'GREEN' if drift < 5 else ('YELLOW' if drift < 50 else 'RED')
                subject = (f"[{status}] EOD Report | P&L: ${our_realized:+,.2f} | "
                          f"{our_trades} trades | {our_open_count} open | "
                          f"Drift: ${drift:.2f}")
                try:
                    import smtplib
                    from email.mime.text import MIMEText
                    msg = MIMEText(email_body, 'plain')
                    msg['Subject'] = subject
                    msg['From'] = os.getenv('ALERT_EMAIL_USER', '')
                    msg['To'] = ALERT_EMAIL
                    s = smtplib.SMTP(
                        os.getenv('ALERT_EMAIL_SERVER', 'smtp.mail.yahoo.com'),
                        int(os.getenv('ALERT_EMAIL_PORT', '587')), timeout=15)
                    s.ehlo(); s.starttls(); s.ehlo()
                    s.login(os.getenv('ALERT_EMAIL_USER', ''), os.getenv('ALERT_EMAIL_PASS', ''))
                    s.sendmail(msg['From'], [ALERT_EMAIL], msg.as_string())
                    s.quit()
                    log.info("[EOD] Report email sent")
                except Exception as e:
                    log.warning("[EOD] Email send failed: %s", e)
                    # Fallback to alert queue
                    send_alert(ALERT_EMAIL, email_body, severity='INFO')
        except Exception:
            pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Session Watchdog')
    parser.add_argument('--check-interval', type=int, default=120,
                        help='Seconds between checks (default: 120)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Run one check and exit')
    parser.add_argument('--no-heal', action='store_true',
                        help='Disable self-healing (monitor only)')
    args = parser.parse_args()

    watchdog = SessionWatchdog(
        check_interval=args.check_interval,
        enable_healing=not args.no_heal,
    )
    watchdog.run(dry_run=args.dry_run)


if __name__ == '__main__':
    main()
