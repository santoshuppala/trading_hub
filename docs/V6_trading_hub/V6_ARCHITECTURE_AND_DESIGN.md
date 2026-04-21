# Trading Hub V6.0 — Architecture & Design Document

**Source of truth:** Current codebase as of 2026-04-15
**No assumptions — every component, subscription, and data flow traced from code**

---

## 1. SYSTEM OVERVIEW

Trading Hub V6.0 is a **process-isolated, event-driven algorithmic trading system** running 5 processes managed by a supervisor. Four independent trading engines communicate via Redpanda (Kafka-compatible) IPC and a shared pickle cache. All events are persisted to TimescaleDB via an immutable event store.

```
┌─────────────────────────────────────────────────────────────────┐
│                        SUPERVISOR (PID 1)                       │
│  scripts/supervisor.py                                          │
│  Manages lifecycle of 4 child processes                         │
│  Restart on crash (max 3-5 retries), EOD shutdown at 4:00 PM   │
└──────┬──────────┬──────────┬──────────┬────────────────────────┘
       │          │          │          │
       ▼          ▼          ▼          ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│   CORE   │ │   PRO    │ │   POP    │ │ OPTIONS  │
│ run_core │ │ run_pro  │ │ run_pop  │ │run_opts  │
│ .py      │ │ .py      │ │ .py      │ │ .py      │
│          │ │          │ │          │ │          │
│ VWAP +   │ │ 11 tech  │ │ Momentum │ │ Multi-leg│
│ Broker + │ │ detectors│ │ + News + │ │ options  │
│ Position │ │ + strat  │ │ Social   │ │ on own   │
│ Mgmt     │ │ classify │ │ screener │ │ Alpaca   │
└──────────┘ └──────────┘ └──────────┘ └──────────┘
     ▲ ▼          │ ▲          │ ▲          │ ▲
     │ │          │ │          │ │          │ │
     │ ├──────────┘ │          │ │          │ │
     │ │  Redpanda  │          │ │          │ │
     │ │  th-orders │          │ │          │ │
     │ ├────────────┘          │ │          │ │
     │ │  th-orders            │ │          │ │
     │ ├───────────────────────┘ │          │ │
     │ │  th-signals              │          │ │
     │ ├──────────────────────────┘          │ │
     │ │  th-pop-signals                     │ │
     │ └─────────────────────────────────────┘ │
     │                                          │
     │         data/live_cache.pkl              │
     └──────────── (Core writes, others read) ──┘
```

---

## 2. PROCESS ARCHITECTURE

### 2.1 Supervisor (`scripts/supervisor.py`)

| Config | Core | Pro | Pop | Options |
|--------|------|-----|-----|---------|
| Script | `scripts/run_core.py` | `scripts/run_pro.py` | `scripts/run_pop.py` | `scripts/run_options.py` |
| Critical | Yes | No | No | No |
| Max restarts | 3 | 5 | 5 | 5 |
| Restart delay | 10s | 15s | 15s | 20s |

**Behaviors:**
- Starts Core first (5s head start for shared state init)
- Sets `SUPERVISED_MODE=1` env var (disables monitor lock file)
- Restart counter resets after 60s stable uptime
- Skip crash analysis for clean exits (exit code 0)
- Logs to `logs/YYYYMMDD/supervisor.log` (no StreamHandler — file only)
- Status written to `data/supervisor_status.json`

### 2.2 Core Process (`scripts/run_core.py` + `monitor/monitor.py`)

**Components created (in order):**

