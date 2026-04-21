# V7: EventBus Handler Priority Map & Convention

**Status:** Documented, audited, and enforced (31/31 checks pass)
**Files created:** `test/test_priority_enforcement.py`
**Files modified:** `monitor/portfolio_risk.py` (priority 0 → 3), `monitor/registry_gate.py` (new, priority 2)

---

## Dispatch Rule

**Higher priority number = runs FIRST in SYNC dispatch.**

Implementation: `event_bus.py` line 1173 stores `(-priority, sub_counter, key)`.
Sort ascending: `-10 < -3 < -2 < -1 < 0` → priority 10 executes before 3 before 2 before 1 before 0.

Same priority: insertion order preserved (FIFO within tier).

---

## V7 Priority Convention

```
Priority  Role                          Components
────────  ────────────────────────────  ──────────────────────────────────
99        Passive audit                 ActivityLogger
10        Persistence layer             EventSourcingSubscriber, DBSubscriber,
                                        IPC publish, SmartRouter health tracking
 4-9      RESERVED (gap)                — do not use —
 3        Portfolio risk gate            PortfolioRiskGate (aggregate limits)
 2        Registry gate                 RegistryGate (cross-layer dedup)
 1        Broker routing                SmartRouter, TradierBroker
 0        Satellite IPC forwarding      _forward_order (Pro/Pop)
default   Strategy engines              StrategyEngine, RiskEngine,
                                        PositionManager, ExecutionFeedback,
                                        StateEngine, EventLogger, DurableEventLog
```

**Rule:** Gates that can BLOCK an order must have higher priority (runs first) than the component that EXECUTES it.

---

## ORDER_REQ Chain (Critical Path)

```
ORDER_REQ arrives
    │
    │  pri=10  EventSourcingSubscriber     Persist to event_store
    │  pri=10  IPC publish                 Forward to Redpanda
    │
    ▼  pri=3   PortfolioRiskGate           Drawdown, notional, margin, Greeks
    │          If blocked → _portfolio_blocked = True
    │
    ▼  pri=2   RegistryGate                Cross-layer dedup (try_acquire)
    │          If blocked → _blocked_ids.add(event_id)
    │
    ▼  pri=1   SmartRouter                 Check is_blocked(), check _portfolio_blocked
    │          Route to Alpaca or Tradier
    │
    ▼  default Broker                      Execute order (only if SmartRouter not active)
```

---

## Bug Found During Audit

**PortfolioRiskGate was at priority 0 in V6.** SmartRouter was at priority 1. Since higher number runs first, SmartRouter executed orders BEFORE PortfolioRiskGate could block them.

**Impact:** If aggregate drawdown or notional limits were breached, the order was already routed to the broker.

**Fix:** PortfolioRiskGate moved to priority 3.

---

## All Handler Priorities (Complete Audit)

### ORDER_REQ Handlers

| Priority | Component | File | Action |
|----------|-----------|------|--------|
| 99 | ActivityLogger | db/activity_logger.py:59 | Audit log |
| 10 | EventSourcingSubscriber | db/event_sourcing_subscriber.py:111 | DB persistence |
| 10 | IPC publish | scripts/run_core.py:209 | Redpanda forward |
| **3** | **PortfolioRiskGate** | **monitor/portfolio_risk.py:63** | **Aggregate risk (V7: was 0)** |
| **2** | **RegistryGate** | **monitor/registry_gate.py:36** | **Cross-layer dedup (V7: new)** |
| 1 | SmartRouter | monitor/smart_router.py:113 | Broker routing |
| 1 | TradierBroker | monitor/tradier_broker.py:57 | Direct execution |
| 0 | IPC _forward_order | scripts/run_pro.py:125, run_pop.py:147 | Satellite forward |
| default | BaseBroker | monitor/brokers.py:55 | Direct execution |

### FILL Handlers

