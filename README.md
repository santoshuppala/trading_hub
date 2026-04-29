# Trading Hub V10

Production algorithmic trading system. Equities + options. Real money via Tradier and Alpaca. Sub-second tick-level entries, phase-based lifecycle exits, context-aware smart stops, 7-layer signal protection.

> **Disclaimer:** Trading involves significant risk. This system trades with real money. Always validate with paper trading first.

---

## System Identity

- **3 coordinated engines** under supervisor: Core (VWAP + Pro), Options, Data Collector
- **200+ tickers** across mega-cap tech, semis, fintech, crypto-adjacent, energy, healthcare, consumer, ETFs
- **Sub-second entries** via WebSocket tick detection (5 detectors), 60s bar-level backup (13 detectors)
- **Phase-based exits** — 5-phase lifecycle per position (Validation → Protection → Breakeven → Harvest → Runner)
- **Dual broker** — Alpaca (execution + free WebSocket fills) + Tradier (streaming data $10/mo + paper execution)
- **Event-driven** — ASYNC EventBus with 26 workers, bounded queues, backpressure, coalescing

---

## Architecture

```
launchd (PID 1)
  └── Outer Watchdog (market-aware, holiday-aware)
       └── Supervisor (process manager, preflight checks, fcntl lock)
            ├── Core Process (VWAP + Pro strategies + Pop scanner)
            │    ├── TradierStreamClient (WebSocket)
            │    │    ├── BarBuilder (tick → 1-min OHLCV bars)
            │    │    └── TickDetector (5 sub-second detectors)
            │    ├── EventBus (ASYNC, 26 workers, bounded queues)
            │    ├── ProSetupEngine (13 detectors, 11 strategies, 3 tiers)
            │    ├── StrategyEngine (VWAP Reclaim)
            │    ├── ExitEngine (5-phase lifecycle)
            │    ├── RiskEngine (VWAP) + RiskAdapter (Pro) — 23 risk checks
            │    ├── SmartRouter → Alpaca + Tradier (lot-aware routing)
            │    ├── FillLedger (FIFO lot matching, P&L authority)
            │    ├── PositionManager + Order WAL (crash recovery)
            │    └── EngineLifecycle (state persistence, kill switch, heartbeat)
            │
            ├── Options Process (13 multi-leg strategies, separate Alpaca account)
            │    ├── OptionsExitEngine (3-phase: OPEN → THETA → CLOSE/ROLL)
            │    ├── OptionsRiskGate + PortfolioGreeksTracker
            │    └── IPC consumer (SIGNAL + POP_SIGNAL from Core via Redpanda)
            │
            └── Data Collector (10 alt data sources, independent pipeline)
```

### Data Flow — Two Parallel Paths

```
PATH A: Sub-second (WebSocket ticks)
  Tradier WebSocket → trade tick (price, size, cvol)
    ├── BarBuilder.on_trade() → accumulates per-ticker per-minute
    │   └── At :00 boundary → BAR event → ProSetupEngine (13 detectors)
    └── TickDetector.on_tick() → 5 tick-level detectors
        └── PRO_STRATEGY_SIGNAL → RiskAdapter → ORDER_REQ → Broker

PATH B: REST polling fallback (12s cycle, illiquid tickers only)
  Tradier REST API → 200 tickers × bars
    └── BAR event → StrategyEngine (VWAP) + ProSetupEngine
        └── SIGNAL → RiskEngine → ORDER_REQ → SmartRouter → Broker

EXITS: WebSocket QUOTE → ExitEngine (5-phase lifecycle, is_quote=True) → SELL
       Reconciler (5 min) → phantom detection → broker query → P&L capture

CROSS-PROCESS (Core → Options):
  SharedCache: Core writes bars/rvol to JSON, Options reads every 10s
  Redpanda IPC: Core publishes SIGNAL + POP_SIGNAL + PRO_STRATEGY_SIGNAL
                Options consumes and injects into local EventBus
  Discovery: Data Collector publishes new tickers → Core adds to scan universe
```

---

## Sub-Second Entry System (TickDetector)

