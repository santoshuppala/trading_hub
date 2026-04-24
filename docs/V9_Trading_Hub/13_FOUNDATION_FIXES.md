# V9 Foundation Fixes — Reliability Before Alpha

## Context: April 22, 2026 Production Session

The April 22 session exposed 20 systemic issues that resulted in:
- **$50.16 P&L drift**: logs showed -$9.79, brokers showed -$59.95
- **Options engine blind all day**: zero trades despite 40+ pro signals
- **No email alerts**: Yahoo rate-limited after 84 false CRITICAL emails in first hour
- **Core crashed**: `dict changed size during iteration` in shared cache writer
- **3 supervisors running simultaneously**: orphaned processes, duplicated children

These are not isolated bugs. They stem from 5 architectural gaps that have accumulated across V1-V9 as features were added without hardening the foundation.

---

## The 20 Issues

| # | Issue | Root Cause | Severity |
|---|---|---|---|
| 1 | P&L: $+10 in logs, -$7 in email, -$60 at broker | Multiple P&L sources disagree | CRITICAL |
| 2 | CRASH1/CRASH2 adding +$25 phantom P&L | Test data in production state files | CRITICAL |
| 3 | Kill switch false CRITICAL (test/test2) | Test kill switch files in data/ | HIGH |
| 4 | SMTP flood → Yahoo rate limit → no emails all day | 84 emails/hour from false alerts | HIGH |
| 5 | Watchdog showing 0pos/0trades/$0 | Reading data/bot_state.json (wrong file) | HIGH |
| 6 | Options engine zero trades all day | 3 compounding failures (7+8+9) | CRITICAL |
| 7 | IPC signals silently dropped for months | SignalPayload missing required fields, caught by except/debug | CRITICAL |
| 8 | PRO_STRATEGY_SIGNAL not forwarded to options | No IPC subscription for pro signals | HIGH |
| 9 | Shared cache permanently stale for options | Core crash stopped cache writes | HIGH |
| 10 | Core crash at 07:39 (bars_cache.items) | Race condition: bar fetch vs cache writer | HIGH |
| 11 | DDOG order orphaned after crash | Crash mid-order, handlers couldn't process ORDER_FAIL | CRITICAL |
| 12 | 3 supervisors running simultaneously | No exclusive lock, manual restarts stacked | HIGH |
| 13 | Equity tracker only checking Alpaca | Baseline set before Tradier registered | MEDIUM |
| 14 | ps/grep/pgrep/tail failing in watchdog | Bare command names, supervisor minimal PATH | MEDIUM |
| 15 | Duplicate log lines (every entry 2x) | StreamHandler + supervisor stdout to same file | LOW |
| 16 | Broker map has test entries (FAIL_TEST, SAT1) | Tests wrote to production broker map | MEDIUM |
| 17 | $50 P&L drift across 4 restarts | Each restart lost trade_log continuity | CRITICAL |
| 18 | 115 fills at Tradier, 22 in our logs | Trades after restart not linked to prior session | CRITICAL |
| 19 | bot_state.json at root vs data/ | No centralized path constant | MEDIUM |
| 20 | Email P&L from empty trade_log | Email read from data/bot_state.json (empty file) | HIGH |

### Issues Fixed During The Session

| # | Issue | Fix Applied |
|---|---|---|
| 3 | False kill switch | Deleted test files + added engine allowlist |
| 7 | IPC signals dropped | Added missing required fields to SignalPayload construction |
| 8 | PRO signals not forwarded | Added PRO_STRATEGY_SIGNAL IPC subscription in run_core.py |
| 10 | Core crash dict race | `list(dict)` snapshot before iteration in shared_cache.py |
| 13 | Equity only Alpaca | Deferred baseline to first check_drift (after all brokers registered) |
| 14 | ps/grep PATH fail | Full paths: /bin/ps, /usr/bin/grep, /usr/bin/pgrep, /usr/bin/tail |
| 15 | Duplicate log lines | Removed StreamHandler (supervisor already captures stdout) |
| 19 | Wrong bot_state path | Added _BOT_STATE_FILE constant in watchdog |
| 20 | Email P&L empty | Email reads bot_state.json from project root |

