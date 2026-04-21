# Trading Hub V7.0 — Design Documentation

**Based on:** V6 architectural review (27 findings, 6 severity levels)
**Principle:** Fix the system without new infrastructure. No Redis, no new message brokers, no new databases.

---

## Documents

| # | Document | Status | Tests |
|---|----------|--------|-------|
| 0 | [V7 Architectural Review](V7_ARCHITECTURAL_REVIEW.md) | Complete | — |
| 1 | [SafeStateFile Design](01_SAFE_STATE_FILE_DESIGN.md) | Implemented | 88/88 pass |
| 2 | [Centralized Registry Design](02_CENTRALIZED_REGISTRY_DESIGN.md) | Implemented | 46/46 pass |
| 3 | [EventBus Priority Map](03_EVENTBUS_PRIORITY_MAP.md) | Implemented | 31/31 pass |
| 4 | [P0 Fixes — Stop Losing Money](04_P0_FIXES_DESIGN.md) | Implemented | 32/32 pass |
| 5 | [P1 Fixes — Stop Silent Failures](05_P1_FIXES_DESIGN.md) | Implemented | 30/30 pass |
| 6 | [P2 Fixes — Ordering & Consistency](06_P2_FIXES_DESIGN.md) | Implemented | 33/33 pass |
| 7 | [P3 Fixes — Observability](07_P3_FIXES_DESIGN.md) | Implemented | 43/43 pass |
| 8 | [P4 Fixes — Extensibility](08_P4_FIXES_DESIGN.md) | Implemented | 49/49 pass |
| 9 | [P5 Fixes — Operational Hardening](09_P5_FIXES_DESIGN.md) | Implemented | 35/35 pass |

**Total tests: 387/387 passing — ALL P0-P5 COMPLETE**

---

## What V7 Fixes (Implemented)

### State File Hardening (docs 01)

| V6 Problem | V7 Fix |
|-----------|--------|
| `load_state()` reads without lock — partial JSON | fcntl shared lock on ALL reads |
| `position_broker_map` TOCTOU — SELL to wrong broker | SafeStateFile with fcntl on SmartRouter |
| No corruption detection — truncated JSON loaded | SHA256 checksum sidecar validated before parse |
| Crash loses state — no backup | Rolling backups: current → .prev → .prev2 |
| Cache 30s stale — satellites trade on old data | `get_bars()` returns empty when stale (TTL 15s) |
| Total state deletion — orphaned broker positions | Graceful recovery to empty state |

### Centralized Registry (docs 02)

| V6 Problem | V7 Fix |
|-----------|--------|
| 4 processes write position_registry.json | Only Core writes (RegistryGate) |
| Lock file deletion race | Satellites read-only, no cross-process write contention |
| No single writer for shared state | Core is authoritative, satellites do pre-flight reads |

### EventBus Priority (docs 03)

| V6 Problem | V7 Fix |
|-----------|--------|
| PortfolioRiskGate at priority 0 — runs AFTER SmartRouter | Moved to priority 3 — runs BEFORE |
| No documented priority convention | Full priority map + enforcement test |
| Priority semantics undocumented | "Higher number = runs first" documented and tested |

### P0 — Stop Losing Money (docs 04)

| V6 Problem | V7 Fix |
|-----------|--------|
| BUY no client_order_id — duplicate orders on replay | Deterministic `th-buy-{ticker}-{event_id}-{attempt}` |
| SmartRouter no ORDER_REQ dedup | LRU `_routed_event_ids` (bounded 5000) |
| Redpanda auto-commit race — 3s replay window | Manual commit after handler success |
| Optimistic SELL FILL — emit when status unknown | Extended verify (3 retries, 8s), ORDER_FAIL on unverified |

### P1 — Stop Silent Failures (docs 05)

| V6 Problem | V7 Fix |
|-----------|--------|
| pickle.load() can hang on corrupt file | Bounded `f.read(_MAX_CACHE_BYTES)` + `pickle.loads` |
| Option chain snapshot no timeout | `concurrent.futures` with 15s timeout per batch |
| Handler execution no timeout — blocks partition queue | `HANDLER_TIMEOUT_SEC=30.0` via futures in `_deliver()` |
| Portfolio risk silently skips margin check on None | Fail-closed: block order + CRITICAL alert |

### P2 — Ordering & Consistency (docs 06)

| V6 Problem | V7 Fix |
|-----------|--------|
| IPC consumer injects on daemon thread — races with main loop | Inbox queue: consumer enqueues, main loop drains |
| No causation chain — FILL doesn't link to ORDER_REQ | `_current_causation_id` → FILL.correlation_id |
| Crash recovery assumes ordering | Already fixed: replay() sorts by stream_seq |
| Projection table dedup | Already fixed: ON CONFLICT DO NOTHING |

---

## V7 Implementation Summary

### New Files Created

| File | Purpose |
|------|---------|
| `lifecycle/safe_state.py` | SafeStateFile + StalenessGuard (6 hardening proposals) |
| `monitor/registry_gate.py` | Centralized cross-layer dedup at Core |
| `test/test_safe_state.py` | 88 checks — state hardening |
| `test/test_registry_gate.py` | 46 checks — centralized registry |
| `test/test_priority_enforcement.py` | 31 checks — priority convention |
| `test/test_p0_fixes.py` | 32 checks — idempotency, dedup, commit, SELL verify |
| `test/test_p1_fixes.py` | 30 checks — timeouts, fail-closed, TTL |
| `test/test_p2_fixes.py` | 33 checks — inbox queue, causation chain, ordering |
| `test/test_p3_fixes.py` | 43 checks — correlation IDs, health checks, metrics, silence |
| `monitor/metrics.py` | MetricsWriter: EventBus metrics → JSON file |
| `test/test_p4_fixes.py` | 49 checks — generic brokers, engine config, registries, validation |
| `test/test_p5_fixes.py` | 35 checks — migration safety, versioning, chaos tests |
| `docs/V7_trading_hub/` | 10 design documents + README |

