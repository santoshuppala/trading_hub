# Persistence & Lifecycle — Design Document

**Source of truth:** Code as of 2026-04-15
**Files:** `db/writer.py`, `db/event_sourcing_subscriber.py`, `db/connection.py`, `db/projection_builder.py`, `db/schema_event_sourcing_pg.sql`, `lifecycle/core.py`, `lifecycle/state_persistence.py`, `lifecycle/kill_switch.py`, `lifecycle/heartbeat.py`, `lifecycle/eod_report.py`, `lifecycle/reconciler.py`, `lifecycle/adapters/base.py`, `lifecycle/adapters/pro_adapter.py`, `lifecycle/adapters/pop_adapter.py`, `lifecycle/adapters/options_adapter.py`

---

## 1. PURPOSE

This document covers two tightly coupled subsystems:

1. **DB Layer** — async event sourcing pipeline that persists every trading event to TimescaleDB with full audit trails and replay capability.
2. **Lifecycle Management** — template-method orchestrator that handles startup preflight, periodic health checks, crash recovery, kill switch, and EOD reporting for all satellite engines.

Together they ensure that (a) no event is silently lost, (b) engines recover from crashes within one session, and (c) daily loss limits are enforced irrecoverably.

---

## 2. DESIGN PRINCIPLES

### 2.1 Immutable Event Store as Source of Truth
Every event is appended to a single `event_store` table with a complete timestamp trail (event_time, received_time, processed_time, persisted_time). Events are never updated or deleted. Projection tables are derived FROM events and can be rebuilt at any time.

### 2.2 Fire-and-Forget Persistence
The DB write path must never block the trading hot path. Events are enqueued via `call_soon_threadsafe` from EventBus worker threads into an async queue. A circuit breaker suspends writes after consecutive failures rather than retrying indefinitely. Failed rows are dropped, not re-queued.

### 2.3 Broker Is Source of Truth for Positions
The reconciler always trusts broker state over local state. Orphaned positions at the broker are added to local tracking; stale positions not at the broker are removed. The system never auto-sells — it only adjusts tracking.

### 2.4 Irrecoverable Kill Switch
Once the daily loss threshold is breached, `halted=True` is set and cannot be cleared within the session. This prevents code paths from accidentally re-enabling trading after a loss limit hit.

### 2.5 Adapter Pattern for Engine Diversity
Each engine (Pro, Pop, Options) has radically different state models, broker access, and position semantics. A single `AbstractEngineAdapter` with 11 abstract methods allows `EngineLifecycle` to manage all three generically.

---

## 3. DB LAYER

### 3.1 Connection Pool (`db/connection.py`)

Module-level singleton backed by `asyncpg.create_pool`:

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `min_size` | 4 | Keeps warm connections for burst writes |
| `max_size` | 20 | Cap to prevent connection exhaustion |
| `command_timeout` | 30s | Kill stuck queries before they cascade |

Per-connection initialization sets `search_path=trading,public` and `timezone=UTC` so all timestamps are UTC-aware (required by PG17 strict mode / asyncpg).

**Lifecycle:** `init_db()` creates the pool (idempotent), `close_db()` tears it down, `get_pool()` raises `RuntimeError` if called before init.

### 3.2 DBWriter (`db/writer.py`)

Async batch writer that bridges sync EventBus threads to async DB writes.

**Architecture:**

```
EventBus worker thread                    asyncio event loop
        |                                        |
        |-- call_soon_threadsafe(queue.put) ----->|
        |                                        |
        |                          _flush_loop (0.5s interval)
        |                                |
        |                          _drain() → group by table
        |                                |
        |                          INSERT ... ON CONFLICT DO NOTHING
```

**Tunables (env-configurable):**

| Env Var | Default | Purpose |
|---------|---------|---------|
| `DB_BATCH_MAX` | 200 | Max rows per flush cycle |
| `DB_FLUSH_INTERVAL` | 0.5s | Sleep between drain attempts |
| `DB_QUEUE_MAXSIZE` | 5000 | Backpressure limit — drops beyond this |
| `DB_CIRCUIT_BREAKER_FAILURES` | 5 | Consecutive failures before opening breaker |
| `DB_CIRCUIT_BREAKER_RESET` | 30s | Time before auto-reset attempt |

