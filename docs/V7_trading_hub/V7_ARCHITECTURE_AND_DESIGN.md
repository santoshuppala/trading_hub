# Trading Hub V7.0 — Architecture & Design Document

**Source of truth:** Current codebase as of 2026-04-15
**No assumptions — every change traced from actual code with file paths**
**Builds on V6 architecture. This document covers ONLY what V7 changed.**

---

## 1. V7 MISSION

V7 resolves 27 findings from the Goldman Sachs architectural review across 6 severity levels (P0-P5). Zero new infrastructure — no Redis, no new message brokers, no new databases. Every fix uses existing Python stdlib, existing Redpanda topics, and existing file-based persistence.

**Evidence:** 387/387 tests passing across 9 test suites. 25 production files modified. 13 new files created.

---

## 2. SYSTEM ARCHITECTURE DELTA (V6 → V7)

### 2.1 What Changed

```
┌──────────────────────────────────────────────────────────────────┐
│                     SUPERVISOR (PID 1)                            │
│  scripts/supervisor.py                                           │
│                                                                  │
│  V7: _load_engine_config() reads ENGINE_CONFIG env var (JSON)    │
│  V7: Auto-discovers scripts/run_*.py not in config               │
│  V7: Detects 'hung' via heartbeat.json mtime > 120s             │
│  V7: Force-kills hung process via SIGKILL, then restarts         │
└──────┬──────────┬──────────┬──────────┬────────────────────────┘
       │          │          │          │
       ▼          ▼          ▼          ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│   CORE   │ │   PRO    │ │   POP    │ │ OPTIONS  │
│          │ │          │ │          │ │          │
│ V7 new:  │ │ V7:      │ │ V7:      │ │ V7:      │
│ Registry │ │ Read-only│ │ Read-only│ │ (same    │
│ Gate     │ │ registry │ │ registry │ │  changes │
│ IPC inbox│ │ layer=pro│ │ layer=pop│ │  as Pro) │
│ Metrics  │ │ corr_id  │ │ corr_id  │ │          │
│ Heartbeat│ │ in IPC   │ │ in IPC   │ │          │
│ file     │ │          │ │          │ │          │
└──────────┘ └──────────┘ └──────────┘ └──────────┘
```

### 2.2 What Did NOT Change

- Process isolation model (5 processes, Redpanda IPC, shared cache)
- EventBus v5.2 core dispatch (priorities, partitioning, coalescing, DLQ)
- Broker execution logic (Alpaca bracket orders, Tradier standalone stops)
- Strategy engines (VWAP, Pro detectors/classifiers, Pop screener, Options selector)
- DB layer (DBWriter batching, EventSourcingSubscriber projections, migration framework)
- Position lifecycle (PositionManager state machine)
- Data sources (Tradier bars, Benzinga news, StockTwits social)

**Known gap (V7.1):** `DataSourceCollector` (Fear & Greed, FRED, Finviz, SEC EDGAR, Polygon, Yahoo) runs ONLY in `run_monitor.py` (monolith mode). In supervised mode (`supervisor.py`), these 6 sources are NOT active. Only Tradier (Core), Benzinga (Pop), and StockTwits (Pop) collect data during supervised sessions.

---

## 3. STATE FILE HARDENING

### 3.1 SafeStateFile (`lifecycle/safe_state.py`)

Central V7 module replacing all ad-hoc state file I/O with 6 safety guarantees:

```
┌─────────────────────────────────────────────────────────────┐
│                    SafeStateFile                             │
│                                                             │
│  Write path:                                                │
│    1. Acquire fcntl EXCLUSIVE lock (blocks readers+writers) │
│    2. Read current version (inside lock)                    │
│    3. Bump version monotonically (v+1)                      │
│    4. Inject _version, _timestamp, _date                    │
│    5. Hash content (SHA256) — skip if unchanged             │
│    6. Rotate backups: current → .prev → .prev2             │
│    7. Atomic write: tmp file → os.replace()                 │
│    8. Write checksum sidecar (.sha256)                      │
│    9. Release lock                                          │
│                                                             │
│  Read path:                                                 │
│    1. Acquire fcntl SHARED lock (blocks writers only)       │
│    2. Read file + checksum sidecar                          │
│    3. Validate SHA256 checksum                              │
│    4. If mismatch → try .prev → try .prev2                 │
│    5. Check staleness (_timestamp vs max_age)               │
│    6. Release lock                                          │
│    7. Return (data, is_fresh)                               │
└─────────────────────────────────────────────────────────────┘
```

