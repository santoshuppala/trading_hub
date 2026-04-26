#!/usr/bin/env python3
"""
Process Supervisor — manages 4 isolated trading engine processes.

Starts, monitors, and restarts each engine independently:
  - Core:    VWAP strategy + broker + position management
  - Pro:     11 technical detectors + strategy classifier
  - Pop:     Momentum screener + Benzinga/StockTwits
  - Options: Multi-leg options on dedicated Alpaca account

Benefits over monolith:
  - Crash isolation: Options crash doesn't kill VWAP trading
  - Independent restarts: Fix and restart one engine without stopping others
  - Separate logs: Each engine has its own log file
  - No GIL contention: True multiprocessing across CPU cores

Usage:
  python scripts/supervisor.py

Or via cron (replace run_monitor.py):
  python scripts/supervisor.py >> logs/cron.log 2>&1
"""
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# Load .env so child processes inherit API keys
_env_path = os.path.join(PROJECT_ROOT, '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.split('#')[0].strip()
            if '=' in _line:
                _k, _v = _line.split('=', 1)
                _v = _v.strip().strip('"').strip("'")
                if _k.strip():  # always use project .env
                    os.environ[_k.strip()] = _v
PYTHON = sys.executable
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, 'scripts')

# Logging
log_dir = os.path.join(PROJECT_ROOT, 'logs')
os.makedirs(log_dir, exist_ok=True)
date_dir = os.path.join(log_dir, datetime.now().strftime('%Y%m%d'))
os.makedirs(date_dir, exist_ok=True)
log_file = os.path.join(date_dir, 'supervisor.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [supervisor] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
    ],
)
log = logging.getLogger(__name__)

# V7 P4-2: Config-driven engine registration.
# Default engines defined here. To add a new engine:
#   1. Create scripts/run_{name}.py
#   2. Add entry to PROCESSES dict below (or set ENGINE_CONFIG env var)
#   3. Add engine-specific config to config.py
# No other code changes required — supervisor, registry, IPC are generic.
# V8: Pro and Pop merged into Core. Only 3 processes: core, options, data_collector.
_DEFAULT_PROCESSES = {
    'core': {
        'script': os.path.join(SCRIPTS_DIR, 'run_core.py'),
        'critical': True,    # If core dies, all trading stops
        'max_restarts': 3,
        'restart_delay': 10,
    },
    'options': {
        'script': os.path.join(SCRIPTS_DIR, 'run_options.py'),
        'critical': False,
        'max_restarts': 5,
        'restart_delay': 20,
    },
}

# V8: Skip scripts that should NOT be auto-discovered
# V10: run_watchdog.py deprecated — inner watchdog merged into outer_watchdog.py
_SKIP_SCRIPTS = {'run_pro.py', 'run_pop.py', 'run_monitor.py', 'run_watchdog.py'}


def _load_engine_config() -> dict:
    """V7 P4-2: Load engine config from ENGINE_CONFIG env var (JSON) or defaults.

    To add a new engine without modifying code:
        export ENGINE_CONFIG='{"arbitrage": {"script": "scripts/run_arbitrage.py",
                               "critical": false, "max_restarts": 5, "restart_delay": 15}}'

    Merges with defaults — custom engines are ADDED, not replacing defaults.
    To disable a default engine, set its value to null:
        export ENGINE_CONFIG='{"pop": null}'
    """
    import json as _json
    processes = dict(_DEFAULT_PROCESSES)

    _disabled = set()  # track explicitly disabled engines (don't auto-discover them)
    custom = os.getenv('ENGINE_CONFIG', '')
    if custom:
        try:
            overrides = _json.loads(custom)
            for name, cfg in overrides.items():
                if cfg is None:
                    processes.pop(name, None)  # disable engine
                    _disabled.add(name)
                    log.info("[supervisor] Engine '%s' disabled via ENGINE_CONFIG", name)
                else:
                    if 'script' not in cfg:
                        cfg['script'] = os.path.join(SCRIPTS_DIR, f'run_{name}.py')
                    cfg.setdefault('critical', False)
                    cfg.setdefault('max_restarts', 5)
                    cfg.setdefault('restart_delay', 15)
                    processes[name] = cfg
                    log.info("[supervisor] Engine '%s' registered via ENGINE_CONFIG", name)
        except Exception as exc:
            log.warning("[supervisor] ENGINE_CONFIG parse failed: %s — using defaults", exc)

    # V7: Auto-discover run_*.py scripts not in config (skip explicitly disabled)
    for f in sorted(os.listdir(SCRIPTS_DIR)):
        if f.startswith('run_') and f.endswith('.py') and f not in _SKIP_SCRIPTS:
            engine_name = f[4:-3]  # run_arbitrage.py → arbitrage
            if engine_name not in processes and engine_name not in _disabled:
                log.info("[supervisor] Auto-discovered engine '%s' from %s", engine_name, f)
                processes[engine_name] = {
                    'script': os.path.join(SCRIPTS_DIR, f),
                    'critical': False,
                    'max_restarts': 5,
                    'restart_delay': 15,
                }

    return processes