**Circuit Breaker:**
- 5 consecutive table-level failures open the breaker.
- While open, all `enqueue()` calls silently drop (incrementing `rows_dropped`).
- After 30s, next `enqueue()` auto-resets and retries.
- Design choice: drop failed rows immediately rather than re-queue. Re-queuing risks infinite retry loops when the issue is a schema mismatch, not a transient network error.

**Table Independence:** Each table within a batch gets its own `pool.acquire()` + transaction. A COPY failure on `heartbeat_events` does not abort writes to `fill_events`.

**INSERT Strategy:** Uses parameterized `executemany()` with `ON CONFLICT DO NOTHING`. The alternative (`COPY` protocol) is faster but fails on JSONB/UUID type coercion and aborts the entire transaction.

**Metrics:** `rows_written`, `rows_dropped`, `batches_flushed` are tracked and reported in `SessionManager.stop()`.

### 3.3 SessionManager (`db/writer.py`)

Tracks session lifecycle in `trading.session_log`:

- `start()` — inserts a row with mode, broker, tickers, config_hash, metadata. Returns `session_id` (UUID).
- `stop()` — updates the row with `stop_ts`, event counts, exit reason, error message.
- Crashed sessions have `stop_ts=NULL` — queryable for crash detection.

### 3.4 EventSourcingSubscriber (`db/event_sourcing_subscriber.py`)

**Registration:** Subscribes to ALL 13 EventTypes at priority 10 (LOW), ensuring it runs after all trading-critical handlers.

**Dual-write pattern:** Every event handler writes BOTH:
1. An immutable record to `event_store` (canonical event)
2. An inline projection row to the relevant table (for fast queries)

**Event type mapping:**

| EventType | event_store type | Projection table |
|-----------|-----------------|------------------|
| BAR | BarReceived | (none — too high volume) |
| SIGNAL | StrategySignal | signal_events |
| ORDER_REQ | OrderRequested | order_req_events |
| FILL | FillExecuted | fill_events |
| POSITION | PositionOpened/Closed/PartialExited | position_events |
| RISK_BLOCK | RiskBlocked | risk_block_events |
| POP_SIGNAL | PopStrategySignal | pop_signal_events |
| PRO_STRATEGY_SIGNAL | ProStrategySignal | pro_strategy_signal_events |
| HEARTBEAT | HeartbeatEmitted | heartbeat_events |
| OPTIONS_SIGNAL | OptionsSignal | (none) |
| QUOTE | QuoteReceived | (none) |
| NEWS_DATA | NewsDataSnapshot | (none) |
| SOCIAL_DATA | SocialDataSnapshot | (none) |

**Aggregate Versioning:** Each `aggregate_id` (e.g., `position_ARM`) gets a monotonically incrementing `aggregate_version` tracked in `_aggregate_versions: Dict[str, int]`. This enables ordered replay of entity history.

**Timestamp Trail:** Every event_store row contains:
- `event_time` — when the event actually occurred (from `Event.timestamp`)
- `received_time` — when the subscriber received it (`_NOW()`)
- `processed_time` — when processing completed (`_NOW()`)
- `persisted_time` — `DEFAULT NOW()` in Postgres (when the INSERT runs)

**Completed Trade Linking:** `_write_completed_trade()` fires on `PositionClosed` events. It computes P&L percentage, trade duration, and links `opened_event_id` (from `correlation_id`) to `closed_event_id`. Writes to the `completed_trades` projection table.

### 3.5 Projection Builder (`db/projection_builder.py`)

Rebuilds read models FROM the event store. All methods are static and async.

