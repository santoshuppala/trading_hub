# V7: P5 Fixes — Operational Hardening

**Status:** Implemented and tested (35/35 checks pass)
**Test file:** `test/test_p5_fixes.py`
**Files modified:** `monitor/event_bus.py`, `db/migrations/run.py`

---

## P5-1: Backward-Compatible Migration Safety Validator

**Problem:** If a migration adds a NOT NULL column without DEFAULT while the system is running, all writes fail. DBWriter circuit breaker opens. Events dropped permanently.

**Fix:** `validate_migration(filename, content)` in `db/migrations/run.py` checks SQL for 5 dangerous patterns before applying:

| Pattern | Danger | Safe Alternative |
|---------|--------|-----------------|
| `NOT NULL` without `DEFAULT` | Breaks running INSERTs | `ADD COLUMN col TYPE DEFAULT val` then `SET NOT NULL` after backfill |
| `DROP TABLE` | Destroys data permanently | RENAME + scheduled drop |
| `DROP COLUMN` | Destroys data | Add new column, backfill, deprecate old |
| `RENAME COLUMN` | Breaks running queries | Add new + backfill + drop old |
| `ALTER TYPE` | Can fail on existing data | Add column with correct type |

**Override mechanism:** Add `-- V7_OVERRIDE:PATTERN` comment to acknowledge risk:
```sql
-- V7_OVERRIDE:DROP TABLE
DROP TABLE legacy_data;  -- acknowledged: data already migrated to new_data
```

**Integration:** Called before each migration in `run_migrations()`. Blocks application with `RuntimeError` if unsafe patterns found.

---

## P5-2: Event Payload Versioning

**Problem:** Event payloads have no version field. If `FillPayload` gains a new required field, old events in the event store can't be deserialized. Projection rebuilds break.

**Fix:** New `payload_version: int = 1` field on `Event` dataclass:

```python
@dataclass
class Event:
    type: EventType
    payload: Any
    ...
    payload_version: int = 1  # V7: increment when payload fields change
```

**Usage for schema evolution:**
- Current events: `payload_version=1`
- After adding fields: `payload_version=2`
- Projection builder checks version and handles old events:
  ```python
  if event.payload_version < 2:
      # Old event — field X doesn't exist, use default
      value = payload.get('new_field', DEFAULT)
  ```

**Backward compat:** Default is 1. All existing events (no version field) deserialize as version 1. No breaking change.

---

## P5-3: Chaos/Failure Injection Tests

6 chaos scenarios tested:

### P5-3a: State File Deletion Recovery
- Delete ALL state files (primary + .prev + .prev2 + checksums)
- Verify: `read()` returns None (no crash), system can write fresh state

### P5-3b: Corrupt State File Recovery
- Write 3 versions (creates full backup chain)
- Corrupt primary (truncated JSON + bad checksum)
- Verify: falls back to .prev
- Corrupt .prev too → falls back to .prev2

### P5-3c: Broker API Timeout Handling
- Register handler that sleeps 100s (simulates hung broker API)
- Set `HANDLER_TIMEOUT_SEC = 0.5`
- Verify: handler times out in <1.5s, doesn't hang process

### P5-3d: Concurrent Registry Access
- 4 threads, each acquiring 10 tickers (40 total)
- All running simultaneously against same registry file
- Verify: zero errors, all 40 acquired, no duplicates

### P5-3e: Stale Cache Trading Prevention
- Core writes cache, satellite reads (OK)
- Core stops writing (crash simulation), wait for TTL
- Verify: `get_bars()` returns EMPTY (not stale data)
- Core recovers → satellite gets fresh data again

### P5-3f: FILL Dedup on Replay
- Emit same FILL event_id 3 times (Redpanda replay simulation)
- Verify: EventBus idempotency window delivers only 1

---

*Implemented 2026-04-15. 35/35 tests passing.*
