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
PYTHON = sys.executable
SCRIPTS_DIR = os.path.join(PROJECT_ROOT, 'scripts')

# Logging
log_dir = os.path.join(PROJECT_ROOT, 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"supervisor_{datetime.now().strftime('%Y-%m-%d')}.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [supervisor] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# Process definitions
PROCESSES = {
    'core': {
        'script': os.path.join(SCRIPTS_DIR, 'run_core.py'),
        'critical': True,    # If core dies, all trading stops
        'max_restarts': 3,
        'restart_delay': 10,
    },
    'pro': {
        'script': os.path.join(SCRIPTS_DIR, 'run_pro.py'),
        'critical': False,   # Can survive without pro-setups
        'max_restarts': 5,
        'restart_delay': 15,
    },
    'pop': {
        'script': os.path.join(SCRIPTS_DIR, 'run_pop.py'),
        'critical': False,
        'max_restarts': 5,
        'restart_delay': 15,
    },
    'options': {
        'script': os.path.join(SCRIPTS_DIR, 'run_options.py'),
        'critical': False,
        'max_restarts': 5,
        'restart_delay': 20,
    },
}

STATUS_FILE = os.path.join(PROJECT_ROOT, 'data', 'supervisor_status.json')


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
            log_path = os.path.join(log_dir, f"{self.name}_{datetime.now().strftime('%Y-%m-%d')}.log")
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

    def check(self) -> str:
        """Check process health. Returns status: running, crashed, stopped."""
        if self.process is None:
            return 'stopped'

        retcode = self.process.poll()
        if retcode is None:
            return 'running'

        # Process exited
        if retcode == 0:
            self.status = 'stopped'
            log.info("[%s] Exited cleanly (code 0)", self.name)
        else:
            self.status = 'crashed'
            self.last_error = f"Exit code {retcode}"
            log.error("[%s] CRASHED (exit code %d)", self.name, retcode)

        return self.status

    def get_crash_traceback(self) -> str:
        """Read the process log file and extract the last traceback."""
        log_path = os.path.join(log_dir, f"{self.name}_{datetime.now().strftime('%Y-%m-%d')}.log")
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

            # Apply
            with open(fix_file, 'w') as f:
                f.write(new_content)

            log.info("[%s] Fix applied to %s: %s", self.name, diagnosis.fix_file, diagnosis.fix_description)
            return True

        except Exception as exc:
            log.error("[%s] Failed to apply fix: %s", self.name, exc)
            return False

    def restart(self) -> bool:
        """Diagnose crash, fix if possible, then restart."""
        if self.restart_count >= self.max_restarts:
            self.status = 'disabled'
            log.error("[%s] Max restarts (%d) reached — DISABLED",
                      self.name, self.max_restarts)
            return False

        # Try to diagnose and fix before restarting
        fixed = self.diagnose_and_fix()
        if fixed:
            log.info("[%s] Bug fixed — restarting with patched code", self.name)
        else:
            log.info("[%s] No fix applied — restarting as-is (may crash again)", self.name)

        self.restart_count += 1
        log.info("[%s] Restarting (%d/%d) after %ds cooldown...",
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
                self.process.wait(timeout=10)
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
        _send(subject, body, severity='CRITICAL')
    except Exception:
        pass


def main():
    log.info("=" * 60)
    log.info("SUPERVISOR STARTING — Process Isolation Mode")
    log.info("=" * 60)
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

    # Start satellite processes
    for name in ['pro', 'pop', 'options']:
        if not managers[name].start():
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

                if status == 'crashed':
                    log.error("[%s] CRASHED — attempting restart", name)

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

        log.info("=" * 60)
        log.info("SUPERVISOR SHUTDOWN COMPLETE")
        for name, m in managers.items():
            log.info("  [%s] restarts=%d status=%s", name, m.restart_count, m.status)
        log.info("=" * 60)


if __name__ == '__main__':
    main()
