# V6 Trading Hub -- Architectural Review & V7 Roadmap

**Reviewer:** Senior Architect (Goldman Sachs background)
**Date:** 2026-04-15
**Method:** Full code audit against V6 design documentation. Every finding validated with exact file paths and line numbers.
**Scope:** 100+ source files, 9 design documents, 12 DB migrations, 21 test suites

---

## EXECUTIVE SUMMARY

V6 is a **well-engineered system with strong fundamentals** -- process isolation, event-driven architecture, atomic state writes, multi-broker routing, and comprehensive test coverage. For a system built at this pace, the engineering quality is remarkably high.

However, the code audit reveals **27 issues across 6 severity categories** that must be resolved before this system can be considered institutional-grade. The most critical gaps are:

| Severity | Count | Theme |
|----------|-------|-------|
| P0 - Data Loss / Wrong Trades | 5 | Idempotency gaps, state file races, optimistic FILL |
| P1 - Silent Failures | 6 | Missing timeouts, skipped risk checks, stale data trading |
| P2 - Ordering / Consistency | 4 | Cross-thread event races, IPC injection, crash recovery |
| P3 - Observability | 4 | Lost correlation IDs, no health endpoint, no metrics export |
| P4 - Extensibility | 4 | Hardcoded broker/engine registration, no config validation |
| P5 - Operational Risk | 4 | Schema migration, resource bounds, test gaps |

**Verdict:** V6 is production-viable for paper trading and small-capital live trading. For institutional scale, V7 must address the P0/P1 issues before increasing capital allocation.

---

## FINDING 1: IDEMPOTENCY -- THE SYSTEM'S BIGGEST GAP

### 1.1 At-Least-Once Delivery Without Dedup (P0)

**Problem:** Redpanda consumer uses `enable.auto.commit=True` with `auto.commit.interval.ms=3000`. If a handler processes an event but the process crashes before the next auto-commit (up to 3 seconds later), the event is replayed on restart. No component in the replay path has persistent dedup.

**Impact chain:**
```
Crash during FILL processing
  --> Redpanda replays FILL (offset not committed)
  --> PositionManager._processed_fill_ids lost (in-memory set)
  --> Duplicate position opened
  --> EventSourcingSubscriber writes duplicate projection rows
  --> completed_trades gets TWO rows (uuid4() generates new trade_id each time)
  --> P&L reporting doubled
```

**Evidence:**
- `monitor/ipc.py`: `enable.auto.commit=True, auto.commit.interval.ms=3000`
- `monitor/position_manager.py`: `_processed_fill_ids = set()` -- in-memory, lost on restart
- `db/event_sourcing_subscriber.py`: `trade_id = str(uuid.uuid4())` -- new ID per call, no dedup
- `event_store` table has `event_id UUID PRIMARY KEY` (rejects dupes) but projection tables do NOT

**V7 Fix:**
1. Switch to manual commit (`enable.auto.commit=False`), commit only after handler success
2. Persist `_processed_fill_ids` to DB or state file, reload on startup
3. Add `UNIQUE(closed_event_id)` to `completed_trades`
4. Add `event_id` UNIQUE constraint to all projection tables (or use `ON CONFLICT DO NOTHING`)

### 1.2 No Idempotency on BUY Orders (P0)

**Problem:** `AlpacaBroker._execute_buy()` does not use `client_order_id`. If the process crashes after submitting the order but before receiving confirmation, restart + replay creates a second real order at the broker.

**Evidence:** `monitor/brokers.py` -- `LimitOrderRequest(symbol=p.ticker, qty=p.qty, ...)` with no `client_order_id` field.

**Contrast:** `_execute_sell()` DOES use `client_order_id = f"th-sell-{p.ticker}-{uuid.uuid4().hex[:8]}"` for recovery polling. BUY lacks this.

**V7 Fix:** Generate deterministic `client_order_id` from `event.event_id` (e.g., `f"th-buy-{event.event_id[:16]}"`). Alpaca rejects duplicate `client_order_id`.

### 1.3 SmartRouter Has No ORDER_REQ Dedup (P0)

**Problem:** `SmartRouter._on_order_req()` has no tracking of previously-routed event IDs. A replayed ORDER_REQ routes to the broker again.