PROCESSES = _load_engine_config()

from config import SUPERVISOR_STATUS_PATH
STATUS_FILE = SUPERVISOR_STATUS_PATH


class ProcessManager:
    """Manages a single child process with restart capability."""

    def __init__(self, name: str, script: str, critical: bool = False,
                 max_restarts: int = 3, restart_delay: int = 10):
        self.name = name
        self.script = script
        self.critical = critical
        self.max_restarts = max_restarts
        self.restart_delay = restart_delay

        self.process: subprocess.Popen = None
        self.restart_count = 0
        self.started_at: str = ''
        self.status: str = 'stopped'  # stopped, running, crashed, disabled
        self.last_error: str = ''

    def start(self) -> bool:
        """Start the process. Returns True on success."""
        if self.process and self.process.poll() is None:
            return True  # Already running

        # Clean up stale lock file if this is the core process (prevents "already running" error)
        if self.name == 'core':
            lock_file = os.path.join(PROJECT_ROOT, '.monitor.lock')
            if os.path.exists(lock_file):
                try:
                    os.unlink(lock_file)
                    log.info("[%s] Removed stale .monitor.lock", self.name)
                except Exception:
                    pass

        try:
            date_dir = os.path.join(log_dir, datetime.now().strftime('%Y%m%d'))
            os.makedirs(date_dir, exist_ok=True)
            log_path = os.path.join(date_dir, f"{self.name}.log")
            self.process = subprocess.Popen(
                [PYTHON, self.script],
                cwd=PROJECT_ROOT,
                stdout=open(log_path, 'a'),
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,  # Own process group for clean kill
            )
            self.started_at = datetime.now(ET).isoformat()
            self.status = 'running'
            self.last_error = ''
            log.info("[%s] Started (PID %d)", self.name, self.process.pid)
            return True
        except Exception as exc:
            self.status = 'crashed'
            self.last_error = str(exc)
            log.error("[%s] Failed to start: %s", self.name, exc)
            return False

    # V7 P3-2: Heartbeat staleness threshold for hung process detection
    # V7.2: Increased from 120s → 180s. Background heartbeat thread writes
    # every 15s, so 180s means 12 missed writes before triggering. This
    # prevents false HUNG detection when emit_batch() blocks processing
    # many sequential sells (each taking ~3s for broker polling).
    _HEARTBEAT_STALE_SEC = 180.0

    def check(self) -> str:
        """Check process health. Returns status: running, crashed, stopped, hung.

        V7 P3-2: If process PID is alive but heartbeat file is stale (>120s),
        returns 'hung' — supervisor should force-kill and restart.
        """
        if self.process is None:
            return 'stopped'

        retcode = self.process.poll()
        if retcode is None:
            # V8: Reset restart counter only after 5 min stable (was 60s).
            # At 60s, a process crashing every 61s would never hit max_restarts.
            if self.restart_count > 0 and self.started_at:
                try:
                    started = datetime.fromisoformat(self.started_at)
                    uptime = (datetime.now(ET) - started).total_seconds()
                    if uptime > 300:
                        self.restart_count = 0
                except Exception:
                    pass

            # V7 P3-2: Check heartbeat file for hung process detection.
            # Only check during market hours (9:30-16:00 ET).
            # Pre-market: Core sleeps for 30+ min waiting for open — no heartbeats
            # emitted during sleep. This is intentional, not a hang.
            if self.name == 'core':
                try:
                    now_et = datetime.now(ET)
                    market_open = (now_et.hour > 9 or
                                   (now_et.hour == 9 and now_et.minute >= 30))
                    market_closed = now_et.hour >= 16

                    if market_open and not market_closed:
                        hb_path = os.path.join(PROJECT_ROOT, 'data', 'heartbeat.json')
                        if os.path.exists(hb_path):
                            import time as _time
                            hb_age = _time.time() - os.path.getmtime(hb_path)
                            if hb_age > self._HEARTBEAT_STALE_SEC:
                                log.error(
                                    "[%s] HUNG: heartbeat stale for %.0fs "
                                    "(threshold %.0fs) — PID alive but not responding",
                                    self.name, hb_age, self._HEARTBEAT_STALE_SEC)
                                self.status = 'hung'
                                self.last_error = f"Heartbeat stale {hb_age:.0f}s"
                                return 'hung'
                except Exception:
                    pass

            return 'running'

        # Process exited
        if retcode == 0:
            self.status = 'stopped'
            log.info("[%s] Exited cleanly (code 0)", self.name)
        elif retcode == 3:
            # V8: Exit code 3 = kill switch halt (daily loss limit breached).
            # Engine intentionally stopped itself. Do NOT restart — restarting
            # would just trigger the kill switch again (same daily PnL in state).
            self.status = 'disabled'
            # Extract kill reason from engine's log
            kill_detail = self._get_kill_reason()
            self.last_error = f"Kill switch: {kill_detail}"
            log.warning("[%s] KILL SWITCH EXIT — %s — disabled until next trading day",
                        self.name, kill_detail)
        elif retcode in (-9, -15):
            # V8: SIGKILL (-9) or SIGTERM (-15) are external signals (manual kill,
            # OOM killer, supervisor stop), not application crashes. Don't count
            # toward max_restarts — operator shouldn't lose an engine because
            # they manually killed it during debugging.
            self.status = 'stopped'
            self.last_error = f"Exit code {retcode} (external signal)"
            log.warning("[%s] Killed by signal %d (external) — will restart without counting",
                        self.name, -retcode)
        else:
            self.status = 'crashed'
            tb = self.get_crash_traceback()
            short_reason = tb.splitlines()[-1].strip() if tb else f"exit code {retcode}"
            self.last_error = f"Exit code {retcode}: {short_reason[:200]}"
            log.error("[%s] CRASHED (exit code %d) — %s", self.name, retcode, short_reason[:300])

        return self.status

    def _get_kill_reason(self) -> str:
        """Extract the kill switch reason from the engine's log file."""
        date_dir = os.path.join(log_dir, datetime.now().strftime('%Y%m%d'))
        log_path = os.path.join(date_dir, f"{self.name}.log")
        try:
            with open(log_path, 'rb') as f:
                # Read last 5KB — kill reason is near the end
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 5000))
                tail = f.read().decode('utf-8', errors='replace')
            # Look for kill switch details
            for line in reversed(tail.splitlines()):
                if 'KILL SWITCH' in line and 'P&L' in line:
                    # Extract: "daily P&L $-3400.00 breached limit $-3000.00"
                    import re
                    m = re.search(r'P&L \$([^ ]+) breached (?:limit )?\$([^ ]+)', line)
                    if m:
                        return f"daily P&L ${m.group(1)} breached ${m.group(2)}"
                if 'KILL SWITCH' in line:
                    # Generic: return the log line content
                    parts = line.split('] ', 1)
                    return parts[-1].strip() if len(parts) > 1 else line.strip()
            return "daily loss limit breached (details not found in log)"
        except Exception:
            return "daily loss limit breached"

    def get_crash_traceback(self) -> str:
        """Read the process log file and extract the last traceback."""
        date_dir = os.path.join(log_dir, datetime.now().strftime('%Y%m%d'))
        log_path = os.path.join(date_dir, f"{self.name}.log")
        try:
            with open(log_path, 'r') as f:
                content = f.read()
            # Find last traceback
            tb_starts = [i for i, line in enumerate(content.split('\n'))
                         if 'Traceback' in line]
            if tb_starts:
                lines = content.split('\n')
                return '\n'.join(lines[tb_starts[-1]:])
            return content[-3000:]  # last 3000 chars as fallback
        except Exception:
            return ''

    def diagnose_and_fix(self) -> bool:
        """Use CrashAnalyzer to diagnose and fix the crash. Returns True if fixed."""
        from scripts.crash_analyzer import CrashAnalyzer

        tb = self.get_crash_traceback()
        if not tb.strip():
            log.warning("[%s] No traceback found in log — cannot diagnose", self.name)
            return False

        analyzer = CrashAnalyzer()
        diagnosis = analyzer.analyze(tb, attempt=self.restart_count)

        log.info("[%s] Diagnosis: %s | %s", self.name, diagnosis.error_type, diagnosis.root_cause)

        if not diagnosis.fix_applicable:
            log.warning("[%s] Crash analyzer cannot auto-fix this error", self.name)
            analyzer.write_crash_report(diagnosis)
            return False

        log.info("[%s] Fix available (confidence %.0f%%): %s",
                 self.name, diagnosis.confidence * 100, diagnosis.fix_description)

        # Apply the fix
        fix_file = os.path.join(PROJECT_ROOT, diagnosis.fix_file)
        if not os.path.exists(fix_file):
            log.error("[%s] Fix target file not found: %s", self.name, fix_file)
            return False

        try:
            with open(fix_file, 'r') as f:
                content = f.read()

            if diagnosis.fix_old not in content:
                log.warning("[%s] Fix old_string not found in file — already fixed or file changed",
                            self.name)
                return False

            new_content = content.replace(diagnosis.fix_old, diagnosis.fix_new, 1)

            # Verify syntax before writing
            import py_compile
            import tempfile
            fd, tmp = tempfile.mkstemp(suffix='.py')
            try:
                with os.fdopen(fd, 'w') as f:
                    f.write(new_content)
                py_compile.compile(tmp, doraise=True)
            except py_compile.PyCompileError as exc:
                log.error("[%s] Fix would break syntax — NOT applying: %s", self.name, exc)
                return False
            finally:
                os.unlink(tmp)

            # V8: Backup before applying fix (rollback if logic is wrong)
            import shutil
            shutil.copy2(fix_file, fix_file + '.pre_autofix')

            # Apply
            with open(fix_file, 'w') as f:
                f.write(new_content)

            log.info("[%s] Fix applied to %s: %s (backup at %s.pre_autofix)",
                     self.name, diagnosis.fix_file, diagnosis.fix_description, fix_file)
            return True

        except Exception as exc:
            log.error("[%s] Failed to apply fix: %s", self.name, exc)
            return False

    def restart(self) -> bool:
        """Diagnose crash, fix if possible, then restart."""
        # V8: Only count actual crashes toward max_restarts, not external signals
        is_crash = self.status == 'crashed'

        if is_crash:
            if self.restart_count >= self.max_restarts:
                self.status = 'disabled'
                log.error("[%s] Max restarts (%d) reached — DISABLED",
                          self.name, self.max_restarts)
                return False
            fixed = self.diagnose_and_fix()
            if fixed:
                log.info("[%s] Bug fixed — restarting with patched code", self.name)
            else:
                log.info("[%s] No fix applied — restarting as-is (may crash again)", self.name)
            self.restart_count += 1
        else:
            # External signal (SIGKILL/SIGTERM) or clean exit — don't count
            log.info("[%s] External signal or clean exit — restarting (not counted toward max)",
                     self.name)

        log.info("[%s] Restarting (crashes=%d/%d) after %ds cooldown...",
                 self.name, self.restart_count, self.max_restarts,
                 self.restart_delay)
        time.sleep(self.restart_delay)
        return self.start()

    def stop(self) -> None:
        """Gracefully stop the process."""
        if self.process and self.process.poll() is None:
            try:
                # Send SIGTERM to process group
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                self.process.wait(timeout=30)  # V8: was 10s, increased to avoid SIGKILL during order flush
                log.info("[%s] Stopped gracefully", self.name)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                log.warning("[%s] Force-killed after timeout", self.name)
            except Exception as exc:
                log.warning("[%s] Stop error: %s", self.name, exc)
        self.status = 'stopped'

    def to_dict(self) -> dict:
        return {
            'name': self.name,
            'status': self.status,
            'pid': self.process.pid if self.process and self.process.poll() is None else None,
            'started_at': self.started_at,
            'restart_count': self.restart_count,
            'last_error': self.last_error,
            'critical': self.critical,
        }


