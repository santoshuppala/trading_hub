# Trading Hub

A real-time algorithmic trading system built on an event-driven architecture with process isolation, multi-broker execution (Alpaca + Tradier with failover), 9 data sources, and an 18-layer risk stack. Supports live execution via Alpaca and Tradier, intraday bar data from Tradier or Alpaca, durable event logging via Redpanda, and multiple intraday strategies with institutional-grade pre-trade risk filters.

> **Disclaimer:** This is for educational purposes only. Trading involves significant risk. Always start with paper trading. Consult a financial advisor before trading with real money.

---

## Architecture

The system runs as a multi-layer event pipeline. Each layer subscribes to one or more `EventType`s on the shared `EventBus` and emits downstream events.

```
Execution Modes:
  MONOLITH: start_monitor.sh -> watchdog.py -> run_monitor.py
  ISOLATED: start_monitor.sh -> supervisor.py -> 4 processes (core, pro, pop, options)

Core Pipeline:
  TradierDataClient -> emit_batch(BAR[])
    +-- StrategyEngine      BAR -> SIGNAL (VWAP reclaim, ETF-aware RVOL)
    +-- ProSetupEngine      BAR -> PRO_STRATEGY_SIGNAL (11 setups, 3 tiers)
    +-- PopStrategyEngine   BAR -> Benzinga/StockTwits -> POP_SIGNAL (6 pop rules)
    +-- OptionsEngine       SIGNAL/POP_SIGNAL/BAR -> OPTIONS_SIGNAL (13 strategies)
    +-- RiskEngine          SIGNAL -> 7 checks + beta/vol sizing -> ORDER_REQ
    +-- PortfolioRiskGate   ORDER_REQ -> drawdown/notional/margin/Greeks -> pass/block
    +-- SmartRouter         ORDER_REQ -> Alpaca or Tradier (failover)
    +-- PositionManager     FILL -> POSITION -> StateEngine
    +-- DataSourceCollector  9 sources -> SmartPersistence -> event_store
```

### Monitor Layers

| Layer | File | Responsibility |
|-------|------|----------------|
| T1 EventBus | `monitor/event_bus.py` | Pub/sub backbone; priority queues; causal partitioning; backpressure; idempotency; SLA tracking |
| T1.5 Event Schema | `monitor/events.py` | Frozen, validated dataclasses; typed enums; PositionSnapshot; PopSignalPayload; OptionsSignalPayload; read-only numpy arrays on BAR DataFrames |
| T2 DurableEventLog | `monitor/event_log.py` | Redpanda producer; write-then-deliver ordering via before-emit hook |
| T3 RiskEngine | `monitor/risk_engine.py` | 7 pre-trade checks; blocks on spread fetch failure; price-divergence guard |
| T3.1 RiskSizer | `monitor/risk_sizer.py` | Beta-adjusted sizing (TQQQ 10->3), 14 correlation groups (max 3), ATR-scaled |
| T3.2 PortfolioRiskGate | `monitor/portfolio_risk_gate.py` | Drawdown halt, notional cap ($100K), margin check, Greeks limits |
| T3.3 KillSwitchGuard | `monitor/kill_switch.py` | Daily loss kill switch; force-closes all positions and halts trading |
| T3.5 PopStrategyEngine | `pop_strategy_engine.py` | Pop-stock screener -> classifier -> router -> POP_SIGNAL + direct execution via PopExecutor (dedicated Alpaca account) |
| T3.6 ProSetupEngine | `pro_setups/engine.py` | 11 pro setups across 3 tiers; 11 detectors + classifier + router + RiskAdapter -> ORDER_REQ -> AlpacaBroker |
| T3.7 OptionsEngine | `options/engine.py` | 13 option strategies; SIGNAL/BAR -> OPTIONS_SIGNAL; independent risk gate ($10K budget); dedicated Alpaca options account |
| T4 StrategyEngine | `monitor/strategy_engine.py` | VWAP Reclaim entry signals; 5 exit conditions; ETF-aware RVOL thresholds |
| T4.1 SentimentBaselineEngine | `monitor/sentiment_baseline.py` | Per-ticker sentiment baselines (replaces hardcoded 2.0/100.0) |
| T5 PositionManager | `monitor/position_manager.py` | Opens/closes positions; computes PnL; persists `bot_state.json`; thread-safe fill handler |
| T6 StateEngine | `monitor/state_engine.py` | Maintains read-only portfolio snapshot for UI and heartbeat |
| T7 ExecutionFeedback | `monitor/execution_feedback.py` | Patches stop/target prices after a buy fill |
| T8 Observability | `monitor/observability.py` | EventLogger, HeartbeatEmitter, EODSummary |
| T8.1 DataSourceCollector | `monitor/data_source_collector.py` | 9 data sources -> SmartPersistence -> event_store; hash-based dedup |
| T9 Monitor | `monitor/monitor.py` | Thin orchestrator; data-fetch loop; Alpaca reconciliation; crash-alerting run loop |
| T9.1 SmartRouter | `monitor/smart_router.py` | Health-based routing between Alpaca and Tradier; circuit breaker (3 failures -> 5min disable) |
| T9.2 TradierBroker | `monitor/tradier_broker.py` | Equity + options execution via Tradier REST API (sandbox verified) |