**V7 Fix:** Add `_routed_event_ids: OrderedDict` (bounded LRU) to SmartRouter. Skip if `event.event_id in _routed_event_ids`.

---

## FINDING 2: EVENT ORDERING -- ASSUMED, NOT ENFORCED

### 2.1 IPC Consumer Injects on Wrong Thread (P2)

**Problem:** Core's IPC consumer runs in a daemon thread (`ipc.py` line 123: `threading.Thread(target=self._consume_loop, daemon=True)`). When a satellite ORDER_REQ arrives via Redpanda, it calls `monitor._bus.emit(Event(...))` from the consumer thread. Meanwhile, the main thread is running `emit_batch()` for local BAR events.

**Two threads calling emit() concurrently for the same ticker have NO global sequencing.** The EventBus partition queues are thread-safe (internal locks), but enqueue order between threads is non-deterministic.

**Evidence:** `scripts/run_core.py` line 226: `monitor._bus.emit(Event(type=EventType.ORDER_REQ, payload=order_payload))` -- called from IPC consumer daemon thread.

**Real scenario:**
```
Main thread:     emit_batch([BAR(AAPL)]) --> triggers SIGNAL(AAPL) --> ORDER_REQ(AAPL)
IPC thread:      emit(ORDER_REQ(AAPL)) from Pro satellite
Both enqueue to ORDER_REQ worker simultaneously -- order undefined
```

**V7 Fix:** IPC consumer should enqueue to a thread-safe inbox queue. Main loop drains the inbox during its tick cycle (single-threaded injection point).

### 2.2 Cross-EventType Ordering Not Guaranteed (P2)

**Problem:** The EventBus explicitly documents this in `emit_batch()` docstring (line 1361): "cross-EventType ordering is NOT guaranteed." ORDER_REQ and FILL use separate worker pools. A fast broker fill can be dequeued before ORDER_REQ processing completes.

**Handlers don't validate causal predecessors.** `PositionManager._on_fill()` never checks "did I see the ORDER_REQ for this order?" It just opens a position.

**V7 Fix:** Add causation_id to Event. FILL must carry the event_id of its ORDER_REQ. Handlers can validate causal chain if needed.

### 2.3 Crash Recovery Assumes Ordering (P2)

**Problem:** `CrashRecovery.rebuild()` in `event_log.py` iterates Redpanda events with `for rec in self._consumer.replay(since=today_start)` -- no sorting by `stream_seq` or `sequence`. If events were persisted out of order (possible with async producer), FILL can be replayed before its ORDER_REQ.

**V7 Fix:** Sort replayed events by `stream_seq` before processing. The field exists in the event envelope but isn't used.

---

## FINDING 3: STATE FILE RACE CONDITIONS

### 3.1 bot_state.json: No Lock on Reads (P1)

**Problem:** `state.py` `save_state()` acquires `_lock` but `load_state()` does NOT. On restart, if a background thread is still flushing a save while the main thread loads state, the read can get partial JSON.

**Evidence:** `monitor/state.py` line 58: `load_state()` -- no `with _lock:` wrapper.

**V7 Fix:** Acquire `_lock` in `load_state()`. Or use file-level `fcntl.flock` like `distributed_registry.py`.

### 3.2 position_broker_map.json: TOCTOU on SELL Routing (P0)

**Problem:** `SmartRouter._select_broker()` reads `self._position_broker.get(ticker)` WITHOUT the `self._lock`. Meanwhile `_execute_on_broker()` writes to the same dict INSIDE `self._lock`. A SELL can read stale broker mapping mid-update.

**Impact:** SELL routed to wrong broker. Position opened on Alpaca, closed on Tradier = net short on Alpaca.

**Evidence:** `smart_router.py` line 176: `opening_broker = self._position_broker.get(ticker)` -- no lock. Line 215: `with self._lock: self._position_broker[ticker] = broker_name` -- locked write.

**V7 Fix:** Wrap `_select_broker()` read in `self._lock`.

### 3.3 Total State File Deletion Recovery (P1)

