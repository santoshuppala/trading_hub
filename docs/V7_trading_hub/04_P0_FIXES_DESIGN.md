# V7: P0 Fixes — Stop Losing Money

**Status:** Implemented and tested (32/32 checks pass)
**Test file:** `test/test_p0_fixes.py`
**Files modified:** `monitor/brokers.py`, `monitor/smart_router.py`, `monitor/ipc.py`

---

## P0-1: BUY Order Idempotency

**Problem:** `AlpacaBroker._execute_buy()` had no `client_order_id`. Crash after submit but before FILL confirmation → restart + replay → second real order at broker.

**Fix:** Deterministic `client_order_id` derived from ORDER_REQ `event.event_id`:
```python
client_oid = f"th-buy-{p.ticker}-{event_id[:12]}-{attempt}"
```
Alpaca rejects duplicate `client_order_id` → no double orders on replay.

**Files:** `monitor/brokers.py` — `_on_order_request()` sets `_source_event_id` on payload via `object.__setattr__` (frozen dataclass). `_execute_buy()` uses it for `client_order_id`.

---

## P0-2: SmartRouter ORDER_REQ Dedup

**Problem:** `SmartRouter._on_order_req()` had no tracking of previously-routed event IDs. Replayed ORDER_REQ routes to broker again.

**Fix:** LRU `_routed_event_ids` dict (bounded to 5000). Skip if `event.event_id` already seen. Pruned to 2500 when capacity exceeded.

**Files:** `monitor/smart_router.py` — `_routed_event_ids` dict added to `__init__`, checked at top of `_on_order_req()`.

---

## P0-3: position_broker_map TOCTOU

**Problem:** `SmartRouter._select_broker()` read broker mapping without lock. SELL could route to wrong broker.

**Fix:** Already resolved by SafeStateFile (fcntl locks on all reads and writes).

**Files:** `monitor/smart_router.py` — `_broker_map_sf = SafeStateFile(...)`.

---

## P0-4: Redpanda Manual Commit

**Problem:** `enable.auto.commit=True` with `auto.commit.interval.ms=3000`. Handler processes event → crash before 3s commit → event replayed.

**Fix:** `enable.auto.commit=False`. Manual `self._consumer.commit(msg, asynchronous=True)` called AFTER handler succeeds. On handler error, offset NOT committed → message replayed → caught by P0-2 dedup + P0-1 client_order_id.

**Files:** `monitor/ipc.py` — Consumer config changed, commit added after handler call in `_consume_loop()`.

---

## P0-5: No Optimistic SELL FILL

**Problem:** If SELL not confirmed within 5s, broker emitted FILL optimistically — assuming order filled when it may not have.

**Fix:** Removed all optimistic FILL paths. Extended verify loop: 3 retries × 1s each (8s total extra). Terminal status (cancelled/expired/rejected) → ORDER_FAIL. Unverified after all retries → ORDER_FAIL + CRITICAL alert for manual check.

**Files:** `monitor/brokers.py` — `_execute_sell()` rewritten with verify loop.

---

## Defense in Depth

P0-1 + P0-2 + P0-4 form a triple safety net for crash replay:
1. Redpanda replays message (P0-4 didn't commit)
2. SmartRouter dedup catches duplicate event_id (P0-2)
3. Even if dedup misses, Alpaca rejects duplicate `client_order_id` (P0-1)

---

*Implemented 2026-04-15. 32/32 tests passing.*
