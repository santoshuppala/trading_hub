# Release Plan v5 — Institutional Compliance & Remaining Gaps

**Date**: 2026-04-15+
**Current Rating**: 7.8/10
**Target**: 8.5/10
**Risk Level**: LOW — mostly testing, tuning, and validation
**Prerequisite**: v1-v4 deployed, paper trading validated

---

## Carried Over from v4

| # | Item | Status | Phase |
|---|------|--------|-------|
| 1 | Options E2E test suite (test_19) | NOT STARTED | 1 |
| 2 | IV history seeding for backtests | NOT STARTED | 2 |
| 3 | ML classifier training | WAITING (needs 2-3 weeks data) | 3 |
| 4 | Threshold tuning | WAITING (needs 1 week of live data) | 4 |
| 5 | Strategy performance review | WAITING (needs 2 weeks data) | 4 |

## New from Architecture Audit

| # | Item | Status | Phase |
|---|------|--------|-------|
| 6 | Options chain.py timeout on fetch calls | OPEN | 1 |
| 7 | Dependency-level health gating (stop trading when all APIs down) | PARTIAL | 1 |
| 8 | Pop engine news/social failure tracking | DONE (just fixed) | - |
| 9 | Broker-down halt in SmartRouter | DONE (just fixed) | - |
| 10 | FILL event_id dedup | DONE (just fixed) | - |
| 11 | Backtest SimulatedTimeSource | DONE (just fixed) | - |

---

## Phase 1: Quick Remaining Fixes (1 day)

### Fix 1.1: Options E2E Test Suite
[Same prompt as v3/v4 — 11 test cases for options engine]

### Fix 1.2: Options Chain Timeout
**Problem**: `options/chain.py` data fetch has no explicit timeout parameter.
**Prompt**: Read options/chain.py, find the HTTP call, add timeout=10.

### Fix 1.3: Dependency Health Gating
**Problem**: System continues generating signals when Benzinga/StockTwits are both down.
**Prompt**: In PopStrategyEngine, if _news_failures >= 10 AND _social_failures >= 10, log WARNING and reduce signal confidence by 50%.

## Phase 2: Backtesting (1 day)

### Fix 2.1: IV History Seeding
[Same prompt as v3/v4]

## Phase 3: ML Training (Week 3+)

### Enhancement 3.1: Train Pop Classifier
[Same as v4]

### Enhancement 3.2: Signal Quality Scoring
[Same as v4]

## Phase 4: Tuning (After Week 1)

### Enhancement 4.1: Threshold Calibration
[Same as v4]

### Enhancement 4.2: Strategy Performance Review
[Same as v4]

---

## Architecture Compliance Status (from audit)

| Concern | Status | Evidence |
|---|---|---|
| Thread-pool blocking | DONE | Process isolation (4 processes) |
| Crash isolation | DONE | Supervisor with independent restart |
| GIL contention | DONE | Multiprocessing (4 cores) |
| Shared state races | DONE | threading.Lock + fcntl file locking |
| Event ordering | DONE | Causal partitioning by ticker |
| Idempotency | DONE | EventBus 10K dedup + FILL event_id check |
| Timeouts/retries | DONE | All APIs have timeouts + circuit breakers |
| Backpressure | DONE | DROP_OLDEST + priority eviction + 60/80/95% monitor |
| Health checks | PARTIAL | Broker + data source circuit breakers; needs dependency-level gating |
| Replay determinism | DONE | SimulatedTimeSource injected in backtests |
