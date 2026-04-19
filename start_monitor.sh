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

# V9: Clear __pycache__ to prevent stale bytecode after code changes
# (caused 32 data_collector crashes on Apr 17 from renamed function)
find . -path ./venv -prune -o -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
echo "$(date) Cleared __pycache__ directories"

# ── Execution mode ───────────────────────────────────────────────────────
# MONITOR_MODE controls how the trading system runs:
#   "monolith"    — single process (run_monitor.py via watchdog) [LEGACY]
#   "supervisor"  — 3 processes via supervisor: core, options, data_collector (V8)
#   "isolated"    — alias for "supervisor" (backward compat)
#
# Set in .env: MONITOR_MODE=supervisor
# Or override: MONITOR_MODE=supervisor ./start_monitor.sh
MONITOR_MODE="${MONITOR_MODE:-monolith}"

if [ "$MONITOR_MODE" = "supervisor" ] || [ "$MONITOR_MODE" = "isolated" ]; then
    # ── Supervisor Mode (V8) ────────────────────────────────────────────
    # V8: 3 processes — core (VWAP+Pro+Pop), options, data_collector
    # Pro and Pop merged into Core. Supervisor manages lifecycle.
    echo "$(date) Starting in SUPERVISOR mode (V8 — 3 engines)"
    python scripts/supervisor.py
    EXIT=$?
    if [ "$EXIT" -ne 0 ]; then
        echo "$(date) Supervisor exited with code $EXIT — NOT falling back to monolith"
        echo "$(date) V8: Monolith fallback disabled. Fix supervisor and restart manually."
    fi
else
    # ── Monolith Mode REMOVED in V8 ─────────────────────────────────────
    echo "$(date) ERROR: MONITOR_MODE must be 'supervisor'. Monolith mode removed in V8."
    echo "$(date) Set MONITOR_MODE=supervisor in .env"
    exit 1
fi