5 tick-level detectors fire on live WebSocket ticks instead of waiting for 60s bar close:

| Detector | Trigger | Stop |
|----------|---------|------|
| sr_flip | Price crosses S/R level | Below flipped level |
| orb | Price breaks opening range (first 30 min only) | Below ORB range |
| momentum_ignition | Volume spike + range expansion in 30s window | Below window low |
| gap_and_go | New session high in gap direction | Below prior high |
| liquidity_sweep | Sweep below support then reverse within 30s | Below sweep low |

### 7 Protection Layers

| Layer | What It Prevents |
|-------|------------------|
| Trade freeze | Blocks all orders until 10+ tickers have 2 live BARs + fresh indicators |
| Data readiness | Per-ticker: needs 2+ live BAR cycles, not just snapshot data |
| Gap regime | 4-tier classification (none/small/medium/large). Large gaps invalidate all S/R levels |
| Adaptive sampling | Under load (>50 ticks/100ms): sample every Nth tick. Large price moves always processed |
| Level versioning | Detects BAR/tick race conditions. Discards signals computed against stale levels |
| Additive confidence | 0.50 x signal + 0.20 x regime + 0.20 x freshness + 0.10 x stability. Prevents collapse |
| Smart stop | Volatility regime (calm/normal/hot) x time-of-day (open/lunch/close) scaling |

---

## Exit Engine — Phase-Based Lifecycle

| Phase | Name | Behavior |
|-------|------|----------|
| 0 | Validation | Per-strategy thesis check. Fail → immediate exit |
| 1 | Protection | Stop only. No RSI/VWAP exits. Higher-low detection |
| 2 | Breakeven | Move stop to entry. RSI trail tightens mildly |
| 3 | Harvest | Confluence-aware partials (33-66%). Structure trail |
| 4 | Runner | 5-bar swing low + VWAP floor + R floor. Let winners run |

10 per-strategy exit profiles. Trade quality scorer (trend_score + failure_score) logs per-bar for ML calibration.

### Options Exit Engine — Risk-Regime Lifecycle

| Phase | Trigger | Behavior |
|-------|---------|----------|
| OPEN | DTE > 21 | Monitor Greeks, IV crush detection |
| THETA | 21 >= DTE > 10 | Tighten stops, theta decay management |
| CLOSE/ROLL | DTE <= 10 | Force close or roll if profitable + safe |

Moneyness pressure (continuous 0→1), weighted risk score (delta 1.5x + vega 1.2x + theta 1.0x), strategy-aware fatal signals.

---

## Smart Stop System

Context-aware stop distance scaled by two real-time factors:

**Volatility regime** — recent price range vs ATR:
```
Calm (range < 0.5 ATR):  buffer = ATR x 0.3  (tight, less noise)
Normal (range ≈ 1 ATR):  buffer = ATR x 0.5  (standard)
Hot (range > 1.5 ATR):   buffer = ATR x 0.8  (wide, survive volatility)
```

**Time of day** — market volatility pattern:
```
9:30-10:00:  x1.5  (opening volatility)
10:00-11:00: x1.2  (settling)
11:00-14:00: x0.8  (lunch, tightest)
14:00-15:00: x1.0  (afternoon)
15:00-16:00: x1.3  (close volatility)
```

Bounds: never tighter than 0.25 ATR, never wider than 1.5 ATR. Trades with stop > 2 ATR rejected (bad R:R).

---

## Risk Stack (23 Checks)

