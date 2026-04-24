# Trading Hub (V9)

A real-time algorithmic trading system built on a hybrid streaming + polling architecture. V9 adds lot-based position tracking (FillLedger), real-time WebSocket streaming, sub-second exit detection, and 47 correctness fixes over V8.

> **Disclaimer:** This is for educational purposes only. Trading involves significant risk. Always start with paper trading. Consult a financial advisor before trading with real money.

---

## Architecture (V9 — Hybrid Streaming + Polling)

3 processes under supervisor. Real-time Tradier WebSocket for exit monitoring, REST polling for entry discovery.

```
Supervisor (startup cleanup: __pycache__, state validation, deprecated files)
  |
  +-- Core (VWAP + Pro 11 strategies + Pop scanner)
  |     +-- TradierStreamClient (WebSocket, <1s exit detection, prod $10/month)
  |     +-- 13 detectors (V9: 2-phase cascade, 80% skip at open)
  |     +-- FillLedger (lot-based P&L, FIFO matching, shadow mode)
  |     +-- BrokerRegistry (extensible multi-broker)
  |     +-- EquityTracker (hourly P&L drift detection)
  |     +-- PortfolioRiskGate (V9: background buying power, <1ms)
  |     +-- SmartRouter -> Alpaca + Tradier (V9: lot-aware routing)
  |     +-- FailoverDataClient (Tradier -> Alpaca IEX auto-switch)
  |     +-- Publishes SIGNAL + POP_SIGNAL to Options via Redpanda
  |
  +-- Options (13 multi-leg strategies, separate Alpaca account)
  |     +-- V9: _halted flag + persisted kill switch state
  |     +-- tick() before BAR emit (defense-in-depth)
  |
  +-- Data Collector (8 alt data sources)

Event Flow:
  Tradier WebSocket -> QUOTE -> StrategyEngine (exit checks, <1ms)
  Tradier REST -> BAR -> StrategyEngine/ProSetupEngine -> SIGNAL
  SIGNAL -> RiskEngine (V9: streaming ask price, 0.02ms) -> ORDER_REQ
  ORDER_REQ -> PortfolioRiskGate (V9: <1ms buying power) -> SmartRouter
  SmartRouter -> Broker -> FILL (V9: Alpaca WebSocket <100ms)
  FILL -> PositionManager -> FillLedger (FIFO lot match) + bot_state.json
  FILL -> PositionManager -> POSITION -> KillSwitch -> event_store DB
```

### Key V9 Changes

| Change | V8 | V9 |
|--------|----|----|
| Exit detection | 0-60s (REST poll) | <1s (WebSocket streaming) |
| Ask quote (risk check) | 300-3000ms (REST) | 0.02ms (streaming cache) |
| Buying power check | 3-10s (inline API) | <1ms (background refresh) |
| Market open spike | ~10s (all detectors) | <2s (Phase 1 cascade) |
| Fill confirmation | 0.5-5s (REST poll) | <100ms (Alpaca WebSocket, FREE) |
| Settlement verify | 1s (blind sleep) | 200-600ms (fast poll) |
| Position tracking | Mutable dict (single entry_price) | FillLedger (lot-based, FIFO P&L) |
| P&L calculation | (exit - entry) × qty (wrong after reconcile) | Per-lot FIFO matching (exact) |
| Entry price on reconcile | 1% drift threshold | Always broker cost basis |
| Shared cache format | Pickle (fragile) | JSON (human-readable, schema-stable) |
| Bar persistence | event_store (97% bloat) | market_bars (10x smaller) |
| Kill switch (options) | Resets on restart | Persisted to disk |
| Alert blackout | 30 min | 5 min + CRITICAL bypass |
| Stale orders | Orphaned forever | Cancelled after 60s |
| Duplicate SELL | Not guarded | Dedup by ticker:order_id |
| Fill dedup | Buggy set truncation | OrderedDict + 1hr TTL |
| Data source failure | Full halt | Auto-failover to Alpaca IEX |
| Fractional shares | Int qty only | Float qty (Alpaca: 0.01 increments) |