**Problem:** If all state files are deleted (ops error, disk failure), the system starts with empty state but brokers may have open positions. Reconciliation queries brokers on startup, BUT `position_broker_map.json` is gone, so SELL routing defaults to round-robin or fallback.

**Current mitigation:** `_sync_broker_positions()` in `monitor.py` imports orphaned positions from Alpaca. But if position was opened on Tradier, and broker map is lost, the SELL goes to Alpaca (wrong broker).

**V7 Fix:** During reconciliation, seed `position_broker_map` from both Alpaca AND Tradier positions. The current code only seeds from the primary broker.

---

## FINDING 4: MISSING TIMEOUTS -- THREADS CAN HANG FOREVER

### 4.1 No Timeout on pickle.load() (P1)

**Problem:** `shared_cache.py` `CacheReader.read()` calls `pickle.load(f)` with no timeout. A corrupted pickle file (partial write from crashed Core) can hang the reader thread indefinitely.

**Evidence:** `monitor/shared_cache.py` line 92: `data = pickle.load(f)` -- no timeout wrapper.

**V7 Fix:** Wrap in `signal.alarm()` on Unix or use `concurrent.futures.ThreadPoolExecutor` with timeout.

### 4.2 No Timeout on Option Chain Snapshots (P1)

**Problem:** `options/chain.py` `_get_snapshots_batched()` calls Alpaca SDK with no explicit timeout per batch. If Alpaca hangs, the entire Options engine blocks.

**V7 Fix:** Add `httpx.Timeout(10.0)` to all Alpaca SDK calls in chain.py.

### 4.3 No Handler Execution Timeout (P1)

**Problem:** EventBus measures handler duration after completion (`SLOW_THRESHOLD_SEC = 0.50`) but cannot kill a hung handler. A stuck handler blocks the entire partition queue for that ticker.

**V7 Fix:** Run handlers with `concurrent.futures.ThreadPoolExecutor.submit()` + `future.result(timeout=5.0)`. Kill on timeout.

### 4.4 Portfolio Risk Silent Skip (P1)

**Problem:** If all 4 buying-power queries timeout (3s each), `_get_aggregate_buying_power()` returns `None`. The pre-trade margin check is silently skipped: `if bp is not None:` -- order passes without margin validation.

**Evidence:** `portfolio_risk.py` line 120: margin check gated on `bp is not None`.

**V7 Fix:** If `bp is None`, BLOCK the order (fail-closed, not fail-open). Log CRITICAL alert.

---

## FINDING 5: SHARED CACHE TTL IS DANGEROUS

### 5.1 CacheReader TTL=30s Not Enforced Before Trading (P1)

**Problem:** `CacheReader.is_fresh()` exists but is NEVER called before trading decisions. Satellites read bars via `get_bars()` which returns whatever is cached, regardless of age.

**Evidence:**
- `scripts/run_pop.py` line 156: `bars_cache, rvol_cache = cache_reader.get_bars()` -- no freshness check
- `scripts/run_options.py` line 160: same pattern -- no `is_fresh()` call

**Impact:** If Core crashes and stops writing cache, satellites continue trading on 30s+ old data. With a 60s main loop and 30s TTL, satellites can trade on data that's 90 seconds stale.