| # | Component | Class | File | Purpose |
|---|-----------|-------|------|---------|
| 1 | EventBus | EventBus | monitor/event_bus.py | Local event dispatch |
| 2 | IPC Publisher | EventPublisher | monitor/ipc.py | Publish to Redpanda |
| 3 | RealTimeMonitor | RealTimeMonitor | monitor/monitor.py | Main orchestrator |
| 4 | — DataClient | TradierDataClient | monitor/tradier_client.py | Market data (bars) |
| 5 | — StrategyEngine | StrategyEngine | monitor/strategy_engine.py | VWAP strategy |
| 6 | — RiskEngine | RiskEngine | monitor/risk_engine.py | Signal → Order gating |
| 7 | — AlpacaBroker | AlpacaBroker | monitor/brokers.py | Order execution |
| 8 | — ExecutionFeedback | ExecutionFeedback | monitor/execution_feedback.py | Patches positions |
| 9 | — PositionManager | PositionManager | monitor/position_manager.py | Position lifecycle |
| 10 | — StateEngine | StateEngine | monitor/state_engine.py | Read-only snapshot |
| 11 | — HeartbeatEmitter | HeartbeatEmitter | monitor/observability.py | 60s heartbeat |
| 12 | — DurableEventLog | DurableEventLog | monitor/event_log.py | Redpanda persistence |
| 13 | DB Layer | DBWriter + EventSourcingSubscriber | db/writer.py, db/event_sourcing_subscriber.py | Async DB persistence |
| 14 | Portfolio Risk | PortfolioRiskGate | monitor/portfolio_risk.py | Aggregate limits |
| 15 | SmartRouter | SmartRouter | monitor/smart_router.py | Multi-broker routing |
| 16 | TradierBroker | TradierBroker | monitor/tradier_broker.py | Secondary broker |
| 17 | RVOL Engine | RVOLEngine | monitor/rvol.py | Volume profiling |
| 18 | Cache Writer | CacheWriter | monitor/shared_cache.py | Write live_cache.pkl |
| 19 | IPC Consumer | EventConsumer | monitor/ipc.py | Receive satellite orders |

**Main loop (every ~60s bar cycle):**
1. Fetch bars from Tradier for all tickers
2. Write to `data/live_cache.pkl` (CacheWriter)
3. Emit BAR events (batch, one per ticker)
4. EventBus processes: BAR → SIGNAL → ORDER_REQ → FILL → POSITION
5. IPC Consumer receives ORDER_REQ from Pro/Pop → injects into bus
6. Heartbeat every 60s
7. EOD gate: no new entries after 3:00 PM ET

### 2.3 Pro Process (`scripts/run_pro.py`)

**Components:**

| Component | Class | File |
|-----------|-------|------|
| EventBus | EventBus | monitor/event_bus.py |
| ProSetupEngine | ProSetupEngine | pro_setups/engine.py |
| RVOL Engine | RVOLEngine | monitor/rvol.py |
| Cache Reader | CacheReader | monitor/shared_cache.py |
| IPC Publisher | EventPublisher | monitor/ipc.py |
| Position Registry | DistributedPositionRegistry | monitor/distributed_registry.py |
| Lifecycle | EngineLifecycle | lifecycle/core.py |
| DB Layer | DBWriter + EventSourcingSubscriber | db/ |

**11 Detectors:** sr, trend, vwap, momentum, volatility, liquidity, gap, fib, flag, inside_bar, orb

**11 Strategies (3 tiers):**
- T1: SR Flip, VWAP Reclaim, Trend Pullback
- T2: ORB, Inside Bar, Gap and Go, Flag/Pennant
- T3: Momentum Ignition, Liquidity Sweep, Fib Confluence, Bollinger Squeeze

**Main loop:** Read cache → emit BAR → detectors fire → classify → generate entry/stop/target → RiskAdapter validates → emit ORDER_REQ → forward to Redpanda `th-orders`

**EOD gate:** 3:45 PM ET

### 2.4 Pop Process (`scripts/run_pop.py`)

**Components:**

| Component | Class | File |
|-----------|-------|------|
| EventBus | EventBus | monitor/event_bus.py |
| PopStrategyEngine | PopStrategyEngine | pop_strategy_engine.py |
| PopExecutor | PopExecutor | pop_strategy_engine.py (inner class) |
| BenzingaNews | BenzingaNewsSentimentSource | pop_screener/benzinga_news.py |
| StockTwits | StockTwitsSocialSource | pop_screener/stocktwits_social.py |
| FeatureEngineer | FeatureEngineer | pop_screener/features.py |
| PopScreener | PopScreener | pop_screener/screener.py |
| StrategyRouter | StrategyRouter | pop_screener/strategy_router.py |
| MomentumEntryEngine | MomentumEntryEngine | pop_screener/strategies/momentum_entry_engine.py |
| IPC Publisher | EventPublisher | monitor/ipc.py |
| Lifecycle | EngineLifecycle | lifecycle/core.py |
| DB Layer | DBWriter + EventSourcingSubscriber | db/ |

