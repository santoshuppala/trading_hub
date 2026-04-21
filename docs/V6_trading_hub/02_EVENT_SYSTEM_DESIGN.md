# 02 -- Event System Design

Source of truth: `monitor/event_bus.py`, `monitor/events.py`, `monitor/ipc.py`,
`monitor/shared_cache.py`, `monitor/event_log.py`.

---

## Purpose

The event system is the central nervous system of the trading hub. Every state
change -- a new bar arriving, a strategy firing a signal, an order being placed,
a fill confirmed by the broker -- flows through a single `EventBus` instance as
a typed, immutable `Event`. The system provides:

1. **In-process pub/sub** with priority dispatch, per-ticker ordering, and
   backpressure management (EventBus).
2. **Typed, validated payloads** that enforce invariants at construction time
   and prevent mutation after emission (events.py).
3. **Inter-process communication** via Redpanda for satellite processes that
   run in separate OS processes (ipc.py).
4. **Shared market data** via an atomic pickle file for read-heavy, low-latency
   data sharing between processes (shared_cache.py).
5. **Durable event logging** to Redpanda for crash recovery and audit
   (event_log.py).

---

## Design Principles

| Principle | How it is enforced |
|---|---|
| **Immutability** | All payloads are `@dataclass(frozen=True)`. DataFrames get deep-copied and numpy arrays marked read-only via `_freeze_df()`. |
| **Validate at the edge** | Every payload runs `__post_init__` validation. Invalid data raises `ValueError` before it touches the bus. |
| **Per-ticker causal ordering** | MD5-based stable hash routes all events for the same ticker to the same worker partition, across all EventTypes. |
| **Never drop money events** | ORDER_REQ and FILL use `BackpressurePolicy.BLOCK` with 1 worker. Durable hook writes to Redpanda synchronously before any handler runs. |
| **Fail loud, degrade gracefully** | Circuit breakers isolate bad (handler, ticker) pairs. Dead-letter queue captures permanently failed events. Backpressure monitor alerts at 95%. |
| **Lock hierarchy prevents deadlocks** | Four purpose-specific locks instead of one global RLock. Each has a clear scope and acquisition order. |

---

## 1. EventBus (`monitor/event_bus.py`)

Version: v5.2. Central pub/sub backbone.

### 1.1 EventType Enum

14 event types defined via `auto()`:

| EventType | Priority | Workers | Queue | Policy | Coalesce |
|---|---|---|---|---|---|
| BAR | LOW (3) | 8 | 500 | DROP_OLDEST | Yes |
| QUOTE | LOW (3) | 2 | 100 | DROP_OLDEST | Yes |
| SIGNAL | MEDIUM (2) | 2 | 50 | DROP_OLDEST | No |
| ORDER_REQ | HIGH (1) | 1 | 100 | BLOCK | No |
| FILL | CRITICAL (0) | 1 | 100 | BLOCK | No |
| ORDER_FAIL | CRITICAL (0) | 1 | 50 | BLOCK | No |
| POSITION | HIGH (1) | 1 | 50 | BLOCK | No |
| RISK_BLOCK | MEDIUM (2) | 1 | 50 | DROP_NEWEST | No |
| HEARTBEAT | LOWEST (4) | 1 | 10 | DROP_OLDEST | No |
| POP_SIGNAL | MEDIUM (2) | 2 | 50 | DROP_OLDEST | No |
| PRO_STRATEGY_SIGNAL | MEDIUM (2) | 2 | 100 | DROP_OLDEST | No |
| OPTIONS_SIGNAL | MEDIUM (2) | 1 | 50 | DROP_OLDEST | No |
| NEWS_DATA | -- | -- | -- | -- | No |
| SOCIAL_DATA | -- | -- | -- | -- | No |

### 1.2 Priority Queue

`_BoundedPriorityQueue` -- thread-safe, bounded, dual-heap priority queue.

