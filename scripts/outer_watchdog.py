#!/usr/bin/env python3
"""
Outer Watchdog — long-running process OUTSIDE the supervisor tree.

This is the TOP-LEVEL guardian. Start it BEFORE the supervisor. It:
  1. Starts the supervisor
  2. Monitors it continuously (every 60s)
  3. Restarts it if it dies or hangs
  4. Shuts down after market close

Hierarchy:
    outer_watchdog.py (this file — start this, not supervisor directly)
      └── supervisor.py (started by us)
            └── core, options, inner watchdog

Why separate:
    If supervisor dies or hangs, inner watchdog dies with it.
    This process is the only thing that can fix that.

Usage:
    python scripts/outer_watchdog.py                  # normal: start + monitor
    python scripts/outer_watchdog.py --check-only     # one check, no loop
    python scripts/outer_watchdog.py --no-start       # monitor only (supervisor already running)

Design:
    - Long-running loop (not cron)
    - Starts supervisor on launch
    - Checks every 60s during market hours
    - Conservative: only restarts supervisor (never touches positions/orders)
    - Max 5 restarts/day, then alerts for manual intervention
"""
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime

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
                    os.environ[_k.strip()] = _v

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Logging
log_dir = os.path.join(PROJECT_ROOT, 'logs', datetime.now().strftime('%Y%m%d'))
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'outer_watchdog.log')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [outer-watchdog] %(message)s',
    handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# State file for tracking restart attempts
_STATE_FILE = os.path.join(PROJECT_ROOT, 'data', 'outer_watchdog_state.json')


