# V9 Database Schema + Operational Readiness

## V9 Database Tables

### market_bars (replaces BarReceived in event_store)

Lean bar storage — ~10x smaller per row than JSONB event_store entries.

```sql
CREATE TABLE market_bars (
    id          BIGSERIAL PRIMARY KEY,
    ticker      VARCHAR(10)    NOT NULL,
    ts          TIMESTAMPTZ    NOT NULL,
    open        NUMERIC(12,4)  NOT NULL,
    high        NUMERIC(12,4)  NOT NULL,
    low         NUMERIC(12,4)  NOT NULL,
    close       NUMERIC(12,4)  NOT NULL,
    volume      BIGINT         NOT NULL,
    rvol        NUMERIC(8,4),
    source      VARCHAR(20)    DEFAULT 'tradier',
    created_at  TIMESTAMPTZ    DEFAULT NOW()
);
CREATE INDEX idx_bars_ticker_ts ON market_bars(ticker, ts);
```

**Write path**: `db/event_sourcing_subscriber.py` → `_on_bar_lean()` writes directly to `market_bars` (no more event_store bloat).

**Size impact**: event_store was 808 MB in 4 days (97% BarReceived). After V9: `market_bars` at ~10x smaller per row, old BarReceived events no longer written.

### fill_lots (lot-level position tracking)

```sql
CREATE TABLE fill_lots (
    id          BIGSERIAL PRIMARY KEY,
    ticker      VARCHAR(10)    NOT NULL,
    side        VARCHAR(4)     NOT NULL,  -- BUY or SELL
    qty         NUMERIC(12,6)  NOT NULL,  -- supports fractional (e.g., 0.236500)
    price       NUMERIC(12,4)  NOT NULL,
    broker      VARCHAR(20)    NOT NULL,
    order_id    VARCHAR(64),
    engine      VARCHAR(20),
    strategy    VARCHAR(40),
    ts          TIMESTAMPTZ    NOT NULL,
    created_at  TIMESTAMPTZ    DEFAULT NOW()
);
CREATE INDEX idx_lots_ticker ON fill_lots(ticker);
CREATE INDEX idx_lots_ts ON fill_lots(ts);
```

**Purpose**: Immutable append-only record of every fill. Supports fractional qty (NUMERIC 12,6) for Alpaca fractional shares. Each BUY lot becomes a `LotState` in the in-memory `FillLedger`.

### lot_matches (FIFO P&L audit trail)

```sql
CREATE TABLE lot_matches (
    id          BIGSERIAL PRIMARY KEY,
    buy_lot_id  BIGINT         REFERENCES fill_lots(id),
    sell_lot_id BIGINT         REFERENCES fill_lots(id),
    qty         NUMERIC(12,6)  NOT NULL,
    buy_price   NUMERIC(12,4)  NOT NULL,
    sell_price  NUMERIC(12,4)  NOT NULL,
    realized_pnl NUMERIC(12,4) NOT NULL,
    created_at  TIMESTAMPTZ    DEFAULT NOW()
);
CREATE INDEX idx_matches_buy ON lot_matches(buy_lot_id);
CREATE INDEX idx_matches_sell ON lot_matches(sell_lot_id);
```

**Purpose**: Every SELL is FIFO-matched against open BUY lots. Each match records exact per-lot P&L. Enables auditable P&L reconstruction at any time.

### Existing tables (V9 updates)

| Table | V9 Change |
|-------|-----------|
| `completed_trades` | `qty INT` → `NUMERIC(12,6)` |
| `position_state` | `qty INT` → `NUMERIC(12,6)` |
| `event_store` | No new BarReceived writes (still receives FILL, SIGNAL, etc.) |


## Analytics Queries

### Daily realized P&L by ticker
```sql
SELECT fl.ticker,
       SUM(lm.realized_pnl) AS daily_pnl,
       COUNT(*)              AS matches
FROM   lot_matches lm
JOIN   fill_lots fl ON fl.id = lm.sell_lot_id
WHERE  fl.ts::date = CURRENT_DATE
GROUP BY fl.ticker
ORDER BY daily_pnl DESC;
```

