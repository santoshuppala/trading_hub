# V7: SafeStateFile — Hardened State Persistence

**Status:** Implemented and tested (88/88 checks pass)
**Files created:** `lifecycle/safe_state.py`, `test/test_safe_state.py`
**Files modified:** `monitor/state.py`, `monitor/distributed_registry.py`, `monitor/shared_cache.py`, `monitor/smart_router.py`, `lifecycle/state_persistence.py`

---

## Problem Statement

V6 state files had 5 categories of risk identified in the architectural review:

| Risk | Severity | Example |
|------|----------|---------|
| No lock on reads | P0 | `load_state()` reads `bot_state.json` without `_lock` — gets partial JSON mid-write |
| TOCTOU race | P0 | SmartRouter reads `position_broker_map` without lock — SELL routes to wrong broker |
| No corruption detection | P1 | Crash during `json.dump()` leaves truncated JSON — loaded as valid on restart |
| No staleness detection | P1 | Core crashes, satellites trade on 30s+ old cache data silently |
| No recovery from total loss | P1 | All state files deleted — system starts with empty state, orphaned broker positions |

---

## Solution: 6 Proposals Implemented

### 1. fcntl.flock on ALL Reads and Writes

**Design:** `_FcntlLock` context manager wraps every file operation.

```python
class _FcntlLock:
    def __init__(self, lock_path: str, mode: int):
        # mode: fcntl.LOCK_EX (exclusive, writes) or fcntl.LOCK_SH (shared, reads)
    def __enter__(self):
        self._fd = open(self._lock_path, 'a+')
        fcntl.flock(self._fd, self._mode)
    def __exit__(self, *args):
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._fd.close()
```

**Guarantees:**
- Writers block all other readers AND writers (exclusive)
- Readers block writers but allow concurrent readers (shared)
- Cross-process safe (fcntl is OS-level, not thread-level)
- Lock file: `{state_file}.flock` (separate from data file)

**Applied to:** `bot_state.json`, `position_registry.json`, `position_broker_map.json`, `{engine}_state.json`, `live_cache.pkl`

### 2. Monotonic Version Numbers

Every `write()` call bumps `_version` by 1. Injected into the state dict:

```json
{
  "_version": 42,
  "_timestamp": "2026-04-15T14:32:00-04:00",
  "_date": "2026-04-15",
  "positions": {...}
}
```

**APIs:**
- `sf.version()` — read current version without loading full state
- `sf.read_if_changed(last_version)` — returns `None` if version unchanged (skip processing)

**Design choice:** Version is read inside the exclusive lock before bumping, so concurrent writers always produce monotonically increasing versions.

### 3. SHA256 Checksums

Every write creates a sidecar file `{state_file}.sha256`:

```
a3f2e1d4b5c6...  (64-char hex digest of the JSON content)
```

Every read validates the checksum BEFORE parsing JSON. If mismatch → file is corrupt → fall back to `.prev`.

**Design choice:** Checksum is computed from the serialized string, not the parsed dict. This catches both content corruption and encoding issues.

### 4. Rolling Backups (.prev, .prev2)

Every write rotates: `current → .prev → .prev2` (with corresponding `.sha256` sidecars).

```
bot_state.json           ← current (v42)
bot_state.json.sha256    ← checksum for v42
bot_state.json.prev      ← previous (v41)
bot_state.json.prev.sha256
bot_state.json.prev2     ← two versions back (v40)
bot_state.json.prev2.sha256
```

**Fallback chain on read:**
1. Try primary → checksum valid? → return data
2. Try `.prev` → checksum valid? → return data (log WARNING)
3. Try `.prev2` → checksum valid? → return data (log WARNING)
4. All corrupt → return `None` (log ERROR)

**Design choice:** Three copies provide protection against single-write corruption AND a secondary corruption during recovery. Two backups handles the case where the crash happened during backup rotation itself.

### 5. Staleness Detection

`SafeStateFile` checks the `_timestamp` field against `max_age_seconds`:

```python
sf = SafeStateFile('data/cache.json', max_age_seconds=15.0)
data, is_fresh = sf.read()
if not is_fresh:
    # State is older than 15 seconds — don't trade on it
```

`StalenessGuard` wraps this for satellite main loops:

