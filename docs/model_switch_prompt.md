# Trading Hub — Model-Switch State Summary Prompt

> Paste this entire document at the start of a new conversation when switching
> AI models or starting a fresh session.  It gives the new model full context
> to continue without re-reading the codebase from scratch.

---

## Project identity

**Name:** Trading Hub
**Repo path:** `~/Documents/santosh/trading_hub/trading_hub/`
**Active branch:** `tradier_platform`
**Language:** Python 3.10+
**Purpose:** Real-time intraday algorithmic trading system — live execution via Alpaca, bar data via Tradier, durable event log via Redpanda.

---

## Architecture: 10-layer event pipeline

```
monitor.run() → emit_batch(BAR[])
    └─ ProSetupEngine     (T3.6)  BAR → 11 detectors → PRO_STRATEGY_SIGNAL → ORDER_REQ
    └─ PopStrategyEngine  (T3.5)  BAR → POP_SIGNAL (durable) + PopExecutor → FILL
    └─ StrategyEngine     (T4)    BAR → SIGNAL
    └─ RiskEngine         (T3)    SIGNAL → ORDER_REQ | RISK_BLOCK
    └─ Broker                     ORDER_REQ → FILL | ORDER_FAIL
    └─ ExecutionFeedback          FILL(buy) → patches stop/target
    └─ PositionManager            FILL → POSITION
    └─ StateEngine                POSITION → read-only snapshot
    └─ HeartbeatEmitter           tick → HEARTBEAT
    └─ EventLogger                * → log line
    └─ DurableEventLog            ORDER_REQ/FILL/POP_SIGNAL → Redpanda
    └─ DBSubscriber               * → DBWriter → TimescaleDB (async batch persist)
```

All inter-layer communication goes through the `EventBus` (`monitor/event_bus.py`).  **No layer calls another layer's methods directly.**

---

## EventBus: key facts

- **Version:** v5.2
- **File:** `monitor/event_bus.py`
- **EventType enum** (all values): `BAR, QUOTE, SIGNAL, ORDER_REQ, FILL, ORDER_FAIL, POSITION, RISK_BLOCK, HEARTBEAT, POP_SIGNAL, PRO_STRATEGY_SIGNAL`
- **Partitioning:** ticker-based hash (md5, stable across restarts).  Same ticker → same worker across ALL EventTypes.  This guarantees `BAR→SIGNAL→ORDER→FILL` for one ticker never races across workers.
- **Backpressure:** `DROP_OLDEST` for market data; `BLOCK` for order/fill path.
- **Durable delivery:** `emit(durable=True)` runs all `before_emit_hooks` (Redpanda produce+flush) synchronously before any handler is called.
- **Payload validation:** `_PAYLOAD_TYPES` dict maps each `EventType` to its frozen dataclass.  `emit()` raises `TypeError` for wrong payload types.
- **BAR config:** `maxsize=500` (125 slots/partition), `n_workers=4`, `DROP_OLDEST`, `coalesce=True`.
- **SIGNAL / POP_SIGNAL config:** `maxsize=50`, `n_workers=2`, `DROP_OLDEST`.
- **ORDER_REQ / FILL config:** `maxsize=100`, `n_workers=1`, `BLOCK`.

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

**Execution isolation:** `PopExecutor` owns its own `TradingClient` built from `APCA_POPUP_KEY` / `APCA_PUPUP_SECRET_KEY`.  It never emits `ORDER_REQ`, so the main `AlpacaBroker` never sees pop orders.  Pop capital is fully separate from VWAP Reclaim capital.

**6 strategy engines:**

| Engine | Entry trigger | Side |
|--------|--------------|------|
| `VWAPReclaimEngine` | 2-bar dip+reclaim, RVOL≥1.5×, RSI 50–70 | Long |
| `ORBEngine` | Break above 15-min opening range high, vol≥1.5× OR avg | Long |
| `HaltResumeEngine` | Halt-like bar (≥10% move) → consolidation → breakout | Long |
| `ParabolicReversalEngine` | ≥50% intraday move + exhaustion candle wick≥40% | **Short** |
| `EMATrendEngine` | Pullback to EMA9, confirmation above EMA20 | Long |
| `BOPBEngine` | Prior high breakout → pullback test → confirmation | Long |

**Short entries** (PARABOLIC_REVERSAL) emit `POP_SIGNAL` only — not translated to `SIGNAL`.  The existing `RiskEngine` only handles longs.

**All thresholds:** `pop_screener/config.py` — 60+ named constants.

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
| T3.5 BAR subscriber + PopExecutor | `pop_strategy_engine.py` |
| All configuration (incl. pop credentials) | `config.py` (tickers, strategy params, credentials, ALPACA_POPUP_KEY, POP_*) |
| Architectural decisions | `docs/dev_notes.md` |
| DB connection pool | `db/connection.py` |
| Async batch writer + SessionManager | `db/writer.py` |
| EventBus → DB bridge | `db/subscriber.py` |
| DB read queries + replay | `db/reader.py` |
| ML feature store | `db/feature_store.py` |
| SQL migrations (001–009) | `db/migrations/sql/*.sql` |
| Migration runner | `db/migrations/run.py` |
| Docker orchestration | `docker/docker-compose.yml` |
| PostgreSQL tuning | `docker/postgresql.conf` |

---

## Current state of the codebase (as of 2026-04-12)

### What works and is production-hardened