- **Min-heap** for dequeue (lowest integer = highest urgency first).
- **Max-heap** for eviction (DROP_OLDEST evicts the lowest-urgency item, not
  the FIFO-oldest).
- **Lazy deletion** via a shared `_invalid` set of sequence numbers. Consumed
  or evicted entries are marked invalid and skipped by the other heap on its
  next pop. Both heaps clean up phantoms as they encounter them.
- Capacity enforced on valid-item count (`_count`), not heap length.
- `threading.Condition` for blocking put/get with timeout.

### 1.3 Async Dispatch

`_PartitionedAsyncDispatcher` creates N worker threads per EventType (see table
above). Partition assignment:

- **Ticker-based**: `_stable_hash(ticker) % n_workers` using MD5 for
  deterministic, cross-restart-stable hashing (Python's `hash()` is randomized
  per PEP 456).
- **Cross-EventType causal ordering**: partition key is ticker only (not
  EventType:ticker). All dispatchers map the same ticker to the same partition
  index, so pipelines like `BAR(AAPL) -> SIGNAL(AAPL) -> ORDER_REQ(AAPL) ->
  FILL(AAPL)` cannot race across workers.
- **No-ticker round-robin**: Events without a ticker (HEARTBEAT, system events)
  distribute via round-robin instead of hashing to a fixed key, preventing hot
  partitions.

**Event coalescing** (BAR, QUOTE): when `coalesce=True`, only the latest event
per ticker is delivered. Stale queued events are skipped via a lazy version
check in the worker. This is a major throughput optimization for 170+ ticker
bots.

### 1.4 Locking Strategy

Four purpose-specific locks prevent deadlock through clear hierarchy:

| Lock | Type | Protects |
|---|---|---|
| `_sub_lock` | `RLock` | Subscriber registry (subscribe/unsubscribe) |
| `_seq_lock` | `Lock` | `_stream_seqs` OrderedDict (per-ticker sequence numbers) |
| `_count_lock` | `Lock` | Global event counters and metrics |
| `_idem_lock` | `Lock` | Idempotency dedup window (seen event IDs) |

Each `_HandlerState` has its own `_lock` so circuit-breaker state updates do not
contend with `subscribe()`.

### 1.5 Backpressure

`_BackpressureMonitor` aggregates depth/capacity across all queues. `emit()`
samples it before enqueueing:

| Threshold | Status | Action |
|---|---|---|
| < 60% | `OK` | No action |
| >= 60% | `WARN` | Warning logged |
| >= 80% | `THROTTLED` | Adaptive micro-sleep (1--10ms) to give workers headroom |
| >= 95% | `CRITICAL` | Error logged + `alert_fn` callback invoked |

`emit()` / `emit_batch()` return a `BackpressureStatus` enum so callers can
react without polling `metrics()`.

### 1.6 Circuit Breaker

Per-(handler, ticker) granularity. One bad ticker (e.g., corrupt data causing a
handler to raise on ZS) does not trip the circuit for all other tickers served
by the same handler.

| Constant | Value |
|---|---|
| `CIRCUIT_BREAKER_THRESHOLD` | 5 consecutive failures |
| `CIRCUIT_BREAKER_COOLDOWN` | 60 seconds |

States: CLOSED (normal) -> OPEN (after 5 failures) -> HALF_OPEN (after 60s
cooldown, retry one event) -> CLOSED (on success) or back to OPEN (on failure).

### 1.7 Retry and Dead Letter Queue

`RetryPolicy` dataclass configurable per handler at `subscribe()` time:

- `max_retries`: additional attempts after first failure (default: 0).
- `backoff_ms`: sleep durations between attempts (default: `[10, 50, 200]`).
  Last value repeats if the list is shorter than `max_retries`.

**Warning**: order execution handlers (AlpacaBroker, PositionManager) should use
`max_retries=0` or implement internal idempotency. Bus-level retry is safe only
for stateless handlers.

Dead letter queue: `deque(maxlen=1000)` of `DLQEntry` objects. Optional
`dlq_handler` callback invoked on each permanently failed event.