| Method | What It Rebuilds | Technique |
|--------|-----------------|-----------|
| `rebuild_position_state(ticker)` | Single ticker's current state | Replays all Position events in sequence order |
| `rebuild_all_position_states()` | All position states | Queries distinct tickers, calls per-ticker rebuild |
| `rebuild_completed_trades(since)` | Trade records | `LAG()` window function to pair OPENED/CLOSED events by `aggregate_id` |
| `rebuild_daily_metrics(date)` | Daily stats | Aggregate query over `completed_trades` |
| `rebuild_all_signal_history()` | Signal records | Queries all Signal-type events |
| `rebuild_all_fill_history()` | Fill records | Queries all FillExecuted events |
| `rebuild_all()` | Everything | Calls all of the above |

**Position State Replay:** Events are fetched ordered by `event_sequence ASC`. The state dict is mutated through `PositionOpened` (initialize), `PartialExited` (update qty), and `PositionClosed` (mark closed). If the final state is CLOSED, the row is deleted from `position_state`; otherwise it's upserted.

**`audit_event_store()` function:** Returns pipeline health metrics — total events, unique types, latency statistics (avg, max, p95 in ms).

### 3.6 Schema (`db/schema_event_sourcing_pg.sql`)

**Core table — `event_store`:**

```sql
event_id              UUID PRIMARY KEY
event_sequence        BIGINT NOT NULL
event_type            TEXT NOT NULL
event_version         INT DEFAULT 1
event_time            TIMESTAMP NOT NULL
received_time         TIMESTAMP NOT NULL
queued_time           TIMESTAMP           -- optional
processed_time        TIMESTAMP NOT NULL
persisted_time        TIMESTAMP DEFAULT NOW()
aggregate_id          TEXT NOT NULL
aggregate_type        TEXT NOT NULL
aggregate_version     INT NOT NULL
event_payload         JSONB NOT NULL
correlation_id        UUID
causation_id          UUID
parent_event_id       UUID
source_system         TEXT NOT NULL
source_version        TEXT
session_id            UUID
```

**Indexes (6):**

| Index | Columns | Purpose |
|-------|---------|---------|
| `idx_event_store_event_time` | `event_time DESC` | Time-range queries |
| `idx_event_store_type` | `(event_type, event_time DESC)` | Filter by type + time |
| `idx_event_store_aggregate` | `(aggregate_type, aggregate_id, event_sequence DESC)` | Entity history replay |
| `idx_event_store_correlation` | `correlation_id` (partial, WHERE NOT NULL) | Trace linked events |
| `idx_event_store_session` | `(session_id, event_time DESC)` | Session-scoped queries |
| `idx_event_store_causation` | `causation_id` (partial, WHERE NOT NULL) | Causal chain traversal |

**Unique constraint:** `(aggregate_type, aggregate_id, event_sequence)` prevents duplicate event writes per entity.

**Projection tables:** `position_state`, `completed_trades`, `signal_history`, `fill_history`, `daily_metrics` — all with `ON CONFLICT DO NOTHING` or `DO UPDATE` semantics for idempotent rebuilds.

**Helper functions:**
- `get_aggregate_events(type, id)` — returns ordered event history for an entity
- `get_latency_metrics(start, end)` — ingest and total latency percentiles
- `audit_event_store(days)` — completeness checks (event counts, missing timestamps)

**Latency view:** `event_store_latencies` — computed view exposing `ingest_latency_ms`, `queue_latency_ms`, `total_latency_ms` for each event.

---

## 4. LIFECYCLE MANAGEMENT

### 4.1 EngineLifecycle (`lifecycle/core.py`)

Template-method orchestrator with three hooks: `startup()`, `tick()`, `shutdown()`.

**Components composed at init:**

```python
EngineLifecycle(engine_name, adapter, bus, alert_email, max_daily_loss, state_dir)
    ├── AtomicStateFile(engine_name, state_dir)
    ├── SatelliteKillSwitch(engine_name, adapter, alert_email, max_daily_loss)
    ├── SatelliteHeartbeat(engine_name, adapter)
    ├── SatelliteEODReport(engine_name, adapter, alert_email)
    └── PositionReconciler(engine_name, adapter)
```

#### 4.1.1 startup() — called once before main loop

