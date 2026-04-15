# Trading Hub — Model-Switch State Summary Prompt

> Paste this entire document at the start of a new conversation when switching
> AI models or starting a fresh session.  It gives the new model full context
> to continue without re-reading the codebase from scratch.

---

## Project identity

**Name:** Trading Hub
**Repo path:** `~/Documents/santosh/trading_hub/trading_hub/`
**Active branch:** `tradinghub_options`
**Language:** Python 3.10+
**Purpose:** Real-time intraday algorithmic trading system — live execution via Alpaca + Tradier, bar data via Tradier, durable event log via Redpanda, persistence via TimescaleDB.
**Current rating:** 7.6/10

---

## Architecture: 18+ layer event pipeline with process isolation

```
monitor.run() → emit_batch(BAR[])
    └─ ProSetupEngine     (T3.6)  BAR → 11 detectors → PRO_STRATEGY_SIGNAL → ORDER_REQ
    └─ PopStrategyEngine  (T3.5)  BAR → POP_SIGNAL (durable) + PopExecutor → FILL
    └─ StrategyEngine     (T4)    BAR → SIGNAL
    └─ OptionsEngine      (T3.7)  SIGNAL/BAR → OPTIONS_SIGNAL (13 strategies, $10K budget)
    └─ RiskEngine         (T3)    SIGNAL → ORDER_REQ | RISK_BLOCK
    └─ PortfolioRisk              Portfolio-level drawdown, notional, Greeks limits
    └─ RiskSizer                  Beta-adjusted sizing, correlation limits, sector caps
    └─ SmartRouter                Multi-broker routing (Alpaca primary, Tradier secondary)
    └─ Broker (Alpaca)            ORDER_REQ → FILL | ORDER_FAIL
    └─ Broker (Tradier)           ORDER_REQ → FILL | ORDER_FAIL (failover)
    └─ OptionsBroker              OPTIONS_SIGNAL → multi-leg FILL (via AlpacaOptionsBroker)
    └─ ExecutionFeedback          FILL(buy) → patches stop/target
    └─ PositionManager            FILL → POSITION
    └─ StateEngine                POSITION → read-only snapshot
    └─ HeartbeatEmitter           tick → HEARTBEAT
    └─ EventLogger                * → log line
    └─ DurableEventLog            ORDER_REQ/FILL/POP_SIGNAL/OPTIONS_SIGNAL → Redpanda
    └─ DBSubscriber               * → DBWriter → TimescaleDB (async batch persist)
    └─ DataSourceCollector        9 sources → SharedCache (market context)
    └─ Watchdog/Supervisor        Process health monitoring + auto-restart
```

All inter-layer communication goes through the `EventBus` (`monitor/event_bus.py`).  **No layer calls another layer's methods directly.**

### Two execution modes

| Mode | Env var | Description |
|------|---------|-------------|
| **Monolith** (default) | `MONITOR_MODE=monolith` | Single process, all engines in one EventBus |
| **Isolated** | `MONITOR_MODE=isolated` | 4 separate processes (T4, T3.6, T3.5, Options), IPC via Redpanda, SharedCache for market data |

---

## Broker accounts (5 total)

