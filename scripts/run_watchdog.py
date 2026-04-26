#!/usr/bin/env python3
"""
DEPRECATED — V10: Watchdog merged into outer_watchdog.py.

The supervisor no longer auto-discovers this script (_SKIP_SCRIPTS).
Use outer_watchdog.py instead — it combines process-level monitoring
(supervisor alive?) with quality-level monitoring (lifecycle health,
P&L drift, data staleness) in a single process outside the supervisor tree.

Start: python scripts/outer_watchdog.py
Or:    ./scripts/install_launchd.sh
"""
import sys
print("DEPRECATED: Use outer_watchdog.py instead.", file=sys.stderr)
print("  python scripts/outer_watchdog.py", file=sys.stderr)
sys.exit(1)
import os
import sys
import runpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set default args
sys.argv = [sys.argv[0], '--check-interval', '120']

runpy.run_path(
    os.path.join(os.path.dirname(__file__), 'session_watchdog.py'),
    run_name='__main__',
)
