# Trading Hub — Development Notes

> Covers all architectural decisions, bugs found, and fixes applied across the development sessions for this project. Intended as a living reference for future sessions.

---

## Table of Contents

1. [Bug Fix: ZS Orphaned Positions](#1-bug-fix-zs-orphaned-positions)
2. [StateEngine Seeding](#2-stateengine-seeding)
3. [EventBus v4 → v5 (7 + 5 improvements)](#3-eventbus-v4--v5)
4. [EventBus v5 → v6 (6 CRITICAL + 4 MEDIUM fixes)](#4-eventbus-v5--v6)
5. [EventBus v6 → v6.1 (7 correctness/reliability fixes)](#5-eventbus-v6--v61)
6. [EventBus v6.1 → v5.2 — Partition correctness](#6-eventbus-v61--v52)
7. [Event Schema v1 → v2 — 8 correctness fixes](#7-event-schema-v1--v2)
8. [Multi-file Production Hardening — 11 fixes](#8-multi-file-production-hardening)
9. [Files Changed Summary](#9-files-changed-summary)
10. [Key Design Decisions](#10-key-design-decisions)

---

## 1. Bug Fix: ZS Orphaned Positions

### Problem

One day 8 ZS positions were bought but only 1 was tracked and sold. The other 7 were silently abandoned. The root cause was positions opened in a prior session (or after a restart) that existed in Alpaca but not in `bot_state.json`.

Additionally, `[HEARTBEAT]` log lines showed `positions=0, trades=0, pnl=$0` even when positions were open — the `StateEngine` snapshot was empty after restart.

### Root Causes

1. **No Alpaca reconciliation on startup.** If the bot restarted mid-session, positions already held in Alpaca were not imported into `self.positions`, so `RiskEngine` would not protect against duplicates and `PositionManager` would not manage exits.
2. **`StateEngine` not seeded from restored state.** `load_state()` restored positions into `self.positions`, but `StateEngine` started with an empty snapshot. The `[HEARTBEAT]` emitter reads from `StateEngine.snapshot()`, not from `self.positions` directly.

### Fix: Alpaca Reconciliation (`monitor/monitor.py`)

Added `_sync_broker_positions(trading_client)` called during `__init__`, before any engine is wired:

```python
def _sync_broker_positions(self, trading_client) -> None:
    if not trading_client:
        return
    try:
        alpaca_positions = trading_client.get_all_positions()
    except Exception as e:
        log.warning(f"[reconcile] Could not fetch Alpaca positions: {e}")
        return

    reconciled = 0
    for ap in alpaca_positions:
        ticker = str(ap.symbol)
        if ticker in self.positions:
            continue  # already tracked locally — trust local record
        avg_entry = float(ap.avg_entry_price or 0)
        qty = int(float(ap.qty or 0))
        if qty <= 0 or avg_entry <= 0:
            continue
        self.positions[ticker] = {
            'entry_price': avg_entry,
            'qty':         qty,
            'stop':        round(avg_entry * 0.97, 4),
            'target':      round(avg_entry * 1.05, 4),
            'entry_time':  'alpaca_restored',
            'reason':      'alpaca_reconciliation',
        }
        self._reclaimed_today.add(ticker)
        log.warning(f"[reconcile] Imported orphaned Alpaca position: {qty} {ticker} @ ${avg_entry:.2f}")
        reconciled += 1

    if reconciled:
        log.warning(f"[reconcile] Imported {reconciled} orphaned position(s). Verify stop/target — defaults are ±3%/±5%.")
    else:
        log.info("[reconcile] Local positions are in sync with Alpaca.")
```

**Call order in `__init__`:**
```
load_state()
→ create TradingClient
→ _sync_broker_positions(trading_client)    ← new
→ create EventBus + engines
→ state_engine.seed(self.positions, self.trade_log)  ← new
```

---

## 2. StateEngine Seeding

### Problem

After restart, `load_state()` correctly restores positions into `self.positions`, but `StateEngine` started fresh with an empty `_positions` dict. Until the first `POSITION` event arrived (which might not happen for hours if no new fills occur), `HeartbeatEmitter` and the Streamlit UI showed zero positions and trades.

### Fix: `seed()` method (`monitor/state_engine.py`)

```python
def seed(self, positions: dict, trade_log: list) -> None:
    with self._lock:
        self._positions = {k: copy.deepcopy(v) for k, v in positions.items()}
        self._trade_log = copy.deepcopy(trade_log)
    log.info(
        f"[StateEngine] Seeded: {len(self._positions)} open positions, "
        f"{len(self._trade_log)} completed trades"
    )
```

Called once after all engines are wired:
```python
self._state_engine = StateEngine(self._bus)
self._state_engine.seed(self.positions, self.trade_log)  # ← new
```

---

## 3. EventBus v4 → v5

### v4 improvements (7 fixes in original build)

1. **Priority enforced in async queues** — `_BoundedPriorityQueue` (heapq). `DROP_OLDEST` evicts lowest-urgency item, not FIFO-oldest.
2. **N workers per EventType** — `_PartitionedAsyncDispatcher`; `hash(ticker) % n_workers` routing (Kafka model). Per-ticker order preserved; tickers processed in parallel.
3. **Split locks** — four purpose-specific locks instead of a single `RLock`: `_sub_lock`, `_seq_lock`, `_count_lock`, `_idem_lock`. Per-handler `_HandlerState._lock` for CB fields.
4. **`emit_batch()`** — O(4) lock acquisitions for N events vs O(4N). Used in the BAR fan-out for 170 tickers.
5. **Systemic backpressure** — `_BackpressureMonitor`; 60%/80%/95% thresholds; fixed sleep on throttle.
6. **LRU stream_seqs** — `OrderedDict` capped at `max_streams=1000`; O(1) eviction.
7. **Durable write-then-deliver** — `add_before_emit_hook()` + `emit(durable=True)`. Redpanda ACKs before any handler runs.

### v5 additions (5 new features)

8. **Retry with backoff + DLQ** — `RetryPolicy` on `subscribe()`; `_deliver()` retries before counting a failure. Permanently failed events → `_dead_letters` deque + optional `dlq_handler`.
9. **Event TTL / expiry** — `event.expiry_ts` (monotonic). `_deliver()` silently skips expired events.
10. **Per-(handler, ticker) circuit breaker** — `_cb_states: Dict[(key, ticker), state]`. One bad ticker does NOT trip the circuit for all other tickers.
11. **Event coalescing** — dispatcher-level `coalesce=True` (later replaced in v6; see below).
12. **SLA tracking** — `event.deadline` field; `_deliver()` logs breaches and counts in `BusMetrics.sla_breaches`.

### v5 structural upgrades

- **Dual-heap O(log n) eviction** — `_BoundedPriorityQueue` now uses `_heap` (min) + `_evict_heap` (max via `-priority`) + `_invalid` set. Eviction is O(log n) instead of O(n).
- **Adaptive backpressure sleep** — `THROTTLE_SLEEP_SEC=0.001`, `MAX_SLEEP_SEC=0.010`; sleep scales linearly from 1ms at 80% pressure to 10ms at 100%.
- **Thread-safe=False** — `subscribe(thread_safe=False)` sets `state._call_lock = threading.Lock()`; `_deliver()` acquires it before calling the handler to serialise concurrent worker invocations.
- **Stable partition hash** — `hashlib.md5` instead of Python `hash()` (which is process-randomised; PEP 456). Ensures the same ticker always routes to the same partition across restarts.
- **`PrometheusExporter`** — optional; delta-based counters + gauges; `collect()` method.

---

## 4. EventBus v5 → v6

Six CRITICAL and four MEDIUM issues were identified and fixed.

### CRITICAL 1: Global sequence counter (fixed in user's manual edit)

**Problem:** `_next_global_seq` was module-level, shared across all `EventBus` instances. Parallel backtests (two buses running simultaneously) raced on a single counter, producing non-monotonic per-bus sequences and breaking replay ordering.

**Fix:** Remove module-level `_global_seq_lock/_global_seq_iter/_next_global_seq`. Add `self._bus_seq_lock = threading.Lock()` and `self._bus_seq_iter = itertools.count(1)` per bus instance.

### CRITICAL 2: Dispatcher-level coalescing broke causal ordering

**Problem:** `coalesce=True` on `_PartitionedAsyncDispatcher` skipped events for **all** handlers — including `StrategyEngine` which needs the complete bar sequence to compute VWAP and RSI correctly. If a bar was coalesced out, the strategy's indicators drifted.

**Fix:**
1. **Removed dispatcher-level coalescing** — removed `coalesce` param from `_PartitionedAsyncDispatcher.__init__`, removed `self._coalesce`, `self._cv`, `self._cv_lock`; reverted `put()` to 2-tuple `(event, snapshot)` items; reverted `_worker()` to 2-tuple unpack.
2. **Per-handler coalescing** — added `coalesce: bool = False` to `subscribe()`; added `_coalesce_seqs: Optional[Dict[str, int]] = None` to `_HandlerState.__slots__` and `__init__`; when `coalesce=True`, `put()` updates `state._coalesce_seqs[ticker] = max(current, event.stream_seq)` for the handler; `_deliver()` checks if `event.stream_seq < state._coalesce_seqs[ticker]` and skips with `event.coalesced = True` if stale.

**Usage:**
```python
# StrategyEngine — DO NOT use coalesce; needs every bar
bus.subscribe(EventType.BAR, strategy.on_bar)

# EventLogger — fine with coalescing; only needs latest state per ticker
bus.subscribe(EventType.BAR, logger.on_event, coalesce=True)
```

### CRITICAL 3: Per-ticker CB state leaked memory

**Problem:** `_cb_states: Dict[(handler, ticker), _HandlerState]` grew without bound. With 165+ tickers and 8+ handlers, it could accumulate thousands of entries — one per (handler, ticker) pair — that were never evicted.

**Fix:**
- Added `MAX_CB_STATES = 2_000` tuning constant.
- Changed `_cb_states` from `Dict` to `OrderedDict`.
- Updated `_get_cb_state()` to use LRU pattern: `move_to_end()` on access, `popitem(last=False)` when `len >= MAX_CB_STATES`.

### CRITICAL 4: RetryPolicy on side-effectful handlers caused duplicate orders

**Problem:** A handler registered with `RetryPolicy(max_retries=2)` on `ORDER_REQ` events would submit the same order up to 3 times if Alpaca's API returned a transient error. The bus-level retry is designed for stateless handlers only.

**Fix:**
- Added `_NO_RETRY_TYPES = frozenset({EventType.ORDER_REQ, EventType.FILL, EventType.POSITION})`.
- In `_deliver()`, before the retry loop: `if event.type in _NO_RETRY_TYPES: max_attempts = 1`.

### CRITICAL 5: TTL expiry broke idempotency (phantom stream_seq gaps)

**Problem:** When `event.expiry_ts` was set and an event expired, the TTL check in `_deliver()` caused the event to be skipped **after** stream_seq and idempotency-window slots were already consumed. The result was a gap in the stream_seq sequence — `StreamMonitor` would log false "gap" warnings on the next delivery. Replaying expired events from the idempotency window was also impossible (already consumed).

**Fix in `emit()`:** Added Step 0 before payload type check:
```python
if event.expiry_ts is not None and self._time_source.monotonic() > event.expiry_ts:
    log.debug(f"[ttl-expired] {event.type.name} dropped at emit() — already expired")
    return BackpressureStatus.SYNC if self._mode == DispatchMode.SYNC else BackpressureStatus.OK
```

**Fix in `emit_batch()`:** Added TTL pre-filter step before idempotency:
```python
_now_mono = self._time_source.monotonic()
events = [e for e in events if e.expiry_ts is None or _now_mono <= e.expiry_ts]
```

### CRITICAL 6 (same as CRITICAL 5 — resolved together)

Coalescing version counter was not partition-local (namespaced only by ticker). Fixed by namespacing the partition key as `f"{event_type.name}:{ticker}"` in `_partition()`.

### MEDIUM 1: Uneven partition capacity (fixed in user's manual edit)

The `_stable_hash` in `_partition()` was updated from `hashlib.md5` (module-level) to `zlib.crc32` (class-level override) for faster hashing. The key was namespaced by EventType: `f"{self._event_type.name}:{ticker}"` to ensure a ticker maps to the same partition index regardless of which dispatcher is calling.

### MEDIUM 2: DLQ was global, not per-handler

**Problem:** A single `deque(maxlen=1_000)` for all handlers meant high-volume `BAR` failures could evict `ORDER_REQ` failures from the DLQ before they were inspected.

**Fix:**
- Changed `self._dead_letters` from `deque(maxlen=1_000)` to `defaultdict(lambda: deque(maxlen=500))`.
- In `_deliver()`: `self._dead_letters[key.__qualname__].append(entry)`.
- Added public method:
```python
def dead_letters(self, handler_name: Optional[str] = None) -> List[DLQEntry]:
    """Return DLQ entries; None returns all handlers sorted by failed_at."""
```

### MEDIUM 3: BackpressureMonitor hid per-EventType pressure

**Problem:** `_BackpressureMonitor.check()` returned a single global pressure ratio. Operators could not tell if it was `BAR` queues (benign) or `ORDER_REQ` queues (dangerous) causing the pressure.

**Fix:**
- Added `pressure_by_type() -> Dict[str, float]` to `_BackpressureMonitor`.
- Added `pressure_by_type: Dict[str, float]` field to `BusMetrics`.
- Populated in `EventBus.metrics()`.

### MEDIUM 4: Coalescing broke CausalityTracker

**Problem:** `CausalityTracker._record()` logged every event including coalesced-out events (which had `event.coalesced = True`). Coalesced events should not appear in the causality DAG since they were never delivered.

**Fix (partial — done in user's edit):** `CausalityTracker._record()` skips events where `event.coalesced = True`.

**Fix (completed in v6):** `_deliver()` sets `event.coalesced = True` when per-handler coalescing skips delivery, so the `CausalityTracker` correctly ignores them.

---

## 5. EventBus v6 → v6.1

Seven correctness and reliability fixes identified by code review on the `tradier_platform` branch.

### CRITICAL 1: Tab/space mixing in `emit_batch` (line 1368–1371)

**Problem:** `# Step 3.5` comment used a tab character while all surrounding code used spaces. Python 3 raises `TabError` when tabs and spaces are mixed in the same block. The comment was also misplaced — inside the `for event in events` loop instead of between Step 3 and Step 3.5 (the `with self._bus_seq_lock` block). This could cause an `IndentationError` at import time, preventing the entire `monitor` package from loading.

**Fix:** Replaced tab with spaces; moved comment to the correct nesting level (outside the for loop, before `with self._bus_seq_lock:`).

### CRITICAL 2: Tab/space mixing in `_deliver` (line 1636)

**Problem:** `# Increment handler-level trip_count` comment used a tab for indentation. Same `TabError` risk as above.

**Fix:** Replaced tab with consistent space indentation (8 spaces, matching the `if state:` block below it).

### HIGH 3: `BaseException` silently kills worker threads

**Problem:** `_deliver()` catches `except Exception` around the handler call (`key(event)`). `KeyboardInterrupt` and `SystemExit` inherit from `BaseException`, not `Exception`, so they propagate through `_deliver()` and up to `_worker()` which has no catch either. Since workers are daemon threads, they die silently — no circuit-breaker trip, no DLQ entry, no log. The partition for that ticker stops processing entirely with no observability.

**Fix:** Changed `except Exception` to `except BaseException`. Added a check for `(KeyboardInterrupt, SystemExit)` that logs at CRITICAL, records the failure for circuit-breaker and DLQ accounting, then breaks out of the retry loop so the permanent-failure path runs. The worker thread still terminates (daemon threads don't re-raise), but now with proper accounting and a log trail.

### HIGH 4: `DROP_OLDEST` livelock under contention

**Problem:** In `_PartitionedAsyncDispatcher.put()`, the `DROP_OLDEST` policy retries `put_nowait()` in a `while True` loop when the queue is full, evicting the lowest-urgency item and retrying. Between `put_nowait` raising `Full` and `evict_lowest_urgency()` being called, another thread can consume an item, making `evict` return `None`. The loop then retries `put_nowait` immediately with no yield — a CPU-bound spin that can starve worker threads under high contention.

**Fix:** Added `time.sleep(0.0001)` (100µs yield) when `evict_lowest_urgency()` returns `None`, giving worker threads time to drain the queue before the next retry.

### HIGH 5: Dead `_stable_hash` method with different algorithm

**Problem:** `_PartitionedAsyncDispatcher` had a dead instance method `_stable_hash(s)` using `zlib.crc32`, but `_partition()` called the **module-level** `_stable_hash()` using `hashlib.md5`. Two different hash algorithms for the same conceptual operation is a maintenance trap — switching the call from module-level to `self._stable_hash()` would silently change partition assignments, breaking per-ticker ordering in replay and backtesting.

**Fix:** Removed the dead method entirely. Only the module-level `_stable_hash` (MD5) is used, matching the architecture docstring and the existing production behavior.

### MEDIUM 6: New `EventType` crashes ASYNC bus with bare `KeyError`

**Problem:** The `EventBus.__init__` ASYNC path iterates `for et in EventType` and indexes `merged[et]`. Adding a new `EventType` enum member without updating `_DEFAULT_ASYNC_CONFIG` causes a bare `KeyError` with no context about which member is missing or how to fix it.

**Fix:** Added pre-flight validation before creating dispatchers:
```python
missing = [et.name for et in EventType if et not in merged]
if missing:
    raise ValueError(
        f"EventBus ASYNC mode requires config for every EventType. "
        f"Missing: {missing}. Add entries to _DEFAULT_ASYNC_CONFIG or "
        f"pass async_config override."
    )
```

### LOW 7: `SimulatedTimeSource.set_time` allows backwards wall clock

**Problem:** `self._t = dt` is set unconditionally, so `now()` can return a time earlier than the previous call. `self._mono` is clamped with `max(0.0, elapsed)`, creating an inconsistency where the wall clock goes backwards but the monotonic clock doesn't. Handlers comparing `event.timestamp` values in backtests could see out-of-order events.

**Fix:** Only update `self._t` when `elapsed > 0` (same guard as the monotonic clock). A backwards `set_time` call is now a no-op for both clocks.

### Known issues (documented, not fixed)

| ID | Severity | Description |
|----|----------|-------------|
| K1 | Medium | `BackpressureStatus.OK` returned for TTL-expired and deduplicated events — callers can't distinguish "delivered" from "silently dropped" |
| K2 | Medium | `emit_batch` with `durable_fail_fast` aborts batch without rollback — events already persisted to Redpanda by earlier hooks are not compensated |
| K3 | Low | `_invalid` set in `_BoundedPriorityQueue` grows without bound when eviction pressure is absent — phantom entries from `get()` accumulate in `_evict_heap` |
| K4 | Low | `itertools.count()` in `_BoundedPriorityQueue._seq` never wraps — not a practical concern for most deployments but slows heap comparisons after billions of events |

---

## 6. EventBus v6.1 → v5.2 — Partition correctness

Two partitioning bugs identified by architecture review on `tradier_platform`.

### BUG 1: Hot partition for no-ticker events

**Problem:** No-ticker events (`HEARTBEAT`, `SYSTEM`, etc.) all hashed to the fixed key `"{EventType}:__no_ticker__"` — a constant string — so every such event always landed on the same worker partition. Under load, HEARTBEAT events piled up on one thread while other workers sat idle. This violates the core principle of Kafka-style partitioning: keyless records should distribute evenly.

**Fix:**
- Added `self._rr_counter = itertools.count()` to `_PartitionedAsyncDispatcher.__init__`.
- `_partition()` for no-ticker events now returns `next(self._rr_counter) % self._n` (round-robin) instead of hashing `__no_ticker__`.

This mirrors Kafka's own behaviour: records with a key → hash partition; records without a key → round-robin.

### BUG 2: Over-constrained partition key breaks cross-EventType causal ordering

**Problem:** The partition key was `f"{self._event_type.name}:{ticker}"`. Because each EventType has its own dispatcher, `hash("BAR:AAPL") % n` and `hash("SIGNAL:AAPL") % n` produce **different** partition indices. A causality pipeline:

```
BAR(AAPL) → worker 2  (BAR dispatcher)
SIGNAL(AAPL) → worker 0  (SIGNAL dispatcher)
ORDER_REQ(AAPL) → worker 3  (ORDER_REQ dispatcher)
```

can race: the SIGNAL handler may start before the BAR handler finishes, breaking causal ordering across event types for the same ticker.

**Fix:** Partition key changed from `f"{event_type.name}:{ticker}"` to `ticker` only. All EventType dispatchers now produce `hash("AAPL") % n` → the **same** partition index, so the entire `BAR → SIGNAL → ORDER → FILL` chain for AAPL routes to logical partition slot `k` across all dispatchers.

### Configurable flag: `causal_partitioning`

A `causal_partitioning: bool = True` parameter was added to `_PartitionedAsyncDispatcher.__init__` and wired through `async_config` per EventType:

| Mode | Key | Use case |
|------|-----|----------|
| `True` (default) | `ticker` | Causality pipelines — all AAPL events share partition index |
| `False` | `EventType:ticker` | Max parallelism — each EventType independently partitions tickers |

Configure per EventType:
```python
async_config = {
    EventType.BAR:   {'causal_partitioning': True, 'n_workers': 4},
    EventType.QUOTE: {'causal_partitioning': False, 'n_workers': 2},
}
```

ORDER_REQ/FILL/POSITION with `n_workers=1` are unaffected by the flag.

---

## 7. Event Schema v1 → v2 — 8 correctness fixes

Eight issues identified by design review of `monitor/events.py`.

### Fix 1: Payloads were mutable (`frozen=True`)

**Problem:** All payload dataclasses used plain `@dataclass`. Any handler could silently mutate a field (`event.payload.current_price = 0`), corrupting the event for all subsequent subscribers.

**Fix:** Applied `@dataclass(frozen=True)` to all 8 payload classes. Python raises `FrozenInstanceError` on any attempted field assignment after construction.

### Fix 2: DataFrame fields were a shared-mutable leak (`BarPayload`)

**Problem (original):** `frozen=True` prevents re-assigning `payload.df`, but NOT in-place value mutation: subscriber A calling `payload.df.iloc[0, 0] = 0` silently corrupts the same DataFrame that subscriber B and C also hold, because all subscribers share one `BarPayload` object. The plain `df.copy(deep=True)` from v2.0 only protected the producer's original — not the shared payload.

**Fix (v2.1):** Replaced `df.copy(deep=True)` with `_freeze_df(df)`:

```python
def _freeze_df(df: pd.DataFrame) -> pd.DataFrame:
    needs_copy = any(df[c].values.flags.writeable for c in df.columns ...)
    out = df.copy(deep=True) if needs_copy else df
    for col in out.columns:
        out[col].values.flags.writeable = False   # raises ValueError on mutation
    return out
```

Now any subscriber attempting `df.iloc[0,0] = x` on a numeric column gets `ValueError: assignment destination is read-only` at the point of mutation instead of silently corrupting downstream computations.

**Cost:** O(rows × cols) copy + O(cols) flag writes — identical allocation budget to a plain deep copy.

**Optimization path (`from_owned`):** When the data client produces a fresh DataFrame per cycle (no shared state), it can call `BarPayload.from_owned(ticker, df)`. This marks the arrays read-only in-place, transferring ownership to the payload. `_freeze_df` detects the already-read-only arrays and skips the copy entirely — O(cols) only.

### Fix 3: Callable in payload broke serialisation and hid side effects

**Problem:** `SignalPayload.refresh_ask` and `OrderRequestPayload.refresh_ask` embedded a live lambda (`lambda t=ticker: self._data.check_spread(t)[1]`) inside the event payload. Callables cannot be serialised to Redpanda/Kafka, break replay/backtesting, make payloads non-comparable, and hide side effects in what should be pure data.

**Fix:** Replaced with `needs_ask_refresh: bool = False`. The handler (broker) owns the fetch logic; the flag is the contract. `AlpacaBroker` now accepts an injectable `quote_fn: Optional[callable] = None` at construction time — the callable is wired once at startup, not per event.

**Impact on callers:**
- `strategy_engine.py`: `refresh_ask=lambda…` → `needs_ask_refresh=(self._data is not None)`
- `risk_engine.py`: `refresh_ask=p.refresh_ask` → `needs_ask_refresh=p.needs_ask_refresh`
- `brokers.py`: `AlpacaBroker(…, quote_fn=data_client.check_spread_ask)` at construction; retry uses `self._quote_fn(p.ticker)`

### Fix 4: Duplicate timestamp in HeartbeatPayload

**Problem:** `HeartbeatPayload` had its own `timestamp: datetime = field(default_factory=lambda: datetime.now(ET))`. `Event.timestamp` (set by the bus at `emit()` time) is the authoritative clock. Two competing timestamps create ambiguity in replay and make it unclear which one to trust.

**Fix:** Removed `timestamp` field from `HeartbeatPayload`. All consumers use `event.timestamp`.

### Fix 5: `position: Optional[dict]` had no schema or validation

**Problem:** `PositionPayload.position` was an untyped `dict`. Consumers accessed `position['entry_price']` with no guarantee the key existed. Adding or renaming a field in `PositionManager` silently broke downstream handlers.

**Fix:** Added `@dataclass(frozen=True) PositionSnapshot` with typed, validated fields:
```
entry_price, entry_time, quantity, partial_done, order_id,
stop_price, target_price, half_target, atr_value
```
`PositionPayload.position` is now `Optional[PositionSnapshot]`. `position_manager.py` constructs `PositionSnapshot(...)` at all three emit sites.

### Fix 6: Bare strings for action/side fields — typo-unsafe

**Problem:** `action: str`, `side: str` allowed any string to pass construction; a typo (`'byu'`, `'SELL'`) would only surface at runtime deep in a handler.

**Fix:** Added `Side`, `SignalAction`, `PositionAction` as `str, Enum` classes with `__str__` returning `.value`. `__post_init__` coerces raw strings via `Side(self.side)`, so callers may pass either `Side.BUY` or `'buy'` — both work. Downstream comparisons (`p.side == 'buy'`) continue to work due to the `str` mixin.

### Fix 7: Float validation did not reject NaN / ±inf

**Problem:** `_require_positive` and `_require_non_negative` only checked `<= 0` / `< 0`. `float('nan')` and `float('inf')` passed validation and could propagate into signal math, causing silent `nan` order quantities.

**Fix:** Added `math.isfinite(value)` guard to both helpers:
```python
if value is None or not math.isfinite(value) or value <= 0:
    raise ValueError(...)
```

### Fix 8: `open_tickers` list was mutable and weakly validated

**Problem:** `HeartbeatPayload.open_tickers: List[str]` was a mutable list. A handler could do `payload.open_tickers.append('FOO')` in-place. The validation only checked `isinstance(self.open_tickers, list)`, not that every element was a `str`.

**Fix:** `__post_init__` coerces to tuple via `object.__setattr__(self, 'open_tickers', tuple(self.open_tickers))` and validates `all(isinstance(t, str) for t in self.open_tickers)`.

---

## 8. Multi-file Production Hardening — 11 fixes

Code review of the full pipeline identified 11 bugs across 9 files.  Prioritised by financial impact.

### FIX 1: Synchronous SMTP blocked the trading loop (`alerts.py`)

**Problem:** `send_alert()` was fully synchronous — it opened an SMTP connection and waited for the server's response on the calling thread.  During a high-volatility alert storm (stop hit on multiple positions simultaneously) or a SMTP server timeout (10–30 s is common), every component that called `send_alert` — `AlpacaBroker`, `PositionManager`, `RiskEngine` — would stall.  The `ORDER_REQ` handler could miss a time-sensitive fill poll window.

**Fix:** Added a `queue.Queue(maxsize=200)` + background daemon thread (`alert-smtp`).  `send_alert()` now enqueues and returns immediately.  The daemon thread delivers emails asynchronously.  If the queue overflows (> 200 pending), the alert is dropped and logged — the trading loop is never blocked.

```python
_alert_queue: queue.Queue = queue.Queue(maxsize=200)

def send_alert(alert_email, message):
    _ensure_worker()
    try:
        _alert_queue.put_nowait((alert_email, message))
    except queue.Full:
        log.warning(f"Alert queue full — dropping: {message[:120]}")
```

Added `timeout=10` to the SMTP `SMTP(…)` constructor to bound the delivery worker's per-email wait.

---

### FIX 2: No max slippage cap on buy retry (`brokers.py`)

**Problem:** `AlpacaBroker._execute_buy()` retried with a fresh ask on every cancel, but had no upper bound on how far the ask could drift.  On a fast-moving stock (e.g. momentum breakout), three retries over 6 seconds could result in paying 1.5–2% above the original signal price — erasing the entire expected profit of a 2×ATR trade.

**Fix:** Added `MAX_SLIPPAGE_PCT = 0.005` (0.5%).  Before each retry, the fresh ask is compared to `original_ask` (captured once at the start of `_execute_buy`):

```python
original_ask = p.price
...
if new_ask > original_ask * (1 + self.MAX_SLIPPAGE_PCT):
    log.warning(f"BUY abandoned: {p.ticker} — slippage cap breached ...")
    send_alert(...)
    self._fail(p)
    return
```

0.5% is deliberately tight: the VWAP reclaim strategy targets 2×ATR profit (typically 1–2%).  Paying more than 0.5% extra means the risk/reward is already compromised before the position is open.

---

### FIX 3: Spread check failure silently bypassed the spread filter (`risk_engine.py`)

**Problem:** `_get_spread()` returned `(0.0, fallback_ask)` when `check_spread()` failed (network error, stale quote, zero bid/ask).  `spread_pct = 0.0` always passes the `> MAX_SPREAD_PCT` check, so the trade was approved based on the signal's potentially stale ask price — the exact situation the live quote check was designed to prevent.

A second problem: if the live ask was > 0.5% away from the signal ask, the entry thesis had already changed but was still approved.

**Fix:** `_get_spread()` now returns `(None, None)` on any failure.  The caller blocks the trade:

```python
spread_pct, ask_price = self._get_spread(ticker, p.ask_price)
if ask_price is None:
    self._block(ticker, p.action, "live quote unavailable — cannot verify spread", event)
    return
```

Added price-divergence guard inside `_get_spread()`:
```python
if signal_ask > 0 and abs(ask_price - signal_ask) / signal_ask > 0.005:
    log.warning(f"[RiskEngine] {ticker}: live ask ${ask_price:.2f} diverges >0.5% ...")
    return None, None
```

---

### FIX 4: Race condition on partial fills (`position_manager.py`)

**Problem:** `_on_fill()` is called from the EventBus dispatch thread.  In ASYNC mode with multiple FILL workers, two concurrent FILL events for the same ticker (e.g. two partial fills from a single order) could interleave:
- Thread A reads `pos['quantity'] = 10`, calculates `remaining = 5`
- Thread B reads `pos['quantity'] = 10` (before Thread A writes)
- Both write `pos['quantity'] = 5` — one sell is silently lost

**Fix:** Added `self._lock = threading.Lock()` to `PositionManager.__init__`.  `_on_fill()` acquires the lock for the entire open/close sequence:

```python
def _on_fill(self, event: Event) -> None:
    p: FillPayload = event.payload
    with self._lock:
        if p.side == 'buy':
            self._open_position(p, event)
        else:
            self._close_position(p, event)
```

---

### FIX 5: Run loop died silently on unhandled exception (`monitor.py`)

**Problem:** `run()` had no top-level exception handler.  Any uncaught exception in `_reset_daily_state()`, `_screener.refresh()`, `fetch_batch_bars()`, or `emit_batch()` would terminate the daemon thread with no alert.  The bot appeared running (PID lock file present) but processed no new events.

**Fix:** Extracted the loop body into `_run_loop()`.  `run()` wraps it in a try/except that fires an alert and cleans up:

```python
def run(self):
    self.running = True
    try:
        self._run_loop()
    except Exception as e:
        log.critical(f"[Monitor] Fatal error in run loop: {e}", exc_info=True)
        send_alert(self._alert_email, f"Monitor crashed — manual restart required: {e}")
        self.running = False
        _remove_lock()
```

Also stored `self._alert_email = alert_email` in `__init__` so `run()` has access to it.

---

### FIX 6: Stale pending signal after sell fill (`execution_feedback.py`)

**Problem:** `_on_fill()` cleared `_pending[ticker]` on buy fill via `pop()`, but on sell fill it only called `_evict_stale()`.  If (due to event bus async delivery) a SIGNAL(buy) was cached after an ORDER_FAIL (e.g. the SIGNAL arrived before the bus confirmed the fail), and then a subsequent sell fill arrived, the stale pending entry survived into the next trade cycle for that ticker.

**Fix:** Added explicit `_pending.pop(p.ticker, None)` on sell fill:
```python
if p.side != 'buy':
    self._pending.pop(p.ticker, None)
    return
```

---

### FIX 7: RVOL inflated before 10:15 AM — false momentum signals (`signals.py`)

**Problem:** In the daily-bars RVOL path, `time_frac` is computed as `elapsed / total_day_seconds`.  At 9:35 AM, `elapsed = 5 min` and `expected_volume ≈ avg_daily_vol × 0.008`.  A stock that traded 3% of its daily volume in 5 minutes would show `RVOL = 3.75×` — far above the 2× threshold — even if that's completely normal opening-range activity.  This generated false VWAP-reclaim buy signals in the volatile open.

**Fix:** Return `1.0` (neutral / no signal) if fewer than 45 minutes of trading have elapsed (before 10:15 AM ET):

```python
elapsed = max(0.0, (now - market_open).total_seconds())
if elapsed < 45 * 60:
    return 1.0   # too early — RVOL denominator too small
```

This aligns with the strategy's documented `TRADE_START_HOUR=9, TRADE_START_MIN=45` window.

---

### FIX 8: Replay returned events in partition order, not causal order (`event_log.py`)

**Problem:** The topic has 9 partitions (one per EventType).  Reading from all partitions with `consumer.poll()` interleaves records by partition arrival order, which is not wall-clock order.  A crash-recovery replay could see `FILL` before `ORDER_REQ` for the same trade — `CrashRecovery.rebuild()` would then attempt to close a position that hadn't been opened yet, discarding the fill.

**Fix:** Collected all records into a list, then sorted by `stream_seq` (globally monotonic counter assigned by the EventBus at `emit()` time) before yielding:

```python
def _sort_key(r):
    seq = r.get('stream_seq')
    if seq is not None:
        try:
            return (0, int(seq), '')
        except (TypeError, ValueError):
            pass
    return (1, 0, r.get('timestamp', ''))

records.sort(key=_sort_key)
```

Timestamp is the fallback for events without a `stream_seq` (e.g. HEARTBEAT).

---

### FIX 9: Corrupt state file wiped without backup (`state.py`)

**Problem:** `load_state()` caught all exceptions and returned empty state.  If `bot_state.json` was corrupted mid-write (e.g. power cut during `os.replace()`), the original corrupt file was silently discarded — impossible to inspect for recovery.

**Fix:** On `json.JSONDecodeError` or any other parse failure, the corrupt file is backed up before returning empty state:

```python
except Exception as e:
    log.error(f"State load failed: {e}")
    if os.path.exists(_STATE_FILE):
        backup = _STATE_FILE + '.corrupt'
        shutil.copy2(_STATE_FILE, backup)
        log.warning(f"Corrupt state file backed up to {backup}")
    return {}, set(), []
```

---

### FIX 10: No 429 backoff on Tradier API (`tradier_client.py`)

**Problem:** `_get()` called `resp.raise_for_status()` on every response.  Tradier's sandbox and live APIs return HTTP 429 (Too Many Requests) during high-traffic periods (e.g. the morning batch fetch for 200 tickers).  `raise_for_status()` on a 429 propagated immediately, leaving the ticker without bars for that cycle.

**Fix:** Added exponential backoff (1 s / 2 s / 4 s) for 429 responses:

```python
for attempt in range(3):
    resp = self._session.get(url, params=params, timeout=15)
    if resp.status_code == 429:
        wait = 2 ** attempt
        log.warning(f"Tradier 429 rate-limit; retrying in {wait}s")
        time.sleep(wait)
        continue
    resp.raise_for_status()
    return resp.json()
resp.raise_for_status()   # final attempt — propagate
```

Also added `f.result(timeout=30)` to `fetch_batch_bars()` so a hung thread worker doesn't hold up the entire batch indefinitely.

---

### FIX 11: No timeout on concurrent RS-filter fetches (`screener.py`)

**Problem:** `_filter_by_relative_strength()` called `f.result()` with no timeout.  A single slow or hung Tradier connection could block the screener's `ThreadPoolExecutor` indefinitely.  The screener is called every 30 minutes from `run()`, so a hung screener call would freeze the entire monitoring loop.

**Fix:** Added `f.result(timeout=30)` — a 30-second per-ticker timeout matches the `_get()` socket timeout (15 s) plus Tradier's 429 backoff budget (max 7 s).

---

## 9. Files Changed Summary

| File | Changes |
|------|---------|
| `monitor/monitor.py` | Added `_sync_broker_positions()` for Alpaca reconciliation on startup; `state_engine.seed()` call; `self._alert_email`; run loop wrapped in try/except; `_run_loop()` extracted |
| `monitor/state_engine.py` | Added `seed(positions, trade_log)` method to pre-populate snapshot on restart |
| `monitor/event_bus.py` | Major v4→v5→v6→v6.1→v5.2 evolution (see sections 3–6 above) |
| `monitor/events.py` | v1→v2: frozen payloads, DataFrame copy, callable removal, PositionSnapshot, Enums, isfinite, timestamp removal (see section 7) |
| `monitor/strategy_engine.py` | Updated SignalPayload construction: removed `refresh_ask` lambda, added `needs_ask_refresh` |
| `monitor/risk_engine.py` | `_get_spread()` now returns `(None, None)` on failure; caller blocks trade; price-divergence guard (>0.5%); `needs_ask_refresh` propagation |
| `monitor/position_manager.py` | `threading.Lock` around `_on_fill`; `PositionSnapshot(...)` replaces `dict(pos)` at all emit sites |
| `monitor/brokers.py` | `MAX_SLIPPAGE_PCT = 0.005`; `original_ask` captured; cap check before retry; `quote_fn` param; `needs_ask_refresh` |
| `monitor/alerts.py` | Async queue + background daemon thread; SMTP never blocks calling thread |
| `monitor/execution_feedback.py` | `_pending.pop(ticker)` on sell fill to clear stale cached signals |
| `monitor/signals.py` | RVOL returns 1.0 before 10:15 AM ET (daily-bars path only) |
| `monitor/event_log.py` | `replay()` collects all records then sorts by `stream_seq` before yielding |
| `monitor/state.py` | Corrupt state file backed up to `.corrupt` before returning empty state |
| `monitor/tradier_client.py` | 429 exponential backoff in `_get()`; `f.result(timeout=30)` in `fetch_batch_bars` |
| `monitor/screener.py` | `f.result(timeout=30)` in `_filter_by_relative_strength` |
| `README.md` | Full rewrite to document 9-layer event-driven architecture |

---

## 10. Key Design Decisions

### Why coalescing must be per-handler

Dispatcher-level coalescing drops an event before it reaches **any** handler. `StrategyEngine` subscribes to BAR and computes RSI, VWAP, and RVOL incrementally — it needs every bar. If bars are coalesced at the dispatcher level, the indicator state diverges from reality. Per-handler coalescing lets `EventLogger` (which only needs the latest value for display) skip stale bars without affecting `StrategyEngine`.

### Why ORDER_REQ / FILL / POSITION must not retry

The bus-level retry loop calls the handler again for the same event. For a stateless handler (e.g. a metrics aggregator), this is safe. For `AlpacaBroker.on_order_req()`, a retry submits the same order a second time — resulting in a duplicate position. Bus-level retry is categorically unsafe for side-effectful handlers; idempotency must be implemented inside the handler itself (e.g. via Alpaca's client_order_id dedup).

### Why TTL check must happen before stream_seq assignment

Stream sequence numbers are monotonic and contiguous per (EventType, ticker). If an expired event consumes a sequence number and is then silently dropped by `_deliver()`, a gap appears at the consumer. `StreamMonitor` would report a false "missed 1 event" warning on every BAR stream that had an expired event. Checking TTL before `emit()` touches `_stream_seqs` or `_seen_ids` keeps the sequence clean.

### Why per-bus sequence counter (not module-level)

Module-level `_global_seq_iter` is shared across all `EventBus` instances in the same process. In backtesting, two buses (one per strategy or one per year in year-by-year compounding) run concurrently. Events from bus A and bus B interleave in the global counter, so replaying bus A's events by sequence number would include bus B's events in the sequence. Per-bus counters (`self._bus_seq_iter`) isolate each bus's sequence space completely.

### Why `hashlib.md5` (not Python `hash()`) for stable partitioning

Python randomises `__hash__` seeds per process (PEP 456 / `PYTHONHASHSEED`). Two restarts may assign ticker AAPL to different partitions. If partition 0 processes AAPL in session 1 and partition 2 does in session 2, replaying session 1's events through session 2's dispatcher breaks per-ticker ordering. MD5 (or CRC32) produces the same integer for the same string across all restarts.

### Why durable hooks run before handlers (not after)

`DurableEventLog` registers a hook that calls `producer.produce(event); producer.flush()`. If this ran after handlers (e.g. after `PositionManager` updated `self.positions`), a crash between the handler completing and the flush completing would leave Redpanda without a record of a position that already changed in memory. Running the hook first means: if Redpanda fails, the event is never delivered to handlers and the position is never mutated — a clean failure.

### Why `BaseException` is caught (not just `Exception`)

Worker threads in `_PartitionedAsyncDispatcher._worker()` are daemon threads — if they die, the JVM (Python runtime) doesn't notice. Before v6.1, `except Exception` in `_deliver()` did not catch `KeyboardInterrupt` or `SystemExit` (both `BaseException` subclasses). A handler raising either would silently kill the worker thread for that partition: no circuit-breaker trip, no DLQ entry, no log, and the partition permanently stops processing events. Catching `BaseException` ensures the failure is accounted for (circuit-breaker + DLQ) and logged at CRITICAL before the thread exits, giving operators visibility into which partition died and why.

### Why `DROP_OLDEST` yields on empty eviction

When the queue is full and `evict_lowest_urgency()` returns `None` (another thread consumed an item between the `put_nowait` failure and the eviction attempt), the producer would immediately retry `put_nowait` in a tight loop. Under sustained high contention with many producers and few consumers, this creates a CPU-bound spin that starves the very workers the producer is waiting for. Adding a 100µs yield (`time.sleep(0.0001)`) gives workers time to drain the queue, and the next `put_nowait` succeeds.

### Default async config

| EventType | Queue size | Overflow | Workers |
|-----------|-----------|----------|---------:|
| BAR | 200 | DROP_OLDEST | 4 |
| QUOTE | 100 | DROP_OLDEST | 2 |
| SIGNAL | 50 | DROP_OLDEST | 2 |
| ORDER_REQ | 100 | BLOCK | 1 |
| FILL | 100 | BLOCK | 1 |
| ORDER_FAIL | 50 | BLOCK | 1 |
| POSITION | 50 | BLOCK | 1 |
| RISK_BLOCK | 50 | DROP_NEWEST | 1 |
| HEARTBEAT | 10 | DROP_OLDEST | 1 |

ORDER_REQ / FILL / POSITION use BLOCK (never drop) + n_workers=1 (strict global ordering). BAR uses DROP_OLDEST (stale bars are worthless) + n_workers=4 (parallel ticker processing).