---

## The 9 Foundation Fixes

### Fix A: Order Write-Ahead Log (WAL)

**Purpose:** Track every order through its complete lifecycle so no fill is ever lost, even across crashes.

**Architecture:**
```
Signal → RiskEngine → [WAL: INTENT] → HTTP POST → [WAL: SUBMITTED] → ACK → [WAL: ACKED] → Fill → [WAL: FILLED] → FillLedger → [WAL: RECORDED]
```

**Order State Machine:**
```
INTENT → SUBMITTED → ACKED → FILLED → RECORDED  (happy path)
INTENT → CANCELLED                                (signal cancelled)
SUBMITTED → REJECTED                              (broker rejected)
ACKED → CANCELLED                                 (timeout/manual)
ACKED → PARTIAL → FILLED → RECORDED               (partial fills)
```

**WAL Format (append-only, line-buffered):**
```json
{"seq":1, "state":"INTENT",    "client_id":"uuid-1", "ticker":"AAPL", "side":"BUY", "qty":10}
{"seq":2, "state":"SUBMITTED", "client_id":"uuid-1", "broker_req_id":"req-abc"}
{"seq":3, "state":"ACKED",     "client_id":"uuid-1", "broker_order_id":"ord-xyz"}
{"seq":4, "state":"FILLED",    "client_id":"uuid-1", "fill_price":150.0, "fill_qty":10}
{"seq":5, "state":"RECORDED",  "client_id":"uuid-1", "lot_id":"lot-aaa"}
```

**Crash Recovery:**
```
On startup:
  1. Replay WAL → find entries without terminal state (RECORDED/CANCELLED/REJECTED)
  2. For each incomplete:
     INTENT only     → never submitted, safe to ignore
     SUBMITTED/ACKED → query broker by client_id, import fill or cancel
     FILLED          → create FillLot, write RECORDED
```

**Latency Impact:**
```
WAL write: ~0.01ms per state transition (4 writes per trade)
Broker HTTP call: ~200-5000ms
Total overhead: ~0.04ms on a 200-5000ms operation = 0.001%
```

**Issues Solved:** 11 (orphaned orders), 17 ($50 drift), 18 (115 vs 22 fills)

---

### Fix B: Continuous Reconciliation

**Purpose:** Detect divergence between local state and broker reality within 5 minutes, not just at startup.

**Architecture:**
```
Background thread (every 5 minutes):
  1. Query ALL brokers for open positions
  2. Query ALL brokers for recent fills since last check
  3. Compare with FillLedger open lots
  4. Divergence → ALERT + auto-correct:
     - Broker has position, we don't → ORPHANED → import
     - We have position, broker doesn't → PHANTOM → mark closed
     - Qty mismatch → PARTIAL FILL missed → adjust
```