### Files Modified

| File | Change |
|------|--------|
| `monitor/state.py` | Delegates to SafeStateFile |
| `monitor/distributed_registry.py` | All reads use shared fcntl lock, writes use SafeStateFile |
| `monitor/shared_cache.py` | fcntl locks, checksums, staleness enforcement, bounded pickle read |
| `monitor/smart_router.py` | SafeStateFile, RegistryGate, ORDER_REQ dedup, generic brokers= |
| `monitor/portfolio_risk.py` | Priority 0→3, fail-closed on None buying power |
| `monitor/events.py` | Added `layer` field to OrderRequestPayload |
| `monitor/event_bus.py` | HANDLER_TIMEOUT_SEC=30s via concurrent.futures in _deliver() |
| `monitor/risk_engine.py` | try_acquire → read-only held_by, layer='vwap' |
| `monitor/position_manager.py` | Removed registry.release (RegistryGate handles it) |
| `monitor/brokers.py` | Deterministic client_order_id, causation chain, no optimistic SELL |
| `monitor/ipc.py` | Manual commit, correlation_id in envelope |
| `monitor/observability.py` | Heartbeat file, silent failure detection |
| `lifecycle/state_persistence.py` | Delegates to SafeStateFile |
| `pro_setups/risk/risk_adapter.py` | Read-only registry, layer='pro' |
| `pop_strategy_engine.py` | Read-only registry, removed release calls |
| `options/chain.py` | 15s timeout on snapshot batches |
| `scripts/run_core.py` | RegistryGate, IPC inbox queue, correlation_id propagation |
| `scripts/supervisor.py` | Heartbeat-based hung detection, force-kill on stale |
| `scripts/run_pro.py` | layer='pro' + correlation_id in IPC payload |
| `scripts/run_pop.py` | layer='pop' + correlation_id in IPC payload |
| `pop_screener/strategy_router.py` | STRATEGY_REGISTRY at module level (pluggable) |
| `config.py` | Validation block: fail-fast ValueError on invalid config |

### P3 — Observability (docs 07)

| V6 Problem | V7 Fix |
|-----------|--------|
| Correlation ID lost across Redpanda IPC | correlation_id in envelope, propagated through consumer |
| No health check — supervisor can't detect hung process | heartbeat.json + supervisor reads mtime, force-kills if >120s |
| No metrics export — internal only | MetricsWriter: EventBus metrics → data/metrics.json every 60s |
| No silent failure detection — zero trades, no alert | HeartbeatEmitter tracks SIGNAL/FILL recency, CRITICAL alert if >30min |

---

### P4 — Extensibility (docs 08)

| V6 Problem | V7 Fix |
|-----------|--------|
| SmartRouter hardcodes alpaca/tradier params | `brokers=` dict param, `_register_broker()`, generic `has_position()` |
| Supervisor hardcodes 4 engines | `_load_engine_config()`, `ENGINE_CONFIG` env var, auto-discover `run_*.py` |
| Pop strategies hardcoded in `__init__` | Module-level `STRATEGY_REGISTRY` dict (same as Options) |
| No config validation — invalid env vars silently propagate | `_validate_positive/range/choice` + fail-fast `ValueError` on startup |

### P5 — Operational Hardening (docs 09)

| V6 Problem | V7 Fix |
|-----------|--------|
| Migrations can break running system (NOT NULL, DROP TABLE) | `validate_migration()` blocks dangerous SQL, override with `V7_OVERRIDE` |
| No event payload versioning — old events break on replay | `payload_version: int = 1` on Event dataclass |
| No chaos/failure tests | 6 scenarios: state deletion, corruption, broker timeout, concurrent registry, stale cache, FILL replay dedup |

---

## How to Run All V7 Tests

```bash
# All 9 test suites (387 checks)
python test/test_safe_state.py \
  && python test/test_registry_gate.py \
  && python test/test_priority_enforcement.py \
  && python test/test_p0_fixes.py \
  && python test/test_p1_fixes.py \
  && python test/test_p2_fixes.py \
  && python test/test_p3_fixes.py \
  && python test/test_p4_fixes.py \
  && python test/test_p5_fixes.py
```

| Suite | Checks | Scope |
|-------|--------|-------|
| test_safe_state.py | 88 | State file hardening |
| test_registry_gate.py | 46 | Centralized registry |
| test_priority_enforcement.py | 31 | EventBus priority convention |
| test_p0_fixes.py | 32 | Idempotency, dedup, commit, SELL verify |
| test_p1_fixes.py | 30 | Timeouts, fail-closed, TTL |
| test_p2_fixes.py | 33 | Inbox queue, causation chain, ordering |
| test_p3_fixes.py | 43 | Correlation IDs, health checks, metrics, silence |
| test_p4_fixes.py | 49 | Generic brokers, engine config, registries, validation |
| test_p5_fixes.py | 35 | Migration safety, payload versioning, chaos tests |
| **Total** | **387** | |

---

*V7 implementation 2026-04-15. ALL P0-P5 COMPLETE. 387/387 tests passing. 25 files modified, 13 files created.*
