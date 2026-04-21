# Phase 1 Deployment: Event Sourcing Migration

**Status**: Ready for deployment
**Date**: 2026-04-13
**Components**: EventSourcingSubscriber + event_store schema + ProjectionBuilder
**Estimated duration**: 30 minutes
**Risk level**: Low (non-breaking, works alongside existing code)

---

## Pre-Deployment Checklist

- [x] `db/event_sourcing_subscriber.py` — Created (450 lines, syntax verified)
- [x] `db/projection_builder.py` — Created (500 lines, syntax verified)
- [x] `db/schema_event_sourcing.sql` — Created (420 lines)
- [x] `run_monitor.py` — Updated to import EventSourcingSubscriber (line 80, 174)
- [x] Documentation complete (README, migration guide, before/after comparison)

---

## Deployment Steps

### Step 1: Deploy Database Schema (5 minutes)

**Location**: `db/schema_event_sourcing.sql`

This creates:
- `event_store` table (immutable append-only)
- 5 projection tables (position_state, completed_trades, signal_history, fill_history, daily_metrics)
- Helper functions and indexes

```bash
# SSH to database server
psql -d tradinghub -f db/schema_event_sourcing.sql

# Expected output: CREATE TABLE, CREATE INDEX, CREATE FUNCTION messages
# Total execution time: ~10-15 seconds

# Verify table created
psql -d tradinghub -c "SELECT COUNT(*) FROM event_store;"
# Should return: 0 (empty table, ready for events)
```

**What happens if it fails**:
- If `event_store` already exists: Script includes `IF NOT EXISTS` — safe to re-run
- If TimescaleDB not installed: Install with `CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE`
- If permissions error: Ensure user has CREATE TABLE privilege

---

### Step 2: Verify run_monitor.py Integration (2 minutes)

The following changes have already been made:

```python
# Line 80: Import EventSourcingSubscriber instead of DBSubscriber
from db.event_sourcing_subscriber import EventSourcingSubscriber

# Line 174: Same import in the DB subscriber section
from db.event_sourcing_subscriber import EventSourcingSubscriber

# Line 175: Instantiate with session_id for tracing
db_sub = EventSourcingSubscriber(bus=monitor._bus, writer=db_writer, session_id=session_id)

# Line 176-177: Register and log
db_sub.register()
log.info("EventSourcingSubscriber registered — all events persisted with correct timestamps")
```

**Check**: Start the monitor and verify log shows:
```
EventSourcingSubscriber registered — all events persisted with correct timestamps
```

---

### Step 3: Run the Monitor (continuous monitoring)

```bash
# Start monitor normally
python run_monitor.py

# Monitor logs should show:
# ✓ "EventSourcingSubscriber registered — all events persisted with correct timestamps"
# ✓ BAR events being processed (timestamps = event.timestamp, not _NOW())
# ✓ POSITION events with correct action (OPENED, CLOSED, PARTIAL_EXIT)
# ✓ SIGNAL events from Pro/Pop/Options engines
```

**Example log entries** (correct timestamps):
```
2026-04-13 09:35:15,247 INFO EventSourcingSubscriber registered
2026-04-13 09:35:25,102 INFO [BAR] AAPL | close=$150.23 | volume=1243 | event_time=09:35:25
2026-04-13 09:36:47,892 INFO [POSITION] ARM action=OPENED qty=5 entry=$154.65 | event_time=09:36:47
2026-04-13 09:45:32,165 INFO [SIGNAL] AMD strategy=momentum_ignition action=BUY | event_time=09:45:32
```

---

### Step 4: Rebuild Projections (5 minutes)

After the monitor has been running for 1-2 hours and event_store is populated:

```python
# In Python REPL or as part of end-of-day script
from db.projection_builder import ProjectionBuilder
import asyncio

async def rebuild():
    await ProjectionBuilder.rebuild_all()
    print("✓ All projections rebuilt from events")

asyncio.run(rebuild())
```

**What gets rebuilt**:
1. `position_state` — Current open positions
2. `completed_trades` — Closed trades with P&L
3. `signal_history` — All signals emitted
4. `fill_history` — All fills executed
5. `daily_metrics` — Daily aggregated stats

**Verify**:
```sql
-- Check events persisted
SELECT COUNT(*) FROM event_store;
-- Should be > 0 (depends on trading duration)

-- Check projections
SELECT COUNT(*) FROM completed_trades;
SELECT * FROM daily_metrics ORDER BY metric_date DESC LIMIT 5;

-- Check event types recorded
SELECT DISTINCT event_type FROM event_store ORDER BY event_type;
```

---

## Verification Queries

### Query 1: Event Timeline Accuracy

```sql
-- Verify event_time is CORRECT (not NOW() like old system)
SELECT
  event_type,
  event_time,
  received_time,
  processed_time,
  EXTRACT(EPOCH FROM (processed_time - event_time)) * 1000 as ingest_latency_ms,
  EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - event_time)) * 1000 as age_ms
FROM event_store
WHERE aggregate_type = 'Position'
ORDER BY event_time DESC
LIMIT 10;
```

**Expected output**: `event_time` should match when trades actually happened (e.g., 10:50:35 for an ARM fill), not the database write time (13:15:02).

### Query 2: Event Types Recorded

```sql
SELECT event_type, COUNT(*) as count
FROM event_store
GROUP BY event_type
ORDER BY count DESC;
```