### Event Bus (v5.2)

Key capabilities built into `event_bus.py`:

- **Priority queues** -- `_BoundedPriorityQueue` (heapq); DROP_OLDEST evicts lowest-urgency items, preserving FILL/ORDER_REQ over BAR
- **Partitioned workers** -- `_PartitionedAsyncDispatcher`; N workers per EventType; ticker-based routing (Kafka partition model) preserves per-ticker causal ordering across all EventType dispatchers
- **Causal ordering** -- `BAR->SIGNAL->ORDER->FILL` for the same ticker always share a partition index; same-ticker events cannot race
- **Round-robin for keyless events** -- no-ticker events (HEARTBEAT) distribute evenly across workers instead of pinning to a hot partition
- **Stable partitioning** -- `hashlib.md5` instead of Python's `hash()` (PEP 456 randomisation)
- **Split locks** -- `_sub_lock`, `_seq_lock`, `_count_lock`, `_idem_lock`; per-handler `_HandlerState._lock` -- no global contention
- **`emit_batch()`** -- O(4) lock acquisitions for N events vs O(4N) for N x `emit()`; used in the BAR fan-out for 100+ tickers
- **Systemic backpressure** -- `_BackpressureMonitor`; 60%/80%/95% thresholds; adaptive micro-sleep; returns `BackpressureStatus` enum
- **LRU stream_seqs** -- `OrderedDict` capped at `max_streams=1000`; O(1) eviction
- **Durable write-then-deliver** -- `add_before_emit_hook()` + `emit(durable=True)`; Redpanda ACKs before any in-process handler runs
- **SLA tracking** -- `event.deadline` field; `_deliver()` counts breaches in `BusMetrics.sla_breaches`
- **Prometheus exporter** -- optional `PrometheusExporter` class; delta-based counters + gauges

Default async config:

| EventType | Queue size | Overflow | Workers | Notes |
|-----------|-----------|----------|---------|-------|
| BAR | 500 | DROP_OLDEST | 8 | 125 slots/partition; no drops on 200-ticker burst |
| SIGNAL, POP_SIGNAL, OPTIONS_SIGNAL | 50 | DROP_OLDEST | 1-2 | Latest supersedes stale |
| ORDER_REQ, FILL | 100 | BLOCK | 1 | Strict ordering; never dropped |
| HEARTBEAT | 10 | DROP_OLDEST | 1 | |

---

## Process Isolation

Set `MONITOR_MODE=isolated` in `.env` to run 4 independent processes:

| Process | Engines | IPC Topic |
|---------|---------|-----------|
| **core** | StrategyEngine + SmartRouter + PositionManager | th-orders, th-signals |
| **pro** | ProSetupEngine (11 detectors) | th-signals |
| **pop** | PopStrategyEngine (Benzinga/StockTwits) | th-pop-signals |
| **options** | OptionsEngine (13 strategies) | th-signals, th-pop-signals |