**Locking implementation** (`_FcntlLock` class):
- Lock file: `{state_file}.flock` (separate from data file)
- Write: `fcntl.flock(fd, LOCK_EX)` — blocks all other operations
- Read: `fcntl.flock(fd, LOCK_SH)` — allows concurrent readers, blocks writers

### 3.2 Files Using SafeStateFile

| State File | Module | V6 Locking | V7 Locking |
|-----------|--------|-----------|-----------|
| `bot_state.json` | `monitor/state.py` | `threading.Lock` (write only, no read lock) | SafeStateFile (fcntl all ops) |
| `position_registry.json` | `monitor/distributed_registry.py` | `fcntl.flock` (write only, reads unlocked) | SafeStateFile (fcntl all ops) |
| `position_broker_map.json` | `monitor/smart_router.py` | `threading.Lock` (write only, TOCTOU on read) | SafeStateFile (fcntl all ops) |
| `{engine}_state.json` | `lifecycle/state_persistence.py` | `threading.Lock` + atomic write | SafeStateFile (fcntl all ops) |
| `live_cache.pkl` | `monitor/shared_cache.py` | Atomic write only | fcntl exclusive/shared + SHA256 checksum |

### 3.3 StalenessGuard (`lifecycle/safe_state.py`)

Monitors state freshness in satellite main loops:

```python
guard = StalenessGuard(cache_sf, max_stale_cycles=2, alert_fn=send_alert)

# In main loop:
if not guard.check():
    break  # halted — ORDER_REQ emission stopped
```

- Cycle 1 stale → WARNING log, continue
- Cycle 2 stale → CRITICAL alert, halt ORDER_REQ emission
- State refreshed → auto-recover

### 3.4 SharedCache V7 Changes (`monitor/shared_cache.py`)

| Feature | V6 | V7 |
|---------|----|----|
| Write locking | Atomic rename only | `fcntl.flock(LOCK_EX)` + SHA256 checksum sidecar |
| Read locking | None | `fcntl.flock(LOCK_SH)` + checksum validation |
| Stale detection | `is_fresh()` exists but never called | `get_bars()` returns empty dicts when stale |
| TTL | 30s | **15s** |
| Pickle safety | `pickle.load(f)` — can hang | `f.read(_MAX_CACHE_BYTES)` → `pickle.loads(raw)` |
| Max read size | Unbounded | `_MAX_CACHE_BYTES = 50MB` |
| Version tracking | None | `_version` field in cache dict |

---

## 4. CENTRALIZED POSITION REGISTRY

### 4.1 RegistryGate (`monitor/registry_gate.py`)

New EventBus subscriber at Core. Only Core writes to `position_registry.json`.

```
Satellite (Pro/Pop/Options)              Core Process
──────────────────────────               ────────────
registry.held_by(ticker)                 RegistryGate._on_order_req()
  ↓ READ-ONLY (shared lock)               ↓
  if held by other → skip                registry.try_acquire(ticker, layer)
  else → emit ORDER_REQ(layer='pro')       ↓ WRITE (exclusive lock)
    ↓                                    if blocked → _blocked_ids.add(eid)
  IPC → Redpanda → Core                              emit RISK_BLOCK
                                         if acquired → pass to SmartRouter
                                           ↓
                                         On POSITION CLOSED:
                                           registry.release(ticker)
                                         On ORDER_FAIL:
                                           registry.release(ticker)
```

**Subscriptions:**
- `ORDER_REQ` priority 2 — acquires on BUY, skips SELL
- `POSITION` priority 2 — releases on CLOSED
- `ORDER_FAIL` priority 2 — releases acquired ticker (prevents leak)

**`layer` field** on `OrderRequestPayload` (`monitor/events.py`):
```python
layer: Optional[str] = None  # 'vwap', 'pro', 'pop', 'options'
```

**Layer inference** (backward compat for payloads without `layer`):
- `"pro:sr_flip:T1:long"` → `'pro'`
- `"pop:VWAP_RECLAIM:pop"` → `'pop'`
- `"options:iron_condor"` → `'options'`
- `"VWAP reclaim"` → `'vwap'` (default)