| Account | Purpose | Env vars |
|---------|---------|----------|
| Alpaca Main | VWAP Reclaim + Pro Setups execution | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` |
| Alpaca Pop | Pop strategy execution (isolated capital) | `APCA_POPUP_KEY`, `APCA_PUPUP_SECRET_KEY` |
| Alpaca Options | Options trading ($10K budget) | `APCA_OPTIONS_KEY`, `APCA_OPTIONS_SECRET` |
| Alpaca Pro | Pro Setups dedicated (when isolated) | `APCA_PRO_KEY`, `APCA_PRO_SECRET` |
| Tradier | Secondary broker (failover + data) | `TRADIER_TRADING_TOKEN`, `TRADIER_ACCOUNT_ID` |

---

## Data sources (9)

| # | Source | Key required? | Status |
|---|--------|---------------|--------|
| 1 | Yahoo Finance (fundamentals, mcap, PE, beta) | No | Active |
| 2 | Fear & Greed Index (CNN) | No | Active |
| 3 | FRED (macro regime: rates, yield curve, VIX) | No (basic) | Active |
| 4 | Finviz (screener: short float, analyst targets) | No | Active |
| 5 | SEC EDGAR (insider filings, 13F) | No (email only) | Active |
| 6 | Alpha Vantage (earnings calendar, technicals) | Yes | Active |
| 7 | Polygon (tick data, aggregates) | Yes | Active |
| 8 | Reddit (WSB sentiment) | Yes (OAuth) | Disabled |
| 9 | Unusual Whales (options flow) | Yes | Disabled |

Files: `data_sources/{yahoo_finance,fear_greed,fred_macro,finviz_data,sec_edgar,alpha_vantage,polygon_data,reddit_sentiment,unusual_whales}.py` + `collector.py` + `persistence.py`

---

## Risk stack: 18 checks

| Category | Checks |
|----------|--------|
| **Pre-trade (RiskEngine)** | Max positions, cooldown, reclaim-today, RVOL threshold, RSI bounds, spread check |
| **Portfolio-level** | Intraday drawdown halt (-$5K), notional cap ($100K), portfolio delta limit, portfolio gamma limit |
| **Position sizing (RiskSizer)** | Beta-adjusted qty (TQQQ 10 shares → 3), correlation group limit (max 3 same sector), sector cap, volatility scaling (ATR-based), news override |
| **Kill switches** | Daily loss halt (-$10K monolith, -$5K portfolio risk), force close time (15:00 ET) |
| **Cross-layer** | GlobalPositionRegistry dedup (prevents duplicate orders across T4/T3.6/T3.5/Options) |

---

## EventBus: key facts

- **Version:** v5.2
- **File:** `monitor/event_bus.py`
- **EventType enum** (all values): `BAR, QUOTE, SIGNAL, ORDER_REQ, FILL, ORDER_FAIL, POSITION, RISK_BLOCK, HEARTBEAT, POP_SIGNAL, PRO_STRATEGY_SIGNAL, OPTIONS_SIGNAL`
- **Partitioning:** ticker-based hash (md5, stable across restarts).  Same ticker → same worker across ALL EventTypes.
- **Backpressure:** `DROP_OLDEST` for market data; `BLOCK` for order/fill path.
- **Durable delivery:** `emit(durable=True)` runs all `before_emit_hooks` (Redpanda produce+flush) synchronously before any handler is called.
- **Payload validation:** `_PAYLOAD_TYPES` dict maps each `EventType` to its frozen dataclass.  `emit()` raises `TypeError` for wrong payload types.

---

## Event payloads: source of truth

All payloads are **frozen dataclasses** in `monitor/events.py`.  Every field is validated in `__post_init__`.  Never pass raw dicts to the bus.

| EventType | Payload class | Key fields |
|-----------|--------------|------------|
| BAR | `BarPayload` | `ticker, df (pd.DataFrame, read-only), rvol_df` |
| SIGNAL | `SignalPayload` | `ticker, action (SignalAction), current_price, ask_price, atr_value, rsi_value, rvol, vwap, stop_price, target_price, half_target, reclaim_candle_low, needs_ask_refresh` |
| ORDER_REQ | `OrderRequestPayload` | `ticker, side (Side), qty, price, reason, needs_ask_refresh, stop_price?, target_price?, atr_value?` |
| FILL | `FillPayload` | `ticker, side, qty, fill_price, order_id, reason, stop_price?, target_price?, atr_value?` |
| POP_SIGNAL | `PopSignalPayload` | `symbol, strategy_type (str), entry_price, stop_price, target_1, target_2, pop_reason (str), atr_value, rvol, vwap_distance, strategy_confidence, features_json` |
| PRO_STRATEGY_SIGNAL | `ProStrategySignalPayload` | `ticker, strategy_name, tier (1/2/3), direction ('long'/'short'), entry_price, stop_price, target_1, target_2, atr_value, rvol, rsi_value, vwap, confidence, detector_signals (JSON)` |
| OPTIONS_SIGNAL | `OptionsSignalPayload` | `ticker, strategy_type (OptionStrategyType enum), underlying_price, expiry_date, net_debit, max_risk, max_reward, atr_value, rvol, rsi_value, legs_json (serialized), source ('signal'/'bar_scan')` |
| POSITION | `PositionPayload` | `ticker, action (PositionAction), position (PositionSnapshot?), pnl?` |
| RISK_BLOCK | `RiskBlockPayload` | `ticker, reason, signal_action` |

`SignalAction` values: `BUY, SELL_STOP, SELL_TARGET, SELL_RSI, SELL_VWAP, PARTIAL_SELL, HOLD`
`Side` values: `BUY, SELL`
`PositionAction` values: `OPENED, PARTIAL_EXIT, CLOSED`

---

## Key invariants (never violate these)

1. `stop_price < current_price` and `target_price > current_price` for BUY signals.
2. `target_1 < target_2` for POP_SIGNAL.
3. Exit signals (SELL_*) bypass ALL RiskEngine checks — exits must always execute.
4. `emit(durable=True)` must be used for `ORDER_REQ`, `FILL`, and `POP_SIGNAL` events.
5. `bot_state.json` is written atomically (tmp → rename) — never write it directly.
6. One monitor instance at a time — enforced via `.monitor.lock`.
7. All payload dataclasses are frozen — use `object.__setattr__` for coercion in `__post_init__` only.

---

## Self-healing infrastructure

| Component | File | Purpose |
|-----------|------|---------|
| Watchdog | `scripts/watchdog.py` | Monitors process health, restarts crashed engines |
| Crash Analyzer | `scripts/crash_analyzer.py` | Post-mortem analysis, pattern detection, auto-fix suggestions |
| Supervisor | `scripts/supervisor.py` | Manages 4 isolated engine processes, IPC coordination |
| CrashRecovery | `monitor/event_log.py` | Rebuilds state from Redpanda on startup |

---

## Pop-stock subsystem: structure

**Location:** `pop_screener/` package + `pop_strategy_engine.py` (T3.5 layer)

**Pipeline:**
```
BAR → PopStrategyEngine._on_bar()
  1. _bar_payload_to_slice()       convert BarPayload.df → MarketDataSlice
  2. news_source.get_news()        NewsSentimentSource (mock or real)
  3. social_source.get_social()    SocialSentimentSource (mock or real)
  4. FeatureEngineer.compute()     → EngineeredFeatures
  5. PopScreener.screen()          → PopCandidate | None
  6. StrategyClassifier.classify() → StrategyAssignment
  7. StrategyRouter.route()        → list[EntrySignal], list[ExitSignal]
  8. emit POP_SIGNAL (durable=True) → Redpanda
  9. for long entries: PopExecutor.execute_entry()
       └─ own TradingClient (pop Alpaca account)
       └─ marketable limit buy → poll → emit FILL (durable=True)
     for short entries (PARABOLIC_REVERSAL): POP_SIGNAL only, no execution
