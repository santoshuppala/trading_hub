# V7: Centralized Position Registry — Single-Writer at Core

**Status:** Implemented and tested (46/46 checks pass)
**Files created:** `monitor/registry_gate.py`, `test/test_registry_gate.py`
**Files modified:** `monitor/events.py`, `monitor/risk_engine.py`, `monitor/position_manager.py`, `monitor/smart_router.py`, `pro_setups/risk/risk_adapter.py`, `pop_strategy_engine.py`, `scripts/run_core.py`, `scripts/run_pro.py`, `scripts/run_pop.py`

---

## Problem Statement

In V6, all 4 processes wrote to `position_registry.json` via fcntl exclusive locks:

```
Pro process ──── try_acquire() ──→ ┐
Pop process ──── try_acquire() ──→ ├─ position_registry.json (fcntl contention)
Options process ─ try_acquire() ──→ ┤
Core process ─── try_acquire() ──→ ┘
```

**Issues:**
- 4-way exclusive lock contention on the critical order path
- Lock file deletion race (process A holds lock on inode X, ops deletes lock file, process B creates new inode Y — both think they have exclusive lock)
- Satellites write shared state directly — violates single-writer principle

---

## Solution: Satellites Read-Only, Core Writes

```
Pro process                              Core process
───────────                              ────────────
RiskAdapter.validate_and_emit()
  │
  ├─ held_by('AAPL') → READ-ONLY        RegistryGate._on_order_req()
  │  (shared fcntl lock, fast)             │
  │  if held by another layer → skip       ├─ try_acquire('AAPL', 'pro')
  │                                        │  ← ONLY Core WRITES
  ├─ emit ORDER_REQ(layer='pro')           │
  │                                        ├─ if blocked → _blocked_ids.add(event_id)
  └─ _forward_order → Redpanda ──────→     │              emit RISK_BLOCK
                                           │
                                           └─ if acquired → pass to SmartRouter
                                                              │
                                           On POSITION CLOSED:
                                             └─ release('AAPL')
```

---

## Components

### RegistryGate (`monitor/registry_gate.py`)

New EventBus subscriber at Core. Sits between PortfolioRiskGate and SmartRouter in the ORDER_REQ handler chain.

**Subscribes to:**
- `ORDER_REQ` (priority 2) — acquire registry on BUY, skip SELL
- `POSITION` (priority 2) — release on CLOSED
- `ORDER_FAIL` (priority 2) — release on failed order (prevents leak)

**Key methods:**
- `is_blocked(event_id)` — called by SmartRouter to check if an ORDER_REQ was blocked
- `_infer_layer(reason)` — backward compatibility: infers layer from reason string if `layer` field missing

**Blocked event tracking:** Uses `_blocked_ids: set` (bounded to 1000 entries) instead of event attribute flags. SmartRouter queries `gate.is_blocked(event.event_id)` before routing.

### OrderRequestPayload `layer` Field

New optional field added to `OrderRequestPayload`:

```python
@dataclass(frozen=True)
class OrderRequestPayload:
    ticker: str
    side: Side
    qty: int
    price: float
    reason: str
    ...
    layer: Optional[str] = None  # V7: 'vwap', 'pro', 'pop', 'options'
```

**Backward compatibility:** `layer=None` triggers layer inference from the `reason` string:
- `"pro:sr_flip:T1:long"` → `'pro'`
- `"pop:VWAP_RECLAIM:pop"` → `'pop'`
- `"options:iron_condor"` → `'options'`
- `"VWAP reclaim"` → `'vwap'` (default)

### IPC Payload Enhancement

Satellite `_forward_order` functions now include `layer` in the Redpanda payload:

```python
publisher.publish(TOPIC_ORDERS, p.ticker, {
    'ticker': p.ticker,
    'side': str(p.side),
    ...
    'source': 'pro',
    'layer': 'pro',  # V7: Core acquires registry on our behalf
})
```

Core's `_on_remote_order` deserializes and passes `layer` to `OrderRequestPayload`.

---

## Satellite Changes

### Before (V6) — Satellites Write

```python
# pro_setups/risk/risk_adapter.py
from monitor.position_registry import registry
if not registry.try_acquire(ticker, layer="pro_setups"):  # WRITE
    return
...
finally:
    if not _approved:
        registry.release(ticker)  # WRITE
```