| # | Check | Component |
|---|-------|-----------|
| 1 | Trade freeze (startup) | TickDetector + RiskAdapter |
| 2 | Stopped-today blacklist | RiskAdapter |
| 3 | Max positions (per-strategy) | RiskEngine / RiskAdapter |
| 4 | Cooldown (300s per ticker) | RiskEngine / RiskAdapter |
| 5 | Reclaimed-today dedup | RiskEngine |
| 6 | RVOL threshold | RiskEngine |
| 7 | RSI range (sentiment-adaptive) | RiskEngine |
| 8 | Fresh ask quote (streaming) | RiskEngine |
| 9 | Spread width | RiskEngine |
| 10 | Price divergence | RiskEngine |
| 11 | Correlation groups (14) | RiskSizer |
| 12 | Beta-weighted exposure | RiskSizer |
| 13 | Beta-adjusted sizing | RiskSizer |
| 14 | ATR-scaled sizing | RiskSizer |
| 15 | Confidence threshold (tier) | RiskAdapter |
| 16 | R:R ratio (tier) | RiskAdapter |
| 17 | Sector concentration (max 2) | RiskAdapter |
| 18 | Earnings block (T2/T3) | RiskAdapter |
| 18b | Alt data conviction boost (0-0.3) | RiskAdapter |
| 19 | Drawdown halt | PortfolioRiskGate |
| 20 | Notional cap | PortfolioRiskGate |
| 21 | Buying power (background refresh) | PortfolioRiskGate |
| 22 | Per-strategy kill switch | PerStrategyKillSwitch |
| 23 | WAL atomic dedup (check_and_intent) | Order WAL |

---

## Position Sizing

Risk-based sizing via `RiskSizer`:
```
base_qty = floor(trade_budget / ask_price)
→ adjusted by beta (high beta = smaller size)
→ adjusted by ATR volatility (high ATR = smaller size)
→ capped by correlation group exposure (max 2 same-sector)
→ capped by buying power (real-time background refresh)
```

Each trade risks a fixed dollar amount, not fixed share count. A $1,000 budget on a $50 stock with 2.0 ATR volatility sizes differently than the same budget on a $10 stock with 0.5 ATR.

---

## Strategies

### VWAP Reclaim (Core)
Entry: 2-bar VWAP reclaim + RSI 50-70 + RVOL >= 2x + SPY above VWAP.
Exit: 5-phase lifecycle. Streaming exit detection (<1s).

### Pro Setups (13 detectors, 11 strategies, 3 tiers)

| Tier | Strategies | Characteristics |
|------|-----------|-----------------|
| T1 | sr_flip, trend_pullback, vwap_reclaim | Intraday close. Tight stops. |
| T2 | orb, gap_and_go, inside_bar, flag_pennant | Partial overnight. Medium stops. |
| T3 | momentum_ignition, fib_confluence, bollinger_squeeze, liquidity_sweep | Partial overnight. Wide stops. |

2-phase detector cascade: Phase 1 (Trend+VWAP+ORB) skips Phase 2 for ~80% of tickers.
ORB cutoff: 30 bars (30 min from open). No more all-day ORB noise.

### Options (13 strategies)
Iron condor, iron butterfly, bull/bear call/put spreads, long call/put, calendar, diagonal, straddle, strangle, collar.
IV rank + earnings calendar + Greeks tracking.

---

## Cold Start, Crash Recovery, Restart Behavior

### Cold Start (system off overnight)
```
Snapshot load → BB: 200x40 bars, emit_bars=True
                TD: levels + indicators (snapshot, NOT data-ready)
                    order_freeze = True
Ticks arrive  → Gap classified per ticker on FIRST TICK
                Adaptive sampling active
9:31:00       → 1st BB flush, ProSetupEngine → live_bar_count=1
9:32:00       → 2nd BB flush → DATA READY, FREEZE LIFTED
                All 5 tick detectors active
```

### Crash Restart (mid-session)
```
Snapshot: today's data (saved at crash). BB emit_bars=True immediately.
Gap regime: classified from first tick vs prev_close.
2 BAR flushes → DATA READY (~90s). No artificial 5-min blind timer.
Positions protected by broker-side stops during 90s gap.
WAL recovery: checks incomplete orders against broker status.
```

### State Persistence
- `bot_state.json` — positions, trade_log, daily P&L. Saved on every state change.
- `fill_ledger.json` — lot-level fills with FIFO matching. P&L authority.
- `data/market_snapshot.json` — bars + indicators for cold start. Saved on EVERY shutdown (not just EOD). ~2MB for 200 tickers x 40 bars.
- `data/order_wal_YYYYMMDD.log` — write-ahead log for order crash recovery.