### 1.8 Batch Emit

`emit_batch()` emits a list of N events with O(4) lock acquisitions instead of
O(4N). Designed for the BAR fan-out loop (170 tickers x 4 locks each -> 4 locks
total).

### 1.9 Stream Sequences

Per-(EventType, ticker) monotonic sequence numbers for ordering and gap
detection. `_stream_seqs` is an `OrderedDict` with LRU eviction at 1000 keys
to prevent unbounded growth.

### 1.10 Durable Write-Then-Deliver

`add_before_emit_hook()` + `emit(durable=True)`. Hooks run synchronously before
any handler. `DurableEventLog` registers a produce+flush hook so Redpanda acks
the event before in-process state changes begin. If the hook fails and
`durable_fail_fast=True` (the default for ORDER_REQ/FILL), the event is dropped
rather than delivered without persistence.

### 1.11 Time Source

Injected clock abstraction for backtesting:

- `WallClockTimeSource`: production default (`datetime.now(ET)`,
  `time.monotonic()`).
- `SimulatedTimeSource`: test/replay clock with `set_time(dt)` and
  `advance(seconds)`. Thread-safe via internal lock.

### 1.12 Key Constants

```python
SLOW_THRESHOLD_SEC        = 0.50   # handler latency warning (500ms)
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_COOLDOWN  = 60.0   # seconds
LATENCY_WINDOW            = 100    # rolling window for P95 latency
```

---

## 2. Events (`monitor/events.py`)

Version: v2. Single source of truth for every event payload.

### 2.1 Design Choices

- **`@dataclass(frozen=True)`** on all payloads -- immutable after construction.
- **`__post_init__` validation** -- invalid payloads raise `ValueError` before
  they reach any subscriber. Guards use `math.isfinite()` to reject NaN/inf.
- **No callables in payloads** -- callables break serialization (Redpanda,
  DurableEventLog) and hide side effects. Replaced with boolean flags (e.g.,
  `needs_ask_refresh: bool`).
- **String enums** with `__str__` returning `.value` so f-strings stay clean.
  Coercion in `__post_init__` means callers may pass raw strings.

### 2.2 Enums

**Side**: `BUY`, `SELL`

**SignalAction**: `BUY`, `SELL_STOP`, `SELL_TARGET`, `SELL_RSI`, `SELL_VWAP`,
`PARTIAL_SELL`, `PARTIAL_DONE`, `HOLD`. Has `_missing_` classmethod for
case-insensitive lookup.

**PositionAction**: `OPENED`, `PARTIAL_EXIT`, `CLOSED`

### 2.3 Validation Helpers

```python
_require_ticker(ticker)        # non-empty string
_require_positive(name, value) # finite float > 0
_require_non_negative(name, v) # finite float >= 0
```

All three reject `None`, `NaN`, and `+-inf`.

### 2.4 DataFrame Immutability

`_freeze_df(df)` provides two-step protection:

1. **Deep copy** -- isolates the payload from the producer's buffer. Skipped if
   all numpy column arrays are already read-only (ownership transfer via
   `BarPayload.from_owned()`).
2. **Read-only numpy flags** -- `values.flags.writeable = False` on every
   numeric column. Any subscriber that attempts in-place mutation gets an
   immediate `ValueError` at the mutation site.

Performance: one deep copy + O(n_cols) flag writes per emit, not per subscriber.
For 390-row x 6-col OHLCV bars across 170 tickers: ~11 MB/min allocation.
`BarPayload.from_owned()` skips the copy for O(n_cols) cost only when the caller
transfers ownership.

### 2.5 Payload Types

