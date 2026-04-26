#!/bin/bash
# Install the trading system as a launchd service (macOS).
#
# This makes launchd (PID 1, cannot crash) responsible for keeping
# the outer watchdog alive. If it dies, launchd restarts it instantly.
#
# Chain: launchd → outer_watchdog → supervisor → engines
#
# Usage:
#   chmod +x scripts/install_launchd.sh
#   ./scripts/install_launchd.sh          # install + start
#   ./scripts/install_launchd.sh stop     # stop
#   ./scripts/install_launchd.sh uninstall # remove
#   ./scripts/install_launchd.sh status   # check status

set -e

LABEL="com.tradinghub.outer-watchdog"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$PROJECT_ROOT/venv/bin/python"
SCRIPT="$PROJECT_ROOT/scripts/outer_watchdog.py"
LOG_DIR="$PROJECT_ROOT/logs"
STDOUT_LOG="$LOG_DIR/outer_watchdog_launchd.log"
STDERR_LOG="$LOG_DIR/outer_watchdog_launchd_err.log"

mkdir -p "$LOG_DIR" "$PLIST_DIR"

case "${1:-install}" in
  stop)
    echo "Stopping $LABEL..."
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    echo "Stopped."
    exit 0
    ;;
  uninstall)
    echo "Uninstalling $LABEL..."
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST_PATH"
    echo "Uninstalled."
    exit 0
    ;;
  status)
    echo "=== Service Status ==="
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null || echo "Not loaded"
    echo ""
    echo "=== Processes ==="
    ps aux | grep -E "(outer_watchdog|supervisor|run_core|run_options)" | grep -v grep || echo "None running"
    echo ""
    echo "=== Recent Log ==="
    tail -5 "$STDOUT_LOG" 2>/dev/null || echo "No log yet"
    exit 0
    ;;
  install)
    ;;
  *)
    echo "Usage: $0 [install|stop|uninstall|status]"
    exit 1
    ;;
esac

# Verify paths
if [ ! -f "$PYTHON" ]; then
  echo "ERROR: Python not found at $PYTHON"
  echo "Run: python -m venv venv && venv/bin/pip install -r requirements.txt"
  exit 1
fi
if [ ! -f "$SCRIPT" ]; then
  echo "ERROR: Script not found at $SCRIPT"
  exit 1
fi

# Stop existing if running
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

# Generate plist
cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$SCRIPT</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$PROJECT_ROOT</string>

    <!-- KeepAlive: launchd restarts if process exits for ANY reason -->
    <key>KeepAlive</key>
    <true/>

    <!-- Throttle: wait 30s before restarting (prevents rapid restart loop) -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <!-- Environment: load .env vars -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:$PROJECT_ROOT/venv/bin</string>
        <key>PYTHONPATH</key>
        <string>$PROJECT_ROOT</string>
    </dict>

    <!-- Logging -->
    <key>StandardOutPath</key>
    <string>$STDOUT_LOG</string>
    <key>StandardErrorPath</key>
    <string>$STDERR_LOG</string>

    <!-- Nice: lower priority than trading processes -->
    <key>Nice</key>
    <integer>5</integer>
</dict>
</plist>
PLIST

echo "Plist written to: $PLIST_PATH"

# Load and start
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"

echo ""
echo "=== Installed and started ==="
echo "Service: $LABEL"
echo "Log:     $STDOUT_LOG"
echo ""
echo "Commands:"
echo "  ./scripts/install_launchd.sh status     # check status"
echo "  ./scripts/install_launchd.sh stop       # stop trading"
echo "  ./scripts/install_launchd.sh uninstall  # remove service"
echo ""
echo "launchd will keep the watchdog alive. If it dies, launchd restarts in 30s."
echo "Chain: launchd (PID 1) → outer_watchdog → supervisor → engines"