- IPC via Redpanda topics (`th-orders`, `th-signals`, `th-pop-signals`)
- Shared cache: `data/live_cache.pkl` (atomic write)
- Distributed registry: `data/position_registry.json` (file-locked)
- Supervisor: independent restart per process, CRITICAL alert if core dies
- Default mode (`MONITOR_MODE=monolith`) runs everything in a single process

---

## Multi-Broker

Set `BROKER_MODE=smart` in `.env` to enable health-based routing:

| Broker | Capabilities | Account |
|--------|-------------|---------|
| **Alpaca** | Equity + options, paper + live | Main + Pop + Options accounts |
| **Tradier** | Equity + options via REST API | Sandbox verified |

- **SmartRouter**: routes orders to the healthiest broker
- **Circuit breaker**: 3 consecutive failures -> 5-minute disable for that broker
- **Failover**: automatic fallback if primary broker is unhealthy
- **Aggregate buying power**: $4M across 4 accounts

---

## Data Sources

| # | Source | Key Required | Data Provided |
|---|--------|-------------|---------------|
| 1 | Yahoo Finance | No | Historical bars, fundamentals |
| 2 | Fear & Greed Index | No | Market sentiment gauge |
| 3 | Finviz | No | Screener, fundamentals, insider data |
| 4 | SEC EDGAR | No | Institutional filings |
| 5 | FRED | Yes | Economic indicators (rates, CPI, unemployment) |
| 6 | Alpha Vantage | Yes | Intraday/daily bars, technicals |
| 7 | Polygon.io | Yes | Real-time quotes, options flow |
| 8 | Reddit | Disabled | Social sentiment (signup pending) |
| 9 | Unusual Whales | Disabled | Options flow, dark pool (signup pending) |

- **SmartPersistence**: hash-based dedup, ~100 writes/day to event_store
- All sources feed into `DataSourceCollector` -> `NEWS_DATA`/`SOCIAL_DATA` events

---

## Risk Stack (18 Checks)

| # | Check | Layer | Action |
|---|-------|-------|--------|
| 1 | Max positions | RiskEngine | Block if at limit |
| 2 | Duplicate ticker | RiskEngine | Block re-entry same day |
| 3 | Spread width | RiskEngine | Block if bid/ask > 0.2% |
| 4 | Price divergence | RiskEngine | Block if price moved > 1% since signal |
| 5 | Trading hours | RiskEngine | Block outside 9:45 AM - 3:00 PM ET |
| 6 | SPY above VWAP | RiskEngine | Block if market not bullish |
| 7 | RVOL threshold | RiskEngine | Block if relative volume too low |
| 8 | Beta-adjusted sizing | RiskSizer | TQQQ: 10 shares -> 3 shares |
| 9 | Correlation group cap | RiskSizer | Max 3 positions per group (14 groups) |
| 10 | ATR-scaled position size | RiskSizer | Volatility-based sizing |
| 11 | Drawdown halt | PortfolioRiskGate | Stop trading if daily drawdown > threshold |
| 12 | Notional cap | PortfolioRiskGate | Block if total notional > $100K |
| 13 | Margin check | PortfolioRiskGate | Block if insufficient margin |
| 14 | Greeks limits | PortfolioRiskGate | Block if portfolio delta/gamma/vega exceed limits |
| 15 | Daily loss kill switch | KillSwitchGuard | Force-close all positions |
| 16 | Cross-layer dedup | GlobalPositionRegistry | No equity + options on same ticker |
| 17 | Per-trade budget | Per-engine risk gate | $500-$1000 per trade depending on engine |
| 18 | News-aware correlation | RiskSizer | Ticker-specific catalysts override group blocks |

---

## Self-Healing

- **Watchdog** (`watchdog.py`): process wrapper, crash analyzer, auto-fix, max 5 retries
- **Crash analyzer**: 6 pattern matchers (kwarg, attribute, import, unbound, validation, args)
- **Supervisor** (`supervisor.py`): per-engine crash detection + fix + restart (isolated mode)
- **CrashRecovery** (`monitor/event_log.py`): rebuilds positions from Redpanda on startup

---

## Dashboards

### Original Dashboard (Streamlit)

```bash
streamlit run app.py
```

Two tabs:
- **Live Monitor** -- start/stop the monitor, open positions, today's trades, live log with auto-refresh
- **Backtest** -- single-ticker or year-by-year compounding backtests

