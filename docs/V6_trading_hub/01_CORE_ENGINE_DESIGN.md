# Core Engine — Design Document

**Source of truth:** Code as of 2026-04-15
**Files:** `scripts/run_core.py`, `monitor/monitor.py`, `monitor/strategy_engine.py`, `monitor/risk_engine.py`, `monitor/position_manager.py`, `monitor/execution_feedback.py`, `monitor/state_engine.py`, `monitor/state.py`

---

## 1. PURPOSE

The Core engine is the central execution process of Trading Hub V6.0. It runs the VWAP reclaim strategy, executes all orders (including those from Pro and Pop satellites via IPC), manages position lifecycle, and persists state. Core is the only process with direct broker access for equity orders.

---

## 2. DESIGN PRINCIPLES

### 2.1 Single-Writer Shared State
All mutable state (`positions`, `reclaimed_today`, `last_order_time`, `trade_log`) has exactly one writer: `PositionManager`. Other components observe these dicts by reference. This eliminates write contention without message-passing overhead — Python's GIL guarantees atomic dict mutations on CPython.

### 2.2 Event-Driven Composition
`RealTimeMonitor` is a thin orchestrator that wires components via `EventBus` subscriptions. It holds no business logic. Each component subscribes to specific `EventType`s and emits downstream events, forming an acyclic chain:

```
BAR → StrategyEngine → SIGNAL → RiskEngine → ORDER_REQ → SmartRouter → Broker → FILL → PositionManager → POSITION
```

### 2.3 Fail-Fast on Untracked Execution
`durable_fail_fast=True` on the EventBus means ORDER_REQ and FILL events are dropped if the Redpanda persistence hook fails. This prevents "invisible" order execution that crash recovery cannot replay.

### 2.4 Broker-Agnostic Signal Flow
StrategyEngine and RiskEngine know nothing about brokers. The `ORDER_REQ` payload is broker-agnostic — SmartRouter selects the broker downstream. This allows broker changes without touching strategy code.

### 2.5 Atomic State Persistence
Every position mutation triggers an immediate `save_state()` to `bot_state.json` via atomic write (tmp file + `os.replace()`). The system can crash at any point and recover from the last persisted state.

---

## 3. INITIALIZATION SEQUENCE

`run_core.py` creates components in dependency order:

| Step | Component | Why This Order |
|------|-----------|---------------|
| 1 | SIGTERM handler | Must be first — enables graceful shutdown |
| 2 | Logging (date-stamped dir) | All subsequent components log |
| 3 | DistributedPositionRegistry | Cross-process dedup must be ready before any orders |
| 4 | CacheWriter | Satellites wait for cache before emitting signals |
| 5 | IPC Publisher | Publish events to Redpanda for audit trail |
| 6 | RealTimeMonitor | Creates all internal engines (see below) |
| 7 | DB Layer (DBWriter + EventSourcingSubscriber) | Must subscribe before first event |
| 8 | PortfolioRiskGate (priority 0) | First gate — blocks before SmartRouter sees ORDER_REQ |
| 9 | SmartRouter + TradierBroker | Multi-broker routing after risk gate |
| 10 | IPC Consumer | Receive satellite orders last (after all handlers registered) |

**Design choice:** Core starts 5 seconds before satellites (supervisor enforces this) so shared cache and broker connections are ready.

---

## 4. MAIN LOOP

```python
while market_open:
    bars = data_client.fetch_batch_bars(tickers)    # Tradier REST API
    cache_writer.write(bars, rvol_data)              # atomic pickle write
    bus.emit_batch([BAR(ticker, df) for ticker in bars])  # per-ticker events
    heartbeat.tick()                                  # 60s HEARTBEAT event
    sleep(60)                                         # next bar cycle
```

**Design choices:**
- **60-second cycle** matches 1-minute bar granularity — no benefit from polling faster
- **Batch bar fetch** (single API call for all tickers) minimizes Tradier rate limit consumption
- **Cache write before emit** ensures satellites have fresh data before Core processes signals
- **Momentum screener refresh** every 30 minutes dynamically adjusts the active ticker watchlist

---

## 5. COMPONENT DESIGNS

### 5.1 StrategyEngine (`monitor/strategy_engine.py`)

**Pattern:** Observer — subscribes to `BAR`, emits `SIGNAL`

**Entry conditions (ALL must pass):**

| # | Condition | Rationale |
|---|-----------|-----------|
| 1 | VWAP reclaim (2-bar) | Price crossed above VWAP after opening below — momentum shift |
| 2 | Close above VWAP | Current bar confirms reclaim |
| 3 | RSI 50–70 | Bullish but not overbought |
| 4 | RVOL ≥ 2.0x (1.5x for ETFs) | Volume confirms conviction |
| 5 | SPY above its VWAP | Market tailwind — gracefully bypassed if data unavailable |

**Exit conditions (ANY triggers):**