### 4.2 Satellite Changes

| Module | V6 | V7 |
|--------|----|----|
| `pro_setups/risk/risk_adapter.py` | `registry.try_acquire(ticker, 'pro_setups')` | `registry.held_by(ticker)` — read-only pre-flight |
| `pop_strategy_engine.py` | `registry.try_acquire(symbol, 'pop')` | `registry.held_by(symbol)` — read-only pre-flight |
| `monitor/risk_engine.py` | `registry.try_acquire(ticker, 'vwap')` | `registry.held_by(ticker)` — read-only pre-flight |
| `monitor/position_manager.py` | `registry.release(ticker)` on close | Removed — RegistryGate handles release |
| All `registry.release()` in finally blocks | Called on every failed check | Removed — Core owns lifecycle |

---

## 5. EVENTBUS PRIORITY CONVENTION

### 5.1 Dispatch Rule

**Higher priority number = runs FIRST in SYNC dispatch.**

Implementation: `event_bus.py` line 1173 stores `(-priority, sub_counter, key)`. Sort ascending means priority 10 executes before 3, which executes before 1.

### 5.2 ORDER_REQ Handler Chain

```
ORDER_REQ event arrives
  │
  │  pri=10  EventSourcingSubscriber      Persist to event_store
  │  pri=10  IPC publish                  Forward to Redpanda
  │
  ▼  pri=3   PortfolioRiskGate            Drawdown, notional, margin, Greeks
  │          V7: was priority 0 (ran AFTER SmartRouter — BUG)
  │          V7: fail-closed on None buying power
  │
  ▼  pri=2   RegistryGate [V7 NEW]        Cross-layer dedup (try_acquire)
  │          if blocked → _blocked_ids.add(event_id)
  │
  ▼  pri=1   SmartRouter                  Check is_blocked(), route to broker
  │          V7: _routed_event_ids dedup
  │          V7: generic brokers= dict
  │
  ▼  default Broker                       Execute order
```

### 5.3 V7 Priority Convention

```
Priority  Role                          V7 Change
────────  ────────────────────────────  ────────────────────
99        Passive audit (ActivityLogger)
10        Persistence + IPC publish
 4-9      RESERVED (gap)                ← V7: do not use
 3        PortfolioRiskGate             ← V7: was 0 (BUG FIX)
 2        RegistryGate                  ← V7: NEW
 1        SmartRouter / Broker
 0        Satellite IPC forwarding
default   Strategy engines, PositionManager
```

---

## 6. IDEMPOTENCY (TRIPLE SAFETY NET)

### 6.1 Layer 1: Redpanda Manual Commit (`monitor/ipc.py`)

```python
# V7: enable.auto.commit = False
# Commit AFTER handler succeeds. Crash before commit → replay.
self._consumer.commit(msg, asynchronous=True)
```

If handler crashes → offset not committed → Redpanda replays message.

### 6.2 Layer 2: SmartRouter ORDER_REQ Dedup (`monitor/smart_router.py`)

```python
self._routed_event_ids: dict = {}  # {event_id: monotonic_time}
self._ROUTED_MAX = 5000

# In _on_order_req:
if eid in self._routed_event_ids:
    return  # duplicate — skip
self._routed_event_ids[eid] = time.monotonic()
```

Replayed ORDER_REQ with same event_id → skipped.

### 6.3 Layer 3: Broker client_order_id (`monitor/brokers.py`)

```python
client_oid = f"th-buy-{p.ticker}-{event_id[:12]}-{attempt}"
req = LimitOrderRequest(..., client_order_id=client_oid)
```

Even if dedup misses → Alpaca rejects duplicate `client_order_id`.

### 6.4 SELL Verification (`monitor/brokers.py`)

V6: Emitted FILL optimistically when status unknown after 5s.
V7: Extended verify loop (3 retries × 1s each). Unverified → ORDER_FAIL + CRITICAL alert.

---

## 7. EVENT ORDERING

### 7.1 IPC Inbox Queue (`scripts/run_core.py`)

V6: IPC consumer daemon thread called `bus.emit()` directly — raced with main loop.
V7: Consumer enqueues to `queue.Queue(maxsize=500)`. Main loop drains:

```python
_ipc_inbox = queue.Queue(maxsize=500)

# Consumer thread (daemon):
def _on_remote_order(key, payload):
    _ipc_inbox.put_nowait(payload)  # never calls bus.emit()

# Main thread (each tick):
def _drain_ipc_inbox():
    while not _ipc_inbox.empty():
        payload = _ipc_inbox.get_nowait()
        monitor._bus.emit(Event(type=EventType.ORDER_REQ, ...))
```

### 7.2 Causation Chain (`monitor/brokers.py`)

Every FILL event carries the ORDER_REQ's event_id as `correlation_id`:

```
SIGNAL(eid=A) → ORDER_REQ(eid=B, corr=A)
  → IPC(correlation_id=B)
  → Core: ORDER_REQ(eid=C, corr=B)
  → Broker: FILL(eid=D, corr=C)     ← V7: _current_causation_id
  → PositionManager: POSITION(eid=E, corr=D)
```

Implementation: `_on_order_request()` sets `self._current_causation_id = event.event_id`. `_emit_fill()` reads it via `getattr(self, '_current_causation_id', None)`.

### 7.3 Crash Recovery Sort (`monitor/event_log.py`)

Already fixed in V6 codebase: `replay()` sorts by `stream_seq` before processing.

### 7.4 Event Payload Versioning (`monitor/event_bus.py`)

```python
@dataclass
class Event:
    ...
    payload_version: int = 1  # V7 P5-2: increment when payload fields change
```

Projection builder can handle old events with different field sets during replay.

---

## 8. TIMEOUTS

### 8.1 Handler Execution Timeout (`monitor/event_bus.py`)

```python
HANDLER_TIMEOUT_SEC = 30.0  # 0 to disable (V6 compat)
```

In `_deliver()`: wraps handler call in `concurrent.futures.ThreadPoolExecutor.submit()` + `future.result(timeout=HANDLER_TIMEOUT_SEC)`. On timeout → `TimeoutError` → counts as failure → circuit breaker triggers after 5 consecutive.

### 8.2 Option Chain Snapshot Timeout (`options/chain.py`)

```python
_SNAPSHOT_TIMEOUT_SEC = 15.0

with concurrent.futures.ThreadPoolExecutor(1) as ex:
    future = ex.submit(self._data_client.get_option_snapshot, req)
    snaps = future.result(timeout=self._SNAPSHOT_TIMEOUT_SEC)
```