### Reconciliation (every 5 min)
3-way sync: local positions vs Alpaca vs Tradier.
- Positions at broker but not local → import (with broker cost basis as entry)
- Positions local but not at broker → phantom close (query broker for real exit price)
- Quantity mismatch → adjust to broker truth
- Phantom P&L captured via `_get_phantom_exit_price()` (queries broker order history)

### Execution Timing
- `TRADE_START_TIME = 09:45` — no entries before this (market needs to settle)
- `FORCE_CLOSE_TIME = 15:00` — force-close positions before EOD
- `is_quote` parameter isolates QUOTE handler from BAR logic (prevents bars_held corruption, buffer poisoning, false VWAP exits)

### Email Alerts
SMTP via Yahoo Mail app password. Severity levels: INFO, WARNING, CRITICAL. CRITICAL bypasses 5-min blackout. Alerts for: kill switch trigger, data pipeline stale >120s, P&L drift >$50, supervisor restart, broker API errors.

### Key Design Decisions
- **Data readiness, not wall-clock**: system activates when data is trustworthy, not after arbitrary timer
- **Trade freeze**: signals observe and log, but no real orders until 10+ tickers verified
- **Stopped-today blacklist**: ticker stopped out → blocked for rest of session (no 17x churn)
- **ORB cutoff at 30 min**: both tick-level (from actual 9:30 ET) and bar-level

---

## Latency Profile

### Entry (V10 — sub-second path)
| Stage | Latency |
|-------|---------|
| WebSocket tick arrival | ~10-50ms |
| TickDetector processing | <50μs (with adaptive sampling) |
| Signal → RiskAdapter | <1ms (EventBus ASYNC) |
| Risk checks (23 gates) | <5ms |
| Broker order submission | 50-200ms |
| Fill confirmation (Alpaca WS) | <100ms |
| **Total** | **~200-400ms** |

### Exit (streaming)
| Stage | Latency |
|-------|---------|
| QUOTE detection | <1s (WebSocket) |
| ExitEngine lifecycle eval | <1ms |
| SELL order | 50-200ms |
| **Total** | **~1-2s** |

### REST Fallback (illiquid tickers)
| Stage | Latency |
|-------|---------|
| Data staleness | 0-12s (REST cycle) |
| Bar-level detection | ~35ms |
| Risk + execution | ~200ms |
| **Total** | **~1-13s** |

---

## EventBus (ASYNC Mode)

26 workers with bounded queues and backpressure:

| Event Type | Workers | Max Queue | Policy |
|------------|---------|-----------|--------|
| BAR | 8 | 500 | DROP_OLDEST + coalesce |
| QUOTE | 2 | 100 | DROP_OLDEST + coalesce |
| PRO_STRATEGY_SIGNAL | 2 | 100 | DROP_OLDEST |
| ORDER_REQ | 1 | 100 | BLOCK (never drop) |
| FILL | 1 | 100 | BLOCK |
| ORDER_FAIL | 1 | 50 | BLOCK |
| POSITION | 1 | 50 | BLOCK |

BackpressureMonitor: WARN at 60%, THROTTLE at 80%, ALERT at 90%.

---

## Resilience & Self-Healing

### Data Failover
`FailoverDataClient` wraps primary (Tradier) and secondary (Alpaca IEX) data sources. If Tradier REST fails 3 consecutive times, auto-switches to Alpaca IEX with CRITICAL alert. Switches back when primary recovers.

### Market Calendar
`market_calendar.py` — US market holidays 2026-2028, early close days (1 PM ET). Outer watchdog and supervisor skip non-trading days. EngineLifecycle uses it for EOD timing.

### Position Registry
`DistributedPositionRegistry` — global position counting across all 3 engines (Core VWAP + Pro + Options). `RegistryGate` enforces the 75-position global cap. Lease-based cleanup on crash recovery.

### Equity Tracker
`EquityTracker` — hourly P&L drift detection. Compares broker account equity vs internal P&L tracking. Alerts if drift > $50 (catches reconciliation errors, phantom fills, missed closes).