**Pipeline:** BAR → fetch news + social → feature engineering → screen (6 rules) → classify → route (7 strategies + momentum fallback) → emit ORDER_REQ → forward to Redpanda `th-orders`

**Also publishes:** POP_SIGNAL to `th-pop-signals` (consumed by Options)

**EOD gate:** 3:45 PM ET

### 2.5 Options Process (`scripts/run_options.py`)

**Components:**

| Component | Class | File |
|-----------|-------|------|
| EventBus | EventBus | monitor/event_bus.py |
| OptionsEngine | OptionsEngine | options/engine.py |
| AlpacaOptionsBroker | AlpacaOptionsBroker | options/broker.py |
| AlpacaOptionChainClient | AlpacaOptionChainClient | options/chain.py |
| IVTracker | IVTracker | options/iv_tracker.py |
| EarningsCalendar | EarningsCalendar | options/earnings_calendar.py |
| OptionStrategySelector | OptionStrategySelector | options/selector.py |
| OptionsRiskGate | OptionsRiskGate | options/risk.py |
| IPC Consumer | EventConsumer | monitor/ipc.py |
| Lifecycle | EngineLifecycle | lifecycle/core.py |
| DB Layer | DBWriter + EventSourcingSubscriber | db/ |

**Consumes from Redpanda:** `th-signals` (Core VWAP), `th-pop-signals` (Pop)

**13 Options strategies:** Long call, long put, bull call spread, bear put spread, bull put spread, bear call spread, iron condor, iron butterfly, butterfly spread, long straddle, long strangle, calendar spread, PMCC

**Own Alpaca account:** `APCA_OPTIONS_KEY` / `APCA_OPTIONS_SECRET`

**EOD gate:** 3:45 PM ET (exits still monitored after cutoff)

---

## 3. EVENT SYSTEM

### 3.1 EventType Enum (`monitor/event_bus.py`)

```
BAR, QUOTE, SIGNAL, ORDER_REQ, FILL, ORDER_FAIL, POSITION,
RISK_BLOCK, HEARTBEAT, POP_SIGNAL, PRO_STRATEGY_SIGNAL,
OPTIONS_SIGNAL, NEWS_DATA, SOCIAL_DATA
```

### 3.2 Event Chain (Critical Path)

```
BAR ──→ StrategyEngine ──→ SIGNAL ──→ RiskEngine ──→ ORDER_REQ
                                                         │
                    ┌────────────────────────────────────┘
                    ▼
         PortfolioRiskGate (priority 0) ──→ blocks or passes
                    │
                    ▼
         SmartRouter (priority 1) ──→ selects Alpaca or Tradier
                    │
                    ▼
         Broker._execute_buy ──→ bracket order (Alpaca) or limit+stop (Tradier)
                    │
                    ▼
         FILL ──→ PositionManager._open_position ──→ POSITION (OPENED)
                    │                                      │
                    ▼                                      ▼
         StateEngine.snapshot()              EventSourcingSubscriber → DB
                                             completed_trades (on CLOSED)
```

### 3.3 Subscription Map (All Processes)

**CORE Process:**