| Step | Action | Why |
|------|--------|-----|
| 1 | `adapter.verify_connectivity()` | Fail early if broker is unreachable |
| 2 | `adapter.cancel_stale_orders()` | Clear orphaned orders from previous crash |
| 3 | `state.load()` + `adapter.restore_state()` | Recover positions/cooldowns/PnL |
| 4 | `reconciler.sync_startup()` | Full sync — broker overrides local state |
| 5 | `state.save(adapter.get_state())` | Persist reconciled state immediately |

#### 4.1.2 tick() — called every loop iteration (~10s)

Returns `False` to stop the engine (kill switch only).

| Step | Frequency | Action |
|------|-----------|--------|
| 1 | Every tick | Kill switch check — if triggered, force_close_all + save + return False |
| 2 | Every 60s | Heartbeat log |
| 3 | minute==0, max 1/hr | Hourly P&L summary |
| 4 | minute==30, max 1/hr | Periodic reconciliation |
| 5 | Every tick | State save (skips if unchanged via MD5 hash) |

**Hourly guard:** Uses `time.monotonic()` with 3500s threshold to prevent double-firing within the same hour.

#### 4.1.3 shutdown() — called in finally block

| Step | Action |
|------|--------|
| 1 | Save current state |
| 2 | Force-close all open positions (reason: `eod_shutdown`) |
| 3 | Generate + send EOD report |
| 4 | Save final state (post-close) |

### 4.2 AtomicStateFile (`lifecycle/state_persistence.py`)

**File:** `data/{engine_name}_state.json`

**Atomic write protocol:**
1. Serialize state to JSON (deterministic via `sort_keys=True`)
2. Compute MD5 hash — skip write if identical to last write
3. Write to `{path}.tmp`
4. `os.replace(tmp, path)` — atomic on POSIX

**Thread safety:** Protected by `threading.Lock`.

**Date staleness:** Every saved state includes `date: YYYY-MM-DD` (ET timezone). On `load()`, if the file date differs from today, the state is discarded. This prevents restoring yesterday's positions/PnL into today's session.

**Corrupt handling:** If `json.load()` raises `JSONDecodeError` or `OSError`, the file is moved to `{path}.corrupt.{YYYYMMDD_HHMMSS}` and load returns `None` (engine starts fresh). Corrupt files are preserved for post-mortem analysis.

**Hash caching:** After a successful `load()`, the MD5 hash is cached so the first `save()` after load doesn't redundantly rewrite identical data.

### 4.3 SatelliteKillSwitch (`lifecycle/kill_switch.py`)

- Calls `adapter.get_daily_pnl()` on every tick.
- If `daily_pnl <= max_daily_loss` (default: -$2000): sets `halted=True`, sends CRITICAL email alert.
- Once `halted=True`, `check()` always returns `True` — no recovery within the session.
- EngineLifecycle responds by calling `adapter.force_close_all('kill_switch_daily_loss')`.

### 4.4 SatelliteHeartbeat (`lifecycle/heartbeat.py`)

- 60-second interval via `time.monotonic()`.
- Logs: engine name, open position count, ticker list (up to 5), trades, wins, win%, P&L.
- Errors in heartbeat are swallowed (DEBUG level) — heartbeat must never crash the engine.

### 4.5 SatelliteEODReport (`lifecycle/eod_report.py`)

Generated at shutdown. Sections:

1. **Overall stats** — trades, wins, losses, total P&L, avg win, avg loss, profit factor
2. **Exit reason breakdown** — count and P&L per reason (e.g., stop_hit, target_hit, eod_shutdown)
3. **Per-strategy breakdown** — count, P&L, win rate per strategy
4. **Trade-by-trade log** — ticker, qty, entry/exit prices, P&L, reason
5. **Engine-specific stats** — from `adapter.get_daily_stats()['extra']` (e.g., Options portfolio Greeks)

**Profit factor:** `gross_wins / |gross_losses|`, returns `inf` if no losses.

**Delivery:** Logged at INFO level AND sent via `monitor.alerts.send_alert()` (email).

### 4.6 PositionReconciler (`lifecycle/reconciler.py`)