| Priority | Component | File |
|----------|-----------|------|
| 99 | ActivityLogger | db/activity_logger.py:59 |
| 10 | EventSourcingSubscriber | db/event_sourcing_subscriber.py:111 |
| 10 | SmartRouter health | monitor/smart_router.py:116 |
| 3 | PortfolioRiskGate | monitor/portfolio_risk.py:64 |
| 0 | RiskAdapter (Pro) | pro_setups/risk/risk_adapter.py:82 |
| default | PositionManager | monitor/position_manager.py:83 |
| default | ExecutionFeedback | monitor/execution_feedback.py:100 |

### POSITION Handlers

| Priority | Component | File |
|----------|-----------|------|
| 99 | ActivityLogger | db/activity_logger.py:59 |
| 10 | EventSourcingSubscriber | db/event_sourcing_subscriber.py:111 |
| 10 | SmartRouter | monitor/smart_router.py:118 |
| 3 | PortfolioRiskGate | monitor/portfolio_risk.py:65 |
| 2 | RegistryGate | monitor/registry_gate.py:37 |
| 0 | RiskAdapter (Pro) | pro_setups/risk/risk_adapter.py:83 |
| default | StateEngine | monitor/state_engine.py:63 |
| default | ExecutionFeedback | monitor/execution_feedback.py:102 |

### BAR Handlers

| Priority | Component | File |
|----------|-----------|------|
| 99 | ActivityLogger | db/activity_logger.py:59 |
| 10 | EventSourcingSubscriber | db/event_sourcing_subscriber.py:111 |
| 3 | OptionsEngine | options/engine.py:217 |
| 2 | ProSetupEngine | pro_setups/engine.py:117 |
| 1 | PopStrategyEngine | pop_strategy_engine.py:522 |
| default | StrategyEngine (VWAP) | monitor/strategy_engine.py:102 |
| default | ExecutionFeedback | monitor/execution_feedback.py:103 |

### SIGNAL Handlers

| Priority | Component | File |
|----------|-----------|------|
| 99 | ActivityLogger | db/activity_logger.py:59 |
| 10 | EventSourcingSubscriber | db/event_sourcing_subscriber.py:111 |
| 10 | IPC signal publish | scripts/run_core.py:209 |
| 3 | OptionsEngine | options/engine.py:216 |
| default | RiskEngine | monitor/risk_engine.py:112 |
| default | ExecutionFeedback | monitor/execution_feedback.py:99 |

### ORDER_FAIL Handlers

| Priority | Component | File |
|----------|-----------|------|
| 99 | ActivityLogger | db/activity_logger.py:59 |
| 10 | EventSourcingSubscriber | db/event_sourcing_subscriber.py:111 |
| 10 | SmartRouter health | monitor/smart_router.py:117 |
| 2 | RegistryGate | monitor/registry_gate.py:38 |
| default | ExecutionFeedback | monitor/execution_feedback.py:101 |

---

## Enforcement Test

`test/test_priority_enforcement.py` (31 checks) runs these validations:

| Test | What It Enforces |
|------|-----------------|
| T1 | Higher number runs first; same priority = insertion order |
| T2 | ORDER_REQ chain: Portfolio(3) → Registry(2) → SmartRouter(1) → Broker(default) |
| T3 | Gate blocks propagate — blocked BUY not routed, SELL always passes |
| T4 | Persistence (10) runs before all business logic |
| T5 | Source code grep confirms actual priority values match convention |
| T6 | BAR ordering: EventSourcing → Options → Pro → Pop → CoreVWAP |
| T7 | Convention scan: no non-logger at >10, SmartRouter at 1, RegistryGate at 2, PortfolioRiskGate at 3, no handler in reserved gap 4-9 |

**To run:** `python test/test_priority_enforcement.py`

If anyone changes a handler priority without updating the convention, this test fails.

---

*Audited from 30+ subscribe() calls across 20 source files. 31/31 tests passing.*
