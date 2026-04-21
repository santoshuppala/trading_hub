# V7: P1 Fixes — Stop Silent Failures

**Status:** Implemented and tested (30/30 checks pass)
**Test file:** `test/test_p1_fixes.py`
**Files modified:** `monitor/shared_cache.py`, `options/chain.py`, `monitor/event_bus.py`, `monitor/portfolio_risk.py`

---

## P1-6: Pickle Bounded Read

**Problem:** `pickle.load(f)` on a corrupt file can hang indefinitely (malformed stream, infinite loop in unpickler).

**Fix:** Read raw bytes first with bounded `f.read(_MAX_CACHE_BYTES)` (50MB cap), then deserialize from memory with `pickle.loads(raw)`. File I/O is bounded by file size. Memory deserialization can't block on I/O.

Checksum path already reads raw bytes for validation → `pickle.loads(raw)` used in both paths.

**Files:** `monitor/shared_cache.py` — `_MAX_CACHE_BYTES = 50 * 1024 * 1024`, pre-V7 path uses `f.read(_MAX_CACHE_BYTES)`.

---

## P1-7: Option Chain Snapshot Timeout

**Problem:** `_get_snapshots_batched()` calls Alpaca SDK with no timeout per batch. If Alpaca API hangs, Options engine blocks indefinitely.

**Fix:** `concurrent.futures.ThreadPoolExecutor` wraps each SDK call with `future.result(timeout=15.0)`. On timeout, batch is skipped (don't retry hangs — API is likely unresponsive). Retries still happen for 429 rate limits.

**Files:** `options/chain.py` — `_SNAPSHOT_TIMEOUT_SEC = 15.0`, `TimeoutError` caught and breaks retry loop.

---

## P1-8: Handler Execution Timeout

**Problem:** EventBus `_deliver()` measures handler duration after completion (`SLOW_THRESHOLD_SEC = 0.50`) but cannot kill a hung handler. Stuck handler blocks entire partition queue for that ticker.

**Fix:** `HANDLER_TIMEOUT_SEC = 30.0` (configurable). When > 0, handler call wrapped in `concurrent.futures.ThreadPoolExecutor.submit()` + `future.result(timeout=...)`. On timeout, `TimeoutError` raised → counted as handler failure → circuit breaker triggers after 5 consecutive failures. Set to 0 for V6 backward compatibility (direct call, no timeout).

**Design choice:** 30s is generous — covers broker API calls (5s fill poll + retries), option chain fetches (15s), and DB writes (batch flush). Handlers that legitimately take >30s should be refactored to use async patterns.

**Files:** `monitor/event_bus.py` — `HANDLER_TIMEOUT_SEC = 30.0`, timeout wrapper in `_deliver()`.

---

## P1-9: Portfolio Risk Fail-Closed

**Problem:** If all 4 buying-power queries timeout (3s each), `_get_aggregate_buying_power()` returns `None`. Old code: `if buying_power is not None and order_notional > buying_power:` — silently SKIPPED margin check on None. Order passed without validation.

**Fix:** Fail-closed: if `buying_power is None`, BLOCK the order. CRITICAL alert sent: "BUYING POWER UNAVAILABLE". Old fail-open pattern (`if bp is not None and ...`) replaced with explicit None check first.

**Principle:** Unknown state = assume unsafe. Never pass an order through an unverifiable risk gate.

**Files:** `monitor/portfolio_risk.py` — explicit `if buying_power is None: self._block(...)` before comparison.

---

## P1-10: Cache TTL Enforced (Previously Fixed)

Already fixed in SafeStateFile work. `CacheReader.get_bars()` returns empty dicts when `not self.is_fresh()`. TTL reduced from 30s to 15s.

---

## P1-11: bot_state.json Read Lock (Previously Fixed)

Already fixed in SafeStateFile work. `monitor/state.py` delegates to `SafeStateFile` which uses fcntl shared lock on reads.

---

*Implemented 2026-04-15. 30/30 tests passing.*
