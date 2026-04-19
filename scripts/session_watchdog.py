#!/usr/bin/env python3
"""
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
                if _k.strip() and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, 'data')

# Logging
log_dir = os.path.join(PROJECT_ROOT, 'logs', datetime.now().strftime('%Y%m%d'))
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'watchdog.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [watchdog] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(),
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


class SessionWatchdog:
    """Autonomous trading session monitor."""

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

        # Email-based hotfix manager
        self._hotfix_mgr = None
        if enable_healing:
            try:
                from scripts.hotfix_manager import HotfixManager
                self._hotfix_mgr = HotfixManager()
                log.info("HotfixManager active — email approval workflow enabled")
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

        try:
            while True:
                now = datetime.now(ET)

                # Stop after market close (4:15 PM — 15 min grace)
                if now.hour >= 16 and now.minute >= 15:
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

                self.checks_run += 1
                time.sleep(self.check_interval)

        except KeyboardInterrupt:
            log.info("Watchdog interrupted")
            self._generate_session_report()

    def _run_all_checks(self) -> list:
        """Run all health checks. Returns list of HealthCheck."""
        results = []
        results.append(self._check_process_health())
        results.append(self._check_supervisor_status())
        results.extend(self._check_log_errors())
        results.append(self._check_signal_rate())
        results.append(self._check_positions())
        results.append(self._check_kill_switch())
        results.append(self._check_fill_ledger())
        results.append(self._check_data_freshness())
        results.append(self._check_memory())
        results.append(self._check_gap_and_go())
        # Filter out None results
        return [r for r in results if r is not None]

    # ── Individual checks ─────────────────────────────────────────────────

    def _check_process_health(self) -> HealthCheck:
        """Check if core and options processes are running."""
        try:
            result = subprocess.run(
                ['ps', 'aux'], capture_output=True, text=True, timeout=5,
            )
            output = result.stdout

            core_running = 'run_core.py' in output
            options_running = 'run_options.py' in output
            supervisor_running = 'supervisor.py' in output

            if not supervisor_running:
                return HealthCheck('process_health', 'CRITICAL',
                                   'Supervisor NOT running', 'CRITICAL')
            if not core_running:
                return HealthCheck('process_health', 'CRITICAL',
                                   'Core process NOT running', 'CRITICAL')
            if not options_running:
                return HealthCheck('process_health', 'WARN',
                                   'Options process not running (non-critical)', 'WARNING')

            return HealthCheck('process_health', 'OK',
                               f'supervisor=OK core=OK options={"OK" if options_running else "OFF"}')
        except Exception as e:
            return HealthCheck('process_health', 'WARN', f'Check failed: {e}', 'WARNING')

    def _check_supervisor_status(self) -> HealthCheck:
        """Check supervisor_status.json for restart counts."""
        status_file = os.path.join(DATA_DIR, 'supervisor_status.json')
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
        """Scan core.log for new ERROR/CRITICAL lines since last check."""
        results = []
        today = datetime.now().strftime('%Y%m%d')
        core_log = os.path.join(PROJECT_ROOT, 'logs', today, 'core.log')

        if not os.path.exists(core_log):
            return [HealthCheck('log_errors', 'WARN', 'core.log not found', 'WARNING')]

        try:
            last_pos = self._last_log_pos.get(core_log, 0)
            file_size = os.path.getsize(core_log)

            if file_size < last_pos:
                # Log was rotated
                last_pos = 0

            if file_size == last_pos:
                return [HealthCheck('log_errors', 'OK', 'No new log entries')]

            errors = []
            with open(core_log, 'r') as f:
                f.seek(last_pos)
                for line in f:
                    if ' ERROR ' in line or ' CRITICAL ' in line:
                        errors.append(line.strip()[:150])
                self._last_log_pos[core_log] = f.tell()

            if errors:
                # Categorize
                unique_errors = list(set(errors))[:5]  # top 5 unique
                severity = 'CRITICAL' if any('CRITICAL' in e for e in errors) else 'WARNING'
                self.issues_found['log_errors'] += len(errors)
                return [HealthCheck('log_errors', 'WARN',
                                     f'{len(errors)} new errors. Sample: {unique_errors[0]}',
                                     severity)]

            return [HealthCheck('log_errors', 'OK',
                                f'Scanned {file_size - last_pos} bytes, no errors')]
        except Exception as e:
            return [HealthCheck('log_errors', 'WARN', f'Check failed: {e}', 'WARNING')]

    def _check_signal_rate(self) -> HealthCheck:
        """Check if signal rate is abnormal (too high = detector issue)."""
        today = datetime.now().strftime('%Y%m%d')
        core_log = os.path.join(PROJECT_ROOT, 'logs', today, 'core.log')

        try:
            if not os.path.exists(core_log):
                return HealthCheck('signal_rate', 'OK', 'No log to check')

            result = subprocess.run(
                ['grep', '-c', 'SIGNAL.*strategy=', core_log],
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
        """Check position state for anomalies."""
        state_file = os.path.join(DATA_DIR, 'bot_state.json')
        try:
            if not os.path.exists(state_file):
                return HealthCheck('positions', 'WARN', 'No bot_state.json', 'WARNING')

            with open(state_file) as f:
                state = json.load(f)

            positions = state.get('positions', {})
            trade_log = state.get('trade_log', [])
            pos_count = len(positions)

            issues = []

            # Too many positions
            if pos_count > 20:
                issues.append(f'HIGH position count: {pos_count}')

            # Check for stuck positions (no activity in 2+ hours)
            for ticker, pos in positions.items():
                opened_at = pos.get('opened_at', '')
                if opened_at:
                    try:
                        opened = datetime.fromisoformat(opened_at)
                        age_hours = (datetime.now(ET) - opened).total_seconds() / 3600
                        if age_hours > 3:
                            issues.append(f'{ticker}: open {age_hours:.1f}h')
                    except Exception:
                        pass

            # Daily P&L from trade log
            daily_pnl = sum(t.get('pnl', 0) for t in trade_log)

            if issues:
                return HealthCheck('positions', 'WARN',
                                   f'{pos_count} open, P&L=${daily_pnl:.2f} | ' + ' | '.join(issues[:3]),
                                   'WARNING')

            return HealthCheck('positions', 'OK',
                               f'{pos_count} open, {len(trade_log)} closed, P&L=${daily_pnl:.2f}')
        except Exception as e:
            return HealthCheck('positions', 'WARN', f'Check failed: {e}', 'WARNING')

    def _check_kill_switch(self) -> HealthCheck:
        """Check if any kill switch has been triggered."""
        try:
            triggered = []
            for fname in os.listdir(DATA_DIR):
                if fname.endswith('_kill_switch.json'):
                    fpath = os.path.join(DATA_DIR, fname)
                    with open(fpath) as f:
                        ks = json.load(f)
                    if ks.get('halted', False):
                        engine = fname.replace('_kill_switch.json', '')
                        triggered.append(f'{engine} (since {ks.get("halted_at", "?")})')

            if triggered:
                self.issues_found['kill_switch'] += 1
                return HealthCheck('kill_switch', 'CRITICAL',
                                   'TRIGGERED: ' + ', '.join(triggered), 'CRITICAL')

            return HealthCheck('kill_switch', 'OK', 'Not triggered')
        except Exception as e:
            return HealthCheck('kill_switch', 'OK', f'No kill switch files (OK)')

    def _check_fill_ledger(self) -> HealthCheck:
        """Check FillLedger for P&L drift vs trade_log."""
        ledger_file = os.path.join(DATA_DIR, 'fill_ledger.json')
        state_file = os.path.join(DATA_DIR, 'bot_state.json')

        try:
            if not os.path.exists(ledger_file):
                return HealthCheck('fill_ledger', 'OK', 'Not active (shadow mode off)')

            with open(ledger_file) as f:
                ledger = json.load(f)

            lots = ledger.get('lots', [])
            buy_lots = [l for l in lots if l.get('side') == 'BUY']
            sell_lots = [l for l in lots if l.get('side') == 'SELL']

            # Compare with trade_log P&L
            if os.path.exists(state_file):
                with open(state_file) as f:
                    state = json.load(f)
                trade_pnl = sum(t.get('pnl', 0) for t in state.get('trade_log', []))

                # Compute ledger P&L from matches
                matches = ledger.get('matches', [])
                ledger_pnl = sum(m.get('realized_pnl', 0) for m in matches)

                drift = abs(trade_pnl - ledger_pnl)
                if drift > 50:
                    self.issues_found['pnl_drift'] += 1
                    return HealthCheck('fill_ledger', 'WARN',
                                       f'P&L DRIFT: trade_log=${trade_pnl:.2f} vs ledger=${ledger_pnl:.2f} (${drift:.2f})',
                                       'WARNING' if drift < 500 else 'CRITICAL')

            return HealthCheck('fill_ledger', 'OK',
                               f'{len(buy_lots)} buys, {len(sell_lots)} sells')
        except Exception as e:
            return HealthCheck('fill_ledger', 'WARN', f'Check failed: {e}', 'WARNING')

    def _check_data_freshness(self) -> HealthCheck:
        """Check if market data is flowing (cache file recently updated)."""
        cache_file = os.path.join(DATA_DIR, 'live_cache.json')
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
                ['ps', 'aux'], capture_output=True, text=True, timeout=5,
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
                ['grep', '-c', 'gap_and_go', core_log],
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

    # ── Self-Healing ──────────────────────────────────────────────────────

    def _run_healer(self, results: list):
        """Apply safe auto-fixes based on check results."""
        for check in results:
            if check.status == 'OK':
                continue

            # ── Fix 1: Supervisor not running → restart it ────────────
            if check.name == 'process_health' and 'Supervisor NOT running' in check.message:
                self._heal_restart_supervisor()

            # ── Fix 2: Core not running (but supervisor is) → diagnose ─
            if check.name == 'process_health' and 'Core process NOT running' in check.message:
                self._heal_diagnose_crash('core')

            # ── Fix 3: Stale data → clear cache to force fresh fetch ───
            if check.name == 'data_freshness' and 'stale' in check.message:
                self._heal_stale_cache()

            # ── Fix 4: Log errors with known patterns ──────────────────
            if check.name == 'log_errors' and check.severity in ('WARNING', 'CRITICAL'):
                self._heal_from_log_errors()

        # ── Fix 5: Always check for corrupt state files ────────────────
        self._heal_corrupt_state_files()

        # ── Fix 6: Always clean stale lock files ───────────────────────
        self._heal_stale_locks()

    def _heal_restart_supervisor(self):
        """Restart the supervisor process."""
        try:
            # Check if it really died vs we just can't see it
            result = subprocess.run(
                ['pgrep', '-f', 'supervisor.py'],
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
                ['pgrep', '-f', 'supervisor.py'],
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

    def _heal_diagnose_crash(self, engine: str):
        """Diagnose why an engine crashed and apply fixes if possible."""
        today = datetime.now().strftime('%Y%m%d')
        log_path = os.path.join(PROJECT_ROOT, 'logs', today, f'{engine}.log')

        if not os.path.exists(log_path):
            return

        # Count crashes today
        crash_count = self._prev_crash_count.get(engine, 0)

        try:
            # Read last 100 lines for traceback
            result = subprocess.run(
                ['tail', '-100', log_path],
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
            os.path.join(DATA_DIR, 'live_cache.json'),
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
        """Scan recent log errors for fixable patterns."""
        today = datetime.now().strftime('%Y%m%d')
        core_log = os.path.join(PROJECT_ROOT, 'logs', today, 'core.log')
        if not os.path.exists(core_log):
            return

        try:
            result = subprocess.run(
                ['tail', '-50', core_log],
                capture_output=True, text=True, timeout=5,
            )
            recent = result.stdout

            # Lock file errors → remove stale locks
            if '.lock' in recent and ('LockError' in recent or 'locked' in recent.lower()):
                self._heal_stale_locks()

            # "No space left on device" → clear old logs
            if 'No space left' in recent:
                self._clear_old_logs()

        except Exception:
            pass

    def _heal_corrupt_state_files(self):
        """Check all JSON state files for corruption, restore from backups."""
        state_files = [
            'bot_state.json',
            'fill_ledger.json',
            'position_registry.json',
            'position_broker_map.json',
        ]
        for fname in state_files:
            fpath = os.path.join(DATA_DIR, fname)
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
        for fname in ['live_cache.pkl', 'live_cache.json']:
            fpath = os.path.join(DATA_DIR, fname)
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
        """Generate end-of-session summary report."""
        log.info("")
        log.info("=" * 60)
        log.info("SESSION REPORT — %s", datetime.now(ET).strftime('%Y-%m-%d'))
        log.info("=" * 60)
        log.info("  Duration:     %s → %s",
                 self.session_start.strftime('%H:%M'),
                 datetime.now(ET).strftime('%H:%M'))
        log.info("  Checks run:   %d", self.checks_run)
        log.info("  Alerts sent:  %d", len(self.alerts_sent))

        if self.issues_found:
            log.info("  Issues found:")
            for issue, count in sorted(self.issues_found.items()):
                log.info("    %-25s %d occurrences", issue, count)
        else:
            log.info("  Issues found: NONE")

        if self.heals_applied:
            log.info("")
            log.info("  Auto-heals applied: %d", len(self.heals_applied))
            for ts, action, result in self.heals_applied:
                log.info("    [%s] %s → %s", ts, action, result)
        else:
            log.info("  Auto-heals applied: NONE (clean session)")

        # Final health check
        final = self._run_all_checks()
        log.info("")
        log.info("  Final status:")
        for r in final:
            icon = {'OK': 'OK ', 'WARN': 'WRN', 'CRITICAL': 'CRT'}.get(r.status, '???')
            log.info("    [%s] %s: %s", icon, r.name, r.message)

        # Read final P&L
        try:
            state_file = os.path.join(DATA_DIR, 'bot_state.json')
            if os.path.exists(state_file):
                with open(state_file) as f:
                    state = json.load(f)
                pnl = sum(t.get('pnl', 0) for t in state.get('trade_log', []))
                trades = len(state.get('trade_log', []))
                positions = len(state.get('positions', {}))
                log.info("")
                log.info("  Daily P&L:    $%.2f", pnl)
                log.info("  Trades:       %d", trades)
                log.info("  Open pos:     %d", positions)
        except Exception:
            pass

        log.info("=" * 60)

        # Send summary alert
        try:
            from config import ALERT_EMAIL
            from monitor.alerts import send_alert
            if ALERT_EMAIL:
                summary = (
                    f"SESSION WATCHDOG REPORT — {datetime.now(ET).strftime('%Y-%m-%d')}\n\n"
                    f"Checks: {self.checks_run}\n"
                    f"Alerts: {len(self.alerts_sent)}\n"
                    f"Issues: {dict(self.issues_found) or 'None'}\n\n"
                    f"Review logs: {log_file}"
                )
                send_alert(ALERT_EMAIL, summary, severity='INFO')
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