- EventBus v5.2: priority queues, causal partitioning, durable delivery, backpressure
- VWAP Reclaim strategy: 9 entry filters, 6 exit conditions, LRU indicator cache
- RiskEngine: 6 pre-trade checks (max positions, cooldown, reclaim-today, RVOL, RSI, spread)
- AlpacaBroker: limit+retry buy (3 retries, 0.5% slippage cap), market sell
- PaperBroker: local simulation, no API needed
- CrashRecovery: exact stop/target/ATR from FillPayload (no more ±% approximations)
- State persistence: atomic writes, Alpaca reconciliation on restart, StateEngine seeding
- 10-test integration suite in `test/`; 9/10 pass (T4 latency: P95 53–58 ms vs 50 ms target)

### What was added in the most recent session

- `PRO_STRATEGY_SIGNAL` EventType + `ProStrategySignalPayload` payload
- `pro_setups/` package: 38 new files — 11 detectors, 11 strategies (3 tiers), classifier, router, RiskAdapter, ProSetupEngine
- `run_monitor.py` wired to instantiate `ProSetupEngine` before `monitor.start()`
- `config.py` added: `PRO_MAX_POSITIONS`, `PRO_TRADE_BUDGET`, `PRO_ORDER_COOLDOWN`
- `POP_SIGNAL` EventType + `PopSignalPayload` payload
- `pop_screener/` package: 6 rule-based screeners, 6 strategy engines, feature engineering, classifier, router
- `pop_strategy_engine.py` T3.5 layer with `PopExecutor` (dedicated Alpaca account, independent risk gate)
- `demo_pop_strategies.py` offline demo (0 errors)
- `stop_price/target_price/atr_value` forwarded through `OrderRequestPayload → FillPayload → CrashRecovery`
- BAR queue depth 200 → 500; TradierDataClient HTTP pool raised to `_MAX_WORKERS`
- SignalAnalyzer LRU indicator cache (500 entries, FIFO eviction)
- `run_monitor.py` wired to instantiate `PopStrategyEngine` with pop credentials before `monitor.start()`
- `config.py` added: `ALPACA_POPUP_KEY`, `ALPACA_PUPUP_SECRET_KEY`, `POP_PAPER_TRADING`, `POP_MAX_POSITIONS`, `POP_TRADE_BUDGET`, `POP_ORDER_COOLDOWN`
- `db/` package: async TimescaleDB event store — 9 SQL migrations, asyncpg pool, batch DBWriter with circuit breaker, DBSubscriber (EventBus → DB bridge), DBReader (replay + analysis), FeatureStore (ML features + outcome labels), SessionManager (session lifecycle)
- `docker/` — Docker Compose for TimescaleDB 2.26 + Redpanda, tuned postgresql.conf
- `db/migrations/run.py` — idempotent migration runner with advisory lock + schema_migrations tracking
- Continuous aggregates: `bar_5m`, `bar_1h` — downsampled OHLCV views
- Compression policies (7-day) + retention policies (7–365 days per table)
- ML feature views: `ml_bar_features`, `daily_trade_summary`, `strategy_signal_rates`, `pro_strategy_performance`

### What is not yet done / known limitations

- Short selling (PARABOLIC_REVERSAL) emits `POP_SIGNAL` only — `PopExecutor` skips `side != 'buy'` entries until short-selling is added
- News/social adapters are mock only — real API adapters (Benzinga, StockTwits, etc.) not implemented
- `social_baseline_velocity` and `headline_baseline_velocity` for feature normalisation are hardcoded defaults — a 7-day rolling average from a data store would be more accurate
- T4 latency test marginal failure (P95 ~53–58 ms vs 50 ms target) — needs profiling to identify the remaining bottleneck

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

### Wiring DB persistence

```python
from db import init_db, close_db, get_writer, DBSubscriber, SessionManager

# Startup:
pool = await init_db()
writer = get_writer()
await writer.start()
subscriber = DBSubscriber(bus=monitor._bus, writer=writer)
subscriber.register()
session = SessionManager()
await session.start(mode="live", broker="alpaca", tickers=TICKERS)

# Shutdown:
await session.stop(exit_reason="clean", writer=writer)
await writer.stop()
await close_db()
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
# Offline demo (no API keys)
python demo_pop_strategies.py --verbose

# Run all integration tests
python test/run_all_tests.py

# Headless live monitor
source venv/bin/activate && python run_monitor.py

# Streamlit UI
streamlit run app.py
```

---

## Branch status

```
branch:  tradier_platform
ahead of main by: ~15 commits
uncommitted changes: yes (db/*, docker/*, README, dev_notes, model_switch_prompt,
                         requirements.txt, .gitignore)
```

Commit all before merging to main.

### Pro-setup env vars (`.env`)

```
PRO_MAX_POSITIONS=3
PRO_TRADE_BUDGET=1000
PRO_ORDER_COOLDOWN=300
```

### Pop-strategy env vars (`.env`)

```
APCA_POPUP_KEY=PKxxxxxxxxxxxxxxxx
APCA_PUPUP_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
POP_PAPER_TRADING=true
POP_MAX_POSITIONS=3
POP_TRADE_BUDGET=500
POP_ORDER_COOLDOWN=300
```

### Database env vars (`.env`)

```
DATABASE_URL=postgresql://trading:trading_secret@localhost:5432/trading_hub
DB_BATCH_MAX=200
DB_FLUSH_INTERVAL=0.5
DB_QUEUE_MAXSIZE=10000
DB_CIRCUIT_BREAKER_FAILURES=5
DB_CIRCUIT_BREAKER_RESET=30
```
