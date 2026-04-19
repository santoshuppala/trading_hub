#!/usr/bin/env python3
"""
Watchdog launcher — auto-discovered by supervisor.

Runs session_watchdog.py with 2-minute check interval.
Non-critical: watchdog crash doesn't affect trading.
"""
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
