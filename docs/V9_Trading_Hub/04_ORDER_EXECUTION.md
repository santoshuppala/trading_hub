# V9 Order Execution Pipeline

## SmartRouter (`monitor/smart_router.py`)

| Property | Value |
|----------|-------|
| Subscribes | ORDER_REQ (priority 1) |
| BUY routing | Round-robin across healthy brokers |
| SELL routing | Route to broker that opened the position |
| Dual-broker | V9: `FillLedger.preview_sell_matches()` for lot-aware split |
| Dedup | Same ORDER_REQ not routed twice (V7 P0-2) |
| Latency | <1ms |

## Broker Execution

### AlpacaBroker (`monitor/brokers.py`)

| Step | What | Latency |
|------|------|---------|
| Build order | Limit order at ask price (V9: float qty, notional mode) | <1ms |
| Submit | POST /v2/orders | 500-2000ms |
| **Fill confirmation** | **V9 (L6): WebSocket trade_updates (FREE)** | **<100ms** |
| Fill confirmation (fallback) | REST polling every 0.5s, 5s timeout | 500-5000ms |
| Settlement verify | V9 (L5): 3×200ms fast poll (was 1s sleep) | 200-600ms |
| Bracket orders | OTOCO stop + target at fill time | included |
| **BUY total (WebSocket)** | | **~700-2700ms** |
| **BUY total (REST fallback)** | | **~1200-7600ms** |
| **SELL total** | Market order | **~500-3100ms** |

**AlpacaFillStream (V9 L6):**
- FREE on paper accounts (`wss://paper-api.alpaca.markets/stream`)
- Not market data — order execution status only
- `threading.Event` for synchronous waiting
- Falls back to REST polling if WebSocket disconnects

### TradierBroker (`monitor/tradier_broker.py`)

| Step | What | Latency |
|------|------|---------|
| Build order | Limit order at ask price (whole shares only) | <1ms |
| Submit | POST /v1/.../orders | 500-2000ms |
| Fill confirmation | REST polling every 0.5s, 5s timeout | 500-5000ms |
| | (sandbox has no WebSocket) | |
| **BUY total** | | **~1000-7000ms** |
| **SELL total** | Market order | **~500-3000ms** |

### V9 Correctness Guards

| Guard | File | What |
|-------|------|------|
| **Duplicate SELL (R4)** | position_manager.py | Dedup by `ticker:order_id` — prevents double execution on Redpanda replay |
| **Fill dedup (R7)** | position_manager.py | OrderedDict + 1hr TTL — replaces buggy set truncation |
| **Stale order cleanup (R3)** | monitor.py | Every 5 min: cancel orders >60s at both brokers |
| **Overfill detection** | position_manager.py | SELL qty > position qty → clamp + ALERT |
| **Connection pool timeout (R5)** | tradier_client.py | 20s hard limit on batch fetch — prevents 40-thread deadlock |


## Position Tracking — FillLedger

### Lot-Based Architecture

```
FILL event arrives
    │
    ▼ PositionManager._on_fill()
    │
    ├── BUY: Create FillLot (immutable) + LotState (remaining_qty) + PositionMeta (stop/target)
    │        Append to FillLedger (append-only)
    │        Persist to fill_ledger.json
    │
    └── SELL: Create SELL FillLot
             FIFO match against oldest open BUY lots
             → LotMatch records with per-lot P&L
             → realized_pnl = sum(matches)
             If all lots consumed → position closed
             If partial → PositionMeta.partial_done = True
```

### Shadow Mode (current)

Both old positions dict AND FillLedger track in parallel:
```
FILL → PositionManager
         ├── Write to positions dict (legacy)
         ├── Write to FillLedger (shadow)
         └── Compare P&L: if |dict_pnl - ledger_pnl| > $0.02 → log SHADOW PNL DRIFT
```

After 1-2 days validation → Phase 2 switchover replaces dict with `PositionProjectionProxy`.

### MutablePositionDict (M2 fix)

When code does `pos['stop_price'] = new_stop` (trailing stop update), the dict writes through to `PositionMeta` so the value persists across projection refreshes.

### Key P&L Fix

**Before V9**: `pnl = (exit_fill - entry_price) × qty` where `entry_price` was a single value per ticker — wrong after reconciliation, partial exits, multi-day holds.

**After V9**: FIFO lot matching. Each SELL consumes oldest BUY lots first. P&L computed per lot match:
```
Buy 5 @ $100, Buy 5 @ $110 → Sell 7 @ $115
  Match 1: 5 from lot1 → pnl = (115-100)×5 = $75
  Match 2: 2 from lot2 → pnl = (115-110)×2 = $10
  Total: $85 (exact, auditable)
```