### After (V7) — Satellites Read-Only

```python
# pro_setups/risk/risk_adapter.py
from monitor.position_registry import registry
holder = registry.held_by(ticker)  # READ-ONLY (shared lock)
if holder and holder != 'pro':
    return  # pre-flight rejection
...
finally:
    pass  # V7: No registry release — Core owns the lock
```

**Same pattern applied to:**
- `monitor/risk_engine.py` — Core VWAP layer
- `pop_strategy_engine.py` — Pop layer
- `monitor/position_manager.py` — removed `registry.release()` on close (RegistryGate handles it)

---

## ORDER_REQ Handler Chain (V7)

EventBus dispatches in descending priority order (higher number = runs first):

```
ORDER_REQ event
  │
  │  priority=10   EventSourcingSubscriber    Persist to DB
  │  priority=3    PortfolioRiskGate          Aggregate risk limits
  │  priority=2    RegistryGate               Cross-layer dedup     ← NEW
  │  priority=1    SmartRouter                Route to broker
  │  default       Broker                     Execute order
```

**Gate chain invariant:** `PortfolioRiskGate(3) → RegistryGate(2) → SmartRouter(1)`

Gates that BLOCK must have higher priority than components that EXECUTE.

---

## Edge Cases

| Scenario | Handling | Tested |
|----------|----------|--------|
| Two layers send ORDER_REQ for same ticker simultaneously | Core EventBus processes sequentially (1 worker for ORDER_REQ). First acquires, second gets ORDER_FAIL. | T2c, T2e |
| Same layer re-acquires (idempotent) | `try_acquire` returns True if already held by same layer | T2h |
| ORDER_FAIL after acquire | RegistryGate releases ticker — no leak | T4b, T4c |
| PARTIAL_EXIT | Does NOT release — only CLOSED releases | T10f |
| Pre-flight says "free" but Core already acquired | Core rejects with RISK_BLOCK — satellite sees rejection via IPC | T3b |
| No `layer` field (backward compat) | Inferred from `reason` string | T5a-T5g |
| Global max reached | RegistryGate blocks, emits RISK_BLOCK | T6c, T6d |
| SELL order | Always passes — never blocked by registry | T1c, T3c |

---

## Test Coverage

| Test | Checks | What It Validates |
|------|--------|-------------------|
| T1: Acquire and pass | 3 | BUY acquired, SELL passes |
| T2: Cross-layer dedup | 8 | Pro holds → Pop/VWAP blocked, different ticker passes, idempotent |
| T3: POSITION CLOSED | 3 | Release on close, re-acquire by other layer |
| T4: ORDER_FAIL release | 3 | No leak on failed order |
| T5: Layer inference | 7 | Backward compat from reason string |
| T6: Global max | 4 | 4th ticker blocked at cap |
| T7: RISK_BLOCK event | 3 | Emitted with holder info |
| T8: Satellite read-only | 6 | held_by checks, no writes |
| T9: Payload layer field | 3 | Preserved, defaults to None, frozen |
| T10: Full lifecycle | 6 | Acquire → block → close → re-acquire → partial exit doesn't release |
| **Total** | **46** | |

---

## Key Design Decisions

1. **`is_blocked(event_id)` over event attribute flag** — EventBus SYNC dispatch doesn't guarantee handler execution order matches subscription priority for dynamically set attributes. Using a lookup set that the gate populates and SmartRouter queries is reliable regardless of dispatch timing.

2. **Pre-flight at satellites** — Satellites still do `held_by()` read-only checks. This filters ~90% of obviously-blocked ORDER_REQs before they hit Redpanda, reducing IPC traffic. The check is advisory (Core is authoritative).

3. **Layer in payload, not header** — `layer` is part of `OrderRequestPayload` (not a Redpanda header) because it participates in routing logic and should be persisted in event_store for audit.

4. **RegistryGate at priority 2** — Between PortfolioRiskGate (3) and SmartRouter (1). Portfolio limits are checked first (no point acquiring registry if portfolio is full). Registry is checked second. SmartRouter executes last.

5. **`_already_locked` for atomic read-modify-write** — `DistributedPositionRegistry.try_acquire()` holds an exclusive lock and needs to call `sf.write()` without re-acquiring. The `_already_locked=True` parameter skips the outer lock.

---

*Implemented 2026-04-15. 46/46 tests passing.*
