# ROOT CAUSE: Unintended SHORT Position Creation on 2026-04-13

## Critical Bug Summary

The trading system unexpectedly created **~15 SHORT positions** totaling **~172 shares** worth **~$20,000+** in short exposure, while `bot_state.json` shows only 3 LONG positions. **Root cause: Side enum mismatch in broker dispatch logic.**

---

## The Bug Chain

### 1. Side Enum Has Lowercase Values
**File: `monitor/events.py` (lines 63-65)**
```python
class Side(str, Enum):
    BUY  = 'buy'    # ← lowercase
    SELL = 'sell'   # ← lowercase
```

### 2. But Broker Dispatch Checks Uppercase String
**File: `monitor/brokers.py` (line 56)**
```python
def _on_order_request(self, event: Event) -> None:
    p: OrderRequestPayload = event.payload
    if p.side == 'BUY':           # ← checking for uppercase 'BUY'
        self._execute_buy(p)
    else:
        self._execute_sell(p)     # ← BUY orders incorrectly routed here!
```

### 3. The Comparison Fails
- `p.side` is a `Side` enum instance with value `'buy'` (lowercase)
- Comparison `p.side == 'BUY'` is **False** because value ≠ 'BUY'
- Order incorrectly routes to `_execute_sell(p)` instead of `_execute_buy(p)`

### 4. _execute_sell() Submits Short Orders to Alpaca
**File: `monitor/brokers.py` (lines 215-236)**
```python
def _execute_sell(self, p: OrderRequestPayload) -> None:
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce

    req = MarketOrderRequest(
        symbol=p.ticker,
        qty=p.qty,
        side=OrderSide.SELL,           # ← Market sell = SHORT if no position
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,
    )
    order = self._client.submit_order(req)
```

When you submit a market SELL for a stock you don't own → **Alpaca creates a SHORT position**.

---

## What Happened on 2026-04-13

**Timeline: 07:22 AM - 07:58 AM**

1. **07:22-07:57**: RiskEngine emits ORDER_REQ events for BUY orders (MSFT, AMAT, ARM, AVGO, DELL, INTU, HOOD, SMCI, UPST, ZM, MU, ZS)
   - Example: `OrderRequestPayload(side=Side.BUY, qty=..., ticker='AVGO', ...)`

2. **Broker dispatch fails**: `p.side == 'BUY'` check fails
   - `p.side` = `Side.BUY` with value `'buy'`
   - `'buy' == 'BUY'` → **False**
   - Falls through to `_execute_sell(p)`

3. **07:33-07:58**: Each failed BUY check triggers `_execute_sell()`, which submits a **market SELL order** to Alpaca
   - This creates SHORT positions:
     - NOW: 11 shares short
     - MRVL: 10 shares short
     - INTU: 2 shares short
     - QCOM: 7 shares short
     - TEAM: 16 shares short
     - HOOD: 14 shares short
     - MU: 2 shares short
     - SMCI: 39 shares short
     - UPST: 37 shares short
     - MSFT: 2 shares short
     - ZM: 13 shares short
     - AMAT: 2 shares short
     - AVGO: 4 shares short
     - ARM: 6 shares short
     - DELL: 5 shares short

4. **Reconciliation reports synced positions**: Because `_sync_broker_positions()` only imports positions with `qty > 0` (line 322 in monitor.py), SHORT positions with `qty < 0` are **silently ignored**
   - Logs show: `[reconcile] Local positions are in sync with Alpaca.`
   - But Alpaca actually has 15+ SHORT positions that local state doesn't know about

---

## Why the Enum Fix in Commit 75785cc Was Incomplete

**Commit 75785cc** updated `SignalAction` enum to uppercase:
```python
class SignalAction(str, Enum):
    BUY          = 'BUY'        # ← fixed to uppercase
    SELL_STOP    = 'SELL_STOP'  # ← fixed to uppercase
```

But **left the `Side` enum as lowercase**:
```python
class Side(str, Enum):
    BUY  = 'buy'   # ← NOT FIXED ❌
    SELL = 'sell'  # ← NOT FIXED ❌
```