| EventType | Handler | File:Line | Priority |
|-----------|---------|-----------|----------|
| BAR | StrategyEngine._on_bar | strategy_engine.py:102 | default |
| BAR | ExecutionFeedback._flush_deferred | execution_feedback.py:103 | default |
| SIGNAL | RiskEngine._on_signal | risk_engine.py:112 | default |
| SIGNAL | ExecutionFeedback._on_signal | execution_feedback.py:99 | default |
| ORDER_REQ | PortfolioRiskGate._on_order_req | portfolio_risk.py:63 | 0 |
| ORDER_REQ | SmartRouter._on_order_req | smart_router.py:109 | 1 |
| FILL | PortfolioRiskGate._on_fill | portfolio_risk.py:64 | 0 |
| FILL | PositionManager._on_fill | position_manager.py:83 | default |
| FILL | ExecutionFeedback._on_fill | execution_feedback.py:100 | default |
| FILL | SmartRouter._on_fill | smart_router.py:112 | 10 |
| POSITION | PortfolioRiskGate._on_position | portfolio_risk.py:65 | 0 |
| POSITION | SmartRouter._on_position | smart_router.py:114 | 10 |
| POSITION | ExecutionFeedback._on_position | execution_feedback.py:102 | default |
| ORDER_FAIL | SmartRouter._on_order_fail | smart_router.py:113 | 10 |
| ORDER_FAIL | ExecutionFeedback._on_order_fail | execution_feedback.py:101 | default |
| *all 14 types* | EventSourcingSubscriber._on_* | event_sourcing_subscriber.py:111 | 10 |

**PRO Process:**

| EventType | Handler | File:Line | Priority |
|-----------|---------|-----------|----------|
| BAR | ProSetupEngine._on_bar | pro_setups/engine.py:117 | 2 |
| ORDER_REQ | _forward_order (IPC) | run_pro.py:109 | 0 |
| FILL | RiskAdapter._on_fill | risk_adapter.py:82 | 0 |
| POSITION | RiskAdapter._on_position | risk_adapter.py:83 | 0 |
| *all 14 types* | EventSourcingSubscriber._on_* | event_sourcing_subscriber.py:111 | 10 |

**POP Process:**

| EventType | Handler | File:Line | Priority |
|-----------|---------|-----------|----------|
| BAR | PopStrategyEngine._on_bar | pop_strategy_engine.py:522 | 1 |
| POP_SIGNAL | _forward_pop_signal (IPC) | run_pop.py:108 | 10 |
| ORDER_REQ | _forward_order (IPC) | run_pop.py:131 | 0 |
| *all 14 types* | EventSourcingSubscriber._on_* | event_sourcing_subscriber.py:111 | 10 |

**OPTIONS Process:**

| EventType | Handler | File:Line | Priority |
|-----------|---------|-----------|----------|
| SIGNAL | OptionsEngine._on_signal | options/engine.py:216 | 3 |
| BAR | OptionsEngine._on_bar | options/engine.py:217 | 3 |
| POP_SIGNAL | OptionsEngine._on_pop_signal | options/engine.py:218 | 3 |
| *all 14 types* | EventSourcingSubscriber._on_* | event_sourcing_subscriber.py:111 | 10 |

---

## 4. INTER-PROCESS COMMUNICATION

### 4.1 Redpanda Topics (`monitor/ipc.py`)

| Topic | Producer | Consumer | Key | Payload |
|-------|----------|----------|-----|---------|
| `th-orders` | Pro, Pop | Core | ticker | OrderRequestPayload dict |
| `th-signals` | Core | Options | ticker | SignalPayload dict |
| `th-pop-signals` | Pop | Options | symbol | PopSignalPayload dict |
| `th-fills` | Core | (logged) | ticker | Fill/Position event |
| `th-bar-ready` | Core | (info) | 'all' | {count, time} |

### 4.2 Shared Cache (`monitor/shared_cache.py`)

**File:** `data/live_cache.pkl`
**Writer:** Core (CacheWriter) — atomic write (tmp + os.replace)
**Readers:** Pro, Pop, Options (CacheReader) — polls mtime, max_age=30s
**Format:** `{timestamp, updated_at, bars: {ticker: DataFrame.to_dict()}, rvol: {ticker: DataFrame.to_dict()}}`

---

## 5. BROKER ARCHITECTURE

### 5.1 SmartRouter (`monitor/smart_router.py`)

```
ORDER_REQ arrives
    │
    ├─ SELL? → check position_broker_map.json → route to opening broker
    │          if unknown: check Alpaca positions API
    │
    └─ BUY? → round-robin between healthy brokers
              alternates: alpaca → tradier → alpaca → ...
              if one unhealthy: all to healthy one

Before SELL: cancel bracket/stop orders at broker
After BUY: save ticker→broker to position_broker_map.json
On POSITION CLOSED: remove from map
```

