# V9 Backtesting Framework

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      BacktestEngine                         │
│                                                             │
│  BarDataLoader ──→ BacktestBus (SYNC) ──→ FillSimulator    │
│       │                  │                      │           │
│  (yfinance/             (BAR events             (simulated  │
│   tradier/               replayed in            fills at    │
│   db)                    timestamp order)       ask/bid)    │
│                          │                      │           │
│                    Strategy Engine          MetricsEngine   │
│                    (VWAP + Pro 11)          (P&L, win rate, │
│                                             profit factor)  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Key Components

| Component | Role |
|-----------|------|
| `BacktestEngine` | Orchestrates the full backtest: loads data, replays bars, collects results |
| `BarDataLoader` | Fetches historical bars from yfinance, Tradier API, or DB |
| `BacktestBus` | Synchronous event bus (no threading, deterministic replay) |
| `FillSimulator` | Simulates order fills at market prices (no slippage model yet) |
| `MetricsEngine` | Computes P&L, win rate, profit factor, max drawdown, Sharpe ratio |

The `BacktestBus` is a synchronous version of the production `EventBus`. Events are processed in strict timestamp order with no concurrency — this ensures deterministic, reproducible results.


## Data Sources

### Source Comparison

| Source | API | Range | Resolution | Latency | Cost |
|--------|-----|-------|------------|---------|------|
| **yfinance** | `yf.download()` | ~60 days | 1-minute | Slow (rate limited, ~5s/ticker) | Free |
| **Tradier** | REST `/markets/history` | 20 days 1m, 40 days 5m | 1m + 5m | Medium (~2s/ticker) | $10/month (existing) |
| **DB** | `SELECT FROM market_bars` | Unlimited | 1m + 5m | Instant (<100ms total) | Free (local DB) |

### Dual Timeframe Loading

When using Tradier or DB sources, bars are loaded in two timeframes:

| Timeframe | Range | Use |
|-----------|-------|-----|
| **1-minute** | Recent 20 trading days | High-precision intraday signals |
| **5-minute** | Full 40 trading days | Longer history for trend/SR/fib detectors |

The engine merges both into a single ordered bar stream per ticker. Where both 1m and 5m bars exist for the same timestamp, 1m takes priority.


## Option C Data Pipeline (Recommended)

The recommended pipeline for backtesting avoids repeated API calls:

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│  Tradier API │ ───→ │  market_bars  │ ───→ │  BacktestEngine │
│  (one-time)  │      │  (DB table)  │      │  (instant load) │
└──────────────┘      └──────────────┘      └──────────────┘
     Step 1:               Step 2:               Step 3:
   backfill_bars.py     Data persisted        --data db flag
   fetches history      permanently            loads in <100ms
```

**Step 1 — Backfill (run once)**:
```bash
python scripts/backfill_bars.py
```
Fetches 1m (20 days) and 5m (40 days) bars from Tradier for all tickers and writes to `market_bars`.

**Step 2 — Data persisted**: Bars live in TimescaleDB. No expiration, no API rate limits.

**Step 3 — Backtest from DB**:
```bash
python backtests/run_backtest.py --data db --csv
```
Loads all bars from DB instantly. A 30-ticker backtest loads in under 1 second.


## Strategies Tested

The backtest engine runs the same strategies as production:

| Engine | Strategy | Detector Count |
|--------|----------|---------------|
| **VWAP** | `vwap_reclaim` | 1 |
| **Pro** | `sr_flip`, `trend_pullback`, `orb`, `gap_and_go`, `inside_bar`, `flag_pennant`, `momentum_ignition`, `fib_confluence`, `bollinger_squeeze`, `liquidity_sweep`, `sentiment_surge` | 11 |
| **Total** | | **12 strategies** |


## gap_and_go Fix

Backtesting revealed that `gap_and_go` was generating 68% of all trades with a losing profile. The fix tightened entry criteria:

| Parameter | Before | After |
|-----------|--------|-------|
| Gap threshold | 0.5% | 1.5% |
| Volume confirmation | None | Required (RVOL > 2x) |
| Trend alignment | None | Required (5m trend match) |
| VWAP position | None | Required (price above VWAP) |
| Continuation filter | 50/50 | 70/30 (strong bias required) |

This effectively eliminated false gap_and_go signals while preserving high-quality setups.


## Backtest Results (April 19, 2026)

Backtest run: 30 tickers, 33 trading sessions (mixed 1m and 5m data).

### Before gap_and_go Fix

| Metric | Value |
|--------|-------|
| Total trades | 34,000 |
| Win rate | 30.6% |
| Net P&L | -$587 |
| Profit factor | 0.96 |
| gap_and_go % of trades | 68% |

### After gap_and_go Fix

| Metric | Value |
|--------|-------|
| Total trades | 20,000 |
| Win rate | 38.7% |
| Net P&L | +$568 |
| Profit factor | 1.06 |
| gap_and_go trades | 0 (effectively filtered out) |

### Key Takeaways

- Removing one bad strategy flipped the system from net-negative to net-positive.
- Profit factor of 1.06 is thin but positive — focus on improving to 1.3+ before scaling capital.
- Trade count reduction from 34K to 20K shows the system was massively over-trading on low-quality signals.


## Running Backtests

### CLI

```bash
# From Tradier API (fetches live, slower)
python backtests/run_backtest.py --data tradier --save-to-db --csv

# From DB (instant, after backfill)
python backtests/run_backtest.py --data db --csv

# From yfinance (free, no API key, slower)
python backtests/run_backtest.py --data yfinance --csv
```

### Dashboard

```bash
streamlit run dashboards/backtest_dashboard.py
# Opens on http://localhost:8502
```

The dashboard provides:
- Per-strategy P&L breakdown
- Win rate by strategy and ticker
- Trade distribution over time
- Equity curve visualization
- Before/after comparison for strategy fixes


## Dry-Run Integration Test

```bash
python -m pytest test/test_v9_dryrun.py -v
```

The dry-run test validates the full backtest pipeline end-to-end:
- BarDataLoader fetches data correctly
- BacktestBus replays events in order
- FillSimulator produces fills
- MetricsEngine computes accurate P&L
- No crashes or unhandled exceptions across a multi-ticker, multi-session run