This created the SHORT position bug.

---

## The Fix

### Change 1: Update Side Enum to Uppercase
**File: `monitor/events.py` (lines 63-65)**
```python
class Side(str, Enum):
    BUY  = 'BUY'    # ← now uppercase
    SELL = 'SELL'   # ← now uppercase
```

### Change 2: Verify Broker Dispatch Logic
**File: `monitor/brokers.py` (line 56)** already uses the correct comparison:
```python
if p.side == 'BUY':  # Now works correctly: Side.BUY == 'BUY' → True
    self._execute_buy(p)
else:
    self._execute_sell(p)
```

### Change 3: Verify Order Creations Use Enum
All FillPayload creations in brokers.py lines 164, 243, 265, 298, 308 use hardcoded uppercase strings:
```python
FillPayload(..., side='BUY', ...)   # ✓ matches Side.BUY = 'BUY'
FillPayload(..., side='SELL', ...) # ✓ matches Side.SELL = 'SELL'
```

---

## Verification

After the fix, test a BUY order:

```python
# Before fix:
p = OrderRequestPayload(side=Side.BUY, ticker='AAPL', qty=1, price=150.0, reason='test')
# p.side = Side.BUY (value='buy')
# broker._on_order_request checks: if Side.BUY == 'BUY':  → False
# → incorrectly routes to _execute_sell() → SHORT order

# After fix:
p = OrderRequestPayload(side=Side.BUY, ticker='AAPL', qty=1, price=150.0, reason='test')
# p.side = Side.BUY (value='BUY')  ← uppercase now
# broker._on_order_request checks: if Side.BUY == 'BUY':  → True
# → correctly routes to _execute_buy() → BUY order
```

---

## Impact of Unintended SHORT Positions

**Account Risk**: ~$20,000 short exposure on 15+ tickers
**Margin Impact**: Potential margin call if market rallies
**State Divergence**: `bot_state.json` shows 3 positions, Alpaca has 18+ positions
**Audit Trail Loss**: SHORT positions not in PostgreSQL (reconciliation ignored them)

---

## Action Required

1. **URGENT**: Close all SHORT positions manually in Alpaca
   - Use Alpaca dashboard or API to close the 15 SHORT positions
   - Or submit 15 market BUY orders to close them

2. **Deploy the fix**: Commit this change and restart monitor
   - Verify no enum validation errors in logs
   - Confirm all BUY orders route to `_execute_buy()`
   - Confirm all SELL orders route to `_execute_sell()`

3. **Reconciliation check**: After restart, verify position counts match
   - `bot_state.json` LONG count should match Alpaca account
   - No orphaned SHORT positions in Alpaca

---

## Lessons Learned

- **Enum value consistency**: Python enum values must match database/API expectations
- **Dispatch logic testing**: String comparisons against enum values should use the enum itself, not hardcoded strings
- **Reconciliation blindness**: Filtering by `qty > 0` hides SHORT positions; consider tracking both sides
- **Multi-phase rollout risk**: Incomplete enum fixes across multiple pull requests can introduce subtle bugs

---

## Files Affected

| File | Issue | Fix |
|------|-------|-----|
| `monitor/events.py` | Side enum values lowercase | Changed to uppercase |
| `monitor/brokers.py` | Dispatch check `p.side == 'BUY'` | Now works with uppercase Side.BUY |
| `monitor/monitor.py` | Reconciliation ignores SHORT positions | Document expected behavior (qty > 0 filter) |

---

## Timeline of Discovery

- **2026-04-13 07:22-07:58**: Monitor runs with unfixed enum code → creates 15 SHORT positions
- **2026-04-13 10:00+**: User notices bot_state.json (3 positions) vs Alpaca (18 positions) mismatch
- **2026-04-13 15:00+**: Root cause identified: Side enum lowercase, broker dispatch fails, routes BUY→SELL
- **2026-04-13 16:00+**: Fix applied: Side enum updated to uppercase
