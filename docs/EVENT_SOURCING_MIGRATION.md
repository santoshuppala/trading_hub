# Event Sourcing Architecture Migration Guide

## Overview

This guide explains how to migrate from the old schema (individual event tables) to the new event sourcing architecture with a central `event_store` table and materialized projections.

**Benefits of Event Sourcing**:
- ✅ Complete, immutable audit trail
- ✅ Accurate timestamp tracking (event_time vs received_time vs processed_time)
- ✅ Latency visibility at every stage of the pipeline
- ✅ Ability to replay events for debugging/recovery
- ✅ Temporal queries at any point in history
- ✅ Financial compliance-ready

## Architecture Comparison

### Old Architecture (Current)
```
PositionManager ─→ EventBus ─→ DBSubscriber ─→ position_events (lossy, wrong timestamps)
                                            ─→ fill_events
                                            ─→ signal_events
                                            ─→ etc.
```

**Problems**:
- ❌ Each table has wrong `ts` (uses _NOW() instead of event.timestamp)
- ❌ Timestamp discrepancies (events appear 3+ hours late)
- ❌ No latency tracking
- ❌ OPENED events missing from database
- ❌ No correlation between events

### New Architecture (Event Sourcing)
```
PositionManager ─→ EventBus ─→ EventSourcingSubscriber ─→ event_store (immutable, append-only)
                                                        ↓
                                                    ProjectionBuilder
                                                        ↓
                                 ┌───────────┬─────────┬──────────────┐
                                 ↓           ↓         ↓              ↓
                            position_state  completed_trades  signal_history  fill_history
                                (read)          (read)           (read)          (read)
```

**Benefits**:
- ✅ Single source of truth: `event_store`
- ✅ Complete timestamp trail: event_time, received_time, processed_time, persisted_time
- ✅ Latency tracking: ingest_latency, queue_latency, total_latency
- ✅ Immutable: no overwrites, only appends
- ✅ Replayable: rebuild state at any point in history

## Step-by-Step Migration

### Phase 1: Create New Schema (Non-Breaking)

```bash
# Run migration in PostgreSQL
psql -U postgres -d tradinghub -f db/schema_event_sourcing.sql

# Verify tables created
psql -U postgres -d tradinghub -c "
SELECT tablename FROM pg_tables
WHERE schemaname = 'public' AND tablename IN
('event_store', 'position_state', 'completed_trades', 'signal_history', 'fill_history', 'daily_metrics');
"
```

Expected output:
```
     tablename
──────────────────
 event_store
 position_state
 completed_trades
 signal_history
 fill_history
 daily_metrics
```

### Phase 2: Update Monitor to Use EventSourcingSubscriber

**File**: `run_monitor.py`

**Before** (old):
```python
from db.subscriber import DBSubscriber

# ... initialization ...

if DB_ENABLED:
    writer = init_writer(loop)
    await writer.start()

    subscriber = DBSubscriber(bus, writer)
    subscriber.register()
```

**After** (new):
```python
from db.event_sourcing_subscriber import EventSourcingSubscriber
from db.writer import SessionManager

# ... initialization ...

if DB_ENABLED:
    # Start database layer
    writer = init_writer(loop)
    await writer.start()

    # Create session
    session_mgr = SessionManager()
    session_id = await session_mgr.start(
        mode='paper' if paper else 'live',
        broker='alpaca',
        tickers=tickers,
        config_hash=None,
        metadata={'strategy': strategy_name}
    )

    # Use new event sourcing subscriber
    subscriber = EventSourcingSubscriber(bus, writer, session_id=session_id)
    subscriber.register()

    # Later, at shutdown:
    await session_mgr.stop(exit_reason='clean', writer=writer)
```

### Phase 3: Rebuild Projections from Event Store