**Persistence:** `data/position_broker_map.json` — loaded on startup, saved on every BUY/CLOSE

### 5.2 Alpaca Broker (`monitor/brokers.py`)

**BUY:** Bracket order (LimitOrderRequest + StopLossRequest + TakeProfitRequest)
**SELL:** Market order (cancel bracket children first)
**Partial fill:** Cancel remainder, resubmit standalone stop for filled qty
**FILL tagging:** `_emit_fill()` tags event with `_routed_broker='alpaca'`

### 5.3 Tradier Broker (`monitor/tradier_broker.py`)

**BUY:** Simple limit order + standalone stop order after fill
**SELL:** Cancel open orders first, then market sell
**Partial fill:** Detected in `_poll_fill()`, cancel remainder
**FILL tagging:** `_emit_fill()` tags event with `_routed_broker='tradier'`

### 5.4 Options Broker (`options/broker.py`)

**Entry:** Multi-leg order via `POST /orders` (order_class=mleg)
**Exit:** Market multi-leg close order
**Fill polling:** 15s timeout, 1 retry
**Race detection:** If cancel fails with "already filled", treat as success

---

## 6. POSITION LIFECYCLE

### 6.1 Position Dict (created by PositionManager)

```python
{
    'entry_price': 193.48,           # from FillPayload.fill_price
    'entry_time': '14:32:00',
    'quantity': 5,
    'partial_done': False,
    'order_id': '8c4f06db-...',
    'stop_price': 193.35,            # from FillPayload.stop_price (NOT placeholder)
    'target_price': 193.74,          # from FillPayload.target_price (NOT placeholder)
    'half_target': 193.545,          # (entry + target) / 2
    'atr_value': 0.12,               # from FillPayload
    'strategy': 'pro:sr_flip',       # parsed from reason field
}
```

### 6.2 Stop/Target Preservation Chain

```
Pro generates stop=193.35 → RiskAdapter → ORDER_REQ(stop=193.35)
→ IPC Redpanda(stop=193.35) → Core receives(stop=193.35)
→ SmartRouter → Broker bracket(stop=193.35)
→ FillPayload(stop=193.35) → PositionManager uses FillPayload.stop (NOT placeholder)
→ bot_state.json(stop=193.35) → StrategyEngine._check_exits(stop=193.35)
```

### 6.3 Crash Recovery

| Layer | Protection | Recovery |
|-------|-----------|----------|
| Broker | Alpaca bracket stop / Tradier standalone stop | Survives crash |
| Disk | bot_state.json (atomic write every mutation) | Loaded on restart |
| Broker map | position_broker_map.json | SELL routes correctly |
| Redpanda | DurableEventLog (FILL events) | CrashRecovery.rebuild() |
| DB | event_store (immutable) | Full audit trail |
| Reconciliation | Compare local vs broker on startup | Add orphans, remove stale |

---

## 7. RISK MANAGEMENT

### 7.1 Risk Gates (in execution order)

| Gate | Scope | File | Priority |
|------|-------|------|----------|
| PortfolioRiskGate | All engines | monitor/portfolio_risk.py | 0 |
| SmartRouter | All engines | monitor/smart_router.py | 1 |
| RiskEngine | Core VWAP | monitor/risk_engine.py | default |
| RiskAdapter | Pro | pro_setups/risk/risk_adapter.py | N/A |
| PopExecutor | Pop | pop_strategy_engine.py | N/A |
| OptionsRiskGate | Options | options/risk.py | N/A |

### 7.2 Kill Switch

| Engine | Threshold | Config | Action |
|--------|-----------|--------|--------|
| Core | `MAX_DAILY_LOSS` (-$10,000) | config.py | Force-close all, halt |
| Pro | `PRO_MAX_DAILY_LOSS` (-$2,000) | config.py | Stop emitting ORDER_REQ |
| Pop | `POP_MAX_DAILY_LOSS` (-$2,000) | config.py | Stop emitting ORDER_REQ |
| Options | `OPTIONS_MAX_DAILY_LOSS` (-$3,000) | config.py | close_all_positions() |

### 7.3 Stop Loss Architecture