def _get_processes() -> str:
    """Get process list."""
    for cmd in ['/bin/ps', '/usr/bin/ps', 'ps']:
        try:
            r = subprocess.run([cmd, 'aux'], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                return r.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return ''


def _is_market_hours() -> bool:
    now = datetime.now(ET)
    today = now.date()

    # Weekend
    if now.weekday() >= 5:
        return False

    # Market holidays (NYSE/NASDAQ full closures)
    try:
        from monitor.market_calendar import is_market_holiday, is_early_close, early_close_hour
        if is_market_holiday(today):
            return False
        # Early close days: market closes at 1 PM ET
        if is_early_close(today):
            close_hour = early_close_hour()
            if now.hour > close_hour or (now.hour == close_hour and now.minute > 5):
                return False
    except ImportError:
        pass  # calendar not available — proceed with standard hours

    # Standard hours: 9:25 AM to 4:00 PM ET
    # Core exits at 16:00 (hour >= 16). Watchdog must match — otherwise it
    # restarts supervisor after EOD, supervisor starts core, core exits again,
    # watchdog thinks it crashed → 3 pointless restart attempts.
    if now.hour < 9 or (now.hour == 9 and now.minute < 25):
        return False
    if now.hour >= 16:
        return False
    return True


def _load_state() -> dict:
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {'restarts_today': 0, 'last_restart': '', 'date': ''}


def _save_state(state: dict):
    try:
        with open(_STATE_FILE, 'w') as f:
            json.dump(state, f, indent=2)
    except Exception:
        pass


def _send_alert(message: str, severity: str = 'CRITICAL'):
    """Send alert via SMTP (bypass trading system alert queue)."""
    try:
        import smtplib
        from email.mime.text import MIMEText

        email_user = os.getenv('ALERT_EMAIL_USER', '')
        email_pass = os.getenv('ALERT_EMAIL_PASS', '')
        alert_email = os.getenv('ALERT_EMAIL', '')
        smtp_server = os.getenv('ALERT_EMAIL_SERVER', 'smtp.mail.yahoo.com')
        smtp_port = int(os.getenv('ALERT_EMAIL_PORT', '587'))

        if not all([email_user, email_pass, alert_email]):
            return

        msg = MIMEText(message, 'plain')
        msg['Subject'] = f'[{severity}] Outer Watchdog — {datetime.now(ET).strftime("%H:%M ET")}'
        msg['From'] = email_user
        msg['To'] = alert_email

        s = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
        s.ehlo(); s.starttls(); s.ehlo()
        s.login(email_user, email_pass)
        s.sendmail(email_user, [alert_email], msg.as_string())
        s.quit()
        log.info("Alert sent: %s", message[:100])
    except Exception as e:
        log.warning("Alert send failed: %s", e)


def _diagnose_death() -> dict:
    """Diagnose why the supervisor died. Returns {reason, fix, details}."""
    today = datetime.now().strftime('%Y%m%d')
    sup_log = os.path.join(PROJECT_ROOT, 'logs', today, 'supervisor.log')
    result = {'reason': 'unknown', 'fix': None, 'details': ''}

    # 1. Read last 50 lines of supervisor log
    traceback = ''
    if os.path.exists(sup_log):
        try:
            r = subprocess.run(['/usr/bin/tail', '-50', sup_log],
                               capture_output=True, text=True, timeout=5)
            tail = r.stdout
            if 'Traceback' in tail:
                traceback = tail[tail.rfind('Traceback'):][:500]
        except Exception:
            pass

    # 2. Pattern match: known failure modes
    if traceback:
        if 'JSONDecodeError' in traceback:
            result['reason'] = 'corrupt JSON state file'
            result['fix'] = 'restore_json'
            result['details'] = traceback[:200]
        elif 'PermissionError' in traceback:
            result['reason'] = 'permission error on file'
            result['fix'] = 'fix_permissions'
            result['details'] = traceback[:200]
        elif 'No space left' in traceback or 'OSError' in traceback:
            result['reason'] = 'disk full or OS error'
            result['fix'] = 'clean_disk'
            result['details'] = traceback[:200]
        elif 'ModuleNotFoundError' in traceback or 'ImportError' in traceback:
            result['reason'] = 'import error (corrupted pycache?)'
            result['fix'] = 'clear_pycache'
            result['details'] = traceback[:200]
        elif 'ConnectionRefusedError' in traceback or '401' in traceback:
            result['reason'] = 'API connection failed (token expired?)'
            result['fix'] = None  # can't auto-fix, just restart
            result['details'] = traceback[:200]
        elif 'MemoryError' in traceback or 'Killed' in tail:
            result['reason'] = 'out of memory (OOM killed)'
            result['fix'] = 'clean_disk'  # free memory by cleaning logs
            result['details'] = 'Process killed by OS'
        else:
            result['reason'] = f'crash: {traceback.split(chr(10))[-1][:100]}'
            result['details'] = traceback[:200]
    else:
        # No traceback — clean exit or killed
        if os.path.exists(sup_log):
            age = (time.time() - os.path.getmtime(sup_log)) / 60
            result['reason'] = f'exited cleanly or killed (log {age:.0f}min old)'
        else:
            result['reason'] = 'no supervisor log found'
            result['fix'] = 'clean_state'

    return result


def _apply_fix(fix_type: str):
    """Apply a pre-restart fix based on diagnosis."""
    try:
        if fix_type == 'restore_json':
            # Restore corrupt JSON files from backups
            from config import DATA_DIR
            for fname in os.listdir(DATA_DIR):
                if fname.endswith('.json') and not fname.endswith(('.prev', '.prev2', '.sha256')):
                    fpath = os.path.join(DATA_DIR, fname)
                    try:
                        with open(fpath) as f:
                            json.load(f)
                    except (json.JSONDecodeError, ValueError):
                        backup = fpath + '.prev'
                        if os.path.exists(backup):
                            import shutil
                            shutil.copy2(backup, fpath)
                            log.info("Restored %s from .prev", fname)

        elif fix_type == 'fix_permissions':
            from config import DATA_DIR
            for fname in os.listdir(DATA_DIR):
                fpath = os.path.join(DATA_DIR, fname)
                try:
                    os.chmod(fpath, 0o644)
                except Exception:
                    pass

        elif fix_type == 'clean_disk':
            # Remove old logs (>3 days)
            import shutil
            from datetime import timedelta
            logs_dir = os.path.join(PROJECT_ROOT, 'logs')
            cutoff = datetime.now() - timedelta(days=3)
            for entry in os.listdir(logs_dir):
                entry_path = os.path.join(logs_dir, entry)
                if os.path.isdir(entry_path) and len(entry) == 8:
                    try:
                        dir_date = datetime.strptime(entry, '%Y%m%d')
                        if dir_date < cutoff:
                            shutil.rmtree(entry_path)
                            log.info("Cleaned old logs: %s", entry)
                    except (ValueError, OSError):
                        pass

        elif fix_type == 'clear_pycache':
            import shutil
            for root, dirs, files in os.walk(PROJECT_ROOT):
                if '__pycache__' in dirs:
                    shutil.rmtree(os.path.join(root, '__pycache__'))

        elif fix_type == 'clean_state':
            # Remove stale lock files
            from config import DATA_DIR
            for fname in os.listdir(DATA_DIR):
                if fname.endswith('.lock') or fname.endswith('.flock'):
                    fpath = os.path.join(DATA_DIR, fname)
                    age = time.time() - os.path.getmtime(fpath)
                    if age > 300:
                        os.remove(fpath)
                        log.info("Removed stale lock: %s", fname)

        log.info("Fix '%s' applied successfully", fix_type)
    except Exception as e:
        log.warning("Fix '%s' failed: %s", fix_type, e)


def check_and_heal():
    """Main check: is the trading system healthy? Fix if not."""
    now = datetime.now(ET)
    today = now.strftime('%Y-%m-%d')

    # Load state (reset daily)
    state = _load_state()
    if state.get('date') != today:
        state = {'restarts_today': 0, 'last_restart': '', 'date': today}

    if not _is_market_hours():
        log.info("Outside market hours — skipping")
        return

    ps = _get_processes()

    # ── Check 1: Is supervisor running? ──────────────────────────
    supervisor_alive = 'supervisor.py' in ps
    core_alive = 'run_core.py' in ps
    options_alive = 'run_options.py' in ps
    inner_watchdog_alive = 'session_watchdog' in ps or 'run_watchdog' in ps

    log.info("Status: supervisor=%s core=%s options=%s watchdog=%s",
             supervisor_alive, core_alive, options_alive, inner_watchdog_alive)

    if supervisor_alive and core_alive:
        # Everything healthy
        log.info("All healthy — nothing to do")
        _save_state(state)
        return

    # ── Check 2: Supervisor dead — diagnose, fix, restart ─────────
    if not supervisor_alive:
        if state['restarts_today'] >= 5:
            msg = (f"OUTER WATCHDOG: Supervisor has been restarted {state['restarts_today']}x today. "
                   f"NOT restarting again — manual intervention required.\n"
                   f"Last restart: {state['last_restart']}")
            log.critical(msg)
            _send_alert(msg)
            _save_state(state)
            return

        # ── Pre-restart diagnostics: WHY did it die? ─────────────
        diagnosis = _diagnose_death()
        log.warning("Supervisor NOT running — diagnosis: %s", diagnosis['reason'])

        # ── Pre-restart fixes: clean up before restarting ────────
        if diagnosis.get('fix'):
            log.info("Applying pre-restart fix: %s", diagnosis['fix'])
            _apply_fix(diagnosis['fix'])

        # ── Restart supervisor ───────────────────────────────────
        if start_supervisor():
            state['restarts_today'] += 1
            state['last_restart'] = now.strftime('%H:%M:%S')
            state.setdefault('restart_log', []).append({
                'time': now.strftime('%H:%M:%S'),
                'reason': diagnosis['reason'],
                'fix': diagnosis.get('fix', 'none'),
            })
            # Keep only last 10 entries
            state['restart_log'] = state['restart_log'][-10:]

            _send_alert(
                f"Outer Watchdog restarted supervisor (#{state['restarts_today']} today).\n"
                f"Reason: {diagnosis['reason']}\n"
                f"Fix applied: {diagnosis.get('fix', 'none')}\n"
                f"Core was {'alive' if core_alive else 'DEAD'}.\n"
                f"Options was {'alive' if options_alive else 'DEAD'}.",
                severity='WARNING')
        else:
            _send_alert(
                "OUTER WATCHDOG: Supervisor restart FAILED.\n"
                f"Diagnosis: {diagnosis['reason']}\n"
                "The trading system is DOWN. Manual intervention required.",
                severity='CRITICAL')

    # ── Check 3: Supervisor alive but core dead ──────────────────
    elif supervisor_alive and not core_alive:
        # Supervisor should auto-restart core. Check if it's stuck.
        today_str = now.strftime('%Y%m%d')
        supervisor_log = os.path.join(PROJECT_ROOT, 'logs', today_str, 'supervisor.log')
        log_age = 999
        if os.path.exists(supervisor_log):
            log_age = (time.time() - os.path.getmtime(supervisor_log)) / 60

        if log_age > 5:
            # Supervisor log stale — it's hung
            log.warning("Supervisor alive but log stale (%.0fmin) and core dead — killing supervisor",
                        log_age)
            try:
                subprocess.run(['/usr/bin/pkill', '-f', 'supervisor.py'],
                               timeout=5, capture_output=True)
                time.sleep(3)
                # Will be restarted on next cron cycle
                _send_alert(
                    f"OUTER WATCHDOG: Supervisor was hung (log stale {log_age:.0f}min, core dead). "
                    f"Killed supervisor — will restart on next check cycle.",
                    severity='WARNING')
            except Exception as e:
                log.error("Kill supervisor failed: %s", e)
        else:
            # Supervisor recently active — give it time to restart core
            log.info("Core dead but supervisor active (log %.0fmin old) — waiting for auto-restart",
                     log_age)

    # ── Check 4: Disk space ──────────────────────────────────────
    try:
        stat = os.statvfs(PROJECT_ROOT)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        if free_gb < 1.0:
            log.critical("DISK LOW: %.1f GB free", free_gb)
            _send_alert(f"DISK SPACE CRITICAL: {free_gb:.1f} GB free. "
                        f"Trading may fail due to state file writes.",
                        severity='CRITICAL')
            # Auto-clean old logs
            try:
                from datetime import timedelta
                logs_dir = os.path.join(PROJECT_ROOT, 'logs')
                cutoff = datetime.now() - timedelta(days=3)
                import shutil
                for entry in os.listdir(logs_dir):
                    entry_path = os.path.join(logs_dir, entry)
                    if os.path.isdir(entry_path) and len(entry) == 8:
                        try:
                            dir_date = datetime.strptime(entry, '%Y%m%d')
                            if dir_date < cutoff:
                                shutil.rmtree(entry_path)
                                log.info("Cleaned old logs: %s", entry)
                        except (ValueError, OSError):
                            pass
            except Exception:
                pass
    except (OSError, AttributeError):
        pass

    _save_state(state)


def start_supervisor() -> bool:
    """Start the supervisor process. Returns True if started successfully."""
    supervisor_script = os.path.join(PROJECT_ROOT, 'scripts', 'supervisor.py')
    if not os.path.exists(supervisor_script):
        log.error("Supervisor script not found: %s", supervisor_script)
        return False

    log.info("Starting supervisor...")
    try:
        subprocess.Popen(
            [sys.executable, supervisor_script],
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(10)  # give it time to start children

        ps = _get_processes()
        if 'supervisor.py' in ps:
            log.info("Supervisor started successfully")
            return True
        else:
            log.error("Supervisor failed to start")
            return False
    except Exception as e:
        log.error("Supervisor start error: %s", e)
        return False


def run_loop(auto_start: bool = True, check_interval: int = 60):
    """Main loop — start supervisor, then monitor continuously.

    Merges outer watchdog (process-level) with inner watchdog (quality checks):
    - Process health: is supervisor/core/options alive?
    - Quality health: lifecycle crashes, P&L drift, data staleness, signal rate
    - Self-healing: corrupt JSON, stale locks, zombie processes, cache cleanup
    - Periodic updates: hourly email with P&L and position summary
    - Session report: EOD comprehensive report with broker reconciliation
    """
    log.info("=" * 60)
    log.info("UNIFIED WATCHDOG STARTED")
    log.info("  Check interval: %ds", check_interval)
    log.info("  Auto-start supervisor: %s", auto_start)
    log.info("=" * 60)

    # Initialize inner watchdog (quality checks + healing + reporting)
    inner = None
    try:
        from scripts.session_watchdog import SessionWatchdog
        inner = SessionWatchdog(check_interval=check_interval, enable_healing=True)
        log.info("Inner watchdog checks loaded (15 health checks + self-healing)")
    except Exception as e:
        log.warning("Inner watchdog init failed: %s — running process checks only", e)

    # Start supervisor if requested and not already running
    if auto_start:
        ps = _get_processes()
        if 'supervisor.py' not in ps:
            if _is_market_hours():
                start_supervisor()
            else:
                log.info("Outside market hours — will start supervisor when market opens")

    # Handle shutdown gracefully
    import signal as _signal
    _running = True
    def _on_shutdown(signum, frame):
        nonlocal _running
        log.info("Shutdown signal received")
        _running = False
    _signal.signal(_signal.SIGTERM, _on_shutdown)
    _signal.signal(_signal.SIGINT, _on_shutdown)

    was_market_hours = False
    eod_done_today = False

    while _running:
        try:
            now = datetime.now(ET)
            in_market = _is_market_hours()

            # ── Outside market hours: sleep, don't exit ──────────
            # launchd KeepAlive would restart us immediately if we exit,
            # wasting CPU. Instead, sleep until next market window.
            if not in_market:
                if was_market_hours and not eod_done_today:
                    # Just crossed market close — run EOD report once
                    log.info("Market closed — generating session report")
                    if inner:
                        try:
                            inner._generate_session_report()
                        except Exception as re:
                            log.warning("Session report failed: %s", re)
                    eod_done_today = True
                    was_market_hours = False

                # Reset EOD flag at midnight
                if now.hour == 0 and now.minute < 2:
                    eod_done_today = False
                    if inner:
                        inner.checks_run = 0
                        inner.alerts_sent.clear()
                        inner.issues_found.clear()
                        inner.heals_applied.clear()

                # Sleep longer outside market hours (5 min)
                time.sleep(300)
                continue

            # ── Market hours: active monitoring ──────────────────

            # Start supervisor at market open
            if in_market and not was_market_hours and auto_start:
                ps = _get_processes()
                if 'supervisor.py' not in ps:
                    log.info("Market opened — starting supervisor")
                    start_supervisor()
                eod_done_today = False  # new trading day
            was_market_hours = in_market

            # ── Outer checks: process-level health ───────────────
            check_and_heal()

            # ── Inner checks: quality-level health + healing ─────
            if inner:
                try:
                    results = inner._run_all_checks()
                    inner._print_results(results)
                    if inner.enable_healing:
                        inner._run_healer(results)
                    # Alert on critical issues
                    for check in results:
                        if check.severity == 'CRITICAL':
                            inner._send_alert(check)
                    # Hourly status email
                    inner._maybe_send_periodic_update(results)
                    inner.checks_run += 1
                except Exception as ie:
                    log.warning("Inner check cycle failed: %s", ie)

            time.sleep(check_interval)

        except Exception as e:
            log.error("Check cycle error: %s", e)
            time.sleep(check_interval)

    log.info("Unified watchdog stopped")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Outer Watchdog')
    parser.add_argument('--check-only', action='store_true',
                        help='Run one check and exit')
    parser.add_argument('--no-start', action='store_true',
                        help='Do not auto-start supervisor')
    parser.add_argument('--interval', type=int, default=60,
                        help='Check interval in seconds (default: 60)')
    args = parser.parse_args()

    if args.check_only:
        check_and_heal()
    else:
        run_loop(auto_start=not args.no_start, check_interval=args.interval)