**Timing:** Full sync at startup (`sync_startup()`), lightweight check every 30 minutes (`sync_periodic()`). Both call the same `_reconcile()` method.

**Rules:**

| Condition | Action | Rationale |
|-----------|--------|-----------|
| At broker, not local | `adapter.add_position(ticker, qty)` | Crash recovery — engine crashed after broker fill |
| In local, not at broker | `adapter.remove_position(ticker)` | Closed externally (manual, bracket stop hit) |
| Mismatch in qty | (not handled) | Future enhancement |
| Local matches broker | No-op, log "in sync" | Happy path |

**Critical safety rule:** NEVER auto-sell. Reconciliation only adjusts tracking state. A human must review and close positions manually if needed.

---

## 5. ENGINE ADAPTERS

### 5.1 AbstractEngineAdapter (`lifecycle/adapters/base.py`)

11 abstract methods that every engine must implement:

| Method | Return | Purpose |
|--------|--------|---------|
| `get_state()` | dict | Serializable state for crash recovery |
| `restore_state(dict)` | None | Apply saved state on restart |
| `get_positions()` | Dict[str, Any] | Current open positions |
| `get_trade_log()` | List[dict] | Today's completed trades |
| `get_daily_pnl()` | float | Today's realized P&L |
| `get_daily_stats()` | dict | Aggregate stats for heartbeat/EOD |
| `get_broker_positions()` | Dict[str, int] | Fetch live positions from broker API |
| `force_close_all(reason)` | None | Emergency liquidation |
| `cancel_stale_orders()` | int | Clear orphaned orders at startup |
| `verify_connectivity()` | bool | Broker reachability check |
| `add_position(ticker, info)` | None | Reconciler adds orphaned position |
| `remove_position(ticker)` | None | Reconciler removes stale position |

### 5.2 ProLifecycleAdapter

**Broker access model:** IPC only. Pro sends ORDER_REQ events to Core via Redpanda; Core's broker executes. Pro has no direct Alpaca/Tradier credentials.

**Position tracking:** `RiskAdapter._positions` (Set[str]). No quantity tracking — just ticker presence.

**State persistence:** Positions, cooldown offsets (monotonic time converted to relative seconds for JSON serialization), trade log, daily P&L.

**Reconciliation source:** `DistributedPositionRegistry` (file-locked cross-process JSON) — not a broker API call.

**force_close_all:** Cannot actually sell. Clears local tracking set and logs a WARNING. Core's broker must handle the actual closes.

**verify_connectivity:** Checks `CacheReader.get_bars()` — Pro reads bar data from shared cache, not from a broker.

### 5.3 PopLifecycleAdapter

**Broker access model:** Own Alpaca account (`APCA_POPUP_KEY` / `APCA_PUPUP_SECRET_KEY`). Direct API access for all broker operations.

**Position tracking:** `PopExecutor._positions` (Set[str]), protected by `threading.Lock`.

**State persistence:** Positions, cooldown offsets (`_last_order_time`), trade log, daily P&L.

**Reconciliation source:** Direct `TradingClient.get_all_positions()` on Pop's Alpaca account.

**force_close_all:** Iterates positions and calls `client.close_position(ticker)` individually. Clears local set after.

**cancel_stale_orders:** Queries open orders via `GetOrdersRequest(status=OPEN)`, cancels each by ID.

### 5.4 OptionsLifecycleAdapter

**Broker access model:** Own Alpaca account (`APCA_OPTIONS_KEY` / `APCA_OPTIONS_SECRET`). Direct API access.

**Position tracking:** `OptionsEngine._positions` (Dict[str, OptionsPosition]), protected by `_positions_lock`. Each position has strategy_type, entry_cost, max_risk, max_reward, expiry_date, legs, Greeks.

**State persistence:** Position metadata (serialized from OptionsPosition objects), daily P&L, entry/exit counters, portfolio-level Greeks (delta, theta, vega), risk gate state (deployed_capital, daily_trade_count).

