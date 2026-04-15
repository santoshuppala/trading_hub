#!/usr/bin/env python3
"""
Watchdog — self-healing process wrapper for run_monitor.py.

Starts the monitor, detects crashes, calls CrashAnalyzer for diagnosis,
applies safe fixes, and restarts. Repeats until the monitor runs cleanly
or max retries are exhausted.

Usage (replaces direct cron call):
    # Old cron:
    #   0 6 * * 1-5 cd /path/to/trading_hub && python run_monitor.py >> logs/cron.log 2>&1
    # New cron:
    #   0 6 * * 1-5 cd /path/to/trading_hub && python scripts/watchdog.py >> logs/cron.log 2>&1

STRICT GUARDRAILS:
  - Max 5 fix attempts per session (then gives up and alerts)
  - Only modifies files in CrashAnalyzer.ALLOWED_FILES
  - Never touches DB schema, credentials, table names
  - Creates a git stash before any fix (so you can roll back)
  - Logs every action to logs/watchdog_YYYY-MM-DD.log
  - Writes crash_report.json for errors it can't fix
"""
import logging
import os
import subprocess
import sys
import time
from datetime import datetime

# Project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

# ── Logging ──────────────────────────────────────────────────────────────
log_dir = os.path.join(PROJECT_ROOT, 'logs')
os.makedirs(log_dir, exist_ok=True)
date_dir = os.path.join(log_dir, datetime.now().strftime('%Y%m%d'))
os.makedirs(date_dir, exist_ok=True)
log_file = os.path.join(date_dir, 'watchdog.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [watchdog] %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────
MAX_RETRIES = 5           # max fix-and-restart attempts
COOLDOWN_SECONDS = 10     # wait between retries
MONITOR_SCRIPT = os.path.join(PROJECT_ROOT, 'run_monitor.py')
PYTHON = sys.executable


def stash_before_fix():
    """Create a git stash so fixes can be rolled back."""
    try:
        result = subprocess.run(
            ['git', 'stash', 'push', '-m', f'watchdog-pre-fix-{datetime.now().isoformat()}'],
            capture_output=True, text=True, timeout=10, cwd=PROJECT_ROOT,
        )
        if result.returncode == 0 and 'No local changes' not in result.stdout:
            log.info("Git stash created before applying fix")
            return True
        return False
    except Exception as exc:
        log.warning("Git stash failed (non-fatal): %s", exc)
        return False


def apply_fix(diagnosis):
    """Apply a fix from the crash analyzer to the source file."""
    from scripts.crash_analyzer import FORBIDDEN_PATTERNS
    import re

    fix_file = diagnosis.fix_file
    fix_old = diagnosis.fix_old
    fix_new = diagnosis.fix_new

    if not fix_file or not fix_old or not fix_new:
        log.error("Fix has empty file/old/new — skipping")
        return False

    # Safety: check forbidden patterns in the new code
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, fix_new, re.IGNORECASE):
            log.error("Fix contains FORBIDDEN pattern (%s) — BLOCKED", pattern)
            return False

    abs_path = os.path.join(PROJECT_ROOT, fix_file)
    if not os.path.exists(abs_path):
        log.error("Fix target file does not exist: %s", abs_path)
        return False

    try:
        with open(abs_path, 'r') as f:
            content = f.read()

        if fix_old not in content:
            log.error("Fix old_string not found in %s — file may have changed", fix_file)
            return False

        # Apply the fix
        new_content = content.replace(fix_old, fix_new, 1)

        with open(abs_path, 'w') as f:
            f.write(new_content)

        log.info("Fix applied to %s: %s", fix_file, diagnosis.fix_description)
        return True

    except Exception as exc:
        log.error("Failed to apply fix to %s: %s", fix_file, exc)
        return False