```python
guard = StalenessGuard(cache_sf, max_stale_cycles=2, alert_fn=send_alert)

# In main loop:
if not guard.check():
    break  # halted — state too old for 2 consecutive cycles
```

**Behaviors:**
- Cycle 1 stale → WARNING log, continue (within tolerance)
- Cycle 2 stale → CRITICAL alert, halt ORDER_REQ emission
- State refreshed → auto-recover, resume trading

**Applied to SharedCache:** `CacheReader.get_bars()` now returns empty dicts when `not is_fresh()`. This prevents stale-data trading (was silently using 30s+ old data in V6).

### 6. Skip-if-Unchanged (Hash Optimization)

`write()` computes SHA256 of the content (excluding metadata fields `_version`, `_timestamp`, `_date`). If the hash matches the last write, the write is skipped entirely:

```python
content_for_hash = {k: v for k, v in data.items() if not k.startswith('_')}
content_hash = hashlib.sha256(json.dumps(content_for_hash, sort_keys=True).encode()).hexdigest()
if content_hash == self._last_hash:
    return False  # no change — skip disk I/O
```

This reduces disk writes by ~90% during steady state (positions don't change every tick).

---

## Migration from V6

Each V6 state module was updated to delegate to `SafeStateFile`:

| V6 Module | V7 Change |
|-----------|-----------|
| `monitor/state.py` | `save_state()`/`load_state()` delegate to `SafeStateFile` instance |
| `monitor/distributed_registry.py` | All methods use `_sf.read()` (shared lock) or `_sf._exclusive_lock()` + `_sf.write(_already_locked=True)` |
| `monitor/shared_cache.py` | `CacheWriter` uses exclusive fcntl + checksum. `CacheReader` validates checksum, enforces staleness in `get_bars()` |
| `monitor/smart_router.py` | `_load_broker_map`/`_save_broker_map` delegate to `SafeStateFile` |
| `lifecycle/state_persistence.py` | `AtomicStateFile` delegates to `SafeStateFile` |

**Backward compatibility:** V7 reads V6 files gracefully (no `_version`/`_timestamp` → treated as version 0, freshness checked via file mtime fallback). No checksum sidecar → skips validation (first V7 write creates it).

---

## Test Coverage

| Test | Checks | What It Validates |
|------|--------|-------------------|
| T1: Basic round-trip | 11 | Write/read, versioning, skip-unchanged |
| T2: Checksums | 4 | Sidecar creation, validation, tamper detection |
| T3: Rolling backups | 8 | Rotation, .prev/.prev2 content, triple-corrupt fallback |
| T4: Version numbers | 6 | Monotonic bump, read_if_changed |
| T5: Staleness | 11 | TTL, StalenessGuard halt/recover/alert |
| T7: Threading | 3 | Concurrent read/write, zero corruption |
| T8: monitor/state.py | 7 | V7 metadata present, positions/reclaimed/trade_log preserved |
| T9: Registry | 14 | acquire/release/held_by with locks, checksums, backups, global max |
| T10: SharedCache | 8 | Checksum, version, staleness enforcement |
| T11: Lifecycle | 8 | AtomicStateFile delegates correctly |
| T12: Total deletion | 5 | Recovery from zero state files |
| T13: SmartRouter | 3 | Wiring verification |
| **Total** | **88** | |

---

## Key Design Decisions

1. **fcntl over threading.Lock** — fcntl works across processes. threading.Lock only works within one process. Since 4 processes access `position_registry.json`, fcntl is required.

2. **Shared lock on reads** — Multiple satellites can read simultaneously without blocking each other. Only writes acquire exclusive locks.

3. **`_already_locked` parameter** — `DistributedPositionRegistry.try_acquire()` needs atomic read-modify-write under a single exclusive lock. Without `_already_locked`, calling `sf.write()` inside `sf._exclusive_lock()` would deadlock (nested exclusive lock on same file).

4. **Content hash excludes metadata** — `_version`, `_timestamp`, `_date` change every write. Hashing only the business data (`positions`, `trade_log`, etc.) enables accurate skip-if-unchanged detection.

5. **Checksum as sidecar file** — Embedding checksum inside the JSON would create a chicken-and-egg problem (checksum changes the content which changes the checksum). Separate `.sha256` file avoids this.

---

*Implemented 2026-04-15. 88/88 tests passing.*