**Expected output**: Should see all event types:
- PositionOpened, PositionClosed, PartialExited
- FillExecuted
- StrategySignal
- ProStrategySignal, PopStrategySignal
- BarReceived
- etc.

**⚠️ If OPENED events missing**: This was the previous bug. Verify that PositionOpened count > 0.

### Query 3: Pipeline Latency

```sql
SELECT
  ROUND(AVG(EXTRACT(EPOCH FROM (processed_time - event_time)) * 1000)::numeric, 2) as avg_ingest_ms,
  ROUND(MAX(EXTRACT(EPOCH FROM (processed_time - event_time)) * 1000)::numeric, 2) as max_ingest_ms,
  ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (processed_time - event_time)) * 1000)::numeric, 2) as p95_ingest_ms
FROM event_store
WHERE event_time > CURRENT_TIMESTAMP - INTERVAL '1 hour';
```

**Expected**: Ingest latency (event received to processed) should be < 500ms (typically 10-100ms).

### Query 4: Projection Accuracy

```sql
-- Compare completed_trades projection with manual count
SELECT COUNT(*) FROM completed_trades;

-- Verify P&L calculation
SELECT SUM(pnl) as total_pnl, COUNT(*) as trades, ROUND(COUNT(*)::float / COUNT(*) FILTER (WHERE pnl > 0) * 100, 1) as win_pct
FROM completed_trades;
```

---

## Troubleshooting

### Issue: No events appearing in event_store after 1 hour

**Diagnosis**:
```sql
SELECT COUNT(*) FROM event_store;
-- If 0, then no events persisted
```

**Solutions**:
1. Check log for "EventSourcingSubscriber registered" message
2. Verify db_writer is running (`grep "DB layer ready" logs/monitor_*.log`)
3. Check for errors in event handlers (`grep "error" logs/monitor_*.log`)
4. Restart monitor: `killall python && python run_monitor.py`

### Issue: Old timestamp bug (events dated at 13:15 instead of 10:50)

**Diagnosis**:
```sql
SELECT event_time, processed_time FROM event_store LIMIT 5;
-- If event_time = processed_time, EventSourcingSubscriber is not being used
```

**Solution**: Verify run_monitor.py line 80 is importing `EventSourcingSubscriber`, not `DBSubscriber`.

### Issue: event_store table not found

**Solution**:
```bash
psql -d tradinghub -f db/schema_event_sourcing.sql
```

Verify with:
```bash
psql -d tradinghub -c "\dt event_store"
```

---

## Expected Results After Deployment

✅ All events persisted with correct timestamps (event_time = when it happened, not when written to DB)
✅ OPENED events recorded (previously missing)
✅ Pipeline latency visible (ingest, queue, processing latencies tracked)
✅ Event correlations preserved (correlation_id links related events)
✅ Session tracking enabled (which monitor session created each event)
✅ Projections rebuilt successfully
✅ Audit trail complete and accurate

---

## Files Modified

| File | Change | Impact |
|------|--------|--------|
| `run_monitor.py` | Import EventSourcingSubscriber | DB events now use correct timestamps |
| (No changes needed to strategy engines, brokers, or other files) | | Event sourcing integrated non-breaking |

---

## Files Created

| File | Purpose | Lines |
|------|---------|-------|
| `db/schema_event_sourcing.sql` | Database schema with event_store | 420 |
| `db/event_sourcing_subscriber.py` | Event subscriber with correct timestamps | 450 |
| `db/projection_builder.py` | Rebuild read models from events | 500 |
| `docs/README_EVENT_SOURCING.md` | Quick start guide | 350+ |
| `docs/EVENT_SOURCING_IMPLEMENTATION.md` | Architecture design | 400 |
| `docs/EVENT_SOURCING_MIGRATION.md` | Migration guide | 350 |
| `docs/BEFORE_AFTER_COMPARISON.md` | Visual before/after comparison | 300+ |
| `docs/COMPREHENSIVE_EVENT_ARCHITECTURE.md` | Advanced architecture design | 400+ |

---

## Next Steps After Phase 1

Once Phase 1 is verified working (events persisted with correct timestamps for 1-2 trading days):

1. **Phase 2**: Extend event_store with additional fields (event_category, causation_chain, tags, severity)
2. **Phase 3**: Implement advanced projections (realtime_state, operation_timeline, causality_chain)
3. **Phase 4**: Build real-time Streamlit dashboard for event visualization

---

## Success Criteria

You'll know Phase 1 is complete and working when:

1. ✅ Monitor starts without errors
2. ✅ Log shows "EventSourcingSubscriber registered — all events persisted with correct timestamps"
3. ✅ `SELECT COUNT(*) FROM event_store` returns > 0 after 1 hour
4. ✅ `SELECT COUNT(*) FROM event_store WHERE event_type = 'PositionOpened'` returns > 0 (not missing OPENED events)
5. ✅ Event timestamps match when trades actually happened (no 3-hour offsets)
6. ✅ Projections rebuild without errors
7. ✅ No warnings or errors in logs related to event persistence

---

**Ready to deploy!** 🚀

Run Step 1 (schema deployment) when your database is ready, then follow Steps 2-4 as the monitor runs.

For questions, see:
- `docs/EVENT_SOURCING_MIGRATION.md` — Step-by-step with FAQ
- `docs/BEFORE_AFTER_COMPARISON.md` — Real example of the bug fix
- `docs/README_EVENT_SOURCING.md` — Quick reference