```
Strategy generates stop → engine-level 0.3% floor enforced
→ full-day S/R pivot scan (strongest tested level)
→ daily ATR fallback (1.5× ATR)
→ R:R validation (reject if < 1:1)
→ bracket/stop order at broker (crash protection)
→ StrategyEngine monitors on every bar (active management)
→ trailing stop updates (ATR-based)
```

---

## 8. DATA SOURCES

### 8.1 Market Data

| Source | API | Data | Rate Limit |
|--------|-----|------|-----------|
| Tradier | REST | 1-min bars, daily OHLCV, quotes | Bearer token, 3 retries on 429 |
| Alpaca | SDK | Bars, quotes (alternative) | API key |

### 8.2 Options Data

| Source | API | Data | Rate Limit |
|--------|-----|------|-----------|
| Alpaca Options | SDK | Option chains, Greeks, snapshots | 3 req/s token bucket |
| Yahoo Finance | yfinance | Earnings calendar | Free, no auth |

### 8.3 News & Social

| Source | API | Data | Rate Limit |
|--------|-----|------|-----------|
| Benzinga | REST | Headlines, sentiment | 500 req/min (Pro) |
| StockTwits | REST | Mentions, bull/bear % | 200 req/hr, 20s throttle |

### 8.4 Alternative Data (`data_sources/`)

**IMPORTANT: These sources are collected by `DataSourceCollector` in `run_monitor.py` (monolith mode) ONLY. They do NOT run in supervised mode (`supervisor.py` → `run_core.py`). If you use the supervisor (recommended), these data sources are NOT active.**

**Active in monolith mode (`run_monitor.py`) — NOT active in supervised mode:**

| Source | Data | When Called | Supervised Mode |
|--------|------|-----------|----------------|
| Fear & Greed | Market sentiment (0-100) | Every 10 min + start/end | NOT ACTIVE |
| FRED | Fed funds, yield curve, VIX | Session start + end | NOT ACTIVE |
| Finviz | Short float, insider trading | Every 10 min + end | NOT ACTIVE |
| SEC EDGAR | Recent filings (10-K, 8-K) | Session start | NOT ACTIVE |
| Polygon | Prev day OHLCV, ticker details | Session start | NOT ACTIVE |
| Yahoo Finance | Earnings calendar, analyst ratings | Session start + end | NOT ACTIVE |

**Active in supervised mode (collected by satellite engines):**

| Source | Data | Engine | Status |
|--------|------|--------|--------|
| Benzinga | Headlines, sentiment | Pop (`pop_screener/benzinga_news.py`) | ACTIVE |
| StockTwits | Mentions, bull/bear % | Pop (`pop_screener/stocktwits_social.py`) | ACTIVE |
| Tradier | 1-min bars, daily OHLCV | Core (`monitor/tradier_client.py`) | ACTIVE |

**Dead code (never imported):**

| Source | File Exists | Status |
|--------|------------|--------|
| Unusual Whales | Yes | Dead code — never imported or instantiated |
| Reddit | Yes | Dead code — never imported or instantiated |
| Alpha Vantage | Yes | Orphaned — initialized in collector but no collect method |

**V7.1 TODO:** Move `DataSourceCollector` into supervised mode (create `run_data_collector.py` or integrate into `run_core.py`) so Fear & Greed, FRED, Finviz, SEC EDGAR, Polygon, and Yahoo data flows during production.

---

## 9. PERSISTENCE

### 9.1 Database (TimescaleDB)

**Event Store:** `event_store` — immutable, append-only, JSONB payloads

**Projection Tables (written inline by EventSourcingSubscriber):**

| Table | Event Source | Key Fields |
|-------|-------------|------------|
| signal_events | SIGNAL | ticker, action, price, stop, target, rsi, rvol |
| order_req_events | ORDER_REQ | ticker, side, qty, price, reason, broker |
| fill_events | FILL | ticker, side, qty, fill_price, broker, bracket_order |
| position_events | POSITION | ticker, action, qty, entry, exit, pnl, broker |
| completed_trades | POSITION(CLOSED) | ticker, entry/exit prices, pnl, strategy, broker |
| risk_block_events | RISK_BLOCK | ticker, reason, signal_action |
| heartbeat_events | HEARTBEAT | open_positions, scan_count |
| pro_strategy_signal_events | PRO_STRATEGY_SIGNAL | ticker, strategy, tier, direction, stops, detector_signals |
| pop_signal_events | POP_SIGNAL | symbol, strategy_type, pop_reason, features_json |