| Payload | Key Fields | Producer | Consumer |
|---|---|---|---|
| `BarPayload` | ticker, df, rvol_df | Market Data | Strategy, State, PositionMgr |
| `QuotePayload` | ticker, bid, ask, spread_pct | Risk Engine | Strategy, Observability |
| `SignalPayload` | ticker, action, current_price, stop/target/half_target, rsi, rvol | Strategy Engine | Risk Engine, PositionMgr |
| `OrderRequestPayload` | ticker, side, qty, limit_price, order_type, reason | Risk Engine | AlpacaBroker |
| `FillPayload` | ticker, side, qty, fill_price, order_id, reason | AlpacaBroker | PositionMgr, EventLog |
| `OrderFailPayload` | ticker, reason, side, qty | AlpacaBroker | Risk Engine |
| `PositionPayload` | ticker, action, snapshot (PositionSnapshot) | PositionMgr | State Engine, Observability |
| `PositionSnapshot` | entry_price, entry_time, quantity, partial_done, order_id, stop/target/half_target, atr_value | -- | -- |
| `RiskBlockPayload` | ticker, reason | Risk Engine | Observability |
| `HeartbeatPayload` | open_tickers (tuple), uptime_sec | Monitor | Observability |
| `PopSignalPayload` | ticker, strategy-specific fields | PopStrategyEngine | RiskAdapter |
| `ProStrategySignalPayload` | ticker, setup type, detector output | ProSetupEngine | ProStrategyRouter |
| `OptionsSignalPayload` | ticker, strategy, option chain data | OptionsEngine | Options execution |
| `NewsDataPayload` | ticker, news data | PopStrategyEngine | EventSourcing (DB only) |
| `SocialDataPayload` | ticker, social data | PopStrategyEngine | EventSourcing (DB only) |

---

## 3. IPC (`monitor/ipc.py`)

Inter-process communication via Redpanda (Kafka-compatible) for process-isolated
engines (Options, Pop, Pro) that run in separate OS processes.

### 3.1 Topics

| Constant | Topic Name | Direction | Content |
|---|---|---|---|
| `TOPIC_ORDERS` | `th-orders` | Satellites -> Core | ORDER_REQ |
| `TOPIC_FILLS` | `th-fills` | Core -> Satellites | FILL, POSITION |
| `TOPIC_SIGNALS` | `th-signals` | Core VWAP -> Options | SIGNAL events |
| `TOPIC_POP` | `th-pop-signals` | Pop -> Options | POP_SIGNAL |
| `TOPIC_BAR_READY` | `th-bar-ready` | Core -> All | New bars notification |
| `TOPIC_REGISTRY` | `th-registry` | All | Position registry acquire/release |

Broker address: `REDPANDA_BROKERS` env var, default `127.0.0.1:9092`.

### 3.2 EventPublisher

Async producer wrapping `confluent_kafka.Producer`.

| Setting | Value | Rationale |
|---|---|---|
| `acks` | 1 | Leader ack only (speed over durability for IPC) |
| `retries` | 5 | Transient failure tolerance |
| `retry.backoff.ms` | 500 | -- |
| `linger.ms` | 20 | Batch for 20ms to reduce per-message overhead |
| `compression.type` | snappy | Fast compression, modest size reduction |
| `queue.buffering.max.messages` | 10,000 | In-memory buffer before backpressure |

- **Background flush thread**: `_bg_flush()` calls `producer.poll(0.25)` in a
  daemon loop, triggering delivery callbacks every 250ms.
- **Message envelope**: `{source, timestamp (UTC ISO), payload}`.
- `publish()`: non-blocking. `publish_sync()`: blocks until ack or timeout.

### 3.3 EventConsumer

Wraps `confluent_kafka.Consumer` with callback dispatch.

| Setting | Value |
|---|---|
| `auto.offset.reset` | latest |
| `auto.commit.interval.ms` | 3000 |
| `session.timeout.ms` | 15,000 |
| `max.poll.interval.ms` | 60,000 |

- `on(topic, handler)`: register a handler per topic.
- `start()`: spawns a daemon thread that polls and dispatches.
- Handlers receive `(key: str, payload: dict)`.

---

## 4. SharedCache (`monitor/shared_cache.py`)

File-based shared market data for satellite processes. Designed for the
core-writes-many / satellites-read-many pattern where Redpanda per-bar messaging
would be excessive overhead.

