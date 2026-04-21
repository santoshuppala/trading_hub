# V9 Strategy Analysis + Risk Engine

## Strategy Engines

### VWAP StrategyEngine (`monitor/strategy_engine.py`)

**Subscribes to**: BAR + QUOTE events

**On BAR (full analysis):**

| Step | What | Latency |
|------|------|---------|
| Validate bar | R1: price>0, OHLC valid, sequence check | <0.5ms |
| Trading window | 9:45 AM - 3:00 PM ET gate | <0.1ms |
| Exit check (if position open) | Stop, target, RSI, VWAP cross, trailing stop | 5-15ms |
| Entry check (if no position) | 2-bar VWAP reclaim, RSI>40, RVOL>1.0 | 5-20ms |
| Emit SIGNAL | {action, price, rsi, atr, stop, target} | <1ms |

**On QUOTE (streaming exit — V9):**

| Step | What | Latency |
|------|------|---------|
| Compare price vs stop/target | Quick arithmetic comparison | <1ms |
| Emit SIGNAL if breached | SELL_STOP or SELL_TARGET | <1ms |

No full indicator analysis on QUOTE — just stop/target comparison against PositionMeta values.

### ProSetupEngine (`pro_setups/engine.py`)

**Subscribes to**: BAR events

**V9 Detector Cascade (L4):**

```
BAR arrives for ticker
    │
    ▼ Precompute indicators (VWAP, ATR, RSI, EMA 9/21/50) — 1-2ms
    │
    ▼ PHASE 1: 3 cheapest detectors (~1.5ms total)
    ┌─────────┬─────────┐
    │ Trend   │ ~0.5ms  │
    │ VWAP    │ ~0.5ms  │
    │ ORB     │ ~0.5ms  │
    └─────────┴─────────┘
    │
    ├── NONE fired? → RETURN (skip Phase 2) — ~80% of tickers at open
    │
    ▼ PHASE 2: 10 remaining detectors (30-60ms)
    ┌──────────────┬──────────┐
    │ SR           │ ~8ms     │
    │ InsideBar    │ ~2ms     │
    │ Gap          │ ~3ms     │
    │ Flag         │ ~5ms     │
    │ Liquidity    │ ~5ms     │
    │ Volatility   │ ~3ms     │
    │ Fib          │ ~8ms     │
    │ Momentum     │ ~3ms     │
    │ Sentiment    │ ~10ms    │  (reads alt_data_cache.json)
    │ NewsVelocity │ ~5ms     │
    └──────────────┴──────────┘
    │
    ▼ Classify → best strategy + tier + confidence — ~10ms
    │
    ▼ Emit SIGNAL
```

**Impact**: market open spike reduced from ~10s to <2s.


## Risk Engine — 7 Pre-Trade Checks (`monitor/risk_engine.py`)

Every BUY SIGNAL passes through all 7 checks. SELL signals bypass (pass-through).

| # | Check | What | Latency |
|---|-------|------|---------|
| 1 | Position cap | Max 15 VWAP + 10 Pro | <0.1ms |
| 2 | Cooldown | 300s per ticker | <0.1ms |
| 3 | Reclaimed today | No re-entry same day | <0.1ms |
| 4 | RSI range | 40-70 (35-80 with high sentiment) | <0.1ms |
| **5** | **Fresh ask quote** | **V9: streaming (<0.02ms) / REST fallback (300-3000ms)** | **0.02ms / 300ms** |
| 6 | Spread | Reject if >0.5% | <0.1ms |
| 7 | Price divergence | Reject if ask >0.5% from signal price | <0.1ms |
| 8 | Correlation risk | Sector overlap check | <1ms |
| 9 | Beta exposure | Portfolio beta-weighted check | <1ms |

**Check 5 detail (V9):**
```
TRY STREAMING FIRST:
  stream_client.get_quote(ticker, max_age=5s)
  → Read from in-memory price cache
  → 0.02ms (dict lookup)
  → Returns None if quote >5s old (staleness guard)

FALLBACK TO REST:
  data_client.check_spread(ticker)
  → GET /v1/markets/quotes?symbols=TICKER
  → 300-3000ms (network round-trip)
```

**Sizing (after all checks pass):**
```python
effective_entry = ask_price × (1 + SLIPPAGE_PCT)  # 0.01% slippage allowance
qty = trade_budget / effective_entry               # V9: float for fractional
# Adjusted by: beta, volatility, conviction multipliers
```

**Total risk engine latency:**
- With streaming: **<5ms**
- Without streaming: **300-3000ms** (dominated by Check 5)


## Portfolio Risk Gate (`monitor/portfolio_risk.py`)

Subscribes to ORDER_REQ at priority 3 — runs BEFORE any broker.

| # | Check | What | Latency |
|---|-------|------|---------|
| A | Max drawdown | -$5,000 daily limit | <0.1ms |
| B | Max notional | $100,000 total exposure | <0.1ms |
| **C** | **Buying power** | **V9: background refresh (L3)** | **<1ms** |
| D | Portfolio delta | 5.0 units limit | <1ms |
| E | Portfolio gamma | 1.0 units limit | <0.1ms |

**Buying power check (V9 L3):**
```
BEFORE V9:
  4 synchronous API calls (Alpaca ×3 + Tradier ×1)
  3s timeout each → 3-10s BLOCKING the EventBus

AFTER V9:
  _BuyingPowerRefresher daemon thread
    Refreshes every 30s in background
    Handler reads from cache: <1ms
    90s staleness tolerance (fail-open with WARNING)
    Broker is ultimate rejection authority
```

**Total portfolio risk latency:**
- Before V9: **3-10s**
- After V9: **<5ms**