```python
# In run_monitor.py after all events have been collected (e.g., at end of day):

from db.projection_builder import ProjectionBuilder, audit_event_store

async def finalize_session():
    """Called at end of trading day or on clean shutdown."""

    # Rebuild projections from events
    log.info("Rebuilding projections from event_store...")
    await ProjectionBuilder.rebuild_all()

    # Audit event store quality
    stats = await audit_event_store()
    log.info(f"Event store audit: {stats}")

    # Log summary
    log.info(f"""
    ═════════════════════════════════════════════════════
    EVENT SOURCING SUMMARY
    ═════════════════════════════════════════════════════
    Total Events: {stats['total_events']}
    Event Types: {stats['unique_event_types']}
    Aggregates: {stats['unique_aggregates']}

    Pipeline Latency:
      Average: {stats['avg_latency_ms']:.1f}ms
      P95:     {stats['p95_latency_ms']:.1f}ms
      Max:     {stats['max_latency_ms']:.1f}ms

    Period: {stats['earliest_event']} to {stats['latest_event']}
    ═════════════════════════════════════════════════════
    """)
```

### Phase 4: Query the Event Store (Examples)

#### Query 1: All events for a position

```sql
-- Complete audit trail for ARM position
SELECT
  event_time AT TIME ZONE 'America/New_York' as time,
  event_type,
  (event_payload->>'action') as action,
  (event_payload->>'qty')::INT as qty,
  (event_payload->>'entry_price')::DECIMAL as price,
  EXTRACT(EPOCH FROM (persisted_time - event_time))::INT as latency_ms
FROM event_store
WHERE aggregate_id = 'position_ARM'
ORDER BY event_sequence ASC;

-- Output:
time                   | event_type      | action  | qty | price  | latency_ms
───────────────────────┼─────────────────┼─────────┼─────┼────────┼────────
2026-04-13 10:50:35   | PositionOpened  | OPENED  |  5  | 154.65 | 50
2026-04-13 11:01:25   | PositionClosed  | CLOSED  |  5  | 156.50 | 75
```

#### Query 2: Find slow events (latency > 1 second)

```sql
SELECT
  event_time AT TIME ZONE 'America/New_York' as time,
  event_type,
  (event_payload->>'ticker') as ticker,
  total_latency_ms,
  EXTRACT(EPOCH FROM (persisted_time - event_time))::INT as pipeline_delay_s
FROM event_store
WHERE total_latency_ms > 1000
ORDER BY total_latency_ms DESC
LIMIT 20;
```

#### Query 3: Latency histogram

```sql
SELECT
  CASE
    WHEN total_latency_ms < 100 THEN '< 100ms'
    WHEN total_latency_ms < 500 THEN '100-500ms'
    WHEN total_latency_ms < 1000 THEN '500ms-1s'
    WHEN total_latency_ms < 5000 THEN '1-5s'
    ELSE '> 5s'
  END as latency_bucket,
  COUNT(*) as events,
  ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM event_store), 1) as pct
FROM event_store
GROUP BY latency_bucket
ORDER BY total_latency_ms;
```

#### Query 4: Daily trading summary

```sql
SELECT
  metric_date,
  total_trades,
  winning_trades,
  losing_trades,
  ROUND(win_rate, 1) as win_pct,
  ROUND(total_pnl, 2) as pnl,
  ROUND(max_win, 2) as biggest_win,
  ROUND(max_loss, 2) as biggest_loss
FROM daily_metrics
ORDER BY metric_date DESC
LIMIT 30;
```

## Backward Compatibility

### Keep Old Tables for Transition

During migration, you can:

1. **Dual-write** (temporary):
   ```python
   # Write to BOTH old and new systems
   old_subscriber = DBSubscriber(bus, writer)  # Old
   new_subscriber = EventSourcingSubscriber(bus, writer, session_id)  # New

   old_subscriber.register()
   new_subscriber.register()
   ```

2. **Validate** that projections match old table data
3. **Deprecate** old tables after 1-2 weeks of successful dual writes

### Migration Checklist

- [ ] Create new schema with `schema_event_sourcing.sql`
- [ ] Deploy EventSourcingSubscriber
- [ ] Update run_monitor.py to use new subscriber
- [ ] Run projections rebuild: `await ProjectionBuilder.rebuild_all()`
- [ ] Validate projection data matches old tables
- [ ] Monitor event_store latencies for 1-2 days
- [ ] Remove old DBSubscriber code
- [ ] Archive old tables (mark as deprecated)
- [ ] Update documentation

## Troubleshooting

### Issue: Projections not matching old data