**Write mechanism:** Async batch writer (DBWriter) — 200 rows/batch, 0.5s flush, circuit breaker on 5 failures

### 9.2 File-Based State

| File | Purpose | Writer | Reader | Atomic |
|------|---------|--------|--------|--------|
| `bot_state.json` | Positions, trades, cooldowns | PositionManager | Monitor startup | Yes (tmp+replace) |
| `data/position_broker_map.json` | Position → broker mapping | SmartRouter | SmartRouter startup | Yes (locked) |
| `data/position_registry.json` | Cross-process position dedup | All processes | All processes | Yes (fcntl.flock) |
| `data/live_cache.pkl` | Shared market data | Core CacheWriter | Satellite CacheReaders | Yes (tmp+replace) |
| `data/iv_history.json` | Options IV rank history | IVTracker | IVTracker startup | Yes (tmp+replace) |
| `data/supervisor_status.json` | Process health | Supervisor | Dashboard | Yes |
| `data/{engine}_state.json` | Engine lifecycle state | EngineLifecycle | EngineLifecycle startup | Yes (tmp+replace) |

---

## 10. LIFECYCLE MANAGEMENT

### 10.1 EngineLifecycle (`lifecycle/core.py`)

Shared module for Pro, Pop, Options. Three hooks called from run scripts:

```python
lifecycle.startup()   # preflight + restore state + reconcile broker
lifecycle.tick()      # heartbeat (60s) + kill switch + hourly P&L + reconcile (30 min) + state save
lifecycle.shutdown()  # close positions + EOD report + final state save
```

### 10.2 Adapters (`lifecycle/adapters/`)

| Adapter | Engine | State Persisted |
|---------|--------|----------------|
| ProLifecycleAdapter | ProSetupEngine + RiskAdapter | positions (Set), cooldowns, dedup, trade_log |
| PopLifecycleAdapter | PopStrategyEngine + PopExecutor | positions (Set), cooldowns, trade_log |
| OptionsLifecycleAdapter | OptionsEngine | positions (Dict), daily P&L, entries/exits, Greeks, risk gate |

### 10.3 EOD Reports

Generated by `SatelliteEODReport` on shutdown:
- Overall stats (trades, wins, P&L, profit factor)
- Exit reason breakdown
- Per-strategy breakdown
- Trade-by-trade log
- Sent via email + logged

---

## 11. CONFIGURATION

### 11.1 Key Config Values (`config.py`)

| Parameter | Default | Env Var |
|-----------|---------|---------|
| TICKERS | 183 symbols | — |
| MAX_POSITIONS | 5 | — |
| GLOBAL_MAX_POSITIONS | 75 | GLOBAL_MAX_POSITIONS |
| MAX_DAILY_LOSS | -$10,000 | MAX_DAILY_LOSS |
| PRO_MAX_DAILY_LOSS | -$2,000 | PRO_MAX_DAILY_LOSS |
| POP_MAX_DAILY_LOSS | -$2,000 | POP_MAX_DAILY_LOSS |
| OPTIONS_MAX_DAILY_LOSS | -$3,000 | OPTIONS_MAX_DAILY_LOSS |
| TRADE_BUDGET | $1,000 | TRADE_BUDGET |
| ORDER_COOLDOWN | 300s | — |
| BROKER_MODE | 'smart' | BROKER_MODE |
| DATA_SOURCE | 'tradier' | DATA_SOURCE |
| PAPER_TRADING | True | PAPER_TRADING |

### 11.2 Accounts

| Account | Env Vars | Purpose |
|---------|----------|---------|
| Alpaca Equity | APCA_API_KEY_ID, APCA_API_SECRET_KEY | Core + Pro execution |
| Alpaca Options | APCA_OPTIONS_KEY, APCA_OPTIONS_SECRET | Options execution |
| Tradier | TRADIER_SANDBOX_TOKEN, TRADIER_ACCOUNT_ID | Secondary broker + data |

