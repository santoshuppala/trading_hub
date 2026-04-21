# V9 Safety Systems + Reconciliation

## Kill Switch

### Per-Strategy Kill Switch (Core — `monitor/kill_switch.py`)
- VWAP: -$5,000 daily limit
- Pro: -$2,000 daily limit
- Tracks realized P&L per strategy prefix
- Halts individual strategies, not entire engine

### Satellite Kill Switch (Options — `lifecycle/kill_switch.py`)
- V9 (L5): halt state persisted to `data/{engine}_kill_switch.json`
- Survives process restart (prevents daily P&L reset exploit)
- Checked on startup: if today's halt exists → stay halted
- Options engine `_halted` flag checked FIRST in `_on_signal`, `_on_bar`, `_execute_entry` (M4)
- `force_close_all()` sets `_halted = True` BEFORE iterating positions (race window fix)
- `run_options.py`: `lifecycle.tick()` runs BEFORE `bus.emit_batch()` (defense-in-depth)


## Reconciliation

### BrokerRegistry (`monitor/broker_registry.py`)
- Central collection of all broker instances
- `get_all_positions()` → normalized `[{symbol, qty, avg_entry, cost_basis}]`
- `get_all_equity()` → `{broker_name: equity_float}`
- `supports_fractional(broker_name)` / `supports_notional(broker_name)`
- Adding new broker = `registry.register('ibkr', ibkr_broker)` → done

### Startup Reconciliation (`monitor/monitor.py`)
1. Load bot_state.json (Tier 1) → DB event_store (Tier 2) → empty (Tier 3)
2. Load fill_ledger.json → FIFO replay to reconstruct daily P&L (M1)
3. Query ALL brokers via BrokerRegistry
4. V9: entry_price ALWAYS from broker cost basis (was 1% threshold → caused $3,098 gap)
5. Create synthetic FillLedger lots for imported positions
6. Reverse reconcile: remove positions not at any broker (phantom cleanup)
7. Seed PortfolioRiskGate.realized_pnl from FillLedger (M3)

### Live Reconciliation (every 10 min)
1. Fetch positions from ALL brokers
2. V9 dampening: skip if fill processed in last 30s (D2)
3. Fix phantoms (in state, not at broker → remove)
4. Fix orphans (at broker, not in state → import with ATR stops)
5. Fix qty mismatches (state ≠ broker → broker wins)
6. V9: entry_price sync (broker cost basis always wins)

### EquityTracker (`monitor/equity_tracker.py`)
- Baseline: capture equity from ALL brokers at session start
- Hourly: compare computed P&L vs broker equity change
- `pnl_fn` abstraction: trade_log now → FillLedger after switchover (M8)
- Thresholds: >$50 WARNING, >$500 CRITICAL alert


## Robustness Fixes

| ID | Fix | File | Impact |
|----|-----|------|--------|
| R1 | Bar data validation | strategy_engine.py | Reject price=0, NaN, H<L, stale, out-of-order |
| R2 | Data source failover | data_client.py | Tradier → Alpaca IEX auto-switch (degraded) |
| R3 | Stale order cleanup | monitor.py | Cancel orphaned orders >60s every 5 min |
| R4 | Duplicate SELL guard | position_manager.py | Prevent double execution on Redpanda replay |
| R5 | Connection pool timeout | tradier_client.py | 20s hard limit prevents 40-thread deadlock |
| R6 | Alert blackout fix | alerts.py | 30 min → 5 min, CRITICAL bypasses blackout |
| R7 | Dedup TTL fix | position_manager.py | OrderedDict + 1hr TTL (was buggy set truncation) |


## Correctness Guards (Streaming)

| Guard | Location | What it prevents |
|-------|----------|------------------|
| Session epoch | tradier_stream.py | Stale events from dead WebSocket connection |
| Quote age (5s) | tradier_stream.py | Stale quotes after reconnect or network delay |
| Gap detection (30s) | tradier_stream.py | Half-open WebSocket (TCP alive, no data) |
| BAR sequence check | strategy_engine.py | Out-of-order BARs from dual-frequency polling |
| OHLC validation | strategy_engine.py | Corrupt bar data (H<L, close>high) |
| QUOTE coalesce | event_bus.py | Only latest quote per ticker delivered |
| DROP_OLDEST | event_bus.py | Keeps newest quotes, evicts oldest |


## Persistence

| File | Content | Write frequency |
|------|---------|-----------------|
| `bot_state.json` | positions dict, reclaimed_today, trade_log | Every mutation + 60s |
| `fill_ledger.json` | ALL today's lots + lot_states + meta | Every lot append |
| `data/{engine}_kill_switch.json` | Halt state (survives restart) | On kill switch trigger |
| `live_cache.json` | Market bars + RVOL (V9: JSON, was pickle) | Every 60s cycle |
| `position_registry.json` | Cross-process position dedup | On acquire/release |
| `position_broker_map.json` | Ticker → broker routing map | On route decision |

All files use SafeStateFile pattern: fcntl lock + SHA256 checksum + .prev/.prev2 rolling backups + atomic write (tmp → fsync → replace).


## Database

| Table | Purpose | V9 Change |
|-------|---------|-----------|
| `event_store` | Immutable event log (JSONB) | BarReceived writes STOPPED |
| `market_bars` | V9: Lean bar table (10x smaller) | NEW — replaces BarReceived |
| `fill_lots` | V9: Lot-level position data | NEW |
| `lot_matches` | V9: FIFO P&L audit trail | NEW |
| `completed_trades` | Historical closed trades | qty INT → NUMERIC(12,6) |
| `position_state` | Current positions (projection) | qty INT → NUMERIC(12,6) |

DB size impact: event_store was 808 MB in 4 days (97% BarReceived). After V9: new bars go to market_bars (~10x smaller per row). No new event_store bloat.