---

## Latency Profile

### Entry Trade

| Stage | Before V9 | After V9 |
|-------|-----------|----------|
| Data staleness | 0-73s | <0.5s (streaming) |
| Strategy analysis | 35ms / 10s spike | 35ms / <2s spike |
| Risk engine | 500-3000ms | <5ms |
| Portfolio risk | 3-10s | <5ms |
| Fill confirmation (Alpaca) | 0.5-5s | <100ms |
| **Total (Alpaca)** | **8-25s** | **~1-3s** |

### Exit Trade

| Stage | Before V9 | After V9 |
|-------|-----------|----------|
| Detection | 0-60s | <1s (streaming) |
| **Total (streaming + Alpaca)** | **1-70s** | **~1-4s** |

### Full Round-Trip

| Scenario | Before V9 | After V9 |
|----------|-----------|----------|
| Best case | ~10s | **~3s** |
| Typical | ~2-3 min | **~15-30s** |
| Worst case | 5+ min | **~1-2 min** |

---

## Token / Account Setup

```
TRADIER PRODUCTION ($10/month) ── TRADIER_TOKEN
  Data: WebSocket streaming + REST bars
  Orders: NEVER (data only)

TRADIER SANDBOX (free) ── TRADIER_SANDBOX_TOKEN
  Orders: Paper trading (buy/sell)
  Data: No streaming

ALPACA PAPER (free) ── APCA_API_KEY_ID
  Orders: Paper trading (buy/sell)
  Fill status: FREE WebSocket trade_updates
  Data: NOT used (IEX only on free tier)
```

---

## Data Sources (10 total)

| # | Source | API Key | Data |
|---|--------|---------|------|
| 1 | Benzinga | Yes | Headlines, sentiment |
| 2 | StockTwits | Optional | Social mentions, sentiment |
| 3 | Fear & Greed | No | Market sentiment index |
| 4 | Finviz | No | Screener, insider, analyst |
| 5 | SEC EDGAR | No | Insider filings |
| 6 | Yahoo Finance | No | Earnings, recommendations |
| 7 | Polygon.io | Yes | Prev-day movers, options |
| 8 | Alpha Vantage | Yes | Technicals |
| 9 | FRED | Yes | Macro indicators (rates, VIX) |
| 10 | UOF Scanner | Via chain API | Unusual options flow |

---

## Risk Stack (20+ Checks)

| # | Check | Component |
|---|-------|-----------|
| 1 | Max positions (per-strategy) | RiskEngine/RiskAdapter |
| 2 | Cooldown (300s per ticker) | RiskEngine/RiskAdapter |
| 3 | RVOL threshold | RiskEngine |
| 4 | RSI range (sentiment-adaptive) | RiskEngine |
| 5 | Fresh ask quote (V9: streaming) | RiskEngine |
| 6 | Spread width | RiskEngine |
| 7 | Price divergence | RiskEngine |
| 8 | Correlation groups (14) | RiskSizer |
| 9 | Beta-weighted exposure | RiskSizer |
| 10 | Beta-adjusted sizing | RiskSizer |
| 11 | ATR-scaled sizing | RiskSizer |
| 12 | Confidence threshold (tier) | RiskAdapter |
| 13 | R:R ratio (tier) | RiskAdapter |
| 14 | Sector concentration | RiskAdapter |
| 15 | Earnings block (T2/T3) | RiskAdapter |
| 16 | Drawdown halt | PortfolioRiskGate |
| 17 | Notional cap | PortfolioRiskGate |
| 18 | Buying power (V9: background) | PortfolioRiskGate |
| 19 | Greeks limits | PortfolioRiskGate |
| 20 | Per-strategy kill switch | PerStrategyKillSwitch |
| 21 | Cross-layer dedup | RegistryGate |
| 22 | V9: Bar data validation | StrategyEngine |
| 23 | V9: Duplicate SELL guard | PositionManager |