def write_status(managers: dict):
    """Write supervisor status to JSON file."""
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    status = {
        'supervisor': {
            'updated_at': datetime.now(ET).isoformat(),
            'mode': 'process_isolation',
        },
        'processes': {name: m.to_dict() for name, m in managers.items()},
    }
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(status, f, indent=2)
    except Exception:
        pass


def send_alert(subject: str, body: str):
    """Try to send a critical alert."""
    try:
        sys.path.insert(0, PROJECT_ROOT)
        from monitor.alerts import send_alert as _send
        from config import ALERT_EMAIL
        # monitor.alerts.send_alert(alert_email, message, severity)
        message = f"{subject}\n\n{body}" if body else subject
        _send(ALERT_EMAIL, message, severity='CRITICAL')
    except Exception as exc:
        log.error("Supervisor alert send failed: %s", exc)


def _startup_cleanup():
    """V9: Clean caches and validate state files before starting processes.

    Addresses:
    - L1: __pycache__ stale bytecode after code changes (caused 32 crashes on Apr 17)
    - L3: Corrupt/stale state files from previous crash
    - L4: Deprecated V7 state files lingering
    """
    import shutil
    from zoneinfo import ZoneInfo
    ET_local = ZoneInfo('America/New_York')

    # L1: Clear __pycache__ — prevents stale .pyc after code changes
    cleared = 0
    for root, dirs, files in os.walk(PROJECT_ROOT):
        if 'venv' in root or '.git' in root:
            continue
        if '__pycache__' in dirs:
            cache_dir = os.path.join(root, '__pycache__')
            try:
                shutil.rmtree(cache_dir)
                cleared += 1
            except Exception:
                pass
    if cleared:
        log.info("[startup] Cleared %d __pycache__ directories", cleared)

    # L3: Validate state files + clean .tmp leftovers
    data_dir = os.path.join(PROJECT_ROOT, 'data')
    if os.path.isdir(data_dir):
        today_str = datetime.now(ET_local).strftime('%Y-%m-%d')

        # Remove stale .tmp files from crashed writes
        for f in os.listdir(data_dir):
            if f.endswith('.tmp'):
                try:
                    os.unlink(os.path.join(data_dir, f))
                    log.info("[startup] Removed stale temp file: %s", f)
                except Exception:
                    pass

        # Validate JSON state files
        from config import BOT_STATE_PATH, BROKER_MAP_PATH, OPTIONS_STATE_PATH
        for path in [BOT_STATE_PATH, BROKER_MAP_PATH, OPTIONS_STATE_PATH,
                     os.path.join(data_dir, 'position_registry.json')]:
            if os.path.exists(path):
                try:
                    import json as _json
                    with open(path) as f:
                        data = _json.load(f)
                    file_date = data.get('_date', '')
                    if file_date and file_date != today_str:
                        log.info("[startup] %s is from %s (not today) — "
                                 "reconciliation will update", path, file_date)
                except (_json.JSONDecodeError, Exception) as exc:
                    log.error("[startup] %s is CORRUPT (%s) — "
                              "removing (will recover from backup)", path, exc)
                    try:
                        os.unlink(path)
                    except Exception:
                        pass

        # L4: Remove deprecated V7 state files
        deprecated = ['pop_state.json', 'pro_state.json']
        for name in deprecated:
            for suffix in ['', '.prev', '.prev2', '.sha256', '.flock', '.v7_archive',
                           '.prev.sha256', '.prev2.sha256']:
                path = os.path.join(data_dir, name + suffix)
                if os.path.exists(path):
                    try:
                        os.unlink(path)
                        log.info("[startup] Removed deprecated %s", name + suffix)
                    except Exception:
                        pass


