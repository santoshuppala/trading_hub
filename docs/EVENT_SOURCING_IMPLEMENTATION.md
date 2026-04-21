# Event Sourcing Implementation for Trading Hub

## Executive Summary

The trading hub's data consistency issues stem from fundamental architectural flaws in how events are persisted to the database. The solution is a complete migration to **event sourcing architecture** with immutable, append-only event store and materialized projections.

### The Problem (Current Architecture)

**All database event timestamps are WRONG** because the code uses `_NOW()` instead of `event.timestamp`:

```python
# CURRENT BUG in db/subscriber.py (ALL 9 handlers):
row = {
    "ts": _NOW(),  # ← WRONG: Current write time, not event time!
    ...
}
```

**Impact**:
- Events from 10:15 AM logged but written to DB at 13:15 (3-hour delay)
- Audit trail shows distorted timeline
- Cannot determine when trades actually occurred
- Violates financial audit standards
- Missing event types (OPENED events not in database)

### The Solution (Event Sourcing)

Replace the entire database persistence layer with event sourcing:

1. **Single immutable event store** (`event_store` table)
   - Append-only: no updates, only inserts
   - Complete timestamp trail: event_time, received_time, processed_time, persisted_time
   - Full audit context: what happened, when, why, in what order

2. **Materialized projections** (position_state, completed_trades, etc.)
   - Read models built FROM events, never the other way around
   - Can be rebuilt anytime from event_store
   - Serve application queries efficiently

3. **Full latency tracking**
   - ingest_latency_ms: How long in EventBus queue?
   - queue_latency_ms: How long in DBWriter queue?
   - total_latency_ms: Total pipeline delay
   - Identifies bottlenecks in real-time

## Files Created

### 1. `db/schema_event_sourcing.sql` (420 lines)
**Purpose**: New database schema with event sourcing tables

**Tables created**:
- `event_store`: Immutable append-only event log (hypertable, partitioned daily)
- `position_state`: Current positions (projection)
- `completed_trades`: Closed trades (projection)
- `signal_history`: All signals emitted (projection)
- `fill_history`: All fills executed (projection)
- `daily_metrics`: Daily aggregated stats (projection)
- `event_types`: Event type registry

**Key features**:
- UUID primary key for event identity
- Event sequence ordering
- Complete timestamp trail (5 timestamps)
- Correlation tracking (link related events)
- Latency calculations (GENERATED columns)
- Proper indexing for common queries
- TimescaleDB hypertable for performance

### 2. `db/event_sourcing_subscriber.py` (450 lines)
**Purpose**: Replaces `db/subscriber.py` - writes to event store instead of individual tables

**Classes**:
- `EventSourcingSubscriber`: New subscriber that writes immutable events

**Key methods**:
- `_write_event()`: Core method to write immutable event with full metadata
- `_on_bar()`, `_on_signal()`, `_on_fill()`, `_on_position()`: Event handlers
- Each handler maps to appropriate event_type and aggregate_type
- Includes complete context: source_system, session_id, correlation_id

**Differences from old DBSubscriber**:
- ✅ Uses `event.timestamp` (correct)
- ✅ Tracks complete timestamp trail
- ✅ Includes correlation tracking
- ✅ Source system attribution
- ✅ Session-aware (which monitor session emitted it?)
- ✅ All events go to single table (normalized)

### 3. `db/projection_builder.py` (500 lines)
**Purpose**: Rebuild read models from event store

**Classes**:
- `ProjectionBuilder`: Static methods to materialize projections

**Key methods**:
- `rebuild_all()`: Rebuild all projections from scratch
- `rebuild_position_state(ticker)`: Replay position events for a ticker
- `rebuild_completed_trades(since)`: Link OPENED→CLOSED events into trades
- `rebuild_daily_metrics(date)`: Aggregate daily statistics
- `rebuild_all_signal_history()`: Materialize signal projection
- `rebuild_all_fill_history()`: Materialize fill projection
- `audit_event_store()`: Quality metrics for event pipeline

**How it works**:
1. Query event_store for events of a type
2. Replay events in sequence to reconstruct state
3. Insert/update projection tables
4. Always idempotent (can run multiple times safely)

### 4. `docs/EVENT_SOURCING_MIGRATION.md` (350 lines)
**Purpose**: Step-by-step migration guide