| Condition | Action | Priority |
|-----------|--------|----------|
| Stop hit | SELL_STOP | Immediate — capital protection |
| Target hit | SELL_TARGET | Profit capture |
| RSI overbought | SELL_RSI | Momentum exhaustion |
| VWAP breakdown | SELL_VWAP | Thesis invalidated |
| 3:00 PM ET (VWAP positions) | Force close | EOD risk reduction |
| 3:00 PM ET (Pro/Pop swing) | PARTIAL_SELL (50%) | Gap protection |

**Stop/Target computation:**
```python
candle_stop = reclaim_candle_low - (atr * 0.5)
pct_stop    = price * (1 - min_stop_pct)
stop_price  = min(candle_stop, pct_stop)
# Floor: 0.3% minimum distance (prevents micro-stops)
# Ceiling: stop must be below current price
target_price = price + (atr * atr_mult)
half_target  = price + (atr * 1.0)
```

**Design choice — 0.3% stop floor:** Added after production incident where tight ATR-based stops triggered immediately on normal price noise, causing rapid churn with commissions.

**Design choice — SPY bias bypass:** SPY data may be unavailable (data source hiccup). Rather than blocking all trades, the system assumes bullish and logs a warning. This is a deliberate availability-over-correctness tradeoff for a non-critical filter.

---

### 5.2 RiskEngine (`monitor/risk_engine.py`)

**Pattern:** Pre-trade gate — converts `SIGNAL` → `ORDER_REQ` | `RISK_BLOCK`

**7-point buy gate (evaluated in order, short-circuits on first failure):**

| # | Gate | Threshold | Rationale |
|---|------|-----------|-----------|
| 1 | Cross-layer dedup | `position_registry.try_acquire()` | Prevents Pro+Pop+Core holding same ticker |
| 2 | Max positions | `len(positions) >= max_positions` | Capital concentration limit |
| 3 | Cooldown | 300s since last order for ticker | Prevents rapid re-entry after exit |
| 4 | Reclaimed-today | Ticker already in set | One VWAP reclaim trade per ticker per day |
| 5 | RVOL threshold | 2.0x stocks / 1.5x ETFs | Volume confirmation (ETFs structurally lower RVOL) |
| 6 | RSI range | 40–75 | Allows recovery trades and early momentum |
| 7 | Spread check | ≤ 0.2% + price divergence ≤ 0.5% | Liquidity and execution quality |

**Design choice — sell bypass:** All sell signals bypass every gate. Exits must never be blocked — capital protection overrides all risk checks.

**Design choice — ETF RVOL threshold:** ETFs (SPY, QQQ, etc.) structurally cannot reach 2.0x RVOL due to diversified baskets. Separate 1.5x threshold prevents systematically excluding ETF trades.

**Design choice — spread + price divergence:** Two-layer check catches both illiquid tickers (wide spread) and fast-moving prices where the signal price is stale (divergence > 0.5% from live ask).

**Sizing:**
```python
qty = max(1, floor(trade_budget / (ask * (1 + SLIPPAGE_PCT))))
# SLIPPAGE_PCT = 0.0001 (0.01%) — conservative buffer
# Further adjusted by RiskSizer: beta, sector volatility
```

---

### 5.3 PositionManager (`monitor/position_manager.py`)

**Pattern:** State machine — single writer for all mutable shared state

**State transitions:**

```
           BUY FILL
  (none) ──────────→ OPENED
                        │
            PARTIAL SELL│         FULL SELL
                        ▼              │
                  PARTIAL_EXIT ────────┤
                        │              │
                        └──────────────▼
                                    CLOSED
```

**OPENED:** Creates position dict from FillPayload (stop_price, target_price from actual signal values — NOT placeholders). Adds ticker to `reclaimed_today`. Emits `POSITION(OPENED)`.

**PARTIAL_EXIT:** Reduces quantity by sold amount. Sets `partial_done=True`. Moves stop to breakeven (`max(stop, entry_price)`). Emits `POSITION(PARTIAL_EXIT)`.

**CLOSED:** Calculates P&L `(exit_price - entry_price) * qty`. Appends to `trade_log`. Removes position. Releases cross-layer registry lock. Emits `POSITION(CLOSED)`.

**Design choice — breakeven stop on partial:** After taking half profit, the remaining position's stop moves to entry price. This guarantees the trade cannot lose money overall — the partial profit covers any remaining loss.

**Design choice — strategy field parsing:** The `reason` string carries the source engine (e.g., `"pro:sr_flip:T1:long"`). PositionManager parses this into `strategy = "pro:sr_flip"`. This tags every position with its origin for EOD reporting and P&L attribution.

**Deduplication:** Tracks processed FILL event IDs (sliding window of 10,000, pruned to 5,000). Prevents duplicate position creation from replayed events.

**Thread safety:** `threading.Lock()` guards all mutations. Combined with EventBus per-ticker partitioning, concurrent fills for different tickers are safe.

---

### 5.4 ExecutionFeedback (`monitor/execution_feedback.py`)

**Problem:** StrategyEngine computes stop/target/ATR, but these values travel through broker-agnostic ORDER_REQ which may not carry them. ExecutionFeedback bridges this gap.

**Pattern:** In-memory cache with deferred retry

