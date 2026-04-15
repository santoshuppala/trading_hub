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
# Falls back to direct run if watchdog itself fails.
python scripts/watchdog.py || python run_monitor.py