### Session Watchdog
`session_watchdog.py` — self-healing monitoring for all child processes:
- Health checks: process alive, zombie detection, log staleness, signal rate, position tracking, kill switch state, memory usage
- Self-healing: corrupt JSON restore from `.prev` backups, stale `.lock` removal, supervisor restart, zombie kill
- `HotfixManager` (V9, replaced by Claude Fix Agent in V10): auto-applied fixes for common runtime errors during paper trading
- 7 hourly email status reports during market hours

### Emergency Rollback
```bash
./emergency_rollback.sh            # Stop all, revert to v9-baseline tag
./emergency_rollback.sh --dry-run  # Preview without changes
```

### Numpy Fast Path
RSI/ATR/VWAP computed via numpy arrays instead of pandas (14x speedup). Content-based LRU cache with `OrderedDict`. Flat-price RSI guard returns 50.0 (not 0.0). `math.isfinite()` validation catches inf values.

---

## Configuration

### Budgets & Limits

| Setting | Value | Env Override |
|---------|-------|-------------|
| VWAP max positions | 5 | MAX_POSITIONS |
| Pro max positions | 15 | PRO_MAX_POSITIONS |
| Options max positions | 5 | OPTIONS_MAX_POSITIONS |
| Global max (all layers) | 75 | GLOBAL_MAX_POSITIONS |
| VWAP budget per trade | $1,000 | TRADE_BUDGET |
| Pro budget per trade | $1,000 | PRO_TRADE_BUDGET |
| Options budget per trade | $2,000 | OPTIONS_TRADE_BUDGET |
| Options total ceiling | $20,000 | OPTIONS_TOTAL_BUDGET |
| VWAP kill switch | -$10,000 | MAX_DAILY_LOSS |
| Pro kill switch | -$2,000 | PRO_MAX_DAILY_LOSS |
| Options kill switch | -$1,000 | OPTIONS_MAX_DAILY_LOSS |
| Order cooldown | 300s | ORDER_COOLDOWN |
| Max intraday drawdown | -$5,000 | MAX_INTRADAY_DRAWDOWN |
| Max notional exposure | $100,000 | MAX_NOTIONAL_EXPOSURE |
| Options min DTE | 20 days | OPTIONS_MIN_DTE |
| Options max DTE | 45 days | OPTIONS_MAX_DTE |
| Options close DTE | 7 days | OPTIONS_DTE_CLOSE |
| Trade start time | 09:45 | TRADE_START_TIME |
| Force close time | 15:00 | FORCE_CLOSE_TIME |

### Key Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| READ_ONLY | false | Block new entries, exits still run |
| FF_TICK_ENTRIES | true | Enable/disable TickDetector |
| RISK_RSI_LOW | 40.0 | RSI lower bound for entries |
| RISK_RSI_HIGH | 75.0 | RSI upper bound |
| RISK_MIN_RVOL | 2.0 | Minimum relative volume |
| RISK_MAX_SPREAD_PCT | 0.002 | Max bid-ask spread |
| BAR_BUILDER_LIVENESS_SEC | 120 | BarBuilder staleness timeout |
| BROKER_MODE | smart | smart (dual) or alpaca/tradier (single) |
| DATA_SOURCE | tradier | Data provider |
| FF_PENDING_ORDER_DEDUP | true | Feature flag: pending order tracking |
| FF_PER_BROKER_KILL | true | Feature flag: per-broker loss limit |
| FF_OPTIONS_LIFECYCLE | true | Feature flag: options lifecycle engine |
| FF_DATA_CIRCUIT_BREAKER | true | Feature flag: data coverage breaker |

---

## Token / Account Setup

```
TRADIER PRODUCTION ($10/month) ── TRADIER_TOKEN
  Data: WebSocket streaming + REST bars (200+ tickers)
  Orders: NEVER (data only)

TRADIER SANDBOX (free) ── TRADIER_SANDBOX_TOKEN
  Orders: Paper trading (buy/sell)

ALPACA PAPER (free) ── APCA_API_KEY_ID
  Orders: Paper trading (buy/sell)
  Fill status: FREE WebSocket trade_updates
```