---

## Strategies

### VWAP Reclaim (Core)

Entry: 2-bar VWAP reclaim + RSI 50-70 + RVOL >= 2x + SPY above VWAP.
Exit: Trailing stop, target (2x ATR), partial at 1x ATR, RSI > 75, VWAP breakdown, EOD close.
V9: Streaming exit detection (<1s).

### Pro Setups (13 detectors, 11 strategies, 3 tiers)

V9 multi-timeframe: 5 detectors use 5-min bars (SR, InsideBar, Flag, Fib, Trend), 8 stay on 1-min.

| Tier | Strategies | Stop | EOD |
|------|-----------|------|-----|
| T1 | sr_flip, trend_pullback, vwap_reclaim | 0.4-0.5 ATR | Full close |
| T2 | orb, gap_and_go, inside_bar, flag_pennant | 1.5-2.0 ATR | Partial sell |
| T3 | momentum_ignition, fib_confluence, bollinger_squeeze, liquidity_sweep | 2.0-2.5 ATR | Partial sell |

V9: 2-phase detector cascade. Phase 1 (Trend+VWAP+ORB, 1.5ms) skips Phase 2 for ~80% of tickers.

### Options (13 strategies, separate process)

Iron condor, iron butterfly, bull/bear call/put spreads, long call/put, calendar, diagonal, straddle, strangle, collar.
V9: Kill switch persisted to disk, _halted flag prevents churn on kill switch trigger.

---

## Quick Start

### 1. Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install websocket-client  # V9: for streaming
```

Python 3.10+ required.

### 2. Configure `.env`

```env
# Tradier (data streaming + paper trading)
TRADIER_TOKEN=your_production_token         # $10/month — streaming + data
TRADIER_SANDBOX_TOKEN=your_sandbox_token    # free — paper trading
TRADIER_ACCOUNT_ID=your_account_id
TRADIER_SANDBOX=true

# Alpaca (paper trading + free fill WebSocket)
APCA_API_KEY_ID=your_key
APCA_API_SECRET_KEY=your_secret
APCA_API_BASE_URL=https://paper-api.alpaca.markets

# Execution mode
BROKER_MODE=smart
MONITOR_MODE=supervisor
DATA_SOURCE=tradier