def verify_syntax(fix_file):
    """Verify the fixed file still compiles."""
    abs_path = os.path.join(PROJECT_ROOT, fix_file)
    try:
        result = subprocess.run(
            [PYTHON, '-c', f"import py_compile; py_compile.compile('{abs_path}', doraise=True)"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            log.info("Syntax check passed for %s", fix_file)
            return True
        else:
            log.error("Syntax check FAILED for %s: %s", fix_file, result.stderr)
            return False
    except Exception as exc:
        log.error("Syntax check error for %s: %s", fix_file, exc)
        return False


def revert_fix(fix_file):
    """Revert a fix by popping git stash."""
    try:
        result = subprocess.run(
            ['git', 'checkout', '--', fix_file],
            capture_output=True, text=True, timeout=10, cwd=PROJECT_ROOT,
        )
        if result.returncode == 0:
            log.info("Reverted fix in %s", fix_file)
            return True
    except Exception:
        pass
    return False


def run_monitor():
    """Run run_monitor.py and capture output. Returns (exit_code, stderr)."""
    log.info("Starting monitor: %s %s", PYTHON, MONITOR_SCRIPT)
    try:
        result = subprocess.run(
            [PYTHON, MONITOR_SCRIPT],
            capture_output=False,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=8 * 3600,  # 8 hours max (6 AM to 4 PM + buffer)
        )
        return result.returncode, ''
    except subprocess.TimeoutExpired:
        log.info("Monitor reached timeout (normal EOD shutdown)")
        return 0, ''
    except Exception as exc:
        return 1, str(exc)


def run_monitor_with_crash_capture():
    """Run monitor and capture stderr for crash analysis."""
    log.info("Starting monitor: %s %s", PYTHON, MONITOR_SCRIPT)
    try:
        result = subprocess.run(
            [PYTHON, MONITOR_SCRIPT],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=8 * 3600,
        )
        # Write stdout to cron.log (normal behavior)
        if result.stdout:
            sys.stdout.write(result.stdout)

        if result.returncode != 0:
            return result.returncode, result.stderr
        return 0, ''
    except subprocess.TimeoutExpired:
        log.info("Monitor reached timeout (normal EOD shutdown)")
        return 0, ''
    except Exception as exc:
        return 1, str(exc)


def extract_traceback(stderr_text: str) -> str:
    """Extract the last traceback from stderr output."""
    # Find all tracebacks
    tb_starts = [i for i, line in enumerate(stderr_text.split('\n'))
                 if line.strip().startswith('Traceback')]
    if not tb_starts:
        return stderr_text[-2000:]  # last 2000 chars as fallback

    lines = stderr_text.split('\n')
    last_tb_start = tb_starts[-1]
    return '\n'.join(lines[last_tb_start:])


def send_alert(subject: str, body: str):
    """Try to send an alert email using the monitor's alert system."""
    try:
        from monitor.alerts import send_alert as _send
        _send(subject, body, severity='CRITICAL')
        log.info("Alert sent: %s", subject)
    except Exception as exc:
        log.warning("Alert send failed (non-fatal): %s", exc)


def main():
    log.info("=" * 60)
    log.info("WATCHDOG STARTED — %s", datetime.now().isoformat())
    log.info("=" * 60)

    from scripts.crash_analyzer import CrashAnalyzer
    analyzer = CrashAnalyzer()

    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            log.info("--- Retry %d/%d (after %ds cooldown) ---",
                     attempt, MAX_RETRIES, COOLDOWN_SECONDS)
            time.sleep(COOLDOWN_SECONDS)

        # Run the monitor
        exit_code, stderr = run_monitor_with_crash_capture()

        if exit_code == 0:
            log.info("Monitor exited cleanly (code 0)")
            break

        log.error("Monitor CRASHED with exit code %d", exit_code)

        if not stderr.strip():
            # Check cron.log for the traceback
            cron_log = os.path.join(log_dir, 'cron.log')
            if os.path.exists(cron_log):
                try:
                    with open(cron_log) as f:
                        stderr = f.read()[-5000:]  # last 5000 chars
                except Exception:
                    pass

        if not stderr.strip():
            log.error("No traceback captured — cannot diagnose")
            send_alert(
                "Monitor crashed — no traceback",
                f"Exit code {exit_code}, attempt {attempt + 1}/{MAX_RETRIES + 1}. "
                f"No stderr captured for diagnosis.",
            )
            continue

        # Analyze the crash
        traceback_text = extract_traceback(stderr)
        log.info("Analyzing crash traceback...")
        diagnosis = analyzer.analyze(traceback_text, attempt=attempt)

        log.info("Diagnosis: %s | %s", diagnosis.error_type, diagnosis.root_cause)
        log.info("Fix applicable: %s (confidence: %.0f%%)",
                 diagnosis.fix_applicable, diagnosis.confidence * 100)

        if not diagnosis.fix_applicable:
            # Can't auto-fix — write crash report and alert
            report_path = analyzer.write_crash_report(diagnosis)
            log.error(
                "Cannot auto-fix this error. Crash report: %s", report_path
            )
            send_alert(
                f"Monitor crash — auto-fix failed: {diagnosis.error_type}",
                f"Error: {diagnosis.error_message}\n"
                f"File: {diagnosis.file_path}:{diagnosis.line_number}\n"
                f"Root cause: {diagnosis.root_cause}\n"
                f"Crash report: {report_path}\n"
                f"Attempt: {attempt + 1}/{MAX_RETRIES + 1}",
            )
            if attempt >= MAX_RETRIES:
                log.error("MAX RETRIES EXHAUSTED — giving up")
                break
            continue

        # Apply the fix
        log.info("Applying fix: %s", diagnosis.fix_description)
        stash_before_fix()

        if not apply_fix(diagnosis):
            log.error("Fix application failed — skipping retry")
            continue

        # Verify syntax
        if not verify_syntax(diagnosis.fix_file):
            log.error("Fix broke syntax — reverting")
            revert_fix(diagnosis.fix_file)
            continue

        log.info("Fix applied and verified — restarting monitor")

    log.info("=" * 60)
    log.info("WATCHDOG FINISHED — %s", datetime.now().isoformat())
    log.info("=" * 60)


if __name__ == '__main__':
    main()