**Critical restore limitation:** OptionsPosition objects are NOT restored from the state file. They require full option chain data (legs, strikes, Greeks) that cannot be reliably serialized. Instead, reconciliation with the broker rebuilds actual positions.

**Reconciliation source:** Direct `TradingClient.get_all_positions()`. Option symbols (e.g., `AAPL260508C00265000`) are parsed to extract the underlying ticker for grouping.

**add_position:** Logs an INFO message and does nothing. Auto-rebuilding option legs from just a ticker is not possible — requires manual review.

**force_close_all:** Delegates to `OptionsEngine.close_all_positions(reason)`.

**get_trade_log:** Returns `[]` — Options engine doesn't maintain an in-memory trade log. EOD report would need to query the event_store.

**Extra stats:** `get_daily_stats()` includes an `extra` dict with portfolio Greeks and deployed capital, which the EOD report renders as engine-specific stats.

---

## 6. DATA FLOW DIAGRAM

```
EventBus worker threads (sync)
    │
    │  call_soon_threadsafe
    ▼
asyncio.Queue (max 5000)
    │
    │  _flush_loop (0.5s)
    ▼
DBWriter._drain()
    │
    ├── GROUP BY table
    │
    ├── event_store ──────────────────────► Immutable audit log
    │
    ├── signal_events ──┐
    ├── fill_events     │
    ├── position_events │
    ├── risk_block_events│
    ├── order_req_events ├────────────────► Inline projection tables
    ├── completed_trades │                  (queryable read models)
    ├── heartbeat_events │
    ├── pop_signal_events│
    └── pro_strategy_    │
        signal_events ──┘
                         │
                         ▼
              ProjectionBuilder.rebuild_*() ─► Derived projections
                                               (position_state,
                                                daily_metrics,
                                                signal_history,
                                                fill_history)
```

---

## 7. FAILURE MODES AND RECOVERY

| Failure | Behavior | Recovery |
|---------|----------|----------|
| DB unreachable | Circuit breaker opens after 5 failures, events dropped for 30s | Auto-reset; events during outage are lost |
| Queue full (5000) | New events dropped silently | Increase `DB_QUEUE_MAXSIZE` or reduce event volume |
| Schema mismatch | `ON CONFLICT DO NOTHING` — row silently skipped | Fix schema, replay from event_store |
| State file corrupt | Backed up as `.corrupt.{ts}`, engine starts fresh | Manual investigation of corrupt backup |
| State file from yesterday | Discarded on load (date check) | Engine starts with clean state |
| Engine crash mid-session | State file has last-known positions | Startup restores state + reconciles with broker |
| Broker position not in local | Reconciler adds to tracking | Crash recovery pattern |
| Local position not at broker | Reconciler removes from tracking | External close / bracket stop hit |
| Daily loss > threshold | Kill switch halts engine for remainder of session | No recovery — restart next day |
| Kill switch + open positions | `force_close_all()` called, then engine stops | Positions liquidated before halt |

---

## 8. KNOWN LIMITATIONS

1. **Failed DB rows are dropped, not retried.** If a schema mismatch causes persistent failures for one table, all events for that table are lost until the schema is fixed. The event_store (JSONB payload) is resilient to this, but inline projections are not.

2. **OptionsAdapter.get_trade_log() returns [].** EOD report for Options shows no trade log. Fix requires querying `completed_trades` from the event_store at report time.

3. **ProAdapter.force_close_all() cannot actually sell.** It clears local tracking but leaves positions open at Core's broker. Requires IPC SELL flow or manual intervention.

4. **Reconciler does not handle quantity mismatches.** If local says 100 shares and broker says 50, no adjustment is made. Only presence/absence is reconciled.

5. **Projection tables are dual-written, not rebuilt.** Inline projections at write time may drift from the event_store if a write succeeds for event_store but fails for the projection table (separate connections). `ProjectionBuilder.rebuild_*()` can fix this but is not called automatically.

6. **No WAL or local buffer for DB outages.** Events during a circuit-breaker-open window are permanently lost. A disk-based WAL would prevent data loss during extended DB outages.