On timeout: batch skipped (don't retry hangs — API is unresponsive).

### 8.3 Pickle Read Bounded (`monitor/shared_cache.py`)

```python
_MAX_CACHE_BYTES = 50 * 1024 * 1024  # 50MB cap
raw = f.read(_MAX_CACHE_BYTES)
data = pickle.loads(raw)
```

File I/O bounded by file size. Memory deserialization can't block on I/O.

### 8.4 Portfolio Risk Fail-Closed (`monitor/portfolio_risk.py`)

V6: `if buying_power is not None and ...` — silently skipped check.
V7: `if buying_power is None:` → BLOCK order + CRITICAL alert.

---

## 9. OBSERVABILITY

### 9.1 Cross-Process Correlation (`monitor/ipc.py`)

```python
# Publisher (satellite):
publisher.publish(TOPIC_ORDERS, ticker, payload,
                  correlation_id=event.event_id)

# Envelope:
{'source': 'pro', 'timestamp': '...', 'correlation_id': 'abc123', 'payload': {...}}

# Consumer (Core):
corr_id = envelope.get('correlation_id', '')
payload['_ipc_correlation_id'] = corr_id

# Drain:
monitor._bus.emit(Event(..., correlation_id=ipc_corr))
```

### 9.2 Heartbeat Health Check (`monitor/observability.py` + `scripts/supervisor.py`)

**HeartbeatEmitter** writes `data/heartbeat.json` every 60s:
```json
{
  "timestamp": "2026-04-15T14:32:00-04:00",
  "wall_clock": 1713203520.0,
  "n_tickers": 183,
  "last_signal_age_sec": 45.2,
  "last_fill_age_sec": 312.7
}
```

**Supervisor** reads mtime: if `time.time() - mtime > 120s` → process is `'hung'` → SIGKILL + restart.

### 9.3 Metrics Export (`monitor/metrics.py`)

`MetricsWriter` snapshots `bus.metrics()` to `data/metrics.json` every 60s:
- `emit_counts` — per-EventType
- `handler_avg_ms`, `handler_max_ms` — latency distribution
- `handler_errors`, `handler_circuit` — error rates and circuit breaker states
- `slow_calls`, `circuit_breaks`, `active_breaks`
- `queue_depths`, `system_pressure` — backpressure indicators
- `sla_breaches`, `dlq_count`, `duplicate_events_dropped`

### 9.4 Silent Failure Detection (`monitor/observability.py`)

HeartbeatEmitter subscribes to SIGNAL and FILL events:
- `_SIGNAL_SILENCE_ALERT_SEC = 1800` — CRITICAL alert if no SIGNAL for 30 minutes
- `_FILL_SILENCE_ALERT_SEC = 3600` — WARNING if no FILL for 60 minutes
- Only during market hours (10:00 AM - 3:45 PM ET)
- Auto-resets when new SIGNAL/FILL arrives

---

## 10. EXTENSIBILITY

### 10.1 Generic Broker Registration (`monitor/smart_router.py`)

```python
# V7: Dict-based (any number of brokers)
SmartRouter(bus=bus, brokers={
    'alpaca': alpaca_broker,
    'tradier': tradier_broker,
    'ibkr': ibkr_broker,
})

# Legacy (backward compat)
SmartRouter(bus=bus, alpaca_broker=ab, tradier_broker=tb)
```

`_register_broker()` handles unsubscription generically. `_select_broker()` uses `broker.has_position(ticker)` (generic) with `broker._client.get_open_position()` fallback (legacy Alpaca).

### 10.2 Config-Driven Engine Registration (`scripts/supervisor.py`)

Three ways to register engines:
1. **Default:** `_DEFAULT_PROCESSES` dict (core, pro, pop, options)
2. **Env var:** `ENGINE_CONFIG='{"arbitrage": {"critical": false}}'` — merged with defaults
3. **Auto-discover:** Any `scripts/run_*.py` not in config → registered automatically

Disable: `ENGINE_CONFIG='{"pop": null}'`

### 10.3 Pop STRATEGY_REGISTRY (`pop_screener/strategy_router.py`)

```python
STRATEGY_REGISTRY: Dict[StrategyType, object] = {
    StrategyType.VWAP_RECLAIM:           VWAPReclaimEngine(),
    StrategyType.ORB:                    ORBEngine(),
    ...
}
```

To add a strategy: create class → add enum → add 1 line to `STRATEGY_REGISTRY`.

### 10.4 Config Validation (`config.py`)

Fail-fast on startup with `ValueError` if any config is invalid:

| Validator | Parameters Checked |
|-----------|-------------------|
| `_validate_positive` | TRADE_BUDGET, MAX_POSITIONS, all per-engine budgets/positions |
| `_validate_range` | MAX_SLIPPAGE_PCT (0-10%), DEFAULT_STOP_PCT (0-50%) |
| `_validate_choice` | BROKER (alpaca/paper), DATA_SOURCE (tradier/alpaca), BROKER_MODE |
| Custom | Kill switches negative, OPTIONS_MIN_DTE < MAX_DTE, drawdown negative |

---

## 11. MIGRATION SAFETY

### 11.1 Pre-Migration Validator (`db/migrations/run.py`)

`validate_migration(filename, content)` blocks dangerous SQL:

| Pattern | Danger | Override |
|---------|--------|---------|
| `NOT NULL` without `DEFAULT` | Breaks running INSERTs | Fix SQL (add DEFAULT) |
| `DROP TABLE` | Destroys data | `-- V7_OVERRIDE:DROP TABLE` |
| `DROP COLUMN` | Destroys data | `-- V7_OVERRIDE:DROP COLUMN` |
| `RENAME COLUMN` | Breaks queries | `-- V7_OVERRIDE:RENAME COLUMN` |
| `ALTER TYPE` | Can fail on data | `-- V7_OVERRIDE:ALTER` |

Applied before each migration in `run_migrations()`. Raises `RuntimeError` on unsafe patterns.

---

## 12. TEST ARCHITECTURE

### 12.1 V7 Test Suites

| Suite | File | Checks | Scope |
|-------|------|--------|-------|
| State hardening | `test/test_safe_state.py` | 88 | fcntl locks, checksums, backups, staleness, threading, integration |
| Centralized registry | `test/test_registry_gate.py` | 46 | Acquire, dedup, release, inference, lifecycle |
| Priority enforcement | `test/test_priority_enforcement.py` | 31 | Dispatch order, convention scan, chain validation |
| P0 fixes | `test/test_p0_fixes.py` | 32 | client_order_id, dedup, manual commit, SELL verify |
| P1 fixes | `test/test_p1_fixes.py` | 30 | Timeouts, fail-closed, TTL, bounded read |
| P2 fixes | `test/test_p2_fixes.py` | 33 | Inbox queue, causation chain, sort, DB dedup |
| P3 fixes | `test/test_p3_fixes.py` | 43 | Correlation IDs, heartbeat, metrics, silence |
| P4 fixes | `test/test_p4_fixes.py` | 49 | Generic brokers, engine config, registries, validation |
| P5 fixes | `test/test_p5_fixes.py` | 35 | Migration safety, versioning, 6 chaos scenarios |
| **Total** | | **387** | |

### 12.2 Chaos Test Scenarios (P5-3)

| Scenario | What It Tests |
|----------|--------------|
| State file deletion | ALL files deleted → graceful None, fresh write recovery |
| State corruption | Truncated JSON + bad checksum → fallback to .prev → .prev2 |
| Broker API timeout | Handler sleeps 100s → times out in 0.5s (test) / 30s (prod) |
| Concurrent registry | 4 threads × 10 tickers → zero errors, no duplicates |
| Stale cache trading | Core stops writing → get_bars() returns empty (not stale data) |
| FILL replay dedup | Same event_id × 3 → only 1 delivered (EventBus idempotency) |

---

## 13. FILES MODIFIED (COMPLETE LIST)

| File | V7 Changes |
|------|-----------|
| `lifecycle/safe_state.py` | **NEW** — SafeStateFile + StalenessGuard |
| `monitor/registry_gate.py` | **NEW** — Centralized cross-layer dedup |
| `monitor/metrics.py` | **NEW** — MetricsWriter (EventBus → JSON) |
| `monitor/state.py` | Delegates to SafeStateFile |
| `monitor/distributed_registry.py` | Shared fcntl lock on all reads |
| `monitor/shared_cache.py` | fcntl locks, checksums, staleness enforcement, bounded read |
| `monitor/smart_router.py` | SafeStateFile, generic brokers=, dedup, RegistryGate |
| `monitor/portfolio_risk.py` | Priority 0→3, fail-closed buying power |
| `monitor/events.py` | `layer` field on OrderRequestPayload |
| `monitor/event_bus.py` | HANDLER_TIMEOUT_SEC, payload_version on Event |
| `monitor/brokers.py` | client_order_id, causation chain, no optimistic SELL |
| `monitor/ipc.py` | Manual commit, correlation_id in envelope |
| `monitor/risk_engine.py` | Read-only held_by, layer='vwap' |
| `monitor/position_manager.py` | Removed registry.release |
| `monitor/observability.py` | Heartbeat file, silent failure detection |
| `lifecycle/state_persistence.py` | Delegates to SafeStateFile |
| `options/chain.py` | 15s timeout on snapshot batches |
| `scripts/run_core.py` | RegistryGate, IPC inbox queue, correlation_id |
| `scripts/run_pro.py` | layer='pro', correlation_id in IPC |
| `scripts/run_pop.py` | layer='pop', correlation_id in IPC |
| `scripts/supervisor.py` | Engine config, auto-discover, hung detection |
| `pop_screener/strategy_router.py` | STRATEGY_REGISTRY at module level |
| `pro_setups/risk/risk_adapter.py` | Read-only registry, layer='pro' |
| `pop_strategy_engine.py` | Read-only registry, removed releases |
| `config.py` | Validation block, fail-fast ValueError |
| `db/migrations/run.py` | validate_migration, _DANGEROUS_PATTERNS |

---

*Generated from codebase on 2026-04-15. 25 production files + 10 test files traced. Every V7 change documented from actual code.*