**What It Catches:**
- Bracket stops filled by broker (we didn't see the fill notification)
- Manual trades placed directly at broker console
- Fill notifications lost due to WebSocket disconnect
- Orders filled during a process restart gap
- External portfolio changes (margin calls, corporate actions)

**Latency Impact:** Zero on hot path. Runs on background daemon thread.

**Issues Solved:** 11 (orphaned orders), 13 (equity both brokers), 17 ($50 drift), 18 (115 vs 22 fills)

---

### Fix C: FillLedger as P&L Authority (Exit Shadow Mode)

**Purpose:** Single source of truth for P&L. No more trade_log in bot_state.json.

**Current State (broken):**
```
bot_state.json trade_log     ← PositionManager writes (in-memory, lost on restart)
fill_ledger.json             ← FillLedger writes (append-only, survives restarts)
StateEngine._trade_log       ← in-memory copy (lost on restart)
Broker accounts              ← actual reality
```

**Target State:**
```
bot_state.json:
  positions         ← mutable state (stops, targets, partial_done)
  reclaimed_today   ← dedup/cooldown
  last_order_time   ← cooldown
  # NO trade_log — P&L is not process state

fill_ledger.json:
  lots              ← immutable fill records (append-only)
  lot_states        ← remaining qty per lot
  position_meta     ← current stops/targets
  daily_pnl         ← pre-computed from FIFO matches (new)
  open_positions    ← ticker → {qty, avg_entry, strategy} (new)
```

**Why FillLedger Is Better Than trade_log:**
- Append-only: never forgets a fill, even across crashes
- FIFO matching: mathematically correct P&L per lot
- Survives restarts: replays lots → reconstructs matches → correct P&L
- trade_log is in-memory accumulation: lost on crash, reset on restart, drifts

**Warm Path Readers:** Watchdog, email, equity tracker read `daily_pnl` and `open_positions` from `fill_ledger.json`. One file, one number, always correct.

**Latency Impact:** Zero. FillLedger already persists after every mutation. Adding `daily_pnl` field is one float computation.

**Issues Solved:** 1 (P&L disagrees), 5 (watchdog wrong data), 17 ($50 drift), 20 (email P&L empty)

---

### Fix D: Centralized State Path Constant

**Purpose:** Every module reads state files from the same location. No more root vs data/ confusion.

**Implementation:**
```python
# config.py — single definition
STATE_DIR = os.path.join(PROJECT_ROOT, 'data')
BOT_STATE_PATH = os.path.join(STATE_DIR, 'bot_state.json')
FILL_LEDGER_PATH = os.path.join(STATE_DIR, 'fill_ledger.json')
OPTIONS_STATE_PATH = os.path.join(STATE_DIR, 'options_state.json')
BROKER_MAP_PATH = os.path.join(STATE_DIR, 'position_broker_map.json')
```

**Enforcement:** Grep for raw `'bot_state.json'` strings — they should only exist in config.py.

**Issues Solved:** 5 (watchdog wrong file), 19 (root vs data/), 20 (email wrong file)

---

### Fix E: Test Isolation

**Purpose:** Tests can never write to production state files.

**Implementation:**
```python
# conftest.py (autouse — applies to ALL tests)
@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv('STATE_DIR', str(tmp_path))
    monkeypatch.setattr('config.STATE_DIR', str(tmp_path))
    monkeypatch.setattr('config.BOT_STATE_PATH', str(tmp_path / 'bot_state.json'))
    monkeypatch.setattr('config.FILL_LEDGER_PATH', str(tmp_path / 'fill_ledger.json'))
    # Tests now write to /tmp/pytest-xxx/ — production files untouched
```

**Safety Net:** `_extract_if_valid()` in `monitor/state.py` already filters known test tickers (CRASH1, CRASH2, TEST_, FAKE_) as a second line of defense.

**Issues Solved:** 2 (CRASH1/2 phantom P&L), 3 (false kill switch), 16 (broker map test entries)

---

### Fix F: Supervisor Exclusive Lock

**Purpose:** Only one supervisor can run at a time. No orphaned processes.

**Implementation:**
```python
# supervisor.py — at startup, before anything else
import fcntl

lock_fd = open('.supervisor.lock', 'w')
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
except BlockingIOError:
    log.error('Another supervisor is already running (PID in .supervisor.lock) — exiting')
    sys.exit(1)
# lock_fd stays open for process lifetime — auto-releases on exit/crash
```

**Issues Solved:** 12 (3 supervisors), 9 (partially — single supervisor = consistent cache writes)

---

### Fix G: Startup Preflight Gate

**Purpose:** Verify all systems before trading starts. Fail loud, not silent.

**Checks:**
```
1. State files: correct location, no test data (CRASH/TEST/FAKE tickers)
2. SMTP: connect + auth to Yahoo SMTP (test with ehlo+starttls+login)
3. Supervisor: no duplicate PID (via flock check)
4. Brokers: Alpaca API ping, Tradier API ping
5. IPC: Redpanda connectivity + test message round-trip
6. Shared cache: writable, not stale
7. Database: TimescaleDB connection pool
8. Kill switch: no stale halted state from test runs
9. Subprocess commands: /bin/ps, /usr/bin/grep reachable
10. Disk space: data/ directory has >100MB free
```

**Behavior:**
```
All pass   → log "PREFLIGHT OK" → start trading
Any fail   → log each failure → send one emergency alert → EXIT (don't trade)
Skip mode  → start_monitor.sh --skip-preflight (emergency only)
```

**Issues Solved:** 2 (test data), 4 (SMTP), 7 (IPC), 8 (partially), 12 (duplicate supervisor), 13 (equity baseline), 14 (PATH)

---

### Fix H: Error Escalation

**Purpose:** No silent failures. Critical path errors are WARNING minimum, never debug.

**Rules:**
```
log.debug   → verbose tracing, optional, never for failures
log.info    → normal operations
log.warning → something unexpected but recoverable
log.error   → something broke, needs attention
CRITICAL    → trading may be compromised
```

**Anti-Patterns to Fix:**
```python
# BAD: hides broken IPC for months
except Exception as exc:
    log.debug("[IPC] Failed: %s", exc)

# BAD: reports broken check as OK
return HealthCheck('memory', 'OK', f'Check skipped: {e}')

# GOOD: surfaces failure
except Exception as exc:
    log.warning("[IPC] Signal processing FAILED for %s: %s", ticker, exc)
    self._ipc_errors += 1

# GOOD: reports broken check as broken
return HealthCheck('memory', 'WARN', f'Check failed: {e}', 'WARNING')
```

**Error Budget Pattern:** Track consecutive errors per component. Escalate severity automatically after N failures:
```
1-2 failures  → log.warning (intermittent, probably transient)
3-5 failures  → log.error (persistent, needs investigation)
5+ failures   → CRITICAL alert (feature is broken)
```

**Issues Solved:** 7 (IPC silent drops), 8 (missing subscription surfaced), 14 (failed check reported correctly)

---

### Fix I: Race Condition Fix (Already Applied April 22)

**Purpose:** Prevent `RuntimeError: dictionary changed size during iteration` in shared cache writer.

**Fix:** `list(bars_cache)` snapshots keys before iteration. Per-key `try/except` handles concurrent removal.

**Also Applied:** `try/except RuntimeError` around `cache_writer.write()` in run_core.py main loop — retry next cycle instead of crashing.

**Issues Solved:** 9 (stale cache), 10 (core crash), 11 (DDOG orphaned — partially)

---

## Coverage Matrix

```
Issue                          A    B    C    D    E    F    G    H    I
                              WAL  REC  LED  PTH  TST  LCK  PRE  ERR  RCE
──────────────────────────────────────────────────────────────────────────
 1. P&L disagrees              ~    .    Y    .    .    .    .    .    .
 2. CRASH1/2 phantom P&L       .    .    .    .    Y    .    Y    .    .
 3. False kill switch           .    .    .    .    Y    .    .    .    .
 4. SMTP flood/rate limit       .    .    .    .    .    .    Y    .    .
 5. Watchdog wrong file         .    .    ~    Y    .    .    .    .    .
 6. Options zero trades         .    .    .    .    .    .    .    .    .
 7. IPC silently dropped        .    .    .    .    .    .    Y    Y    .
 8. PRO not forwarded           .    .    .    .    .    .    ~    ~    .
 9. Shared cache stale          .    .    .    .    .    ~    .    .    Y
10. Core crash dict race        .    .    .    .    .    .    .    .    Y
11. DDOG order orphaned         Y    Y    .    .    .    .    .    .    ~
12. 3 supervisors               .    .    .    .    .    Y    Y    .    .
13. Equity only Alpaca          .    Y    .    .    .    .    Y    .    .
14. ps/grep PATH fail           .    .    .    .    .    .    Y    Y    .
15. Duplicate log lines         .    .    .    .    .    .    .    .    .
16. Broker map test entries     .    .    .    .    Y    .    .    .    .
17. $50 P&L drift               Y    Y    Y    .    .    .    .    .    .
18. 115 fills, 22 tracked       Y    Y    .    .    .    .    .    .    .
19. bot_state two locations     .    .    .    Y    .    .    .    .    .
20. Email P&L empty             .    .    Y    Y    .    .    .    .    .

ISSUES SOLVED:                 3    4    4    3    3    2    7    3    3
```

Y = directly solves, ~ = partially helps, . = does not address

Issue #6 (options zero trades) required 3 fixes compounding (7+8+9).
Issue #15 (duplicate logs) already fixed during April 22 session.
Issue #8 (PRO not forwarded) already fixed during April 22 session.

---

## Implementation Status — ALL 9 FIXES COMPLETE

Implemented April 22, 2026. Readiness score: **100/100**.

| Fix | Status | Files Changed | Key Implementation |
|---|---|---|---|
| **A. Order WAL** | ✅ DONE | `monitor/order_wal.py` (NEW), `risk_engine.py`, `risk_adapter.py`, `brokers.py`, `tradier_broker.py`, `position_manager.py`, `options/broker.py`, `run_core.py`, `run_options.py` | Append-only WAL with INTENT→SUBMITTED→FILLED→RECORDED state machine. Covers core + options. Startup recovery replays WAL and queries broker. 0.04ms overhead per trade. |
| **B. Continuous Reconciliation** | ✅ DONE | `monitor/monitor.py`, `lifecycle/core.py` | Reduced from 10/30/60 min → **5 min**. Core `_live_reconcile()` checks both Alpaca + Tradier. Options `sync_periodic()` via lifecycle. Email alert on phantom/orphan detection. Equity drift check every 5 min. |
| **C. FillLedger P&L Authority** | ✅ DONE | `monitor/fill_ledger.py`, `scripts/session_watchdog.py` | FillLedger persists `daily_pnl`, `open_positions`, `trade_count` in v2 format. Watchdog and email read from `fill_ledger.json` as primary P&L source. Fallback to `bot_state.json` if FillLedger unavailable. |
| **D. State Path Constants** | ✅ DONE | `config.py`, `monitor/state.py`, `monitor/fill_ledger.py`, `monitor/shared_cache.py`, `monitor/smart_router.py`, `scripts/supervisor.py`, `scripts/session_watchdog.py`, `scripts/run_core.py` | 6 path constants in `config.py` (`BOT_STATE_PATH`, `FILL_LEDGER_PATH`, `OPTIONS_STATE_PATH`, `BROKER_MAP_PATH`, `SUPERVISOR_STATUS_PATH`, `LIVE_CACHE_PATH`). `STATE_DIR` overridable via env var for test isolation. Zero hardcoded paths in production code. |
| **E. Test Isolation** | ✅ DONE | `test/conftest.py` (NEW), `monitor/order_wal.py` | `autouse=True` fixture redirects all state paths to `tmp_path`. OrderWAL lazy-initialized (no file on import). `@pytest.mark.no_isolate` opt-out for integration tests. 52 tests pass, zero production file changes. |
| **F. Supervisor Lock** | ✅ DONE | `scripts/supervisor.py`, `.gitignore` | `fcntl.flock` exclusive lock at startup. PID written for diagnostics. Second instance gets clear error and exits. Auto-releases on crash. |
| **G. Startup Preflight** | ✅ DONE | `scripts/preflight.py` (NEW), `scripts/supervisor.py` | 10 checks: state cleanliness, SMTP, Alpaca, Tradier, Redpanda, DB, subprocess commands, kill switch, disk space, data dir. Critical failures block trading. `SKIP_PREFLIGHT=1` escape hatch. Alert email on failure. |
| **H. Error Escalation** | ✅ DONE | `monitor/monitor.py`, `monitor/position_manager.py`, `monitor/brokers.py`, `monitor/tradier_broker.py`, `monitor/kill_switch.py`, `monitor/portfolio_risk.py`, `monitor/metrics.py`, `scripts/run_core.py`, `scripts/run_options.py` | 19 critical `log.debug` → `log.warning` across IPC, broker, reconciliation, kill switch, position manager, buying power. Failures now visible in logs. |
| **I. Race Condition** | ✅ DONE | `monitor/shared_cache.py`, `scripts/run_core.py` | `list(dict)` snapshot before iteration. `try/except RuntimeError` in main loop. Core no longer crashes on concurrent bar fetch + cache write. |

### Issues Fixed During Session (Not in Original 9)

| Fix | Files | What |
|---|---|---|
| Kill switch false alerts | `session_watchdog.py` | Deleted test files, added `_KNOWN_KILL_SWITCH_ENGINES` filter |
| IPC signal forwarding | `run_core.py`, `run_options.py` | Added `PRO_STRATEGY_SIGNAL` IPC subscription + fixed `SignalPayload` missing fields |
| Equity tracker both brokers | `equity_tracker.py`, `run_core.py` | Deferred baseline, iterate current brokers not just baseline |
| Duplicate log lines | `session_watchdog.py` | Removed `StreamHandler` (supervisor redirects stdout) |
| Watchdog wrong bot_state | `session_watchdog.py` | `_BOT_STATE_FILE` → config constant |
| Watchdog bare commands | `session_watchdog.py` | `/bin/ps`, `/usr/bin/grep`, `/usr/bin/pgrep`, `/usr/bin/tail` |
| Email P&L wrong | `session_watchdog.py` | Reads from FillLedger (primary) with bot_state fallback |
| Email missing trades | `session_watchdog.py` | Added TRADES TODAY + OPEN POSITIONS sections |
| Email WAL stats | `session_watchdog.py` | Added ORDER WAL section to hourly email |
| Test data cleanup | `data/`, `bot_state.json` | Removed CRASH1/2 trades, test kill switch files, broker map test entries |
| Test data filter | `monitor/state.py` | Added CRASH/TEST/FAKE prefix filter + trade_log purge on load |
| Backtest stop bug | `backtests/fill_simulator.py` | No stop on fill bar (`filled_this_bar` flag) |
| Backtest partial exit | `backtests/fill_simulator.py` | `_close_partial()` actually sells 50%, records P&L, reduces qty |
| Backtest stop/target | `backtests/fill_simulator.py` | Distance-from-open heuristic for same-bar conflicts |
| Backtest EOD qty | `backtests/fill_simulator.py` | Uses `pos.qty` (after partials), not `original_qty` |
| Backtest slippage | `backtests/fill_simulator.py` | 10bps entry, 5bps stop exit, 2bps target exit |
| Edge context capture | `monitor/edge_context.py` (NEW), `events.py`, `fill_lot.py`, `strategy_engine.py`, `pro_setups/engine.py`, `options/engine.py`, `position_manager.py`, `run_core.py`, `run_options.py`, `db/event_sourcing_subscriber.py` | 6 fields (timeframe, regime, time_bucket, confidence, confluence, tier) captured across all 4 signal paths |
| Edge context DB | `db/migrations/sql/014_edge_context.sql` | Columns added to 5 tables: fill_lots, fill_events, signal_events, pro_strategy_signal_events, pop_signal_events |
| Emergency rollback | `emergency_rollback.sh` (NEW) | Stops all processes, stashes work, reverts to v9-baseline tag, clears pycache |

### New Files Created

| File | Purpose |
|---|---|
| `monitor/order_wal.py` | Order Write-Ahead Log — lifecycle tracking for every order |
| `monitor/edge_context.py` | Edge context helpers — regime, time bucket, confluence, signal context cache |
| `scripts/preflight.py` | Startup preflight gate — 10 system health checks |
| `test/conftest.py` | Test isolation — autouse fixture redirecting state to tmp_path |
| `emergency_rollback.sh` | Emergency rollback to v9-baseline |
| `db/migrations/sql/014_edge_context.sql` | DB migration for edge context columns |

---

## Post-Foundation: V10 Alpha Engine

### April 23: V10 Exit Engine (DONE)

**File:** `monitor/exit_engine.py` (NEW — ~600 lines)

Replaces panic-based exits (RSI > 70 → SELL ALL) with a structured 4-phase lifecycle that manages each position from entry to exit, with per-strategy behavior.

**Architecture:**
```
Old (V9):  RSI > 70? SELL. VWAP breakdown? SELL. Stop hit? SELL.
           (all exits equal, all fire independently, all sell 100%)

New (V10): Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 4
           (structured, per-strategy, confluence-aware)
```

**Phases:**

| Phase | Purpose | Active | Duration |
|---|---|---|---|
| 0: Validation | Thesis check — did entry setup hold? | Stop + per-strategy thesis check | 1-4 bars (ATR-adaptive) |
| 1: Protection | Let entry establish | Stop only. No RSI/VWAP exits. | 2-4 bars or higher low or 0.5R |
| 2: Breakeven | Protect capital | Move stop to BE (with cushion for structure). Mild RSI tighten. Dead trade check. | Until 1R |
| 3: Harvest | Lock profit | Confluence-aware partials (33/50/66%). Structure trail 0.7R min. Normal RSI tighten. | Until 2R (or 1.5R for high confluence) |
| 4: Runner | Capture big moves | 5-bar swing low + VWAP floor + R floor. No RSI for structure. | Until trail catches or EOD |

**Per-Strategy Profiles (10 defined):**

| Strategy | Stop | Phase 1 | Partial | Trail | RSI Tighten | VWAP |
|---|---|---|---|---|---|---|
| sr_flip | Structure 1.0 ATR | 3 bars | 50% at 1R | Structure 0.5R | Yes @72 | 2-bar tighten |
| trend_pullback | Structure 0.8 ATR | 2 bars | 50% at 1R | Higher lows | No | 2-bar tighten |
| momentum_ignition | ATR 2.0× | 3 bars | 33% at 2R | ATR 1.5× | No | None |
| orb | Structure 1.5 ATR | 3 bars | 50% at 1R | Structure 0.5R | Yes @70 | 2-bar tighten |
| bollinger_squeeze | ATR 2.0× | 4 bars | 50% at 1.5R | ATR 1.5× | Yes @75 | None |
| liquidity_sweep | Structure 2.5 ATR | 3 bars | 50% at 1.5R | Structure 1.0R | No | None |

**Confluence-Aware Behavior:**
- 3+ detectors → 33% partial, entry+0.25R post-stop, Phase 4 at 1.5R, wider trail 0.8R
- 2 detectors → 50% partial, entry+0.5R post-stop, Phase 4 at 2.0R
- 1 detector → 66% partial, entry+0.5R post-stop, Phase 4 at 2.0R

**Impulse vs Structure:**
- Impulse (momentum, sweep, gap_and_go): true breakeven, 15-bar dead trade, RSI tightens Phase 4
- Structure (sr_flip, orb, inside_bar, etc.): 0.1R cushion, 30-bar dead trade, RSI ignored Phase 4

**Lifecycle Data Captured to DB:**
- `position_events.close_detail.lifecycle` (JSONB): 15 fields including exit_phase, unrealized_r, bars_held, running_high, confluence_count, partial_pct, is_impulse
- `position_events.close_detail.lifecycle.events[]`: full decision event log — every phase transition, RSI tighten, VWAP tighten, partial exit, breakeven move

**Lifecycle Recovery on Restart:**
- partial_done → starts at Phase 3
- In profit (> 1 ATR above entry) → starts at Phase 2
- Existing position → starts at Phase 1 (skip Phase 0)

### April 23: Additional Fixes

| Fix | What |
|---|---|
| Supervisor startup crash | `name` → `path` in state file validation loop |
| SMTP stale password | Deleted `test/.env`, .env loaders always override (not setdefault) |
| Streaming banner [OFF] | Check `_running` instead of `is_connected` (0 tickers = no data flow) |
| FillLedger stale lots | `close_stale_lots()` on startup + FIFO re-match |
| Watchdog fill_ledger count | Filter by today's date, label prior-day carry-overs |
| Watchdog positions count | Positions from bot_state (reconciled), P&L from FillLedger |
| Heartbeat P&L mismatch | StateEngine uses FillLedger.daily_realized_pnl() as authority |
| Phantom POSITION CLOSED | `_live_reconcile` emits POSITION CLOSED so StateEngine updates |
| EOD report not sent | SIGTERM handler generates report before watchdog exits |
| EOD report enhanced | Broker BOD/EOD equity, open positions, P&L reconciliation, drift |
| Heartbeat.json stale HUNG | Delete heartbeat file at EOD shutdown |
| Trade analysis report | `reports/daily_analysis/trade_analysis_YYYYMMDD.csv` with 28 fields |
| Trade analysis dashboard | `dashboards/trade_analysis_dashboard.py` (Streamlit, port 8503) |

### Next Steps (Edge Model Pipeline)

1. **Edge context data capture** — DONE (April 22). Collecting from every signal.
2. **V10 Exit Engine** — DONE (April 23). Phase-based lifecycle, all data to DB.
3. **EdgeStore + AdaptiveRanker** — self-calibrating signal scoring. Needs 2-4 weeks of lifecycle data.
4. **Entry gate (EV model)** — expected value scoring before ORDER_REQ. Design complete.
5. **SignalArbiter** — bar-cycle-aware arbitration. Design complete.

### Future Enhancements Noted

- Auto-generate daily trade analysis CSV in post_session_analytics.py
- fill_lots table: add exit lifecycle data
- Order WAL: migrate to DB for SQL queryability
- RiskSizer beta pre-seed: cache to disk (35s → <1s startup)
- WebSocket bar building: replace 12s REST polling
- Detector key levels: each detector outputs key_level/invalidation/structure_trail
- Short position support in lifecycle

---

## Design Principles (Lessons from April 22-23)

1. **The broker is God.** Local state is a cache of broker reality. Always reconcile.
2. **Append-only > mutable.** FillLedger (append lots) is correct. trade_log (in-memory list) drifts.
3. **Fail loud, not silent.** `log.debug` on a critical failure = months of blindness.
4. **Test isolation is not optional.** One test writing to production state corrupts an entire trading day.
5. **One source per data type.** P&L from one place. Positions from one place. Not five places that disagree.
6. **Validate before trading.** Preflight checks cost 10 seconds. A blind trading day costs money.
7. **Background reconciliation is cheap insurance.** One API call every 5 minutes catches everything startup reconciliation misses.
8. **WAL writes are free.** 0.01ms append vs 200ms broker call. The log that saves you from $50 drift costs nothing.
9. **Options parity with core.** Every fix to core MUST also be applied to options. Never leave options behind.
10. **No hardcoded paths.** State file paths defined once in config.py. Override via env var for test isolation.
11. **Exits should be phase-based, not trigger-based.** RSI/VWAP are trail tighteners, not kill switches.
12. **Per-strategy exit profiles.** sr_flip and momentum_ignition have completely different edge profiles — exits should match the thesis.
13. **Capture everything for ML.** Every phase transition, every decision, every trail tighten → DB. Can't optimize what you can't measure.