---

## Quick Start

```bash
# 1. Install
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Configure .env
cp .env.example .env
# Fill in: TRADIER_TOKEN, TRADIER_SANDBOX_TOKEN, TRADIER_ACCOUNT_ID,
#          APCA_API_KEY_ID, APCA_API_SECRET_KEY,
#          APCA_OPTIONS_KEY, APCA_OPTIONS_SECRET (if options),
#          DATABASE_URL, REDPANDA_BROKERS, ALERT_EMAIL_TO

# 3. Infrastructure
cd docker && docker compose up -d    # TimescaleDB + Redpanda
python -m db.migrations.run

# 4. Run
bash start_monitor.sh

# 5. Cron (auto-start weekdays)
# 0 6 * * 1-5 /path/to/trading_hub/start_monitor.sh >> /path/to/logs/cron.log 2>&1
```

Python 3.10+ required. macOS/Linux.

### Required .env Keys

| Key | Required | Purpose |
|-----|----------|---------|
| TRADIER_TOKEN | Yes | Production streaming data ($10/mo) |
| TRADIER_SANDBOX_TOKEN | Yes | Paper trading execution |
| TRADIER_ACCOUNT_ID | Yes | Tradier account for orders |
| APCA_API_KEY_ID | Yes | Alpaca paper trading |
| APCA_API_SECRET_KEY | Yes | Alpaca paper trading |
| APCA_OPTIONS_KEY | Options only | Separate Alpaca options account |
| APCA_OPTIONS_SECRET | Options only | Separate Alpaca options account |
| DATABASE_URL | Yes | PostgreSQL connection string |
| REDPANDA_BROKERS | Yes | Redpanda IPC (default: 127.0.0.1:9092) |
| ALERT_EMAIL_TO | Optional | Yahoo Mail for alerts |
| ALERT_EMAIL_FROM | Optional | Sender address |
| ALERT_EMAIL_APP_PASSWORD | Optional | Yahoo app password |

---

## Database

| Table | Purpose |
|-------|---------|
| event_store | Immutable event log (SIGNAL, FILL, POSITION, ORDER_FAIL) |
| market_bars | Lean bar table (1-min OHLCV, 10x smaller than event_store) |
| fill_lots | Lot-level position data |
| lot_matches | FIFO P&L audit trail |
| completed_trades | Trade summaries with lifecycle data |
| data_source_snapshots | Alt data from 10 sources |

---

## Data Sources (10)

| # | Source | Data |
|---|--------|------|
| 1 | Tradier | Streaming ticks, REST bars, Level-1 quotes |
| 2 | Alpaca | WebSocket fill updates, order execution |
| 3 | Benzinga | Headlines, sentiment |
| 4 | Finviz | Screener, insider, analyst targets |
| 5 | SEC EDGAR | Insider filings |
| 6 | Yahoo Finance | Earnings, recommendations |
| 7 | Polygon.io | Previous-day movers, options |
| 8 | Alpha Vantage | Technicals |
| 9 | FRED | Macro indicators (rates, VIX) |
| 10 | Fear & Greed | Market sentiment index |

---

## File Structure