### Pro Dashboard (Bloomberg-style)

```bash
streamlit run dashboards/trading_dashboard.py
```

Dark terminal theme with 3 additional tabs:
- **Risk Analytics** -- portfolio Greeks, drawdown, correlation matrix
- **Strategy Attribution** -- per-strategy P&L, win rate, Sharpe
- **Market Regime** -- VIX regime detection, sector rotation, breadth

Both dashboards auto-load API keys from `.env`.

---

## Project Structure

```
trading_hub/
+-- monitor/
|   +-- event_bus.py          # T1   -- EventBus v5.2 (priority queues, causal partitioning, backpressure)
|   +-- events.py             # T1.5 -- Frozen payload dataclasses; enums; PopSignalPayload
|   +-- event_log.py          # T2   -- Redpanda durable event log + CrashRecovery
|   +-- risk_engine.py        # T3   -- Pre-trade risk checks (7 filters)
|   +-- risk_sizer.py         # T3.1 -- Beta-adjusted sizing, correlation groups, ATR scaling
|   +-- portfolio_risk_gate.py # T3.2 -- Drawdown, notional, margin, Greeks limits
|   +-- kill_switch.py        # T3.3 -- Daily loss kill switch
|   +-- strategy_engine.py    # T4   -- VWAP Reclaim signal generation
|   +-- sentiment_baseline.py # T4.1 -- Per-ticker sentiment baselines
|   +-- position_manager.py   # T5   -- Position lifecycle + bot_state.json persistence
|   +-- state_engine.py       # T6   -- Read-only portfolio snapshot (seeded on restart)
|   +-- execution_feedback.py # T7   -- Stop/target patch after buy fill
|   +-- observability.py      # T8   -- EventLogger, HeartbeatEmitter, EODSummary
|   +-- data_source_collector.py # T8.1 -- 9 data sources, SmartPersistence
|   +-- monitor.py            # T9   -- Orchestrator; run loop; Alpaca reconciliation
|   +-- smart_router.py       # T9.1 -- Health-based multi-broker routing
|   +-- tradier_broker.py     # T9.2 -- Tradier REST API broker
|   +-- brokers.py            # AlpacaBroker (limit+retry buy, market sell) + PaperBroker
|   +-- data_client.py        # Factory: TradierDataClient | AlpacaDataClient
|   +-- tradier_client.py     # Tradier REST API: bars, quotes; pooled HTTP adapter
|   +-- alpaca_data_client.py # Alpaca data API: bars, quotes, screener
|   +-- screener.py           # MomentumScreener -- refreshes watchlist every 30 min
|   +-- signals.py            # VWAP Reclaim signal math; LRU indicator cache (500 entries)
|   +-- rvol.py               # ETF-aware RVOL computation (0.7 threshold for ETFs)
|   +-- state.py              # load_state() / save_state() -- atomic bot_state.json I/O
|   +-- alerts.py             # Email alerts via Yahoo SMTP
|   +-- orders.py             # (legacy) direct order helpers
|
+-- pop_screener/             # Multi-strategy pop-stock subsystem
|   +-- config.py             # All 60+ configurable thresholds (one place)
|   +-- models.py             # Data models: NewsData, SocialData, EngineeredFeatures, etc.
|   +-- ingestion.py          # Pluggable adapters: news / social / market / momentum
|   +-- features.py           # FeatureEngineer: VWAP, ATR, RSI, trend cleanliness, sentiment
|   +-- screener.py           # PopScreener: 6 rule-based pop detectors
|   +-- classifier.py         # StrategyClassifier: deterministic pop -> strategy mapping
|   +-- strategy_router.py    # StrategyRouter: primary -> secondary fallback engine dispatch
|   +-- benzinga_news.py      # Benzinga v2 News API adapter (60s cache)
|   +-- stocktwits_social.py  # StockTwits v2 Streams adapter (5m cache)
|   +-- strategies/
|       +-- vwap_reclaim_engine.py      # Dip-and-reclaim pattern with RVOL/RSI filters
|       +-- orb_engine.py               # Opening Range Breakout with volume confirmation
|       +-- halt_resume_engine.py       # Halt-like spike + consolidation + breakout
|       +-- parabolic_reversal_engine.py# Exhaustion candle -> short reversal
|       +-- ema_trend_engine.py         # Pullback-to-EMA in a confirmed uptrend
|       +-- bopb_engine.py              # Breakout -> pullback -> confirmation entry
|
+-- pro_setups/               # 11-setup pro strategy subsystem
|   +-- engine.py             # T3.6 -- ProSetupEngine: BAR -> 11 detectors -> PRO_STRATEGY_SIGNAL
|   +-- detectors/            # 11 detectors: Trend, VWAP, SR, ORB, InsideBar, Gap, Flag,
|   |                         #               Liquidity, Volatility(BB), Fib, Momentum
|   +-- classifiers/          # StrategyClassifier: detector outputs -> (strategy, tier)
|   +-- strategies/           # 11 strategy modules in tier1/ tier2/ tier3/
|   |   +-- tier1/            # trend_pullback, vwap_reclaim, sr_flip
|   |   +-- tier2/            # orb, inside_bar, gap_and_go, flag_pennant
|   |   +-- tier3/            # liquidity_sweep, bollinger_squeeze, fib_confluence, momentum_ignition
|   +-- router/               # ProStrategyRouter: PRO_STRATEGY_SIGNAL -> RiskAdapter
|   +-- risk/                 # RiskAdapter: tier-aware risk gate -> ORDER_REQ
|
+-- options/                  # Options engine subsystem (T3.7)
|   +-- engine.py             # T3.7 -- OptionsEngine: SIGNAL/BAR -> OPTIONS_SIGNAL
|   +-- selector.py           # OptionStrategySelector: signal/bar conditions -> strategy type
|   +-- chain.py              # AlpacaOptionChainClient: Alpaca options data API
|   +-- risk.py               # OptionsRiskGate: independent $10K budget gate
|   +-- broker.py             # AlpacaOptionsBroker: multi-leg order execution
|   +-- strategies/           # 13 strategy builders
|       +-- base.py           # BaseOptionsStrategy, OptionLeg, OptionsTradeSpec
|       +-- directional.py    # LongCall, LongPut
|       +-- vertical.py       # BullCallSpread, BearPutSpread, BullPutSpread, BearCallSpread
|       +-- volatility.py     # LongStraddle, LongStrangle
|       +-- neutral.py        # IronCondor, IronButterfly
|       +-- time_based.py     # CalendarSpread, DiagonalSpread
|       +-- complex.py        # ButterflySpread
|
+-- analytics/                # Risk analytics and strategy attribution
|
+-- dashboards/               # Pro dashboard (Bloomberg-style dark theme)
|   +-- trading_dashboard.py  # Risk Analytics, Strategy Attribution, Market Regime tabs
|
+-- db/                       # Async TimescaleDB event store
|   +-- __init__.py           # Public API: init_db, close_db, get_writer, SessionManager
|   +-- connection.py         # asyncpg pool singleton (min=4, max=20)
|   +-- writer.py             # Async batch DBWriter with circuit breaker
|   +-- subscriber.py         # EventBus -> DBWriter bridge (all event types)
|   +-- event_sourcing_subscriber.py # Event sourcing bridge
|   +-- reader.py             # Read queries: bars, fills, signals, sessions, replay
|   +-- feature_store.py      # ML feature extraction + outcome labelling
|   +-- migrations/
|       +-- run.py            # Idempotent SQL migration runner
|       +-- sql/              # 001-012 ordered migrations
|
+-- docker/                   # Container orchestration
|   +-- docker-compose.yml    # TimescaleDB 2.26 + Redpanda
|   +-- postgresql.conf       # Tuned PG16 config (SSD, 256MB shared_buffers)
|
+-- data/                     # Runtime data (live cache, position registry, IV history)
|
+-- pop_strategy_engine.py    # T3.5 -- BAR subscriber; runs pop pipeline; emits POP_SIGNAL + SIGNAL
+-- demo_pop_strategies.py    # Offline demo: full pipeline with synthetic data
|
+-- strategies/               # Backtrader strategy definitions
|   +-- vwap_reclaim.py
|   +-- ema_rsi.py
|   +-- trend_atr.py
|   +-- momentum_breakout.py
|   +-- mean_reversion.py
|
+-- test/                     # Integration + unit test suite (18 tests)
|   +-- test_1_synthetic_feed.py
|   +-- test_2_tradier_sandbox.py
|   +-- ...
|   +-- test_18_risk_kill_switch.py
|   +-- run_all_tests.py
|
+-- watchdog.py               # Process wrapper: crash analyzer, auto-fix, max 5 retries
+-- supervisor.py             # Isolated mode: 4-process supervisor with per-engine restart
+-- app.py                    # Streamlit UI -- live monitor + backtest tabs
+-- config.py                 # Shared settings: tickers, strategy params, credentials
+-- main.py                   # Backtrader backtesting engine
+-- run_monitor.py            # Headless launcher (no UI)
+-- vwap_utils.py             # VWAP computation utilities
+-- start_monitor.sh          # Shell launcher for cron
+-- bot_state.json            # Intraday state -- positions, reclaimed tickers, trade log
+-- docs/dev_notes.md         # Full architectural decision log + bug fix history
+-- logs/                     # Daily log files: monitor_YYYY-MM-DD.log
+-- .env                      # API keys and SMTP credentials (never commit)
+-- requirements.txt
```