### 4.1 File Format

Path: `data/live_cache.pkl`

```python
{
    'timestamp': float,           # monotonic clock at write time
    'updated_at': str,            # UTC ISO datetime
    'bars':  {ticker: df.to_dict('list')},
    'rvol':  {ticker: df.to_dict('list')},
}
```

### 4.2 CacheWriter (Core Process Only)

- Converts DataFrames to `dict('list')` format for pickle serialization.
- **Atomic write**: `tempfile.mkstemp()` in the same directory, then
  `os.replace(tmp_path, cache_path)`. `os.replace()` is atomic on POSIX --
  readers never see a partial file.
- Uses `pickle.HIGHEST_PROTOCOL` for speed.
- On write failure: temp file is cleaned up, warning logged, no crash.

### 4.3 CacheReader (Satellite Processes)

- **mtime-based freshness**: `os.path.getmtime()` checked before reading. If
  the file has not been modified since last read, returns cached data without
  disk I/O.
- **max_age**: configurable staleness threshold (default 30 seconds). Callers
  use this to decide whether data is fresh enough for trading decisions.
- **Error resilience**: on read failure (corrupt file, race condition), falls
  back to previously cached data instead of returning None.
- `get_bars()`: convenience method returning `(bars_cache, rvol_cache)` as
  DataFrames.

---

## 5. DurableEventLog (`monitor/event_log.py`)

Crash-safe persistence layer. Without it, the bus is volatile -- a crash between
FILL and the PositionManager's POSITION event leaves the system in an unknown
state.

### 5.1 Design Choices

- **Selective persistence**: subscribes to ALL EventTypes but only writes
  CRITICAL events (ORDER_REQ, FILL, POSITION, ORDER_FAIL) to Redpanda. BAR,
  SIGNAL, HEARTBEAT, etc. are persisted in TimescaleDB only. This reduces
  Redpanda write load by approximately 60%.
- **Dual write path**: async `_on_event` for non-critical events (subscribed
  handler), synchronous `_durable_produce` for money events (before-emit hook).
- **Durable hook for money events**: `register_durable_hook()` installs a
  synchronous before-emit hook on the EventBus. For ORDER_REQ and FILL,
  Redpanda acks the event BEFORE any in-process handler runs. This eliminates
  the async-write/handler-race window.

### 5.2 Producer Configuration

| Setting | Value | Rationale |
|---|---|---|
| `acks` | 1 | Leader ack (TimescaleDB is second durability layer) |
| `retries` | 10 | Higher than IPC -- data loss is unacceptable |
| `retry.backoff.ms` | 1000 | 1s backoff |
| `linger.ms` | 50 | Batch for 50ms, reduces per-event latency |
| `batch.size` | 102,400 | 100KB batches |
| `compression.type` | snappy | 50-70% payload reduction |
| `queue.buffering.max.messages` | 50,000 | Large buffer for burst absorption |

Background flush thread: drains producer queue every 250ms (`FLUSH_INTERVAL`).

### 5.3 Durable Produce Timeouts

Synchronous flush timeouts vary by event criticality:

| Event | Timeout | Rationale |
|---|---|---|
| FILL | 10s | Money changed hands, must persist |
| ORDER_REQ | 5s | About to execute |
| Other | 3s | Default |

On flush failure: error logged, exception re-raised so EventBus can log it as
a hook failure. With `durable_fail_fast=True`, the event is dropped rather than
delivered without persistence.

### 5.4 Topic Design

| Property | Value |
|---|---|
| Name | `trading-hub-events` |
| Partitions | 9 (one per EventType, with headroom) |
| Key | ticker string (per-ticker ordering within partition) |
| Retention | Redpanda default (configurable via rpk) |

### 5.5 Serialization Contract

JSON envelope fields: `event_id`, `correlation_id`, `sequence`, `stream_seq`,
`timestamp` (ISO-8601), `event_type` (EventType.name), `payload_type` (class
name), `payload` (dict).