**Sections**:
- Architecture comparison (old vs new)
- Phase-by-phase migration (4 phases)
- Query examples (how to use event store)
- Backward compatibility (dual-write during transition)
- Troubleshooting common issues
- Production considerations (retention, backup, compression)
- Testing strategies
- FAQ

## How It Works: Complete Pipeline

### Before (Wrong Architecture)

```
Market Event (10:50:35 AM)
         ↓
PositionManager (receives fill)
         ↓
Emits POSITION event with timestamp=10:50:35
         ↓
EventBus routes to DBSubscriber
         ↓
DBSubscriber:
  row = {"ts": _NOW(), ...}  ← BUG! Uses 10:50:40 (current time)
         ↓
DBWriter queue (backed up, many events waiting)
         ↓
Monitor restarts multiple times (11:03:25)
         ↓
Queue flushed, but using _NOW() from flush time
         ↓
Database INSERT at 13:15:02 with ts=13:15:02  ← WRONG TIMESTAMP
         ↓
Result: "Trade occurred at 1:15 PM?" (Actually 10:50 AM, 3-hour error!)
```

### After (Correct Event Sourcing)

```
Market Event (10:50:35 AM)
         ↓
PositionManager receives fill, captures current time
         ↓
Emits POSITION event with:
  - event.timestamp = 10:50:35.000 (when it happened)
  - event_id = <UUID>
         ↓
EventBus routes to EventSourcingSubscriber
         ↓
EventSourcingSubscriber builds event record:
  - event_time = 10:50:35.000      ← When it happened
  - received_time = 10:50:35.001   ← When system got it
  - processed_time = 10:50:35.100  ← When we processed it
  - persisted_time = NULL          ← Will be set by DB
  - aggregate_id = "position_ARM"
  - correlation_id = <UUID>
         ↓
DBWriter queue (batches events for efficiency)
         ↓
Monitor restarts - no problem! Queue flushed with correct timestamps
         ↓
Database INSERT to event_store:
  All timestamps preserved!
         ↓
ProjectionBuilder queries event_store:
  "Show me all OPENED→CLOSED pairs"
         ↓
Rebuilds position_state, completed_trades with accurate times
         ↓
Result: "Trade occurred 10:50:35 AM" ✅ CORRECT
```

## Key Design Principles

### 1. Immutability
Events NEVER change once written. Only append new events.

```sql
-- Event once written:
INSERT INTO event_store (event_id, event_type, event_payload, ...)
VALUES (...)

-- Never:
UPDATE event_store SET event_payload = ...  -- ❌ WRONG
DELETE FROM event_store ...                 -- ❌ WRONG

-- Only:
INSERT INTO event_store (...)  -- ✅ CORRECT
```

### 2. Complete Timestamps
Every event carries complete timing information:

- **event_time**: When the market event occurred
- **received_time**: When the system first saw it
- **processed_time**: When we processed it
- **persisted_time**: When it hit the database

This allows measuring:
- Ingest latency: received - event
- Queue latency: processed - received
- Persistence latency: persisted - processed
- **Total latency**: persisted - event

### 3. Projections Built From Events
Read models are ALWAYS materialized from the event store, never the source of truth:

```
event_store (source of truth)
    ↓
    ├─→ ProjectionBuilder.rebuild_position_state()
    │       ↓
    │   position_state (read model)
    │
    ├─→ ProjectionBuilder.rebuild_completed_trades()
    │       ↓
    │   completed_trades (read model)
    │
    └─→ ProjectionBuilder.rebuild_daily_metrics()
            ↓
        daily_metrics (read model)
```

If a projection becomes corrupt, delete it and rebuild from events.

### 4. Event Correlation
Related events are linked via `correlation_id`:

```
SIGNAL event (ticker=ARM, action=BUY, correlation_id=<uuid-1>)
    ↓
ORDER_REQ event (caused by signal, causation_id=<uuid-1>)
    ↓
FILL event (filled order, causation_id=<order-event-id>)
    ↓
POSITION event (opened position, causation_id=<fill-event-id>)
    ↓
POSITION event (closed position, correlation_id=<uuid-1>)

Now you can query: "Show me the entire lifecycle of this signal"
```

## Usage Examples

### Query 1: Audit Trail for a Trade