---

## Quick Start

### 1. Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Python 3.10+ required.

### 2. Configure `.env`

Create `.env` in the project root. No quotes, no spaces around `=`:

```
# Alpaca (order execution + optional data)
APCA_API_KEY_ID=PKxxxxxxxxxxxxxxxx
APCA_API_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Tradier (bar data -- recommended for 1-min bars)
TRADIER_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx

# Tradier Broker (optional -- for multi-broker mode)
TRADIER_ACCOUNT_ID=xxxxxxxx
TRADIER_BROKER_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx

# Broker mode: 'alpaca' | 'tradier' | 'smart' (failover)
BROKER=alpaca
BROKER_MODE=smart

# Monitor mode: 'monolith' | 'isolated' (4 processes)
MONITOR_MODE=monolith

# Data source: 'tradier' | 'alpaca'
DATA_SOURCE=tradier

# Email alerts (Yahoo Mail app password -- 16 chars, no spaces)
ALERT_EMAIL_USER=you@yahoo.com
ALERT_EMAIL_PASS=your16charapppassword
ALERT_EMAIL_FROM=you@yahoo.com
ALERT_EMAIL_TO=you@yahoo.com

# Pop-strategy dedicated Alpaca account (separate from main VWAP account)
APCA_POPUP_KEY=PKxxxxxxxxxxxxxxxx
APCA_PUPUP_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
POP_PAPER_TRADING=true
POP_MAX_POSITIONS=3
POP_TRADE_BUDGET=500
POP_ORDER_COOLDOWN=300

# Options engine dedicated Alpaca account (T3.7, $10K budget)
APCA_OPTIONS_KEY=PKxxxxxxxxxxxxxxxx
APCA_OPTIONS_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
OPTIONS_PAPER_TRADING=true
OPTIONS_MAX_POSITIONS=5
OPTIONS_TRADE_BUDGET=500
OPTIONS_TOTAL_BUDGET=10000
OPTIONS_ORDER_COOLDOWN=300
OPTIONS_MIN_DTE=20
OPTIONS_MAX_DTE=45

# Pro-setups subsystem
PRO_MAX_POSITIONS=3
PRO_TRADE_BUDGET=1000
PRO_ORDER_COOLDOWN=300

# Database
DB_ENABLED=true
DATABASE_URL=postgresql://postgres:trading_secret@localhost:5432/tradinghub

# Redpanda / Kafka (optional -- for durable event log)
REDPANDA_BROKERS=127.0.0.1:9092

# External data APIs
BENZINGA_API_KEY=bz.xxxxxxxxxxxxxxxxxxxx
STOCKTWITS_TOKEN=
FRED_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALPHA_VANTAGE_KEY=xxxxxxxxxxxxxxxx
POLYGON_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Risk
MAX_DAILY_LOSS=-10000
GLOBAL_MAX_POSITIONS=8
```