Kafka message headers: `event_id`, `correlation_id`, `event_type`, `stream_seq`,
and `durable` (for hook-produced messages).

Payload field serialization rules:

| Type | Serialized as |
|---|---|
| Callable | Omitted entirely |
| `pd.DataFrame` | `{__dataframe__: true, shape, columns, last_row}` |
| `pd.Series` | `{__series__: true, length}` |
| `datetime` | ISO-8601 string |
| `set` | Sorted list of strings |
| Other | `str()` fallback |

Deserialization produces plain dicts, not typed dataclasses. Callers that need
live payloads reconstruct them from the dict.

### 5.6 EventLogConsumer

Read-side interface for replay and audit.

- **`replay(n, since)`**: yields deserialized event records. Collects all
  records first, then sorts by `stream_seq` (globally monotonic) with fallback
  to ISO timestamp. Without sorting, reading from N partitions would produce
  interleaved order -- ORDER_REQ could appear after FILL, corrupting recovery.
- **`tail(timeout_sec)`**: yields live events as they arrive. Subscribes to
  the topic and polls with 1s timeout.
- **`seek_to_time(dt)`**: rewinds the consumer to the first message at or after
  the given datetime using `offsets_for_times()`.

Consumer configuration: `auto.offset.reset=earliest`, auto-commit every 5s,
10KB min fetch, 30s session timeout.

### 5.7 CrashRecovery

`CrashRecovery.rebuild()` reconstructs position state from today's event log:

1. Replays all events since midnight ET via `EventLogConsumer.replay(since=...)`.
2. Filters for FILL events only (ground truth for broker confirmations).
3. Rebuilds `positions` dict and `trade_log` list consistent with what
   PositionManager would have built.
4. BUY fills open positions; SELL fills close them and append to trade log.
5. Returns `(positions, trade_log)` for injection into RealTimeMonitor on
   restart.

---

## Error Handling Summary

| Failure Mode | Handling |
|---|---|
| Invalid payload construction | `ValueError` in `__post_init__`, event never reaches bus |
| Handler raises exception | Circuit breaker counts failure; retry per `RetryPolicy`; DLQ after exhaustion |
| Handler consistently fails for one ticker | Per-(handler, ticker) circuit breaker opens; other tickers unaffected |
| Async queue full (DROP_OLDEST) | Lowest-urgency item evicted from priority queue |
| Async queue full (BLOCK) | `emit()` blocks until space available (ORDER_REQ, FILL) |
| Async queue full (RAISE) | `BackpressureError` raised to caller |
| System-wide backpressure >= 80% | Adaptive micro-sleep in `emit()` (1-10ms) |
| Redpanda durable hook fails | Event dropped if `durable_fail_fast=True`; logged as hook failure |
| Redpanda async produce fails | Error count incremented; warning logged; bus continues |
| SharedCache write fails | Temp file cleaned up; warning logged; previous cache remains valid |
| SharedCache read fails | Falls back to previously cached data |
| Crash mid-session | `CrashRecovery.rebuild()` replays FILL events from Redpanda to reconstruct positions |

---

## Thread Safety Summary

| Component | Mechanism |
|---|---|
| EventBus subscription registry | `_sub_lock` (RLock) |
| Stream sequence numbers | `_seq_lock` + OrderedDict LRU (1000 keys) |
| Event counters | `_count_lock` |
| Idempotency dedup | `_idem_lock` + bounded OrderedDict |
| Handler circuit breaker state | Per-`_HandlerState` lock |
| Priority queue | `threading.Condition` per queue instance |
| SimulatedTimeSource | Internal `threading.Lock` |
| DurableEventLog error count | `threading.Lock` |
| SharedCache writes | Atomic via `tempfile` + `os.replace()` (no lock needed) |
| SharedCache reads | mtime check (no lock; stale reads acceptable) |
| IPC Publisher | confluent_kafka internal thread safety + daemon flush thread |
| IPC Consumer | Single daemon poll thread with callback dispatch |
