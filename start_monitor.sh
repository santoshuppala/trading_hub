#!/bin/bash
# Daily trading monitor launcher
# Scheduled via cron at 6:00 AM PST (weekdays only)

# Ensure standard tools are available (cron has a minimal PATH)
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

cd /Users/santosh/Documents/santosh/trading_hub/trading_hub

# Load .env — parse line by line, strip quotes and inline comments
if [ -f .env ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        # Strip inline comments
        line="${line%%#*}"
        # Trim leading/trailing whitespace
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        # Skip empty lines and lines without =
        [ -z "$line" ] && continue
        [[ "$line" != *=* ]] && continue
        key="${line%%=*}"
        val="${line#*=}"
        # Strip surrounding single or double quotes from value
        val="${val%\"}" ; val="${val#\"}"
        val="${val%\'}" ; val="${val#\'}"
        export "$key=$val"
    done < .env
fi

# Activate virtual environment
source venv/bin/activate

# Run monitor via watchdog (auto-recovery on crash)
# Watchdog starts run_monitor.py, detects crashes, diagnoses via crash_analyzer,
# applies safe fixes (file allowlist, no DB/credential changes), and restarts.
#
# If watchdog itself fails to import (syntax error in watchdog.py etc.),
# fall back to direct run_monitor.py so we get at least one attempt.
# If that also fails, send an alert and exit — don't loop forever on unfixed bugs.
WATCHDOG_EXIT=0
python scripts/watchdog.py || WATCHDOG_EXIT=$?

if [ "$WATCHDOG_EXIT" -ne 0 ]; then
    echo "$(date) WATCHDOG FAILED (exit=$WATCHDOG_EXIT) — attempting direct run_monitor.py"
    python run_monitor.py
    MONITOR_EXIT=$?
    if [ "$MONITOR_EXIT" -ne 0 ]; then
        echo "$(date) CRITICAL: Both watchdog and direct monitor failed. No trading today."
        echo "$(date) Check logs/watchdog_*.log and logs/crash_report_*.json for details."
        # Try to send an alert email
        python -c "
try:
    from monitor.alerts import send_alert
    send_alert('CRITICAL: Trading monitor failed to start — both watchdog and direct run crashed. Check logs immediately.', severity='CRITICAL')
except Exception:
    pass
" 2>/dev/null
    fi
fi