### 3. Start infrastructure

```bash
cd docker && docker compose up -d
python -m db.migrations.run
```

### 4. Run

```bash
# Headless (recommended)
bash start_monitor.sh

# Or directly
python run_monitor.py

# Streamlit UI
streamlit run app.py

# Pro dashboard
streamlit run dashboards/trading_dashboard.py
```

### 5. Cron (auto-start daily)

```
0 6 * * 1-5 /path/to/trading_hub/start_monitor.sh >> /path/to/logs/cron.log 2>&1
```

Runs at 6:00 AM PST (9:00 AM ET) on weekdays.

### Single-instance enforcement

Only one monitor can run at a time. A second start attempt is refused with the PID of the running process. Lock file is `.monitor.lock`; cleaned up automatically on stop.

---

## Strategies

### Strategy 1: VWAP Reclaim (T4)

#### Entry -- all 9 conditions must hold simultaneously

| # | Filter | Detail |
|---|--------|--------|
| 1 | **2-bar VWAP reclaim** | Dipped below VWAP -> closed above VWAP for 2 consecutive bars |
| 2 | **Opened above VWAP** | Day bias is bullish |
| 3 | **RSI 50-70** | Momentum without being overbought |
| 4 | **RVOL >= 2x** (stocks) / **>= 0.7x** (ETFs) | ETF-aware relative volume threshold |
| 5 | **SPY above VWAP** | Market tailwind |
| 6 | **Bid/ask spread <= 0.2%** | Spread not wider than target profit margin |
| 7 | **Not already traded today** | No re-entry on same ticker intraday |
| 8 | **Trading hours 9:45-3:00 PM ET** | Avoid noisy open and illiquid close |
| 9 | **Max positions not exceeded** | Configurable; default 5 |

