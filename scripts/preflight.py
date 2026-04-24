"""
Startup Preflight Gate — validates system health before trading begins.

Runs 10 checks. If any CRITICAL check fails, trading is blocked.
WARNING checks are logged but don't prevent startup.

Called by supervisor.py before starting any engine processes.
Can also be run standalone: python scripts/preflight.py

Usage:
    from scripts.preflight import run_preflight
    errors, warnings = run_preflight()
    if errors:
        sys.exit(1)  # don't trade

Skip in emergencies:
    SKIP_PREFLIGHT=1 ./start_monitor.sh
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import subprocess
import sys
import time
from typing import List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load .env
_env_path = os.path.join(PROJECT_ROOT, '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.split('#')[0].strip()
            if '=' in _line:
                _k, _v = _line.split('=', 1)
                _v = _v.strip().strip('"').strip("'")
                if _k.strip():
                    os.environ[_k.strip()] = _v  # always use project .env (overrides stale env)


def run_preflight() -> Tuple[List[str], List[str]]:
    """Run all preflight checks.

    Returns:
        (errors, warnings) — errors are CRITICAL (block trading),
        warnings are logged but non-blocking.
    """
    errors = []
    warnings = []

    def _check(name, fn):
        try:
            result = fn()
            if result is None:
                log.info("[PREFLIGHT]  OK   %s", name)
            elif result.startswith('WARN:'):
                msg = result[5:].strip()
                log.warning("[PREFLIGHT]  WRN  %s: %s", name, msg)
                warnings.append(f"{name}: {msg}")
            else:
                log.error("[PREFLIGHT]  FAIL %s: %s", name, result)
                errors.append(f"{name}: {result}")
        except Exception as exc:
            log.error("[PREFLIGHT]  FAIL %s: %s", name, exc)
            errors.append(f"{name}: {exc}")

    log.info("[PREFLIGHT] ════════════════════════════════════════════")
    log.info("[PREFLIGHT] STARTUP PREFLIGHT — 10 checks")
    log.info("[PREFLIGHT] ════════════════════════════════════════════")

    _check("1. State files clean",         _check_state_files)
    _check("2. SMTP connectivity",         _check_smtp)
    _check("3. Alpaca broker",             _check_alpaca)
    _check("4. Tradier broker",            _check_tradier)
    _check("5. Redpanda/IPC",              _check_redpanda)
    _check("6. Database",                  _check_database)
    _check("7. Subprocess commands",       _check_commands)
    _check("8. Kill switch state",         _check_kill_switch)
    _check("9. Disk space",               _check_disk_space)
    _check("10. Data directory",           _check_data_dir)

    log.info("[PREFLIGHT] ════════════════════════════════════════════")
    if errors:
        log.error("[PREFLIGHT] %d CRITICAL failures — TRADING BLOCKED", len(errors))
        for e in errors:
            log.error("[PREFLIGHT]   ✗ %s", e)
    if warnings:
        log.warning("[PREFLIGHT] %d warnings (non-blocking)", len(warnings))
        for w in warnings:
            log.warning("[PREFLIGHT]   ! %s", w)
    if not errors and not warnings:
        log.info("[PREFLIGHT] ALL CHECKS PASSED")
    elif not errors:
        log.info("[PREFLIGHT] PASSED with %d warnings", len(warnings))
    log.info("[PREFLIGHT] ════════════════════════════════════════════")

    return errors, warnings


# ══════════════════════════════════════════════════════════════════════════
# Individual checks — return None for OK, string for failure
# ══════════════════════════════════════════════════════════════════════════

def _check_state_files():
    """Check state files for test data pollution."""
    from config import BOT_STATE_PATH

    if not os.path.exists(BOT_STATE_PATH):
        return None  # no state file = fresh start, OK

    try:
        with open(BOT_STATE_PATH) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return f"bot_state.json corrupt: {e}"

    # Check for test tickers in positions
    positions = state.get('positions', {})
    test_tickers = [t for t in positions
                    if t.startswith('CRASH') or t.startswith('TEST')
                    or t.startswith('FAKE') or t in ('SAT1', 'SAT2', 'SAT3',
                    'DUP_TEST', 'FAIL_TEST', 'OVER_SELL', 'DEDUP1')]
    if test_tickers:
        return f"Test data in positions: {test_tickers}"

    # Check for test tickers in trade_log
    trade_log = state.get('trade_log', [])
    test_trades = [t.get('ticker') for t in trade_log
                   if t.get('ticker', '').startswith('CRASH')
                   or t.get('ticker', '').startswith('TEST')]
    if test_trades:
        return f"Test data in trade_log: {test_trades}"

    return None


def _check_smtp():
    """Test SMTP connection + auth. Non-blocking warning if fails."""
    user = os.getenv('ALERT_EMAIL_USER', '')
    passwd = os.getenv('ALERT_EMAIL_PASS', '')
    server = os.getenv('ALERT_EMAIL_SERVER', 'smtp.mail.yahoo.com')
    port = int(os.getenv('ALERT_EMAIL_PORT', '587'))

    if not user or not passwd:
        return "WARN: ALERT_EMAIL_USER/PASS not set — no email alerts"

    try:
        s = smtplib.SMTP(server, port, timeout=10)
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(user, passwd)
        s.quit()
        return None
    except smtplib.SMTPAuthenticationError:
        return "SMTP auth failed — check ALERT_EMAIL_PASS (Yahoo app password)"
    except (ConnectionRefusedError, TimeoutError, OSError) as e:
        return f"WARN: SMTP connection failed: {e} — emails may not work"


def _check_alpaca():
    """Verify Alpaca API credentials and connectivity."""
    key = os.getenv('APCA_API_KEY_ID', '')
    secret = os.getenv('APCA_API_SECRET_KEY', '')

    if not key or not secret:
        return "APCA_API_KEY_ID or APCA_API_SECRET_KEY not set"

    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(key, secret, paper=True)
        account = client.get_account()
        equity = float(account.equity)
        log.info("[PREFLIGHT]        Alpaca equity: $%s", f"{equity:,.2f}")
        return None
    except Exception as e:
        return f"Alpaca API failed: {e}"


def _check_tradier():
    """Verify Tradier API credentials and connectivity."""
    token = os.getenv('TRADIER_SANDBOX_TOKEN', '') or os.getenv('TRADIER_TOKEN', '')
    acct = os.getenv('TRADIER_ACCOUNT_ID', '')

    if not token or not acct:
        return "WARN: TRADIER_SANDBOX_TOKEN or TRADIER_ACCOUNT_ID not set"

    try:
        import requests
        sandbox = os.getenv('TRADIER_SANDBOX', 'true').lower() == 'true'
        base = 'https://sandbox.tradier.com' if sandbox else 'https://api.tradier.com'
        resp = requests.get(
            f'{base}/v1/accounts/{acct}/balances',
            headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
            timeout=10,
        )
        if resp.status_code == 200:
            equity = resp.json().get('balances', {}).get('total_equity', 0)
            log.info("[PREFLIGHT]        Tradier equity: $%s", f"{equity:,.2f}")
            return None
        return f"Tradier API returned {resp.status_code}"
    except Exception as e:
        return f"WARN: Tradier API failed: {e}"


def _check_redpanda():
    """Verify Redpanda/Kafka is reachable."""
    brokers = os.getenv('REDPANDA_BROKERS', '127.0.0.1:9092')

    try:
        import socket
        host, port = brokers.split(':')
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, int(port)))
        sock.close()
        if result == 0:
            return None
        return f"WARN: Redpanda not reachable at {brokers} — IPC will fail"
    except Exception as e:
        return f"WARN: Redpanda check failed: {e}"


def _check_database():
    """Verify TimescaleDB connectivity."""
    dsn = os.getenv('DATABASE_URL', '')
    db_enabled = os.getenv('DB_ENABLED', 'true').lower() == 'true'

    if not db_enabled:
        return "WARN: DB_ENABLED=false — events not persisted"

    if not dsn:
        return "WARN: DATABASE_URL not set — no event persistence"

    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.close()
        conn.close()
        return None
    except ImportError:
        return "WARN: psycopg2 not installed — DB persistence disabled"
    except Exception as e:
        return f"WARN: Database connection failed: {e}"


def _check_commands():
    """Verify subprocess commands are reachable (PATH issue in supervisor)."""
    commands = [
        ('/bin/ps', ['aux']),
        ('/usr/bin/grep', ['--version']),
        ('/usr/bin/pgrep', ['-V']),
        ('/usr/bin/tail', ['--help']),
    ]
    missing = []
    for cmd, args in commands:
        try:
            subprocess.run([cmd] + args, capture_output=True, timeout=5)
        except FileNotFoundError:
            missing.append(cmd)
        except Exception:
            pass  # command exists but args failed — that's OK

    if missing:
        return f"Commands not found: {missing} — watchdog checks will fail"
    return None


def _check_kill_switch():
    """Check for stale/test kill switch files."""
    from config import DATA_DIR

    known_engines = {'vwap', 'pro', 'pop', 'options', 'core'}
    stale = []
    test_ks = []

    try:
        for fname in os.listdir(DATA_DIR):
            if fname.endswith('_kill_switch.json'):
                engine = fname.replace('_kill_switch.json', '')
                if engine not in known_engines:
                    test_ks.append(fname)
                    continue
                fpath = os.path.join(DATA_DIR, fname)
                try:
                    with open(fpath) as f:
                        ks = json.load(f)
                    if ks.get('halted', False):
                        ks_date = ks.get('date', '')
                        from datetime import datetime
                        from zoneinfo import ZoneInfo
                        today = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')
                        if ks_date != today:
                            stale.append(f"{engine} (from {ks_date})")
                except Exception:
                    pass
    except FileNotFoundError:
        pass

    if test_ks:
        return f"Test kill switch files: {test_ks} — remove them"
    if stale:
        return f"WARN: Stale kill switch from prior day: {stale}"
    return None


def _check_disk_space():
    """Check data directory has sufficient free space."""
    from config import DATA_DIR
    try:
        stat = os.statvfs(DATA_DIR)
        free_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
        if free_mb < 100:
            return f"Only {free_mb:.0f}MB free in {DATA_DIR} — risk of write failures"
        return None
    except Exception as e:
        return f"WARN: Disk space check failed: {e}"


def _check_data_dir():
    """Verify data directory exists and is writable."""
    from config import DATA_DIR
    if not os.path.isdir(DATA_DIR):
        return f"DATA_DIR does not exist: {DATA_DIR}"
    try:
        test_file = os.path.join(DATA_DIR, '.preflight_write_test')
        with open(test_file, 'w') as f:
            f.write('ok')
        os.remove(test_file)
        return None
    except Exception as e:
        return f"DATA_DIR not writable: {e}"


# ══════════════════════════════════════════════════════════════════════════
# Standalone execution
# ══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    errors, warnings = run_preflight()
    if errors:
        print(f"\n{len(errors)} CRITICAL failures — DO NOT TRADE")
        sys.exit(1)
    else:
        print(f"\nPASSED ({len(warnings)} warnings)")
        sys.exit(0)