```sql
-- Get complete history of ARM position
SELECT
  event_type,
  event_time AT TIME ZONE 'America/New_York' as time,
  event_payload->>'action' as action,
  event_payload->>'qty' as qty,
  total_latency_ms
FROM event_store
WHERE aggregate_id = 'position_ARM'
ORDER BY event_sequence;

-- Output shows: OPENED at 10:50:35, CLOSED at 11:01:25, with exact latencies
```

### Query 2: Pipeline Performance

```sql
-- Find slow events (took > 1 second to persist)
SELECT
  event_type,
  event_time,
  total_latency_ms,
  queue_latency_ms
FROM event_store
WHERE total_latency_ms > 1000
ORDER BY total_latency_ms DESC
LIMIT 10;

-- Identifies bottlenecks in the trading pipeline
```

### Query 3: Daily Summary

```sql
-- Get daily metrics (automatically built from completed_trades)
SELECT
  metric_date,
  total_trades,
  winning_trades,
  ROUND(win_rate, 1) as win_pct,
  ROUND(total_pnl, 2) as pnl
FROM daily_metrics
WHERE metric_date >= CURRENT_DATE - INTERVAL '7 days'
ORDER BY metric_date DESC;
```

### Query 4: Replay Events (for debugging)

```sql
-- Replay all events for a position to understand what happened
SELECT
  event_sequence,
  event_type,
  event_time,
  event_payload
FROM event_store
WHERE aggregate_id = 'position_NVDA'
  AND event_time > '2026-04-13 10:00'
  AND event_time < '2026-04-13 12:00'
ORDER BY event_sequence;

-- Can replay in your application to reconstruct exact state at any time
```

## Migration Steps

### Step 1: Deploy Schema (Non-Breaking)
```bash
psql -d tradinghub -f db/schema_event_sourcing.sql
```

### Step 2: Update Monitor Code
Update `run_monitor.py`:
```python
from db.event_sourcing_subscriber import EventSourcingSubscriber

subscriber = EventSourcingSubscriber(bus, writer, session_id=session_id)
subscriber.register()
```

### Step 3: Rebuild Projections
```python
from db.projection_builder import ProjectionBuilder

await ProjectionBuilder.rebuild_all()
```

### Step 4: Verify & Monitor
```sql
SELECT COUNT(*) FROM event_store;  -- Should see all events
SELECT * FROM daily_metrics;        -- Should match manual calculations
```

### Step 5: Deprecate Old Tables
```sql
ALTER TABLE trading.position_events COMMENT = 'DEPRECATED: Use event_store';
ALTER TABLE trading.fill_events COMMENT = 'DEPRECATED: Use event_store';
-- Keep old data for 30 days, then archive
```

## Benefits Summary

### For Trading
- ✅ Accurate trade history with exact timestamps
- ✅ Can replay any position to see what happened
- ✅ Latency visibility (identify slow operations)
- ✅ No data loss (immutable archive)

### For Compliance
- ✅ Complete audit trail (every event recorded)
- ✅ Temporal queries (state at any point in time)
- ✅ Financial-grade data integrity
- ✅ Regulatory-friendly architecture

### For Debugging
- ✅ Replay events to reproduce issues
- ✅ Correlation tracking (follow event chains)
- ✅ Latency measurements (find bottlenecks)
- ✅ Compare event_time vs received_time vs persisted_time

### For Operations
- ✅ Can rebuild projections anytime (if corrupt)
- ✅ No schema migrations needed (JSONB payload flexible)
- ✅ TimescaleDB compression for old data
- ✅ Efficient daily partitioning

## Next Steps

1. **Review** `db/schema_event_sourcing.sql` and create tables
2. **Review** `db/event_sourcing_subscriber.py` code quality
3. **Test** migration with `docs/EVENT_SOURCING_MIGRATION.md` guide
4. **Deploy** in non-prod environment first
5. **Monitor** latencies and projection accuracy
6. **Deprecate** old tables after 2 weeks successful dual-write
7. **Document** any custom queries that depend on event_store

## Questions?

See `docs/EVENT_SOURCING_MIGRATION.md` for:
- Step-by-step migration guide
- Query examples
- Troubleshooting
- FAQ
- Production considerations

This architecture ensures your trading hub has a complete, auditable, replayable event log - the foundation of financial-grade trading systems.