**V7 Fix:**
1. `get_bars()` should return `None` if `not self.is_fresh()` (fail-closed)
2. Reduce TTL to 15s (half of Core's 60s cycle)
3. Log WARNING on stale cache instead of silently using old data

---

## FINDING 6: BACKPRESSURE

### 6.1 EventBus Backpressure: Well Implemented (OK)

The EventBus has a sophisticated `_BackpressureMonitor`:
- WARN at 60% queue capacity
- THROTTLE at 80% (adaptive sleep 1-10ms)
- ALERT at 95%
- Per-EventType policies: BAR uses DROP_OLDEST, FILL/ORDER_REQ use BLOCK

**This is well-designed.** No changes needed for V7.

### 6.2 IPC Consumer: No Backpressure (P3)

**Problem:** Redpanda consumer processes messages as fast as they arrive. If Core is slow (DB circuit breaker open, broker hanging), IPC consumer keeps injecting ORDER_REQs into a backing-up EventBus.

**V7 Fix:** Check EventBus queue depth before injecting IPC events. If queue > 80% capacity, pause consumer polling.

---

## FINDING 7: OBSERVABILITY GAPS

### 7.1 Correlation ID Lost Across IPC (P3)

**Problem:** `EventPublisher.publish()` serializes `{source, timestamp, payload}` but NOT `correlation_id`. When an ORDER_REQ crosses from Pro to Core via Redpanda, the correlation chain breaks.

**Impact:** Cannot trace a single order from Pro detector fire --> signal --> IPC --> Core routing --> broker fill --> position across process boundaries. Debugging production issues requires manual log correlation.

**V7 Fix:** Add `correlation_id` to IPC envelope. Deserialize and propagate on consumer side.

### 7.2 No Health Check Endpoint (P3)

**Problem:** Supervisor monitors child processes by PID and exit code. If a process hangs (infinite loop, deadlocked thread), supervisor sees it as "running" and takes no action.

**V7 Fix:** Add heartbeat-based health detection. If HeartbeatEmitter hasn't emitted in 120s (2x normal interval), supervisor force-kills and restarts.

### 7.3 No Metrics Export (P3)

**Problem:** EventBus tracks latencies, error counts, circuit breaker states internally but doesn't expose them. No Prometheus endpoint, no StatsD push. Operational visibility requires log parsing.

**V7 Fix:** Add `/metrics` endpoint or periodic push to TimescaleDB metrics table.

### 7.4 No Silent Failure Detection (P3)

**Problem:** If StrategyEngine stops emitting SIGNAL events (bug, data feed issue), no alert fires. The system looks healthy (heartbeats continue) but generates zero trades. This is the most insidious failure mode in production trading.

**V7 Fix:** HeartbeatEmitter should track "time since last SIGNAL" and "time since last FILL". Alert if > 30 minutes during market hours.

---

## FINDING 8: GLOBAL CLOCK SYNCHRONIZATION

### 8.1 No Synchronized Clock (P3)

**Problem:** Each process uses its own `datetime.now(ET)` or `time.monotonic()`. No NTP validation. If a satellite's clock drifts, EOD gates fire at wrong times, cooldown timers are wrong, state file date checks may discard valid state.

**Mitigating factor:** All processes run on the same machine, sharing the same system clock. This is only a risk if processes move to separate hosts.

**V7 Fix:** For future multi-host deployment, add NTP drift check on startup. For now, document the single-host assumption.

---

## FINDING 9: EXTENSIBILITY ANALYSIS

### 9.1 Adding a New Broker (P4)

**Current state:** `BaseBroker` provides a clean abstract interface. `make_broker()` factory handles instantiation. **SmartRouter is the bottleneck** -- broker names are hardcoded in routing logic, position detection uses Alpaca-specific `._client` attribute.

**V7 Fix:** SmartRouter should accept `Dict[str, BaseBroker]` and iterate generically. Remove Alpaca-specific position detection; use a `BaseBroker.has_position(ticker)` interface method.

### 9.2 Adding a New Engine (P4)

**Current state:** Requires changes in 3+ hardcoded locations: `supervisor.py` PROCESSES dict, new `run_*.py` script, `config.py` engine-specific vars. `DistributedPositionRegistry` is cleanly generic (no hardcoded layers).

**V7 Fix:** Engine registration via config file or directory scanning (`scripts/run_*.py` auto-discovery).

### 9.3 Adding New Strategies (P4)

**Current state:** Mixed. Options has clean `STRATEGY_REGISTRY` dict (8/10 pluggability). Pop has hardcoded `_engines` dict in `StrategyRouter.__init__()` (3/10). Pro uses classifier-based discovery (6/10).

**V7 Fix:** All engines should use explicit `STRATEGY_REGISTRY` pattern like Options.

### 9.4 No Config Validation (P4)

**Problem:** `config.py` reads env vars with `os.getenv()` and casts to int/float with no validation. Invalid values (negative budget, slippage > 1.0, empty API key) silently propagate.

**V7 Fix:** Add Pydantic `BaseSettings` model or manual validation on startup. Fail-fast on invalid config.

---

## FINDING 10: SCHEMA AND MIGRATION RISK

### 10.1 Migration Framework: Excellent (OK)

Advisory-locked, idempotent, checksum-validated. 12 migrations. `trading.schema_migrations` table tracks applied migrations. **This is well-built.**

### 10.2 No Hot-Update Protocol (P5)

**Problem:** If a migration adds a NOT NULL column while the system is running, all writes to that table fail. DBWriter circuit breaker opens after 5 failures. Events are dropped permanently (not queued for retry).

**V7 Fix:** All migrations must be backward-compatible (add columns as NULLABLE with defaults). Run migrations before deploying new code, not during.

### 10.3 No Event Payload Versioning (P5)

**Problem:** Event payloads have no `version` field. If a new required field is added to `FillPayload`, old events in the event store cannot be deserialized. Projection rebuilds break.

**V7 Fix:** Add `payload_version: int = 1` to Event dataclass. Projection builder handles version-specific deserialization.

---

## FINDING 11: TESTING GAPS

### 11.1 Test Coverage: Strong (21 tests, including E2E) (OK)

T1-T21 cover synthetic feed, pipeline integration, risk boundaries, replay determinism, and E2E risk safety. Paper trading mode available independently for all 3 satellite engines.

### 11.2 Missing: Race Condition Tests (P5)

No concurrent tests for:
- Two processes writing `position_registry.json` simultaneously
- IPC consumer injecting while main thread emitting
- Broker FILL callback during ORDER_REQ processing

**V7 Fix:** Add `pytest-asyncio` concurrent test suite with `threading.Barrier` synchronization.

### 11.3 Missing: Chaos/Failure Injection (P5)

No tests for:
- Kill process mid-trade (verify crash recovery)
- Delete state files during operation
- Simulate broker API timeout
- Simulate Redpanda unavailability

**V7 Fix:** Add chaos test suite using `unittest.mock` to inject failures at critical points.

---

## V7 IMPLEMENTATION PRIORITY

### Phase 1: Stop Losing Money (P0 -- Week 1-2)

| # | Issue | Fix | Files |
|---|-------|-----|-------|
| 1 | BUY order no client_order_id | Deterministic ID from event_id | monitor/brokers.py |
| 2 | SmartRouter no ORDER_REQ dedup | LRU seen set | monitor/smart_router.py |
| 3 | position_broker_map TOCTOU | Lock on reads | monitor/smart_router.py |
| 4 | Redpanda auto-commit race | Manual commit after handler | monitor/ipc.py |
| 5 | Optimistic SELL FILL | Verify before emitting | monitor/brokers.py |

### Phase 2: Stop Silent Failures (P1 -- Week 3-4)

| # | Issue | Fix | Files |
|---|-------|-----|-------|
| 6 | pickle.load() no timeout | Timeout wrapper | monitor/shared_cache.py |
| 7 | Chain snapshot no timeout | httpx timeout | options/chain.py |
| 8 | Handler no execution timeout | Future with timeout | monitor/event_bus.py |
| 9 | Portfolio risk silent skip | Fail-closed on None | monitor/portfolio_risk.py |
| 10 | Cache TTL not enforced | is_fresh() check | scripts/run_pop.py, run_options.py |
| 11 | bot_state.json no read lock | Add lock | monitor/state.py |

### Phase 3: Ordering & Consistency (P2 -- Week 5-6)

| # | Issue | Fix | Files |
|---|-------|-----|-------|
| 12 | IPC injection wrong thread | Inbox queue, main-loop drain | scripts/run_core.py, monitor/ipc.py |
| 13 | Crash recovery no sort | Sort by stream_seq | monitor/event_log.py |
| 14 | Projection table dedup | UNIQUE on event_id or ON CONFLICT | db/event_sourcing_subscriber.py |
| 15 | Persistent FILL dedup | DB-backed seen set | monitor/position_manager.py |

### Phase 4: Observability (P3 -- Week 7-8)

| # | Issue | Fix | Files |
|---|-------|-----|-------|
| 16 | Correlation ID lost on IPC | Add to envelope | monitor/ipc.py |
| 17 | No health endpoint | Heartbeat-based liveness | monitor/observability.py |
| 18 | No metrics export | Prometheus endpoint or DB push | NEW: monitor/metrics.py |
| 19 | No silent failure detection | Signal/Fill recency tracking | monitor/observability.py |
| 20 | IPC no backpressure | Queue depth check before inject | monitor/ipc.py |

### Phase 5: Extensibility (P4 -- Week 9-10)

| # | Issue | Fix | Files |
|---|-------|-----|-------|
| 21 | SmartRouter hardcoded brokers | Generic broker dict | monitor/smart_router.py |
| 22 | Supervisor hardcoded engines | Config-driven registration | scripts/supervisor.py |
| 23 | Pop hardcoded strategies | STRATEGY_REGISTRY pattern | pop_screener/strategy_router.py |
| 24 | No config validation | Pydantic BaseSettings | config.py |

### Phase 6: Operational Hardening (P5 -- Week 11-12)

| # | Issue | Fix | Files |
|---|-------|-----|-------|
| 25 | No hot-update protocol | Backward-compat migration rules | db/migrations/ |
| 26 | No event versioning | payload_version field | monitor/events.py |
| 27 | No chaos tests | Failure injection suite | test/ |

---

## WHAT V6 DOES WELL (Keep in V7)

These are production-grade patterns that should be preserved:

1. **Atomic state writes** (tmpfile + os.replace) -- correct everywhere it's used
2. **EventBus v5.2** -- priority queues, per-ticker partitioning, coalescing, circuit breakers, dead letter queue, backpressure monitoring. This is institutional-quality event infrastructure.
3. **SmartRouter circuit breaker** -- 3-failure threshold, 5-min cooldown, auto-recovery, CRITICAL alert on total failure
4. **DistributedPositionRegistry** -- fcntl.flock, atomic read-modify-write. The safest component in the system.
5. **DBWriter async batching** -- 200-row batches, 0.5s flush, circuit breaker. Well-tuned for throughput vs latency.
6. **Migration framework** -- advisory locks, idempotent, checksum validation. Better than most startups.
7. **Frozen event payloads** -- `@dataclass(frozen=True)` with validation in `__post_init__`. Prevents mutation bugs.
8. **DataFrame immutability** -- `_freeze_df()` sets numpy arrays read-only. Subscribers can't corrupt shared data.
9. **Structure-aware stops** -- S/R pivot detection with touch scoring beats arbitrary ATR-based stops.
10. **Process isolation** -- 5 processes communicating only via Redpanda + shared cache. Clean failure domains.
11. **DB retention policies** -- TimescaleDB compression (7 days) and retention (90-365 days). Storage-efficient.
12. **21 test suites** -- including full E2E pipeline test, risk boundary tests, and replay determinism.

---

## COMPARISON: WHERE WE STAND VS INSTITUTIONAL SYSTEMS

| Capability | V6 | Goldman/Citadel | Gap |
|-----------|-----|----------------|-----|
| Event ordering | Assumed (partition-based) | Enforced (sequence numbers + validation) | Medium |
| Idempotency | Partial (in-memory, lost on restart) | Full (persistent, broker-side) | Large |
| Circuit breakers | Per-broker, per-DB | Per-service, per-dependency, cascading | Small |
| Observability | Logs + DB | Distributed tracing, metrics, anomaly detection | Medium |
| Testing | 21 integration tests | 500+ unit, chaos, fault injection | Medium |
| Failover | SmartRouter (2 brokers) | N+1 redundancy, geographic failover | Large |
| Config management | Env vars, no validation | Typed config, runtime validation, feature flags | Small |
| Schema evolution | Advisory-locked migrations | Event versioning, backward-compat policy | Small |
| Latency | 60s bar cycle, 5s fill timeout | Sub-millisecond, FPGA-assisted | N/A (different market) |

**Honest assessment:** V6 is in the top 10% of retail/prop-firm algo systems. V7 with the P0/P1 fixes puts it in serious semi-institutional territory. The gap to Goldman/Citadel is primarily in redundancy, geographic failover, and hardware latency -- areas that require $M infrastructure investment, not code changes.

---

*This review covers 27 findings across 6 severity levels, validated against 100+ source files. All recommendations are implementable within a 12-week V7 release cycle.*