```
trading_hub/
├── monitor/              # Core real-time trading layer (51 files)
│   ├── monitor.py        # Event orchestrator
│   ├── event_bus.py      # ASYNC priority dispatch (26 workers)
│   ├── tick_detector.py  # Sub-second entry detection (5 detectors)
│   ├── bar_builder.py    # WebSocket tick → 1-min OHLCV bars
│   ├── smart_stop.py     # Context-aware stop distance
│   ├── market_snapshot.py # Cold-start persistence
│   ├── exit_engine.py    # 5-phase lifecycle exits
│   ├── risk_engine.py    # VWAP risk gate (7 checks)
│   ├── strategy_engine.py # VWAP Reclaim strategy
│   ├── position_manager.py # Position state machine
│   ├── fill_ledger.py    # FIFO lot matching, P&L authority
│   ├── order_wal.py      # Write-ahead log, crash recovery
│   ├── smart_router.py   # Alpaca + Tradier lot-aware routing
│   ├── tradier_broker.py # Tradier execution + stop management
│   ├── brokers.py        # Alpaca execution + bracket orders
│   ├── tradier_stream.py # WebSocket streaming client
│   └── ...               # alerts, metrics, registry, kill switch, etc.
│
├── pro_setups/           # Pro technical strategies
│   ├── engine.py         # 13 detectors, classifier, smart stops
│   ├── detectors/        # ORB, S/R, gap, momentum, VWAP, trend, etc.
│   ├── strategies/       # 11 strategy implementations
│   ├── router/           # Signal → RiskAdapter routing
│   └── risk/             # Independent risk gate + stopped-today blacklist
│
├── options/              # Options trading (separate account)
│   ├── engine.py         # Entry/exit with Greeks tracking
│   ├── options_exit_engine.py # 3-phase risk-regime lifecycle
│   ├── strategies/       # 13 multi-leg strategy types
│   └── ...               # chain, broker, risk, IV tracker
│
├── lifecycle/            # Engine lifecycle management
│   ├── core.py           # Startup/shutdown/tick orchestration
│   ├── reconciler.py     # Broker ↔ local position sync
│   ├── kill_switch.py    # Daily loss halt
│   └── ...               # heartbeat, EOD report, state persistence
│
├── scripts/              # Run scripts and tools
│   ├── run_core.py       # Main trading loop (ET logging)
│   ├── run_options.py    # Options engine launcher
│   ├── supervisor.py     # Multi-engine process manager
│   ├── outer_watchdog.py # launchd-integrated watchdog
│   └── ...               # preflight, analytics, backfill
│
├── data_sources/         # Alternative data collection (10 sources)
├── backtests/            # Backtesting framework (event-replay harness)
│   └── run_backtest.py   # Run: --data tradier --save-to-db --csv
├── dashboards/           # Streamlit analysis dashboards
│   ├── backtest_dashboard.py    # Port 8502: backtest results viewer
│   └── trade_analysis_dashboard.py # Port 8503: live trade analysis
├── db/                   # Database migrations and schema
├── docker/               # TimescaleDB + Redpanda compose
└── config.py             # All settings, paths, credentials
```

---

## Backtesting

Event-replay harness with Tradier + DB data pipeline:
- **Data**: Fetch 1m/5m bars from Tradier API, persist to `market_bars` DB table. Future loads read from DB (no API calls).
- **Dual timeframe**: 1-minute bars (recent 20 days) + 5-minute bars (full 40-day window).
- **Backfill**: `python scripts/backfill_bars.py` populates DB from Tradier history.
- **Run**: `python backtests/run_backtest.py --data tradier --save-to-db --csv`
- **Dashboards**: `streamlit run dashboards/backtest_dashboard.py` (port 8502), `streamlit run dashboards/trade_analysis_dashboard.py` (port 8503)

### Analytics
- `scripts/post_session_analytics.py` — daily trade analysis, P&L breakdown
- `scripts/analyze_options_pf.py` — options PF by strategy, exit reason, risk score
- `scripts/weekly_options_review.py` — weekly calibration with threshold recommendations (auto-triggered Fridays)
- `reports/daily_analysis/trade_analysis_YYYYMMDD.csv` — per-trade detail export

---

## Production Hardening (V10)

47 fixes across 20 files:

| Category | Key Fixes |
|----------|-----------|
| Thread safety | Atomic kill switch lock, dict snapshots, stream client lock, pending order tracking |
| Order execution | Stop-above-entry fix, Tradier tag dedup, sell verification, exponential backoff |
| State management | Disk-full protection, atomic writes, version monotonic check |
| Risk controls | Per-broker daily loss, data coverage circuit breaker (30%/3 cycles), trade freeze |
| Data quality | Indicator freshness tracking, gap regime classification, level stability (2-bar minimum) |
| Monitoring | Data staleness alert (120s), stale order cleanup (60s), BarBuilder auto-restart |
| Operations | READ_ONLY mode, ET timestamps, stopped-today blacklist, ORB 30-min cutoff |