### Open positions (lots not fully consumed)
```sql
SELECT ticker, side, qty, price, broker, ts
FROM   fill_lots
WHERE  side = 'BUY'
  AND  id NOT IN (
    SELECT buy_lot_id FROM lot_matches
    GROUP BY buy_lot_id
    HAVING SUM(qty) >= (SELECT qty FROM fill_lots f2 WHERE f2.id = buy_lot_id)
  )
ORDER BY ts;
```

### Bar range and VWAP
```sql
SELECT ticker,
       MIN(low)  AS session_low,
       MAX(high) AS session_high,
       SUM(close * volume) / NULLIF(SUM(volume), 0) AS vwap
FROM   market_bars
WHERE  ts::date = CURRENT_DATE
GROUP BY ticker;
```


## Validation Results (2026-04-18)

All 3 tables validated with real broker data:

| Test | Result |
|------|--------|
| Insert synthetic bars (5 rows) | OK |
| Insert real Alpaca positions (DXCM, META) | OK |
| Insert real Tradier positions (APLD, CLSK) | OK |
| Fractional qty storage (0.236500) | OK — NUMERIC(12,6) preserves precision |
| FIFO lot match with P&L ($1.18) | OK |
| Daily P&L aggregation query | OK |
| Bar range query | OK |
| VWAP computation query | OK |
| Open lots query | OK |
| Column type verification (all numeric) | OK |
| Foreign key integrity (invalid lot_id rejected) | OK |
| Test data cleanup | OK |
| **Total** | **14/14 PASSED** |


## Startup Sequence (run_core.py)

V9 startup executes 12 steps in order. All steps verified with real tokens on 2026-04-18.

```
STEP  1: EventBus                          ✓  In-process event backbone
STEP  2: Tradier WebSocket streaming       ✓  Production token, real-time quotes
STEP  3: FailoverDataClient                ✓  Tradier primary, Alpaca IEX fallback
STEP  4: FillLedger                        ✓  Lot-based tracking, loads from disk
STEP  5: BrokerRegistry                    ✓  Alpaca + Tradier registered
STEP  6: EquityTracker                     ✓  Baseline capture + hourly drift
STEP  7: PortfolioRiskGate                 ✓  Background buying power refresher
STEP  8: StrategyEngine (VWAP)             ✓  BAR + QUOTE subscribers
STEP  9: ProSetupEngine                    ✓  13 detectors, cascade, 5-min cache
STEP 10: PositionManager                   ✓  Shadow FillLedger, dedup guards
STEP 11: SmartRouter                       ✓  Lot-aware dual-broker routing
STEP 12: Monitor                           ✓  Reconciliation, streaming wiring
```

**V9 Component Status Banner** (logged at startup):
```
════════════════════════════════════════════════════
  SYSTEM VERSION: V9
  Components: 9/9 active
    FillLedger ........... SHADOW
    BrokerRegistry ....... ACTIVE (2 brokers)
    EquityTracker ........ ACTIVE
    TradierStream ........ ACTIVE
    AlpacaFillStream ..... ACTIVE
    FailoverData ......... ACTIVE (primary: tradier)
    BuyingPowerRefresher . ACTIVE (30s interval)
    DetectorCascade ...... ACTIVE (Phase1: 3, Phase2: 10)
    5minAggregation ...... ACTIVE (per-ticker cache)
════════════════════════════════════════════════════
```


## RVOL Optimization

### Problem
RVOL seeding required 183 API calls on every restart — took 51 seconds (blocking trading window).

### Solution: Three-layer optimization

| Layer | What | Latency |
|-------|------|---------|
| **Disk cache** | `data/rvol_profiles_YYYYMMDD.json` — same-day restart loads profiles instantly | **0.4ms** |
| **Parallel seeding** | `ThreadPoolExecutor(40)` with 30s timeout — first startup of the day | **~5-11s** (was 51s) |
| **Streaming cvol** | Tradier trade events include cumulative volume — instant RVOL updates | **<1ms** per update |

### Flow
```
Restart at 10:30 AM (same day)?
  → Load rvol_profiles_20260418.json (0.4ms)
  → Profiles are current, no API calls needed

First startup at 9:25 AM?
  → No cache file for today
  → Parallel seed: 40 threads × ~5 API calls each = ~5-11s
  → Save to rvol_profiles_20260418.json

During session:
  → Tradier WebSocket trade events include cumulative volume
  → update_from_cvol(ticker, cvol) computes instant RVOL ratio
  → No bar accumulation needed (streaming cvol is already cumulative)
```


