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

# ── Execution mode ───────────────────────────────────────────────────────
# MONITOR_MODE controls how the trading system runs:
#   "monolith"  — single process (run_monitor.py via watchdog) [DEFAULT]
#   "isolated"  — 4 separate processes via supervisor
#
# Set in .env: MONITOR_MODE=isolated
# Or override: MONITOR_MODE=isolated ./start_monitor.sh
MONITOR_MODE="${MONITOR_MODE:-monolith}"

if [ "$MONITOR_MODE" = "isolated" ]; then
    # ── Process Isolation Mode ───────────────────────────────────────────
    # 4 independent processes: core, pro, pop, options
    # Supervisor manages lifecycle, restarts crashed engines independently
    echo "$(date) Starting in PROCESS ISOLATION mode (supervisor)"
    python scripts/supervisor.py
    EXIT=$?
    if [ "$EXIT" -ne 0 ]; then
        echo "$(date) Supervisor exited with code $EXIT"
        # Fall back to monolith mode
        echo "$(date) Falling back to monolith mode (watchdog)"
        python scripts/watchdog.py || python run_monitor.py
    fi
else
    # ── Monolith Mode (default) ──────────────────────────────────────────
    # Single process via watchdog (auto-recovery on crash)
    echo "$(date) Starting in MONOLITH mode (watchdog)"
    WATCHDOG_EXIT=0
    python scripts/watchdog.py || WATCHDOG_EXIT=$?

    if [ "$WATCHDOG_EXIT" -ne 0 ]; then
        echo "$(date) WATCHDOG FAILED (exit=$WATCHDOG_EXIT) — attempting direct run_monitor.py"
        python run_monitor.py
        MONITOR_EXIT=$?
        if [ "$MONITOR_EXIT" -ne 0 ]; then
            echo "$(date) CRITICAL: Both watchdog and direct monitor failed. No trading today."
            echo "$(date) Check logs/watchdog_*.log and logs/crash_report_*.json for details."
            python -c "
try:
    from monitor.alerts import send_alert
    send_alert('CRITICAL: Trading monitor failed to start — both watchdog and direct run crashed. Check logs immediately.', severity='CRITICAL')
except Exception:
    pass
" 2>/dev/null
        fi
    fi
fi