def main():
    # ── V10: Exclusive lock — only one supervisor at a time ──────────
    # Prevents duplicate supervisors from spawning duplicate child processes.
    # Uses fcntl.flock (OS-enforced, auto-releases on crash/exit).
    import fcntl
    _lock_path = os.path.join(PROJECT_ROOT, '.supervisor.lock')
    try:
        _lock_fd = open(_lock_path, 'w')
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
    except BlockingIOError:
        # Another supervisor holds the lock
        try:
            with open(_lock_path) as f:
                existing_pid = f.read().strip()
        except Exception:
            existing_pid = '?'
        log.error(
            "Another supervisor is already running (PID %s). "
            "Kill it first or remove %s. Exiting.", existing_pid, _lock_path)
        sys.exit(1)
    except Exception as exc:
        log.warning("Supervisor lock failed (non-fatal): %s", exc)
    # _lock_fd stays open for process lifetime — lock auto-releases on exit

    # Tell child processes they're running under supervisor (skip lock file)
    os.environ['SUPERVISED_MODE'] = '1'

    log.info("=" * 60)
    log.info("SUPERVISOR STARTING — Process Isolation Mode")
    log.info("=" * 60)

    # V9: Clean caches and validate state before starting anything
    _startup_cleanup()

    # V10: Preflight gate — verify system health before trading
    if os.getenv('SKIP_PREFLIGHT', '').strip() == '1':
        log.warning("PREFLIGHT SKIPPED (SKIP_PREFLIGHT=1) — emergency mode")
    else:
        try:
            from scripts.preflight import run_preflight
            pf_errors, pf_warnings = run_preflight()
            if pf_errors:
                log.error("PREFLIGHT FAILED — %d critical errors. NOT starting engines.", len(pf_errors))
                send_alert(
                    "PREFLIGHT FAILED — Trading Blocked",
                    "Critical errors:\n" + "\n".join(f"  ✗ {e}" for e in pf_errors)
                    + ("\n\nWarnings:\n" + "\n".join(f"  ! {w}" for w in pf_warnings) if pf_warnings else "")
                    + "\n\nOverride: SKIP_PREFLIGHT=1 ./start_monitor.sh"
                )
                sys.exit(1)
        except Exception as exc:
            log.warning("Preflight check failed to run (non-fatal): %s", exc)

    log.info("Processes: %s", ', '.join(PROCESSES.keys()))

    # Handle SIGTERM gracefully
    shutdown = False

    def _on_sigterm(signum, frame):
        nonlocal shutdown
        shutdown = True
        log.info("SIGTERM received — shutting down all processes")

    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT, _on_sigterm)

    # Create process managers
    managers = {}
    for name, config in PROCESSES.items():
        managers[name] = ProcessManager(
            name=name,
            script=config['script'],
            critical=config['critical'],
            max_restarts=config['max_restarts'],
            restart_delay=config['restart_delay'],
        )

    # Start core FIRST (it initializes shared cache and registry)
    log.info("Starting core process first (initializes shared state)...")
    if not managers['core'].start():
        log.error("CRITICAL: Core process failed to start — aborting")
        send_alert("Supervisor: Core process failed to start",
                    "Core process could not be started. No trading today.")
        return

    # Wait for core to initialize shared cache
    time.sleep(5)

    # V8: Start satellite processes (dynamic — no hardcoded names)
    for name, manager in managers.items():
        if name != 'core':
            if not manager.start():
                log.warning("[%s] Failed to start — will retry in main loop", name)

    log.info("All processes started. Entering monitoring loop.")
    write_status(managers)

    # Monitoring loop
    try:
        while not shutdown:
            now = datetime.now(ET)

            # EOD shutdown
            if now.hour >= 16:
                log.info("4:00 PM ET — initiating EOD shutdown")
                break

            # Check each process
            for name, manager in managers.items():
                status = manager.check()

                # V7 P3-2: Handle 'hung' status (force-kill then restart)
                if status == 'hung':
                    log.error("[%s] HUNG — force-killing (PID %s)",
                              name, manager.process.pid if manager.process else '?')
                    try:
                        import signal as _sig
                        os.killpg(os.getpgid(manager.process.pid), _sig.SIGKILL)
                    except Exception:
                        try:
                            manager.process.kill()
                        except Exception:
                            pass
                    # V8: Reap zombie process before clearing reference
                    try:
                        if manager.process:
                            manager.process.wait(timeout=1)
                    except Exception:
                        pass
                    manager.process = None
                    manager.status = 'crashed'
                    # Fall through to restart logic below

                if status in ('crashed', 'stopped', 'hung'):
                    if status == 'crashed' or status == 'hung':
                        log.error("[%s] %s — attempting restart", name, status.upper())
                    else:
                        log.warning("[%s] STOPPED unexpectedly — attempting restart", name)

                    if manager.critical:
                        # Core crashed — this is serious
                        if not manager.restart():
                            log.error("CRITICAL: Core process unrecoverable — stopping all")
                            send_alert(
                                "CRITICAL: Core trading process crashed and won't restart",
                                f"Core crashed after {manager.max_restarts} restart attempts. "
                                f"Last error: {manager.last_error}. All trading stopped.",
                            )
                            shutdown = True
                            break
                    else:
                        # Non-critical engine — restart independently
                        if not manager.restart():
                            send_alert(
                                f"Trading engine '{name}' disabled after max restarts",
                                f"Engine '{name}' crashed {manager.max_restarts} times. "
                                f"Last error: {manager.last_error}. "
                                f"Other engines continue trading normally.",
                            )

            write_status(managers)
            time.sleep(30)  # Check every 30 seconds

    except KeyboardInterrupt:
        log.info("Supervisor interrupted by user.")
    finally:
        # Stop all processes
        log.info("Stopping all processes...")
        for name in reversed(list(managers.keys())):  # Stop satellites first, core last
            managers[name].stop()

        write_status(managers)

        # V10: Delete heartbeat file — prevents false HUNG detection on next startup.
        # Without this, the stale heartbeat from today triggers "64337s stale" at 6 AM tomorrow.
        try:
            hb_path = os.path.join(PROJECT_ROOT, 'data', 'heartbeat.json')
            if os.path.exists(hb_path):
                os.remove(hb_path)
                log.info("[shutdown] Removed heartbeat.json (prevents stale HUNG on next startup)")
        except Exception:
            pass

        # V7.1: Run post-session analytics (ML features, strategy scoring)
        try:
            import subprocess
            analytics_script = os.path.join(SCRIPTS_DIR, 'post_session_analytics.py')
            if os.path.exists(analytics_script):
                log.info("Running post-session analytics...")
                result = subprocess.run(
                    [sys.executable, analytics_script],
                    capture_output=True, text=True, timeout=300,  # 5 min timeout
                    cwd=PROJECT_ROOT,
                    env={**os.environ, 'PYTHONPATH': PROJECT_ROOT},
                )
                if result.returncode == 0:
                    log.info("Post-session analytics completed successfully")
                else:
                    log.warning("Post-session analytics failed (exit %d): %s",
                                result.returncode, result.stderr[:500])
        except subprocess.TimeoutExpired:
            log.warning("Post-session analytics timed out (5 min limit)")
        except Exception as exc:
            log.warning("Post-session analytics error: %s", exc)

        log.info("=" * 60)
        log.info("SUPERVISOR SHUTDOWN COMPLETE")
        for name, m in managers.items():
            log.info("  [%s] restarts=%d status=%s", name, m.restart_count, m.status)
        log.info("=" * 60)


if __name__ == '__main__':
    main()
