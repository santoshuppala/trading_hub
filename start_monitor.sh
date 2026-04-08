#!/bin/bash
# Daily trading monitor launcher
# Scheduled via cron at 6:00 AM PST (weekdays only)

# Ensure standard tools are available (cron has a minimal PATH)
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

cd /Users/santosh/Documents/santosh/trading_hub/trading_hub

# Load .env — parse line by line, skip blank lines and comments
if [ -f .env ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        # Strip inline comments, trim whitespace
        line="${line%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        # Export non-empty KEY=VALUE lines
        [ -n "$line" ] && [ "${line#*=}" != "$line" ] && export "$line"
    done < .env
fi

# Activate virtual environment
source venv/bin/activate

# Run monitor (logs go to logs/monitor_YYYY-MM-DD.log automatically)
python run_monitor.py