## File-Based Persistence

| File | Content | Write trigger |
|------|---------|---------------|
| `bot_state.json` | Positions dict, reclaimed_today, trade_log | Every mutation + 60s periodic |
| `fill_ledger.json` | All today's lots + lot_states + meta | Every lot append / meta change |
| `data/{engine}_kill_switch.json` | Halt state (survives restart) | On kill switch trigger |
| `live_cache.json` | Market bars + RVOL (V9: JSON, was pickle) | Every 60s cycle |
| `data/rvol_profiles_YYYYMMDD.json` | RVOL profiles for today | After parallel seed completes |
| `position_registry.json` | Cross-process position dedup | On acquire/release |
| `position_broker_map.json` | Ticker → broker routing | On route decision |

All files use **SafeStateFile**: `fcntl` lock + SHA256 checksum + `.prev`/`.prev2` rolling backups + atomic write (tmp → fsync → replace).


## Monday Monitoring Checklist

### Log lines to watch in `core.log`

| Log pattern | What it means |
|-------------|---------------|
| `SYSTEM VERSION: V9` | Startup banner — all components listed |
| `SHADOW PNL DRIFT` | FillLedger vs legacy dict P&L mismatch (investigate) |
| `QUOTE EXIT` | Streaming exit triggered (working correctly) |
| `EQUITY CHECK` | Hourly drift check result |
| `FAILOVER` | Data source switched (Tradier → Alpaca IEX) |
| `STREAMING RECONNECT` | WebSocket reconnected (session epoch incremented) |
| `STALE_ORDER_CLEANUP` | Orphaned orders cancelled (every 5 min) |
| `RVOL CACHE LOADED` | Disk cache used (fast restart) |
| `BUYING_POWER_REFRESH` | Background refresh completed |

### Expected signal rates (V9 vs V8)

| Detector | V8 (1-min) | V9 (5-min) |
|----------|-----------|-----------|
| SR pivots | 20-30/day | 3-5/day |
| Inside bars | 50+/day | 5-8/day |
| Flag patterns | 15-20/day | 2-4/day |
| Fib signals | 10-15/day | 3-6/day |
| Trend flips | Every 20 min | Stable session trend |


## Backtesting Data Pipeline

### BarDataLoader Sources

The backtest engine uses `BarDataLoader` to fetch historical bar data from three sources:

| Source | Method | Range | Resolution | Speed |
|--------|--------|-------|------------|-------|
| **yfinance** | `yf.download()` | 60 days | 1-minute | Slow (rate limited) |
| **Tradier** | REST `/markets/history` | 20 days 1m, 40 days 5m | 1m + 5m | Medium (~2s/ticker) |
| **DB** | `SELECT FROM market_bars` | Unlimited (whatever is stored) | 1m + 5m | Instant (<100ms) |

### Dual Timeframe Loading

Tradier and DB sources load bars in two timeframes:
- **1-minute bars**: Recent 20 trading days (intraday precision)
- **5-minute bars**: Full 40 trading days (longer history window)

The backtest engine merges both into a unified bar stream per ticker, using 1m where available and falling back to 5m for older dates.

### Option C Pipeline (Recommended)

The preferred data pipeline for backtesting:

```
Step 1: Fetch from Tradier API → save to market_bars table
Step 2: All future backtest runs load from DB (instant, no API calls)
```

This avoids repeated API calls and rate limiting. Once bars are in the DB, backtests load in seconds regardless of ticker count.

### Backfill Script

```bash
python scripts/backfill_bars.py
```

Fetches historical 1m and 5m bars from Tradier for all tickers in the watchlist and persists them to the `market_bars` table. Run once to seed the DB, then periodically to keep data current.

### Backtest CLI

```bash
# Run backtest with Tradier data, save results to DB and CSV
python backtests/run_backtest.py --data tradier --save-to-db --csv

# Run from DB (instant, after backfill)
python backtests/run_backtest.py --data db --csv
```

### Backtest Dashboard

```bash
streamlit run dashboards/backtest_dashboard.py
# Runs on port 8502
```

Interactive Streamlit dashboard for exploring backtest results: per-strategy P&L, win rates, trade distribution, equity curves, and before/after comparison for strategy fixes.