**Flow:**
```
SIGNAL(BUY) → cache signal → FILL(BUY) → look up cached signal → patch position dict
                                              │
                                              └─ position not yet created?
                                                 → defer, retry on next BAR/POSITION event (max 5 retries)
```

**Design choices:**
- **TTL = 120s:** Signals older than 2 minutes are stale and discarded
- **Identity check:** If cached signal's price diverges >2% from position's entry_price, the signal is from a different trade (same ticker) — drop it
- **Max 5 retries:** Prevents indefinite memory growth from orphaned deferrals

**Note:** With the V6 fix to pass stop/target through FillPayload directly, ExecutionFeedback is now a backup mechanism. The primary path uses FillPayload values.

---

### 5.5 StateEngine (`monitor/state_engine.py`)

**Pattern:** Read-only snapshot — decouples observers from writer locks

**Design choice:** PositionManager mutates the canonical `positions` dict. StateEngine maintains an independent deep copy, updated only on POSITION events (atomic event boundaries). This means:
- Heartbeat, UI, and observability read from StateEngine without blocking PositionManager
- Snapshot is always consistent (never mid-mutation)
- RLock protects snapshot reads during concurrent events

**Seeded on startup** from restored state (bot_state.json) so heartbeats report correct data before any new POSITION events.

---

### 5.6 State Persistence (`monitor/state.py`)

**What's persisted:**
```json
{
  "date": "2026-04-15",
  "positions": {"AAPL": {"entry_price": 193.48, "stop_price": 193.35, ...}},
  "reclaimed_today": ["AAPL", "MSFT"],
  "trade_log": [{"ticker": "GOOG", "pnl": 23.50, ...}]
}
```

**What's NOT persisted:** `last_order_time` — cooldowns intentionally reset on restart to allow immediate re-entry.

**Atomic write pattern:**
```python
with self._lock:
    tmp = path + '.tmp'
    json.dump(state, open(tmp, 'w'))
    os.replace(tmp, path)  # atomic on POSIX
```

**Date validation:** On load, if file date doesn't match today (ET timezone), the state is discarded. New trading day = clean slate.

**Corrupt file handling:** If JSON parse fails, the file is backed up to `.corrupt` for post-mortem analysis. Returns empty state.

---

## 6. STARTUP RECONCILIATION

On every restart, Monitor performs broker reconciliation:

| Scenario | Detection | Action |
|----------|-----------|--------|
| Position at broker, not in local state | Query Alpaca + Tradier positions API | Import into `positions` dict with estimated stops |
| Position in local state, not at any broker | Compare local vs broker positions | Remove from local (bracket stop likely fired while down) |
| Quantity mismatch | Compare local qty vs broker qty | Update local to match broker (broker is truth) |

**Design principle:** Broker is always source of truth. Local state can be stale (crash during mutation), but broker positions reflect actual market exposure.

**Stop estimation for orphaned imports:**
```python
if atr_available:
    stop = entry - (1.5 * atr)
    target = entry + (2.0 * atr)
else:
    stop = entry * 0.97    # 3% fallback
    target = entry * 1.05  # 5% fallback
```

---

## 7. ERROR HANDLING PHILOSOPHY

| Scenario | Approach | Rationale |
|----------|----------|-----------|
| Non-critical data unavailable (SPY, RVOL) | Gracefully bypass, log warning | Availability over correctness for supplementary signals |
| Critical data unavailable (price, spread) | Block entry, emit RISK_BLOCK | Never enter blind |
| Sell signal with any error | Execute anyway | Capital protection overrides all |
| FILL event duplicate | Dedup via event ID set | Prevent double position creation |
| Mid-mutation crash | Atomic state write + broker stop orders | Both software and hardware recovery |
| Stale signal patching position | Identity check (2% price divergence) | Prevent wrong stops on different trade |

---

## 8. KEY CONSTANTS

| Constant | Value | Location | Purpose |
|----------|-------|----------|---------|
| Main loop interval | 60s | run_core.py | Bar cycle |
| SIGNAL_TTL_SEC | 120s | execution_feedback.py | Stale signal expiry |
| PATCH_RETRY_MAX | 5 | execution_feedback.py | Deferred patch limit |
| ORDER_COOLDOWN | 300s | config.py | Per-ticker re-entry cooldown |
| MIN_RVOL | 2.0 (1.5 ETF) | risk_engine.py | Volume threshold |
| RSI_LOW / RSI_HIGH | 40 / 75 | risk_engine.py | Momentum range |
| MAX_SPREAD_PCT | 0.002 | risk_engine.py | Liquidity threshold |
| SLIPPAGE_PCT | 0.0001 | risk_engine.py | Sizing buffer |
| MIN_STOP_PCT | 0.003 | strategy_engine.py | 0.3% stop floor |
| Screener refresh | 30 min | monitor.py | Watchlist update |
| FORCE_CLOSE_TIME | 15:00 ET | config.py | VWAP position EOD close |
| Fill dedup window | 10,000 IDs | position_manager.py | Sliding dedup set |

---

*Traced from 8 source files. Every constant, threshold, and design choice documented from code.*
