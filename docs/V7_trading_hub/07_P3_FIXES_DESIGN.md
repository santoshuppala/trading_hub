# V7: P3 Fixes — Observability

**Status:** Implemented and tested (43/43 checks pass)
**Test file:** `test/test_p3_fixes.py`
**Files created:** `monitor/metrics.py`
**Files modified:** `monitor/ipc.py`, `monitor/observability.py`, `scripts/supervisor.py`, `scripts/run_core.py`, `scripts/run_pro.py`, `scripts/run_pop.py`

---

## P3-1: Correlation ID Across IPC Boundary

**Problem:** `EventPublisher.publish()` serialized `{source, timestamp, payload}` but NOT `correlation_id`. When an ORDER_REQ crossed from Pro/Pop to Core via Redpanda, the correlation chain broke. No way to trace an order across process boundaries.

**Fix:** Three changes:
1. `EventPublisher.publish()` accepts `correlation_id` parameter, includes it in envelope
2. `EventConsumer._consume_loop()` extracts `correlation_id` from envelope, injects as `_ipc_correlation_id` into payload
3. Satellites pass `correlation_id=event.event_id` when publishing ORDER_REQ
4. Core's `_drain_ipc_inbox()` propagates `_ipc_correlation_id` to the emitted Event

**Tracing chain (end-to-end):**
```
Pro: SIGNAL(eid=A) → ORDER_REQ(eid=B, corr=A)
  → IPC publish(correlation_id=B)
  → Redpanda envelope: {correlation_id: B, payload: {...}}
Core: _drain_ipc_inbox → Event(ORDER_REQ, correlation_id=B)
  → RegistryGate → SmartRouter → Broker
  → FILL(eid=C, correlation_id=B)  [from P2-2 causation chain]
  → POSITION(eid=D, correlation_id=C)
```

---

## P3-2: Health Check (Heartbeat File + Supervisor Hung Detection)

**Problem:** Supervisor monitored children by PID and exit code only. A hung process (infinite loop, deadlocked thread) appears "running" — no detection.

**Fix (two-part):**

**Part A — HeartbeatEmitter writes `data/heartbeat.json`:**
```json
{
  "timestamp": "2026-04-15T14:32:00-04:00",
  "wall_clock": 1713203520.0,
  "n_tickers": 183,
  "last_signal_age_sec": 45.2,
  "last_fill_age_sec": 312.7
}
```
Written atomically (tmp + replace) every 60s by `_write_heartbeat_file()`.

**Part B — Supervisor reads heartbeat file in `check()`:**
- If `time.time() - mtime(heartbeat.json) > 120s` → returns `'hung'`
- On `'hung'`: force-kill via `SIGKILL` to process group, then restart
- `_HEARTBEAT_STALE_SEC = 120.0` (2x heartbeat interval)

---

## P3-3: Metrics Export

**Problem:** EventBus tracked latencies, error counts, circuit breaker states internally but didn't expose them. No monitoring visibility without log parsing.

**Fix:** New `monitor/metrics.py` — `MetricsWriter` class:
- Reads `bus.metrics()` every 60s
- Writes to `data/metrics.json` (atomic tmp + replace)
- Exposed fields: emit_counts, handler_avg_ms, handler_max_ms, handler_errors, handler_circuit, slow_calls, circuit_breaks, active_breaks, queue_depths, system_pressure, sla_breaches, dlq_count, duplicate_events_dropped, retried_deliveries
- `read()` method for dashboard consumption

**No new infrastructure** — file-based, same pattern as heartbeat.json and state files.

---

## P3-4: Silent Failure Detection

**Problem:** If StrategyEngine stops emitting SIGNAL events (bug, data feed stall), no alert fires. Heartbeats continue, system looks healthy, but generates zero trades.

**Fix:** HeartbeatEmitter now tracks event recency:
- Subscribes to `EventType.SIGNAL` and `EventType.FILL` at priority 0
- `_on_signal()` / `_on_fill()` update `_last_signal_time` / `_last_fill_time`
- `_check_silent_failures()` called every heartbeat tick:
  - If `signal_age > 30 minutes` during market hours → CRITICAL alert
  - If `fill_age > 60 minutes` during market hours → WARNING alert
  - Alert resets when a new SIGNAL/FILL arrives
  - Market hours guard: only checks 10:00 AM - 3:45 PM ET

**Thresholds:**
- `_SIGNAL_SILENCE_ALERT_SEC = 1800` (30 minutes)
- `_FILL_SILENCE_ALERT_SEC = 3600` (60 minutes — fills are rarer than signals)

---

*Implemented 2026-04-15. 43/43 tests passing.*
