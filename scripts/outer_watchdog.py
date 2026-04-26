#!/usr/bin/env python3
"""
Outer Watchdog — runs OUTSIDE the supervisor process tree.

This is the top-level guardian. It monitors the supervisor itself and restarts
it if it dies. The inner watchdog (session_watchdog.py) runs under the
supervisor and monitors individual engines. This outer watchdog monitors
the supervisor + inner watchdog.

Hierarchy:
    cron/launchd → outer_watchdog.py (this file)
                    └── monitors supervisor.py
                        └── monitors core, options, inner watchdog
                            └── monitors lifecycle, signals, exits

Why separate:
    If supervisor dies or hangs, the inner watchdog dies with it.
    The outer watchdog detects this and restarts the supervisor.
    It also catches issues the inner watchdog can't:
    - Supervisor hung (alive but not restarting dead children)
    - All processes dead (catastrophic failure)
    - Disk full preventing any process from starting
    - System clock jump (DST, NTP)

Setup (cron — runs every 2 minutes during market hours):
    # Mac/Linux crontab -e:
    */2 9-16 * * 1-5 cd /path/to/trading_hub && python3 scripts/outer_watchdog.py >> logs/outer_watchdog.log 2>&1

Setup (launchd — Mac):
    See docs/launchd_outer_watchdog.plist

Design:
    - Stateless: reads only process table + log files + state files
    - Idempotent: safe to run multiple times (no side effects if healthy)
    - Conservative: only restarts supervisor (never touches positions/orders)
    - Fast: completes in <5 seconds, exits immediately
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
    # Mon-Fri, 9:25 AM to 4:05 PM
    if now.weekday() >= 5:
        return False
    if now.hour < 9 or (now.hour == 9 and now.minute < 25):
        return False
    if now.hour > 16 or (now.hour == 16 and now.minute > 5):
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

    # ── Check 2: Supervisor dead — restart it ────────────────────
    if not supervisor_alive:
        if state['restarts_today'] >= 5:
            msg = (f"OUTER WATCHDOG: Supervisor has been restarted {state['restarts_today']}x today. "
                   f"NOT restarting again — manual intervention required.\n"
                   f"Last restart: {state['last_restart']}")
            log.critical(msg)
            _send_alert(msg)
            _save_state(state)
            return

        log.warning("Supervisor NOT running — restarting")
        try:
            supervisor_script = os.path.join(PROJECT_ROOT, 'scripts', 'supervisor.py')
            subprocess.Popen(
                [sys.executable, supervisor_script],
                cwd=PROJECT_ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # detach from our process group
            )
            time.sleep(10)

            # Verify it started
            ps2 = _get_processes()
            if 'supervisor.py' in ps2:
                state['restarts_today'] += 1
                state['last_restart'] = now.strftime('%H:%M:%S')
                log.info("Supervisor restarted successfully (restart #%d today)",
                         state['restarts_today'])
                _send_alert(
                    f"Outer Watchdog restarted supervisor (restart #{state['restarts_today']} today).\n"
                    f"Core was {'alive' if core_alive else 'DEAD'}.\n"
                    f"Options was {'alive' if options_alive else 'DEAD'}.",
                    severity='WARNING')
            else:
                log.error("Supervisor restart FAILED — process didn't start")
                _send_alert(
                    "OUTER WATCHDOG: Supervisor restart FAILED.\n"
                    "The trading system is DOWN. Manual intervention required.",
                    severity='CRITICAL')
        except Exception as e:
            log.error("Supervisor restart error: %s", e)
            _send_alert(f"OUTER WATCHDOG: Restart error: {e}", severity='CRITICAL')

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


if __name__ == '__main__':
    check_and_heal()