---

## 12. FILE STRUCTURE

```
trading_hub/
├── scripts/
│   ├── supervisor.py          # Process manager
│   ├── run_core.py            # Core process entry
│   ├── run_pro.py             # Pro process entry
│   ├── run_pop.py             # Pop process entry
│   ├── run_options.py         # Options process entry
│   ├── _db_helper.py          # Shared DB init
│   ├── crash_analyzer.py      # Auto-fix
│   ├── watchdog.py            # Monolith mode watchdog
│   └── reconcile_manual_closes.py  # Orphan cleanup
├── monitor/
│   ├── monitor.py             # RealTimeMonitor
│   ├── event_bus.py           # EventBus v5.2
│   ├── events.py              # Payload dataclasses
│   ├── ipc.py                 # Redpanda IPC
│   ├── shared_cache.py        # CacheWriter/Reader
│   ├── strategy_engine.py     # VWAP strategy
│   ├── risk_engine.py         # Risk gating
│   ├── brokers.py             # AlpacaBroker
│   ├── tradier_broker.py      # TradierBroker
│   ├── smart_router.py        # SmartRouter
│   ├── position_manager.py    # Position lifecycle
│   ├── execution_feedback.py  # Fill patching
│   ├── state_engine.py        # Read-only snapshot
│   ├── portfolio_risk.py      # Aggregate limits
│   ├── observability.py       # Heartbeat, EOD
│   ├── event_log.py           # DurableEventLog
│   ├── alerts.py              # SMTP alerts
│   ├── state.py               # bot_state.json
│   ├── rvol.py                # RVOLEngine
│   ├── data_client.py         # Data client factory
│   ├── tradier_client.py      # Tradier API
│   ├── signals.py             # SignalAnalyzer
│   └── distributed_registry.py # Position registry
├── pro_setups/
│   ├── engine.py              # ProSetupEngine
│   ├── detectors/ (11 files)  # Technical detectors
│   ├── strategies/ (11 files) # Entry strategies (3 tiers)
│   ├── risk/risk_adapter.py   # Pro risk gate
│   └── router/                # Signal router
├── pop_strategy_engine.py     # PopStrategyEngine + PopExecutor
├── pop_screener/
│   ├── screener.py            # 6 screening rules
│   ├── features.py            # Feature engineering
│   ├── strategy_router.py     # 7 strategies + momentum fallback
│   ├── benzinga_news.py       # Benzinga adapter
│   ├── stocktwits_social.py   # StockTwits adapter
│   └── strategies/ (7 files)  # Entry engines
├── options/
│   ├── engine.py              # OptionsEngine
│   ├── broker.py              # AlpacaOptionsBroker
│   ├── chain.py               # Option chain client
│   ├── selector.py            # Strategy selector
│   ├── iv_tracker.py          # IV rank tracking
│   ├── risk.py                # OptionsRiskGate
│   ├── earnings_calendar.py   # Earnings safety
│   └── strategies/ (5 files)  # 13 options strategies
├── lifecycle/
│   ├── __init__.py            # EngineLifecycle
│   ├── core.py                # Orchestrator
│   ├── state_persistence.py   # AtomicStateFile
│   ├── kill_switch.py         # SatelliteKillSwitch
│   ├── heartbeat.py           # SatelliteHeartbeat
│   ├── eod_report.py          # SatelliteEODReport
│   ├── reconciler.py          # PositionReconciler
│   └── adapters/ (3 files)    # Pro/Pop/Options adapters
├── db/
│   ├── writer.py              # DBWriter (async batch)
│   ├── event_sourcing_subscriber.py  # Event → DB
│   └── migrations/sql/ (12 files)
├── data_sources/ (10 files)   # Alternative data
├── config.py                  # Central config
├── .env                       # API keys
├── data/                      # State files
└── logs/YYYYMMDD/             # Daily log dirs
```

---

*Generated from codebase on 2026-04-15. 100+ source files traced. Every component, subscription, data flow, and persistence mechanism documented from actual code.*
