# V9 Session Watchdog and Self-Healing

## Overview

The Session Watchdog (`scripts/session_watchdog.py`) is a standalone process that monitors all 4 trading hub processes and performs automatic self-healing when issues are detected. It runs alongside the Supervisor and provides an additional layer of resilience.

**Monitored Processes:**

| Process | Log File | Critical |
|---------|----------|----------|
| Supervisor | `supervisor.log` | Yes |
| Core | `core.log` | Yes |
| Options | `options.log` | No |
| DataCollector | `data_collector.log` | No |


## Health Checks

The watchdog runs a comprehensive suite of health checks every cycle:

| # | Check | What It Does | Failure Action |
|---|-------|-------------|----------------|
| 1 | **Process alive** | Verifies each process PID is running | Restart via supervisor |
| 2 | **Zombie detection** | Checks log staleness (no writes in N minutes) | Kill zombie, restart |
| 3 | **Log errors** | Scans recent log lines for ERROR/CRITICAL patterns | Alert + escalate |
| 4 | **Signal rate** | Monitors signals/hour against expected baseline | Alert if too low |
| 5 | **Positions** | Validates position count and P&L drift | Alert on anomaly |
| 6 | **Kill switch** | Checks if any engine kill switch has fired | Alert (no auto-fix) |
| 7 | **Fill ledger** | Validates lot integrity and shadow mode drift | Alert on mismatch |
| 8 | **Data freshness** | Checks last bar timestamp against wall clock | Restart data source |
| 9 | **Memory** | Monitors RSS memory usage per process | Kill + restart if excessive |
| 10 | **gap_and_go** | Detects gap_and_go dominating trade volume | Alert (strategy issue) |

### Zombie Detection

A process is considered a zombie when its log file has not been written to for a configurable threshold (default: 5 minutes during market hours). This catches processes that are technically alive (PID exists) but stuck in an infinite loop, deadlock, or unresponsive state.

```
Log last modified: 10:15:03
Current time:      10:21:47
Staleness:         6m 44s  → ZOMBIE detected
Action:            Kill PID, restart via supervisor
```


## Self-Healing Actions

The watchdog automatically repairs common issues without human intervention:

| # | Issue | Fix | Details |
|---|-------|-----|---------|
| 1 | **Corrupt JSON** | Restore from `.prev` backup | SafeStateFile keeps `.prev` and `.prev2` rolling backups |
| 2 | **Stale .lock files** | Remove lock file | Detects lock files older than 5 minutes (process died holding lock) |
| 3 | **__pycache__ bloat** | Clear `__pycache__` dirs | Prevents stale bytecode from causing import issues |
| 4 | **Supervisor down** | Restart supervisor | Re-launches `scripts/supervisor.py` which restarts child processes |
| 5 | **Zombie process** | Kill + restart | Sends SIGKILL to stuck process, supervisor restarts it |


## HotfixManager

`scripts/hotfix_manager.py` automatically detects and fixes common runtime errors by patching source files. Designed for **paper trading** use — auto-apply mode keeps the system running through fixable crashes.

### Pattern Matching

The HotfixManager scans log files for traceback patterns and applies targeted fixes:

| Error Type | Example | Fix Strategy |
|------------|---------|-------------|
| `NameError` | `name 'xyz' is not defined` | Add missing import or variable initialization |
| `AttributeError` | `'NoneType' has no attribute 'foo'` | Add None guard before access |
| `TypeError` | `unsupported operand type(s)` | Add type check or cast |
| `ImportError` | `No module named 'xyz'` | Fix import path or add fallback |
| `KeyError` | `KeyError: 'missing_key'` | Switch to `.get()` with default |
| `IndexError` | `list index out of range` | Add bounds check |
| `ZeroDivisionError` | `division by zero` | Add zero-guard denominator check |

### Safety Guarantees

| Safety Feature | Description |
|----------------|-------------|
| `.bak` backup | Original file backed up before any modification |
| Syntax check | `py_compile.compile()` validates fix before writing |
| Max 5 per session | No more than 5 hotfixes applied per trading session |
| Protected files | Critical files (e.g., `event_bus.py`, `fill_ledger.py`) are never auto-patched |
| Revert on failure | If syntax check fails, `.bak` is restored automatically |

### Modes

| Mode | Behavior | Use Case |
|------|----------|----------|
| **Auto-apply** | Fix is applied immediately, logged, and emailed | Paper trading |
| **Email approval** | Fix is drafted and emailed; human must approve before apply | Live trading |

```
[HOTFIX] NameError in pro_setups/detectors/gap_detector.py:47
  Error: name 'np' is not defined
  Fix: Added 'import numpy as np' at line 1
  Backup: gap_detector.py.bak
  Syntax check: PASSED
  Status: APPLIED (auto-apply mode)
```


## Periodic Email Updates

The watchdog sends 7 hourly status emails during market hours (9:30 AM - 4:00 PM ET, every hour on the half-hour).

Each email includes:

- Process status (alive/dead/zombie for each of the 4 processes)
- Open positions and unrealized P&L
- Trades executed since last email
- Kill switch status
- Any errors or warnings from the last hour
- Self-healing actions taken
- Hotfixes applied (if any)
- Memory usage per process
- Signal rate vs. expected baseline


## Resilient Run Loop

The watchdog uses a `_run_one_cycle()` architecture with cycle-level exception handling:

```python
consecutive_errors = 0

while running:
    try:
        _run_one_cycle()
        consecutive_errors = 0
    except Exception as e:
        consecutive_errors += 1
        log.error(f"Cycle error ({consecutive_errors}/10): {e}")

        if consecutive_errors >= 10:
            _send_escalation_email(e)
            _emergency_shutdown()
            break

    sleep(cycle_interval)
```

- Each cycle is independently try/except wrapped
- A single cycle failure does not crash the watchdog
- After **10 consecutive errors**, the watchdog sends an escalation email and initiates emergency shutdown
- Successful cycles reset the error counter to zero