# Database
DB_ENABLED=true
DATABASE_URL=postgresql://trading:trading@localhost:5432/tradinghub
REDPANDA_BROKERS=127.0.0.1:9092
```

### 3. Start infrastructure

```bash
cd docker && docker compose up -d    # TimescaleDB + Redpanda
python -m db.migrations.run          # schema migrations
psql $DATABASE_URL < db/schema_v9_fill_ledger.sql  # V9 tables
```

### 4. Run

```bash
bash start_monitor.sh
```

### 5. Cron

```
0 6 * * 1-5 /path/to/trading_hub/start_monitor.sh >> /path/to/logs/cron.log 2>&1
```

---

## Database

| Table | Purpose | V9 |
|-------|---------|-----|
| `event_store` | Immutable event log | BarReceived writes stopped |
| `market_bars` | V9: Lean bar table (10x smaller) | NEW |
| `fill_lots` | V9: Lot-level position data | NEW |
| `lot_matches` | V9: FIFO P&L audit trail | NEW |
| `completed_trades` | Trade summaries | qty: NUMERIC(12,6) |
| `data_source_snapshots` | Alt data from 8 sources | unchanged |

---

## V9 Docs

- `docs/V9_Trading_Hub/01_ARCHITECTURE_OVERVIEW.md` — system identity + process map
- `docs/V9_Trading_Hub/02_DATA_PIPELINE.md` — streaming + polling + EventBus
- `docs/V9_Trading_Hub/03_STRATEGY_AND_RISK.md` — detectors + risk checks + latency
- `docs/V9_Trading_Hub/04_ORDER_EXECUTION.md` — brokers + fills + FillLedger
- `docs/V9_Trading_Hub/05_LATENCY_PROFILE.md` — measured benchmarks + before/after
- `docs/V9_Trading_Hub/06_SAFETY_AND_RECONCILIATION.md` — kill switch + reconciliation + guards
- `docs/V9_Trading_Hub/07_MULTI_TIMEFRAME_DETECTORS.md` — 5-min bars for SR/Trend/Fib/Flag/InsideBar + activation timeline
- `docs/V9_Trading_Hub/08_DATABASE_AND_OPERATIONS.md` — schema, analytics queries, startup sequence, RVOL optimization
- `docs/V9_Trading_Hub/09_WATCHDOG_AND_SELF_HEALING.md` — session watchdog, self-healing, HotfixManager
- `docs/V9_Trading_Hub/10_BACKTESTING.md` — backtest engine, data pipeline, strategies, results
- `docs/V9_Trading_Hub/11_EVOLUTION_AND_ROADMAP.md` — V1-V9 timeline, system metrics, future roadmap
- `docs/V9_Trading_Hub/12_FUTURE_ENHANCEMENTS.md` — complete roadmap: profitability filter, ranking wiring, data upgrades, sector rotation, hedging
- `docs/V9_Trading_Hub/13_FOUNDATION_FIXES.md` — 9 foundation fixes + V10 exit engine: Order WAL, reconciliation, FillLedger authority, phase-based exits, trade analysis

### Previous versions
- `docs/V8_trading_hub/` — V8 architecture (reference)
- `docs/V7_trading_hub/` — V7 design docs
- `docs/V6_trading_hub/` — V6 design docs

---

## Requirements

- Python 3.10+
- Docker (TimescaleDB 2.26+ / PostgreSQL 16 + Redpanda)
- Tradier account ($10/month for streaming, sandbox free for paper trading)
- Alpaca account (paper trading free, WebSocket fills free)
- Yahoo Mail app password for alerts (optional)
- macOS/Linux

---

## Backtesting

V9 includes a full backtesting framework with a Tradier + DB data pipeline.

- **Data pipeline (Option C)**: Fetch 1m/5m bars from Tradier API, persist to `market_bars` DB table, future loads read from DB instantly (no API calls).
- **Dual timeframe**: 1-minute bars for recent 20 days, 5-minute bars for full 40-day window. `BarDataLoader` supports yfinance, Tradier, and DB sources.
- **Backfill**: `python scripts/backfill_bars.py` populates DB from Tradier history.
- **Run**: `python backtests/run_backtest.py --data tradier --save-to-db --csv`
- **Dashboard**: `streamlit run dashboards/backtest_dashboard.py` (port 8502) — interactive results viewer with per-strategy breakdown.

---

## Session Watchdog

`scripts/session_watchdog.py` provides self-healing monitoring for 3 child processes under Supervisor (Core, Options, DataCollector).

- **Health checks**: Process alive, zombie detection (log staleness), log error scanning, signal rate monitoring, position tracking, kill switch state, fill ledger integrity, data freshness, memory usage, gap_and_go detection.
- **Self-healing**: Corrupt JSON restore from `.prev` backups, stale `.lock` file removal, `__pycache__` clearing, supervisor restart, zombie process kill.
- **HotfixManager**: Auto-applies fixes for common runtime errors (NameError, AttributeError, TypeError, ImportError, KeyError, IndexError, ZeroDivisionError) during paper trading. Creates `.bak` backups, syntax-checks before write, max 5 fixes per session, protected files list.
- **Email updates**: 7 hourly status reports during market hours (9:30 AM - 4:00 PM ET).
- **Resilient loop**: `_run_one_cycle()` with cycle-level try/except. Escalates after 10 consecutive errors.

---

## Emergency Rollback

`emergency_rollback.sh` reverts the entire codebase to the V9 baseline (`v9-baseline` tag, commit `b838c04`) — the last tested, stable production run.

```bash
./emergency_rollback.sh            # Stop all processes, revert code, clear pycache
./emergency_rollback.sh --dry-run  # Preview without changes
```

Stops all trading processes, stashes uncommitted work, checks out baseline code, clears `__pycache__`, and verifies key files. Does NOT auto-restart — run `start_monitor.sh` manually after verifying.

---

## V10 Foundation Hardening (April 22, 2026)

The April 22 session exposed 20 systemic issues including $50 P&L drift, options engine blind all day, and 3 concurrent supervisors. All 9 foundation fixes implemented same-day:

| Fix | What |
|---|---|
| **Order WAL** | Write-ahead log tracks every order INTENT→SUBMITTED→FILLED→RECORDED. Covers core + options. 0.04ms overhead. |
| **Continuous Reconciliation** | Broker vs local comparison every 5 min (was 10/30/60). Alert on divergence. |
| **FillLedger P&L Authority** | `daily_pnl` + `open_positions` persisted in fill_ledger.json v2. Single P&L source. |
| **State Path Constants** | 6 paths in config.py. Zero hardcoded paths in production code. |
| **Test Isolation** | `conftest.py` autouse fixture. Tests write to tmp_path, never production. |
| **Supervisor Lock** | `fcntl.flock` prevents duplicate supervisors. |
| **Startup Preflight** | 10 checks (SMTP, brokers, state, IPC, DB) before trading starts. |
| **Error Escalation** | 19 critical `log.debug` → `log.warning`. No more silent failures. |
| **Race Condition** | `list(dict)` snapshot prevents `dict changed size during iteration` crash. |

Also fixed: 5 backtest simulator bugs, edge context data capture (4 signal paths + DB), IPC signal forwarding, email P&L/trades, watchdog health checks. See `docs/V9_Trading_Hub/13_FOUNDATION_FIXES.md`.

## V10 Exit Engine (April 23, 2026)

April 23 analysis revealed the system captured -8% of $984 available profit. Entries were valid — exits were destroying value (78 trades held <10 seconds, RSI/VWAP panic exits killed winners).

Complete exit engine redesign: `monitor/exit_engine.py`

| Phase | Purpose | Key Behavior |
|---|---|---|
| **Phase 0** | Entry validation | Per-strategy thesis check (SR held? ORB held? Volume confirming?) |
| **Phase 1** | Protection | Stop only. No RSI/VWAP exits. Higher low detection. |
| **Phase 2** | Breakeven | Stop to entry (cushion for structure). RSI tightens trail mildly. |
| **Phase 3** | Profit harvest | Confluence-aware partials (33-66%). Structure trail. RSI tightens normally. |
| **Phase 4** | Runner | 5-bar swing low + VWAP floor + R floor. Let winners run. |

10 per-strategy exit profiles. Impulse vs structure classification. All lifecycle decisions captured to DB (`position_events.close_detail.lifecycle`).

**Trade analysis:** `reports/daily_analysis/trade_analysis_YYYYMMDD.csv` + Streamlit dashboard at `dashboards/trade_analysis_dashboard.py` (port 8503).

---

## First Production Run (April 20, 2026)

The system ran its first production session on April 20. All 4 processes started and stayed alive for the full session. No trades were executed due to a `gap_and_go` low-coverage crash that caused the strategy engine to halt early. The root cause was identified and fixed:

- `gap_and_go` threshold raised from 0.5% to 1.5% with added volume confirmation, trend alignment, VWAP requirement, and tighter continuation filters (70/30).
- Backtesting confirmed the fix: trades dropped from 34K to 20K, win rate improved from 30.6% to 38.7%, P&L swung from -$587 to +$568, profit factor improved from 0.96 to 1.06.