**Cause**: Timestamps were wrong in old tables

**Solution**:
```python
# event_store has CORRECT timestamps, old tables have WRONG timestamps
# This is expected and correct!
SELECT COUNT(*) FROM event_store WHERE aggregate_type = 'Position'
  → Should be >> count in old position_events table
```

### Issue: Events appearing late in event_store

**Cause**: Events were in queue for a while (expected during high load)

**Solution**: Monitor latencies with:
```sql
SELECT
  AVG(total_latency_ms) as avg_latency,
  MAX(total_latency_ms) as max_latency,
  COUNT(*) FILTER (WHERE total_latency_ms > 5000) as slow_events
FROM event_store
WHERE event_time > NOW() - INTERVAL '1 hour';
```

### Issue: OPENED events still missing from old position_events

**Cause**: Old code had a bug - not all events were persisted

**Solution**: Use new event_store which doesn't have this bug. The projection will show all OPENED events correctly.

## Production Considerations

### 1. Hypertable Partitioning

```sql
-- event_store is partitioned by event_time into daily chunks
-- This improves query performance and retention management

-- Check chunk sizes
SELECT show_chunks('event_store');

-- Compress old chunks (after 30 days)
SELECT compress_chunk(chunk) FROM show_chunks('event_store')
WHERE chunk_time < NOW() - INTERVAL '30 days';
```

### 2. Retention Policy

```sql
-- Keep event_store data for 2 years, projections for 1 year
CREATE POLICY event_store_retention AS
DROP TABLE event_store WHERE event_time < NOW() - INTERVAL '2 years';
```

### 3. Backup Strategy

```bash
# Backup event_store (source of truth)
pg_dump -t event_store tradinghub > event_store_backup.sql

# Rebuild projections from backup if needed
# (This is the advantage of event sourcing - you can always rebuild)
```

## Testing Event Sourcing

```python
# test/test_event_sourcing.py

@pytest.mark.asyncio
async def test_position_opened_recorded():
    """Verify position opened events are recorded."""
    bus = EventBus()
    writer = DBWriter(asyncio.get_event_loop())
    subscriber = EventSourcingSubscriber(bus, writer, session_id='test-session')
    subscriber.register()

    # Emit position opened event
    bus.emit(Event(
        type=EventType.POSITION,
        payload=PositionPayload(
            ticker='TEST',
            action='OPENED',
            position=PositionSnapshot(...),
        )
    ))

    # Verify in event_store
    async with get_pool().acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM event_store WHERE aggregate_id = $1",
            "position_TEST"
        )

    assert count == 1, "Position opened event not recorded"

@pytest.mark.asyncio
async def test_latency_tracked():
    """Verify latencies are calculated."""
    # ... emit event ...

    # Verify latency fields are populated
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT total_latency_ms, ingest_latency_ms FROM event_store LIMIT 1"
        )

    assert row["total_latency_ms"] is not None
    assert row["ingest_latency_ms"] is not None
```

## Questions & Answers

**Q: Will this slow down trading?**
- A: No. The event store is written asynchronously by DBWriter, just like the old system.

**Q: What if the database goes down?**
- A: Events are queued in memory and flushed when DB recovers (circuit breaker handles this). No trading impact.

**Q: Can I query the event store in real-time?**
- A: Yes, it's a regular PostgreSQL table. Use the helper functions for common queries.

**Q: Do I have to rebuild projections manually?**
- A: Ideally, have a scheduled job that rebuilds daily. Or rebuild on-demand using ProjectionBuilder.

**Q: What about event versioning?**
- A: The `event_version` field tracks schema versions. You can handle migrations in the replay logic.

## Migration Completion

Once this is complete:
1. **event_store** is your source of truth (immutable)
2. **Projections** are read models (can be rebuilt anytime)
3. **Timestamps are accurate** (event_time, received_time, processed_time)
4. **Latencies are tracked** (identify slow parts of pipeline)
5. **Audit trail is complete** (every event recorded)

You can now:
- ✅ Replay events for debugging
- ✅ Query at any point in time
- ✅ Measure pipeline latencies
- ✅ Meet financial audit requirements
- ✅ Detect anomalies (e.g., events slow to persist)