#### Exit conditions (first triggered wins)

| Condition | Description |
|-----------|-------------|
| **Trailing stop** | Price drops 1xATR below entry |
| **Full target** | Price reaches 2xATR above entry |
| **Partial exit** | Sell half at 1xATR profit; trail remainder |
| **RSI overbought** | RSI crosses above 75 |
| **VWAP breakdown** | Price closes below VWAP |
| **EOD force-close** | All positions closed at 3:00 PM ET |

---

### Strategy 2: Pop-Stock Multi-Strategy (T3.5)

6 rule-based pop detectors, 6 strategy engines, Benzinga/StockTwits data, dedicated Alpaca account.

See `docs/dev_notes.md` section 12 for full architecture.

### Strategy 3: Pro-Setup Multi-Tier System (T3.6)

11 deterministic setups across 3 tiers. See `docs/dev_notes.md` section 18.

### Strategy 4: Options Engine (T3.7)

13 option strategies across 6 categories. $10K budget, dedicated Alpaca options account. See `docs/dev_notes.md` section 21.

---

## Database Layer (TimescaleDB)

All events are persisted asynchronously to TimescaleDB (PostgreSQL 16 + TimescaleDB 2.26+).

```bash
cd docker && docker compose up -d
python -m db.migrations.run
```

12 ordered SQL migrations in `db/migrations/sql/`. See `docs/analysis_queries.md` for 60+ ready-to-use SQL queries.

---

## Test Suite

18 tests (36 functions, 195+ assertions). Run all:

```bash
python test/run_all_tests.py
```

---

## Logging

Both run modes write to `logs/monitor_YYYY-MM-DD.log`. Stops automatically at 3:15 PM ET with an EOD summary (total trades, wins/losses, win rate, per-trade breakdown, total PnL).

---

## Requirements

- Python 3.10+
- Docker (for TimescaleDB + Redpanda)
- Alpaca account -- free paper trading at [alpaca.markets](https://alpaca.markets)
- Tradier account -- free developer sandbox at [tradier.com](https://tradier.com) (recommended for bar data)
- Yahoo Mail app password for email alerts (optional)
- Redpanda or Kafka broker for durable event log (optional -- bot runs without it)
- TimescaleDB 2.26+ / PostgreSQL 16 (optional -- bot runs without it; use `cd docker && docker compose up -d`)
- macOS/Linux for cron scheduling
