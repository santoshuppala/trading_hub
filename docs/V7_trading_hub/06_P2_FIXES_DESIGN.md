# V7: P2 Fixes — Ordering & Consistency

**Status:** Implemented and tested (33/33 checks pass)
**Test file:** `test/test_p2_fixes.py`
**Files modified:** `scripts/run_core.py`, `monitor/brokers.py`
**Files verified (already fixed):** `monitor/event_log.py`, `db/writer.py`

---

## P2-1: IPC Consumer Inbox Queue

**Problem:** Core's IPC consumer runs in a daemon thread. When a satellite ORDER_REQ arrives via Redpanda, it calls `monitor._bus.emit()` from the consumer thread. Meanwhile, the main thread runs `emit_batch()` for local BAR events. Two threads calling `emit()` concurrently for the same ticker have no global sequencing.

**Scenario:**
```
Main thread:  emit_batch([BAR(AAPL)]) → SIGNAL(AAPL) → ORDER_REQ(AAPL)
IPC thread:   emit(ORDER_REQ(AAPL)) from Pro satellite
              ↑ races with main thread — order undefined
```

**Fix:** Consumer thread enqueues payloads into `queue.Queue(maxsize=500)`. Main loop calls `_drain_ipc_inbox()` each tick to emit on the main thread. Single-threaded injection ensures correct ordering with local event chain.

```python
# Consumer thread (daemon) — enqueue only, never emit
def _on_remote_order(key, payload):
    _ipc_inbox.put_nowait(payload)

# Main thread (tick cycle) — drain and emit
def _drain_ipc_inbox():
    while not _ipc_inbox.empty():
        payload = _ipc_inbox.get_nowait()
        monitor._bus.emit(Event(type=EventType.ORDER_REQ, payload=...))
```

**Drain position:** Called at the START of each main loop tick, before cache update. This ensures satellite ORDER_REQs are processed in the same thread context as local events.

**Overflow:** If inbox reaches 500 (maxsize), `put_nowait()` raises `queue.Full` → logged as ERROR, payload dropped. This is a backpressure signal — Core is not draining fast enough.

**Files:** `scripts/run_core.py` — `_ipc_inbox`, `_on_remote_order()` (enqueue), `_drain_ipc_inbox()` (drain), main loop updated.

---

## P2-2: Causation Chain (FILL ← ORDER_REQ)

**Problem:** FILL events had no link back to the ORDER_REQ that triggered them. Cross-EventType tracing was impossible. Handlers couldn't validate "did ORDER_REQ exist before this FILL?"

**Fix:** `_on_order_request()` stores `self._current_causation_id = event.event_id` before calling `_execute_buy()` or `_execute_sell()`. `_emit_fill()` auto-reads `_current_causation_id` and sets it as `correlation_id` on the FILL event. Cleared to `None` after execution.

**Result:** Every FILL event's `correlation_id` points to the ORDER_REQ `event_id` that caused it:
```
ORDER_REQ (event_id=abc123)
    → AlpacaBroker._execute_buy()
    → FILL (event_id=def456, correlation_id=abc123)
        ↑ links back to ORDER_REQ
```

**Tracing chain:**
```
SIGNAL.event_id → ORDER_REQ.correlation_id (set by RiskEngine)
ORDER_REQ.event_id → FILL.correlation_id (set by broker, V7)
FILL.event_id → POSITION.correlation_id (set by PositionManager, existing)
```

**All 7 `_emit_fill()` calls** in AlpacaBroker (BUY fill, SELL fill, partial fills, recovery fills) automatically carry the causation_id — no per-call changes needed.

**Files:** `monitor/brokers.py` — `_on_order_request()` sets `_current_causation_id`, `_emit_fill()` reads it via `getattr(self, '_current_causation_id', None)`.

---

## P2-3: Crash Recovery Sorts by stream_seq (Already Fixed)

**Verified:** `EventLogConsumer.replay()` in `monitor/event_log.py` already collects all records into a list and sorts by `_sort_key`:

```python
def _sort_key(r):
    seq = r.get('stream_seq')
    if seq is not None:
        return (0, int(seq), '')       # primary: stream_seq (monotonic)
    return (1, 0, r.get('timestamp', ''))  # fallback: ISO timestamp
```

**Guarantees:**
- Events with `stream_seq` sort before those without (tier 0 vs tier 1)
- Within tier 0: sorted by monotonic sequence number (causal order)
- Within tier 1: sorted by ISO timestamp string (lexicographic = chronological)
- Result: ORDER_REQ always appears before its FILL in replay

---

## P2-4: Projection Table Dedup (Already Fixed)

**Verified:** `db/writer.py` uses `ON CONFLICT DO NOTHING` for ALL parameterized INSERTs:

```python
sql = f"INSERT INTO trading.{table} ({col_list}) VALUES ({val_refs}) ON CONFLICT DO NOTHING"
```

**Guarantees:**
- `event_store` has `event_id UUID PRIMARY KEY` → rejects duplicate events
- Projection tables (signal_events, fill_events, etc.) use same INSERT pattern
- `completed_trades` uses `trade_id UUID PRIMARY KEY`
- Duplicate events from Redpanda replay → silently rejected at DB level

---

*Implemented 2026-04-15. 33/33 tests passing.*