```

**6 strategy engines:**

| Engine | Entry trigger | Side |
|--------|--------------|------|
| `VWAPReclaimEngine` | 2-bar dip+reclaim, RVOL>=1.5x, RSI 50-70 | Long |
| `ORBEngine` | Break above 15-min opening range high, vol>=1.5x OR avg | Long |
| `HaltResumeEngine` | Halt-like bar (>=10% move) → consolidation → breakout | Long |
| `ParabolicReversalEngine` | >=50% intraday move + exhaustion candle wick>=40% | **Short** |
| `EMATrendEngine` | Pullback to EMA9, confirmation above EMA20 | Long |
| `BOPBEngine` | Prior high breakout → pullback test → confirmation | Long |

---

## File map: where to find things

| What you need | File |
|---------------|------|
| EventType enum | `monitor/event_bus.py` line ~275 |
| All payload dataclasses | `monitor/events.py` |
| Bus async config | `monitor/event_bus.py` `_DEFAULT_ASYNC_CONFIG` dict |
| Bus priority config | `monitor/event_bus.py` `_DEFAULT_PRIORITY` dict |
| `_PAYLOAD_TYPES` map | `monitor/event_bus.py` line ~742 |
| VWAP Reclaim strategy logic | `monitor/signals.py` `SignalAnalyzer` |
| Pre-trade risk checks (6) | `monitor/risk_engine.py` `RiskEngine._handle_buy()` |
| Portfolio-level risk | `monitor/portfolio_risk.py` |
| Beta-adjusted sizing + correlation | `monitor/risk_sizing.py` |
| Smart broker routing | `monitor/smart_router.py` |
| Tradier broker adapter | `monitor/tradier_broker.py` |
| Inter-process communication | `monitor/ipc.py` |
| Shared market data cache | `monitor/shared_cache.py` |
| Distributed position registry | `monitor/distributed_registry.py` |
| Order execution (buy/sell) | `monitor/brokers.py` `AlpacaBroker` / `PaperBroker` |
| Position lifecycle | `monitor/position_manager.py` |
| Crash recovery (Redpanda replay) | `monitor/event_log.py` `CrashRecovery` |
| State persistence (bot_state.json) | `monitor/state.py` |
| Pop screener thresholds | `pop_screener/config.py` |
| Pop data models | `pop_screener/models.py` |
| Pop feature engineering | `pop_screener/features.py` |
| Pop screening rules | `pop_screener/screener.py` |
| Pop classifier | `pop_screener/classifier.py` |
| Pro-setup engine (T3.6) | `pro_setups/engine.py` |
| 11 detectors | `pro_setups/detectors/*.py` |
| Pro strategy classifier | `pro_setups/classifiers/strategy_classifier.py` |
| 11 strategy modules | `pro_setups/strategies/tier{1,2,3}/*.py` |
| Pro risk adapter | `pro_setups/risk/risk_adapter.py` |
| Options engine (T3.7) | `options/engine.py` |
| Options strategy selector | `options/selector.py` |
| Options chain client | `options/chain.py` |
| Options risk gate | `options/risk.py` |
| Options broker | `options/broker.py` |
| Options strategies (13) | `options/strategies/{base,directional,vertical,volatility,neutral,time_based,complex}.py` |
| T3.5 BAR subscriber + PopExecutor | `pop_strategy_engine.py` |
| Data source collector | `data_sources/collector.py` |
| Smart persistence (dedup) | `data_sources/persistence.py` |
| Individual data sources | `data_sources/{yahoo_finance,fear_greed,fred_macro,finviz_data,sec_edgar,alpha_vantage,polygon_data,reddit_sentiment,unusual_whales}.py` |
| Watchdog | `scripts/watchdog.py` |
| Crash analyzer | `scripts/crash_analyzer.py` |
| Process supervisor | `scripts/supervisor.py` |
| All configuration | `config.py` |
| DB connection pool | `db/connection.py` |
| Async batch writer + SessionManager | `db/writer.py` |
| EventBus → DB bridge | `db/subscriber.py` |
| DB read queries + replay | `db/reader.py` |
| ML feature store | `db/feature_store.py` |
| SQL migrations (001-012) | `db/migrations/sql/*.sql` |
| Migration runner | `db/migrations/run.py` |
| Activity logger | `db/activity_logger.py` |
| Cross-layer position dedup | `monitor/position_registry.py` |
| Docker orchestration | `docker/docker-compose.yml` |

---

## Current state of the codebase (as of 2026-04-14)

### What works and is production-hardened

- EventBus v5.2: priority queues, causal partitioning, durable delivery, backpressure
- VWAP Reclaim strategy: 9 entry filters, 6 exit conditions, LRU indicator cache
- RiskEngine: 6 pre-trade checks (max positions, cooldown, reclaim-today, RVOL, RSI, spread)
- Portfolio risk: drawdown halt, notional cap, portfolio Greeks limits
- Risk sizing: beta-adjusted quantities, correlation group limits, sector caps, volatility scaling
- Smart routing: multi-broker failover (Alpaca primary, Tradier secondary), circuit breaker per broker
- AlpacaBroker: limit+retry buy (2 retries, 5s timeout, 0.5% slippage cap), market sell with idempotent client_order_id
- TradierBroker: REST-based execution, sandbox + live modes
- CrashRecovery: wired in run_monitor.py; rebuilds from Redpanda on startup
- State persistence: atomic writes, Alpaca reconciliation on restart, StateEngine seeding
- GlobalPositionRegistry: cross-layer dedup prevents duplicate orders across T4/T3.6/T3.5
- Data source collector: 9 sources with circuit breaker, smart persistence (dedup), configurable refresh
- Self-healing: watchdog monitors process health, crash analyzer detects patterns, supervisor auto-restarts
- Integration test: 38/38 passing (scripts/test_isolated_mode.py)
- April 14 live trading: 126 trades, 80W/46L, +$31.80 realized

### What is not yet done / known limitations

- Pop screener has never generated a live signal with real RVOL data
- Process isolation (MONITOR_MODE=isolated) untested under real market load
- ML classifier needs 2-3 weeks of signal data before training
- Options backtesting requires IV history seeding
- PopStrategyEngine thread safety (race condition on _positions)
- Short selling (PARABOLIC_REVERSAL) emits POP_SIGNAL only — execution deferred

---

## Common patterns used in this codebase

### Subscribing to events

```python
bus.subscribe(EventType.BAR, self._on_bar, priority=1)
# Handler signature:
def _on_bar(self, event: Event) -> None:
    payload: BarPayload = event.payload
    ...
```

### Emitting events

```python
# Non-durable (most events)
self._bus.emit(Event(type=EventType.SIGNAL, payload=signal_payload))

# Durable (ORDER_REQ, FILL, POP_SIGNAL)
self._bus.emit(
    Event(type=EventType.POP_SIGNAL, payload=pop_payload, correlation_id=source_event.event_id),
    durable=True,
)
```

### Adding a new EventType (checklist)

1. Add `NEW_EVENT = auto()` to `EventType` in `monitor/event_bus.py`
2. Add frozen dataclass to `monitor/events.py` with `__post_init__` validation
3. Import in `event_bus.py` and add to `_PAYLOAD_TYPES`
4. Add to `_DEFAULT_ASYNC_CONFIG` (maxsize, policy, n_workers)
5. Add to `_DEFAULT_PRIORITY`
6. Update `monitor/events.py` docstring ("Adding a new event" section)

### Payload coercion pattern (frozen dataclass)

```python
# Inside __post_init__ of a frozen dataclass:
object.__setattr__(self, 'side', Side(self.side))   # coerce str → enum
```

---

## Things to avoid

- **Never** modify existing `StrategyEngine`, `RiskEngine`, or `PositionManager` logic to accommodate new strategies — add new layers or extend via event types instead.
- **Never** put raw dicts on the EventBus — always use the typed payload dataclasses.
- **Never** write `bot_state.json` directly — always use `save_state()` from `monitor/state.py`.
- **Never** use `hash()` for partition keys — use `hashlib.md5` (hash is process-randomised; md5 is stable).
- **Never** emit `ORDER_REQ` or `FILL` without `durable=True`.
- **Never** skip the spread check for buy entries — `RiskEngine` does this, not the strategy engine.
- **Never** add logic to strategy engines that depends on position state — that belongs in `PositionManager`.
- **Never** call `bus.subscribe()` after `bus.start()` in async mode — subscriptions must be registered before the dispatcher threads begin.

---

## How to run the system

```bash
# Monolith mode (default — single process)
source venv/bin/activate && python run_monitor.py

# Isolated mode (4 processes, IPC via Redpanda)
MONITOR_MODE=isolated python run_monitor.py

# Integration test (38 checks)
python scripts/test_isolated_mode.py

# Run all unit tests
python test/run_all_tests.py

# Streamlit UI
streamlit run app.py

# Offline demo (no API keys)
python demo_pop_strategies.py --verbose
```

---

## Branch status

```
branch:  tradinghub_options
ahead of main by: ~20+ commits
```

---

## .env variables needed (complete list)

### Core trading
```
APCA_API_KEY_ID=PKxxxxxxxxxxxxxxxx          # Alpaca main account
APCA_API_SECRET_KEY=xxxxxxxxxxxxxxxx        # Alpaca main account
PAPER_TRADING=true                           # true=paper, false=live
DATA_SOURCE=tradier                          # 'tradier' or 'alpaca'
BROKER=alpaca                                # 'alpaca' or 'paper'
BROKER_MODE=alpaca                           # 'alpaca', 'tradier', or 'smart'
TRADIER_TOKEN=xxxxxxxx                       # market data token
```

### Pop-strategy dedicated account
```
APCA_POPUP_KEY=PKxxxxxxxxxxxxxxxx
APCA_PUPUP_SECRET_KEY=xxxxxxxxxxxxxxxx
POP_PAPER_TRADING=true
POP_MAX_POSITIONS=25
POP_TRADE_BUDGET=10000
POP_ORDER_COOLDOWN=300
```

### Pro-setups subsystem
```
PRO_MAX_POSITIONS=25
PRO_TRADE_BUDGET=1000
PRO_ORDER_COOLDOWN=300
```

### Options engine
```
APCA_OPTIONS_KEY=PKxxxxxxxxxxxxxxxx
APCA_OPTIONS_SECRET=xxxxxxxxxxxxxxxx
OPTIONS_PAPER_TRADING=true
OPTIONS_MAX_POSITIONS=5
OPTIONS_TRADE_BUDGET=500
OPTIONS_TOTAL_BUDGET=10000
OPTIONS_ORDER_COOLDOWN=300
OPTIONS_MIN_DTE=20
OPTIONS_MAX_DTE=45
OPTIONS_LEAPS_DTE=365
```

### Tradier trading (secondary broker)
```
TRADIER_TRADING_TOKEN=xxxxxxxx
TRADIER_ACCOUNT_ID=xxxxxxxx
TRADIER_SANDBOX=true
TRADIER_SANDBOX_TOKEN=xxxxxxxx
```

### Portfolio risk
```
MAX_DAILY_LOSS=-10000
MAX_INTRADAY_DRAWDOWN=-5000
MAX_NOTIONAL_EXPOSURE=100000
MAX_PORTFOLIO_DELTA=5.0
MAX_PORTFOLIO_GAMMA=1.0
GLOBAL_MAX_POSITIONS=75
```

### External data APIs
```
BENZINGA_API_KEY=xxxxxxxx
STOCKTWITS_TOKEN=xxxxxxxx               # optional (public API works without)
FRED_API_KEY=xxxxxxxx
ALPHA_VANTAGE_KEY=xxxxxxxx
POLYGON_KEY=xxxxxxxx
SEC_EDGAR_EMAIL=your@email.com
REDDIT_CLIENT_ID=xxxxxxxx               # disabled for now
REDDIT_CLIENT_SECRET=xxxxxxxx           # disabled for now
```

### Database
```
DATABASE_URL=postgresql://trading:trading_secret@localhost:5432/trading_hub
DB_ENABLED=true
DB_BATCH_MAX=200
DB_FLUSH_INTERVAL=0.5
DB_QUEUE_MAXSIZE=10000
DB_CIRCUIT_BREAKER_FAILURES=5
DB_CIRCUIT_BREAKER_RESET=30
```

### Alerts
```
ALERT_EMAIL_TO=your@email.com
```
