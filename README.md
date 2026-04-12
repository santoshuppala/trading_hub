# Trading Hub

A real-time algorithmic trading system built on an event-driven architecture. Supports live execution via Alpaca, intraday bar data from Tradier or Alpaca, durable event logging via Redpanda, and multiple intraday strategies with institutional-grade pre-trade risk filters.

> **Disclaimer:** This is for educational purposes only. Trading involves significant risk. Always start with paper trading. Consult a financial advisor before trading with real money.

---

## Architecture

The system is organised as a 10-layer event pipeline. Each layer subscribes to one or more `EventType`s on the shared `EventBus` and emits downstream events.

```
monitor.run() → emit_batch(BAR[])
    └─ ProSetupEngine     BAR  → PRO_STRATEGY_SIGNAL → ORDER_REQ (11 setups, 3 tiers) [T3.6 — new]
    └─ PopStrategyEngine  BAR  → POP_SIGNAL (durable) + direct fill via PopExecutor [T3.5]
    └─ StrategyEngine     BAR  → SIGNAL (buy / sell_* / partial_sell / hold)        [T4]
    └─ RiskEngine         SIGNAL → ORDER_REQ | RISK_BLOCK                           [T3]
    └─ Broker             ORDER_REQ → FILL | ORDER_FAIL
    └─ ExecutionFeedback  FILL(buy) → patches stop/target on position record
    └─ PositionManager    FILL → POSITION (opened / partial_exit / closed)
    └─ StateEngine        POSITION → read-only UI snapshot
    └─ HeartbeatEmitter   tick() → HEARTBEAT every 60 s
    └─ EventLogger        * → structured log line for every event
    └─ DurableEventLog    ORDER_REQ/FILL/POP_SIGNAL → Redpanda (ACKed before handlers run)
```

### Monitor layers

| Layer | File | Responsibility |
|-------|------|----------------|
| T1 EventBus | `monitor/event_bus.py` | Pub/sub backbone; priority queues; causal partitioning; backpressure; idempotency; SLA tracking |
| T1.5 Event Schema | `monitor/events.py` | Frozen, validated dataclasses; typed enums; PositionSnapshot; PopSignalPayload; read-only numpy arrays on BAR DataFrames |
| T2 DurableEventLog | `monitor/event_log.py` | Redpanda producer; write-then-deliver ordering via before-emit hook |
| T3 RiskEngine | `monitor/risk_engine.py` | 6 pre-trade checks; blocks on spread fetch failure; price-divergence guard |
| T3.6 ProSetupEngine | `pro_setups/engine.py` | 11 pro setups across 3 tiers; 11 detectors + classifier + router + RiskAdapter → ORDER_REQ → AlpacaBroker |
| T3.5 PopStrategyEngine | `pop_strategy_engine.py` | Pop-stock screener → classifier → router → POP_SIGNAL + direct execution via PopExecutor (dedicated Alpaca account) |
| T4 StrategyEngine | `monitor/strategy_engine.py` | VWAP Reclaim entry signals; 5 exit conditions |
| T5 PositionManager | `monitor/position_manager.py` | Opens/closes positions; computes PnL; persists `bot_state.json`; thread-safe fill handler |
| T6 StateEngine | `monitor/state_engine.py` | Maintains read-only portfolio snapshot for UI and heartbeat |
| T7 ExecutionFeedback | `monitor/execution_feedback.py` | Patches stop/target prices after a buy fill |
| T8 Observability | `monitor/observability.py` | EventLogger, HeartbeatEmitter, EODSummary |
| T9 Monitor | `monitor/monitor.py` | Thin orchestrator; data-fetch loop; Alpaca reconciliation; crash-alerting run loop |

### Event Bus (v5.2)

Key capabilities built into `event_bus.py`:

- **Priority queues** — `_BoundedPriorityQueue` (heapq); DROP_OLDEST evicts lowest-urgency items, preserving FILL/ORDER_REQ over BAR
- **Partitioned workers** — `_PartitionedAsyncDispatcher`; N workers per EventType; ticker-based routing (Kafka partition model) preserves per-ticker causal ordering across all EventType dispatchers
- **Causal ordering** — `BAR→SIGNAL→ORDER→FILL` for the same ticker always share a partition index; same-ticker events cannot race
- **Round-robin for keyless events** — no-ticker events (HEARTBEAT) distribute evenly across workers instead of pinning to a hot partition
- **Stable partitioning** — `hashlib.md5` instead of Python's `hash()` (PEP 456 randomisation)
- **Split locks** — `_sub_lock`, `_seq_lock`, `_count_lock`, `_idem_lock`; per-handler `_HandlerState._lock` — no global contention
- **`emit_batch()`** — O(4) lock acquisitions for N events vs O(4N) for N×`emit()`; used in the BAR fan-out for 100+ tickers
- **Systemic backpressure** — `_BackpressureMonitor`; 60%/80%/95% thresholds; adaptive micro-sleep; returns `BackpressureStatus` enum
- **LRU stream_seqs** — `OrderedDict` capped at `max_streams=1000`; O(1) eviction
- **Durable write-then-deliver** — `add_before_emit_hook()` + `emit(durable=True)`; Redpanda ACKs before any in-process handler runs
- **SLA tracking** — `event.deadline` field; `_deliver()` counts breaches in `BusMetrics.sla_breaches`
- **Prometheus exporter** — optional `PrometheusExporter` class; delta-based counters + gauges

Default async config:

| EventType | Queue size | Overflow | Workers | Notes |
|-----------|-----------|----------|---------|-------|
| BAR | 500 | DROP_OLDEST | 4 | 125 slots/partition; no drops on 200-ticker burst |
| SIGNAL, POP_SIGNAL | 50 | DROP_OLDEST | 2 | Latest supersedes stale |
| ORDER_REQ, FILL | 100 | BLOCK | 1 | Strict ordering; never dropped |
| HEARTBEAT | 10 | DROP_OLDEST | 1 | |

---

## Project Structure

```
trading_hub/
├── monitor/
│   ├── event_bus.py          # T1   — EventBus v5.2 (priority queues, causal partitioning, backpressure)
│   ├── events.py             # T1.5 — Frozen payload dataclasses; enums; PopSignalPayload
│   ├── event_log.py          # T2   — Redpanda durable event log + CrashRecovery
│   ├── risk_engine.py        # T3   — Pre-trade risk checks (6 filters)
│   ├── strategy_engine.py    # T4   — VWAP Reclaim signal generation
│   ├── position_manager.py   # T5   — Position lifecycle + bot_state.json persistence
│   ├── state_engine.py       # T6   — Read-only portfolio snapshot (seeded on restart)
│   ├── execution_feedback.py # T7   — Stop/target patch after buy fill
│   ├── observability.py      # T8   — EventLogger, HeartbeatEmitter, EODSummary
│   ├── monitor.py            # T9   — Orchestrator; run loop; Alpaca reconciliation
│   ├── brokers.py            # AlpacaBroker (limit+retry buy, market sell) + PaperBroker
│   ├── data_client.py        # Factory: TradierDataClient | AlpacaDataClient
│   ├── tradier_client.py     # Tradier REST API: bars, quotes; pooled HTTP adapter
│   ├── alpaca_data_client.py # Alpaca data API: bars, quotes, screener
│   ├── screener.py           # MomentumScreener — refreshes watchlist every 30 min
│   ├── signals.py            # VWAP Reclaim signal math; LRU indicator cache (500 entries)
│   ├── state.py              # load_state() / save_state() — atomic bot_state.json I/O
│   ├── alerts.py             # Email alerts via Yahoo SMTP
│   └── orders.py             # (legacy) direct order helpers
│
├── pop_screener/             # Multi-strategy pop-stock subsystem (NEW)
│   ├── config.py             # All 60+ configurable thresholds (one place)
│   ├── models.py             # Data models: NewsData, SocialData, EngineeredFeatures, etc.
│   ├── ingestion.py          # Pluggable adapters: news / social / market / momentum
│   ├── features.py           # FeatureEngineer: VWAP, ATR, RSI, trend cleanliness, sentiment
│   ├── screener.py           # PopScreener: 6 rule-based pop detectors
│   ├── classifier.py         # StrategyClassifier: deterministic pop → strategy mapping
│   ├── strategy_router.py    # StrategyRouter: primary → secondary fallback engine dispatch
│   └── strategies/
│       ├── vwap_reclaim_engine.py      # Dip-and-reclaim pattern with RVOL/RSI filters
│       ├── orb_engine.py               # Opening Range Breakout with volume confirmation
│       ├── halt_resume_engine.py       # Halt-like spike + consolidation + breakout
│       ├── parabolic_reversal_engine.py# Exhaustion candle → short reversal
│       ├── ema_trend_engine.py         # Pullback-to-EMA in a confirmed uptrend
│       └── bopb_engine.py              # Breakout → pullback → confirmation entry
│
├── pro_setups/               # 11-setup pro strategy subsystem (NEW)
│   ├── engine.py             # T3.6 — ProSetupEngine: BAR → 11 detectors → PRO_STRATEGY_SIGNAL
│   ├── detectors/            # 11 detectors: Trend, VWAP, SR, ORB, InsideBar, Gap, Flag,
│   │                         #               Liquidity, Volatility(BB), Fib, Momentum
│   ├── classifiers/          # StrategyClassifier: detector outputs → (strategy, tier)
│   ├── strategies/           # 11 strategy modules in tier1/ tier2/ tier3/
│   │   ├── tier1/            # trend_pullback, vwap_reclaim, sr_flip
│   │   ├── tier2/            # orb, inside_bar, gap_and_go, flag_pennant
│   │   └── tier3/            # liquidity_sweep, bollinger_squeeze, fib_confluence, momentum_ignition
│   ├── router/               # ProStrategyRouter: PRO_STRATEGY_SIGNAL → RiskAdapter
│   └── risk/                 # RiskAdapter: tier-aware risk gate → ORDER_REQ
│
├── pop_strategy_engine.py    # T3.5 — BAR subscriber; runs pop pipeline; emits POP_SIGNAL + SIGNAL
├── demo_pop_strategies.py    # Offline demo: full pipeline with synthetic data
│
├── strategies/               # Backtrader strategy definitions
│   ├── vwap_reclaim.py
│   ├── ema_rsi.py
│   ├── trend_atr.py
│   ├── momentum_breakout.py
│   └── mean_reversion.py
│
├── test/                     # Integration + unit test suite (10 tests)
│   ├── test_1_synthetic_feed.py
│   ├── test_2_tradier_sandbox.py
│   ├── test_3_redpanda_consistency.py
│   ├── test_4_market_open_latency.py
│   ├── test_5_network_warmup.py
│   ├── test_6_pipeline_integration.py
│   ├── test_7_risk_boundaries.py
│   ├── test_8_signal_edge_cases.py
│   ├── test_9_eventbus_advanced.py
│   ├── test_10_state_persistence.py
│   └── run_all_tests.py
│
├── app.py                    # Streamlit UI — live monitor + backtest tabs
├── config.py                 # Shared settings: tickers, strategy params, credentials
├── main.py                   # Backtrader backtesting engine
├── run_monitor.py            # Headless launcher (no UI)
├── vwap_utils.py             # VWAP computation utilities
├── start_monitor.sh          # Shell launcher for cron
├── bot_state.json            # Intraday state — positions, reclaimed tickers, trade log
├── docs/dev_notes.md         # Full architectural decision log + bug fix history
├── logs/                     # Daily log files: monitor_YYYY-MM-DD.log
├── .env                      # API keys and SMTP credentials (never commit)
└── requirements.txt
```

---

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Python 3.10+ required.

---

## Configuration

### `.env` file

Create `.env` in the project root. No quotes, no spaces around `=`:

```
# Alpaca (order execution + optional data)
APCA_API_KEY_ID=PKxxxxxxxxxxxxxxxx
APCA_API_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Tradier (bar data — recommended for 1-min bars)
TRADIER_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx

# Broker: 'alpaca' (live/paper) | 'paper' (local simulation, no API needed)
BROKER=alpaca

# Data source: 'tradier' | 'alpaca'
DATA_SOURCE=tradier

# Email alerts (Yahoo Mail app password — 16 chars, no spaces)
ALERT_EMAIL_USER=you@yahoo.com
ALERT_EMAIL_PASS=your16charapppassword
ALERT_EMAIL_FROM=you@yahoo.com
ALERT_EMAIL_TO=you@yahoo.com

# Pro-setups subsystem (uses main Alpaca account, same as VWAP strategy)
PRO_MAX_POSITIONS=3
PRO_TRADE_BUDGET=1000
PRO_ORDER_COOLDOWN=300

# Pop-strategy dedicated Alpaca account (separate from main VWAP account)
APCA_POPUP_KEY=PKxxxxxxxxxxxxxxxx
APCA_PUPUP_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
POP_PAPER_TRADING=true          # set false for live pop execution
POP_MAX_POSITIONS=3
POP_TRADE_BUDGET=500
POP_ORDER_COOLDOWN=300

# Redpanda / Kafka (optional — for durable event log)
REDPANDA_BROKERS=127.0.0.1:9092
```

### `config.py`

All tunable strategy values live in `config.py` — tickers, per-ticker params, max positions, trade budget, order cooldown, etc. Both the UI and headless launcher read from it.

### `pop_screener/config.py`

All 60+ pop-screener thresholds (RVOL, gap size, sentiment delta, ATR multipliers, etc.) are isolated here. Change one value and every downstream rule picks it up automatically. No magic numbers elsewhere.

---

## Running

### Streamlit UI

```bash
streamlit run app.py
```

Opens two tabs:
- **Live Monitor** — start/stop the monitor, open positions, today's trades, live log with auto-refresh
- **Backtest** — single-ticker or year-by-year compounding backtests

### Headless

```bash
source venv/bin/activate
python run_monitor.py
# or
bash start_monitor.sh
```

Logs to `logs/monitor_YYYY-MM-DD.log`. Stops automatically at 3:15 PM ET with an EOD summary.

### Pop-strategy demo (offline, no API keys)

```bash
python demo_pop_strategies.py
python demo_pop_strategies.py --verbose          # shows full feature vectors
python demo_pop_strategies.py --symbols AAPL NVDA TSLA
```

### Cron (auto-start daily)

```
0 6 * * 1-5 /path/to/trading_hub/start_monitor.sh >> /path/to/logs/cron.log 2>&1
```

Runs at 6:00 AM PST (9:00 AM ET) on weekdays.

### Single-instance enforcement

Only one monitor can run at a time. A second start attempt is refused with the PID of the running process. Lock file is `.monitor.lock`; cleaned up automatically on stop.

---

## Strategies

### Strategy 1: VWAP Reclaim (existing, T4)

#### Entry — all 9 conditions must hold simultaneously

| # | Filter | Detail |
|---|--------|--------|
| 1 | **2-bar VWAP reclaim** | Dipped below VWAP → closed above VWAP for 2 consecutive bars |
| 2 | **Opened above VWAP** | Day bias is bullish |
| 3 | **RSI 50–70** | Momentum without being overbought |
| 4 | **RVOL ≥ 2×** | Twice the usual volume at this time of day |
| 5 | **SPY above VWAP** | Market tailwind |
| 6 | **Bid/ask spread ≤ 0.2%** | Spread not wider than target profit margin |
| 7 | **Not already traded today** | No re-entry on same ticker intraday |
| 8 | **Trading hours 9:45–3:00 PM ET** | Avoid noisy open and illiquid close |
| 9 | **Max positions not exceeded** | Configurable; default 5 |

#### Exit conditions (first triggered wins)

| Condition | Description |
|-----------|-------------|
| **Trailing stop** | Price drops 1×ATR below entry |
| **Full target** | Price reaches 2×ATR above entry |
| **Partial exit** | Sell half at 1×ATR profit; trail remainder |
| **RSI overbought** | RSI crosses above 75 |
| **VWAP breakdown** | Price closes below VWAP |
| **EOD force-close** | All positions closed at 3:00 PM ET |

---

### Strategy 2: Pop-Stock Multi-Strategy Architecture (new, T3.5)

A parallel strategy layer that screens for "pop" events and routes each candidate to the best-fit strategy engine. All rules are deterministic — same inputs always produce the same output.

#### Pop detection (6 rule-sets, priority-ordered)

| Rule | Key conditions | Strategy assigned |
|------|---------------|-------------------|
| **High-Impact News** | `sentiment_delta > 0.40`, `headline_velocity > 8×`, `\|gap\| ≥ 4%` | ORB; +HALT/PARABOLIC if extreme |
| **Earnings** | Large gap + earnings flag proxy | VWAP_RECLAIM (gap < 8%) or ORB (gap ≥ 8%) |
| **Low Float** | `float < 20M shares`, `RVOL > 4×`, `\|gap\| > 5%` | HALT_RESUME or PARABOLIC; VWAP explicitly blocked |
| **Moderate News** | `sentiment_delta > 0.20`, `headline_velocity 1.5–8×`, `\|gap\| < 4%` | VWAP_RECLAIM or EMA_TREND |
| **Sentiment Pop** | `social_velocity > 3×`, `bullish_skew > 20%` | VWAP_RECLAIM or EMA_TREND + BOPB |
| **Unusual Volume** | `RVOL > 3×`, `price_momentum > 2%` | VWAP_RECLAIM or EMA_TREND or BOPB |

#### Strategy engines

| Engine | Signal type | Entry trigger | Key exit |
|--------|-------------|---------------|----------|
| **VWAP Reclaim** | Long | 2-bar dip+reclaim, RVOL ≥ 1.5×, RSI 50–70 | VWAP breakdown × 2 bars |
| **ORB** | Long | Breakout above 15-min OR high, vol ≥ 1.5× OR avg | Price re-enters OR range on low vol |
| **Halt Resume** | Long | Halt-like bar → consolidation → breakout | 7% reversal from session high |
| **Parabolic Reversal** | Short | 50%+ intraday move + exhaustion candle wick | Price reclaims exhaustion high |
| **EMA Trend** | Long | Pullback to EMA9, confirmation candle above EMA20 | 2 bars below EMA20 |
| **BOPB** | Long | Prior-high breakout → pullback test → confirmation | 2 bars back below prior high |

#### Pipeline flow

```
BAR event
  └─ PopStrategyEngine (T3.5)
       ├─ ingestion:   get_news() + get_social() + MarketDataSlice from BarPayload
       ├─ features:    EngineeredFeatures (ATR, RSI, VWAP distance, trend cleanliness…)
       ├─ screener:    PopCandidate (first matching rule wins)
       ├─ classifier:  StrategyAssignment (primary + secondaries + confidence)
       ├─ router:      EntrySignal + ExitSignal via best-fit engine
       ├─ emit:        POP_SIGNAL (durable → Redpanda)
       └─ execute:     PopExecutor → dedicated Alpaca account → FILL (durable) → PositionManager
                       (short/PARABOLIC_REVERSAL: POP_SIGNAL only — execution deferred)
```

#### Activating the pop strategy engine

`PopStrategyEngine` is wired in `run_monitor.py` automatically after `RealTimeMonitor` is constructed:

```python
from pop_strategy_engine import PopStrategyEngine
pop_engine = PopStrategyEngine(
    bus=monitor._bus,
    pop_alpaca_key=ALPACA_POPUP_KEY,       # APCA_POPUP_KEY env var
    pop_alpaca_secret=ALPACA_PUPUP_SECRET_KEY,  # APCA_PUPUP_SECRET_KEY env var
    pop_paper=POP_PAPER_TRADING,           # default True
    pop_max_positions=POP_MAX_POSITIONS,   # default 3
    pop_trade_budget=float(POP_TRADE_BUDGET),   # default $500
    pop_order_cooldown=POP_ORDER_COOLDOWN, # default 300 s
    alert_email=ALERT_EMAIL,
)
```

The `PopExecutor` inside uses its own `TradingClient` (separate Alpaca sub-account), submits orders directly, and emits `FILL` events on the shared bus so `PositionManager` tracks pop positions normally. The main `AlpacaBroker` never sees pop orders — no double-execution risk.

If `APCA_POPUP_KEY` / `APCA_PUPUP_SECRET_KEY` are absent or `POP_PAPER_TRADING=true`, `PopExecutor` runs in paper mode (simulated fills at signal price, no real orders).

#### Swapping mock data sources for real APIs

Each source adapter in `pop_screener/ingestion.py` is a plain class with one public method. To swap:

1. Implement the same method signature (e.g. `get_news(symbol, window_hours) → list[NewsData]`)
2. Inject into `PopStrategyEngine` via the constructor
3. No other code changes required

---

---

### Strategy 3: Pro-Setup Multi-Tier System (new, T3.6)

11 deterministic setups across 3 tiers, each with tier-specific stop/exit rules. Runs additively alongside existing strategies — no shared state or interference.

#### Tier rules

| Tier | Win-rate profile | SL | Partial exit | Full exit | Trail |
|------|-----------------|-----|-------------|-----------|-------|
| **Tier 1** | High (>55%) | 0.3–0.4 ATR | 1R | 2R | Higher lows (long) |
| **Tier 2** | Moderate (45–55%) | 0.8–1.0 ATR | 1.5R | 3R | EMA20 or VWAP |
| **Tier 3** | Low (<45%) | 1.5–2.0 ATR | 3R | 6–8R | Structure (swing) |

#### Setups

| Setup | Tier | Detector(s) | Entry trigger | Direction |
|-------|------|-------------|---------------|-----------|
| **Trend Pullback** | 1 | TrendDetector + VWAPDetector | EMA alignment + pull to EMA9/20 + bullish bar | Long |
| **VWAP Reclaim** | 1 | VWAPDetector | Dip below → close above VWAP + EMA20 positive slope | Long |
| **S/R Flip** | 1 | SRDetector | Former resistance acting as support (or vice versa) | Long/Short |
| **ORB** | 2 | ORBDetector | Close above 15-min OR high + 1.5× volume | Long |
| **Inside Bar** | 2 | InsideBarDetector | Break of mother bar boundary with trend context | Long/Short |
| **Gap and Go** | 2 | GapDetector + VWAPDetector | Gap ≥ 0.5%, unfilled, close near session extreme | Long/Short |
| **Flag/Pennant** | 2 | FlagDetector | Pole + tight consolidation + channel breakout | Long/Short |
| **Liquidity Sweep** | 3 | LiquidityDetector | Stop-hunt pierce of swing + reversal candle | Long/Short |
| **Bollinger Squeeze** | 3 | VolatilityDetector | BB bandwidth squeeze → directional breakout + volume | Long/Short |
| **Fib Confluence** | 3 | FibDetector + (SR/VWAP/Trend) | 38.2% or 61.8% Fib + ≥1 confirming factor | Long/Short |
| **Momentum Ignition** | 3 | MomentumDetector | 3× volume + 1.5 ATR 3-bar expansion + directional close | Long/Short |

#### Pipeline flow

```
BAR event
  └─ ProSetupEngine (T3.6)
       ├─ 11 detectors run in sequence (each returns DetectorSignal)
       ├─ StrategyClassifier → (strategy_name, tier, direction, confidence)
       ├─ strategy.detect_signal() → final confirmation
       ├─ strategy.generate_entry/stop/exit() → price levels
       ├─ emit PRO_STRATEGY_SIGNAL (non-durable, for routing)
       └─ ProStrategyRouter → RiskAdapter.validate_and_emit()
            ├─ checks: max_positions, cooldown, duplicate, R:R, ATR
            ├─ sizes position: 2% budget risk per trade
            └─ emit ORDER_REQ (durable) → AlpacaBroker → FILL → PositionManager
```

#### Detailed entry/exit logging

Every signal logs:
```
[ProSetupEngine][NVDA] SIGNAL  strategy=momentum_ignition  tier=3  dir=long
  entry=487.3200  stop=483.6400  t1=498.9200  t2=516.8000
  R:R=8.00  ATR=1.9200  RVOL=4.23  RSI=68.5  conf=82%
  detectors_fired=['momentum', 'trend']  elapsed=3.2ms

[RiskAdapter][NVDA][momentum_ignition][T3] ENTRY ▶ dir=long  entry=487.3200
  stop=483.6400  target1=498.9200  target2=516.8000
  qty=4  R:R=8.00  risk_$=14.72  conf=82%  ATR=1.9200
```

#### Activating

```python
from pro_setups.engine import ProSetupEngine
pro_engine = ProSetupEngine(
    bus            = monitor._bus,
    max_positions  = PRO_MAX_POSITIONS,     # default 3
    order_cooldown = PRO_ORDER_COOLDOWN,    # default 300 s
    trade_budget   = float(PRO_TRADE_BUDGET),  # default $1000
)
```

Already wired in `run_monitor.py`. Configure via `.env`:

```
PRO_MAX_POSITIONS=3
PRO_TRADE_BUDGET=1000
PRO_ORDER_COOLDOWN=300
```

---

### Order execution

- **Buys**: marketable limit order at ask price; polls for fill every 250 ms up to 2 s; cancel-and-retry with fresh ask up to 3 times; **0.5% max slippage cap** — abandons retry if ask drifts > 0.5%
- **Sells**: market order for guaranteed exit speed
- **Slippage model**: 0.01% applied to entry price; Alpaca is commission-free

---

## Stock Scanning

**Base watchlist**: ~165 liquid stocks across mega-cap tech, semiconductors, software, fintech, financials, energy, healthcare, consumer, and sector ETFs — defined in `config.py`.

**Dynamic momentum additions**: refreshed every 30 minutes by `MomentumScreener`:
- Top 50 most active stocks by volume
- Top 20 gainers

Filtered by **Relative Strength**: only stocks outperforming SPY over the last 5 trading days are added.

**Data fetching**: all tickers fetched in a single batch API call per cycle; fan-out via `emit_batch()` (O(4) lock acquisitions regardless of ticker count).

---

## State Persistence & Crash Recovery

`bot_state.json` is written atomically (tmp → replace) after every position change:

```json
{
  "date": "2026-04-11",
  "positions": {
    "AAPL": {"entry_price": 175.0, "qty": 5, "stop": 172.0, "target": 178.5}
  },
  "reclaimed_today": ["ZS"],
  "trade_log": [
    {"ticker": "ZS", "entry_price": 116.9, "exit_price": 116.64, "qty": 1, "pnl": -0.26}
  ]
}
```

On restart, `load_state()` validates the date and restores positions. Then `RealTimeMonitor.__init__` runs **Alpaca reconciliation**: any position Alpaca holds but `bot_state.json` doesn't know about is imported automatically. `CrashRecovery` in `event_log.py` replays Redpanda events to rebuild exact stop/target/ATR values (no more ±3%/5% fallback approximations since stop_price/target_price/atr_value now flow through the full OrderRequestPayload → FillPayload chain).

---

## Logging

Both run modes write to `logs/monitor_YYYY-MM-DD.log`:

| Event | Level | What's logged |
|-------|-------|---------------|
| Monitor start/stop | INFO | Strategy, tickers, broker, data source, PID |
| Heartbeat (60 s) | INFO | Tickers, open positions, trades, win rate, running PnL |
| POP_SIGNAL | INFO | Symbol, strategy_type, entry, stop, targets, pop_reason, confidence |
| SIGNAL | INFO | Ticker, action, price, RSI, RVOL |
| ORDER_REQ | INFO | Side, qty, ticker, price, reason |
| FILL | INFO | Side, qty, ticker, fill price, order ID |
| ORDER_FAIL | WARNING | Side, qty, ticker, reason |
| POSITION | INFO | Ticker, action, PnL (on close) |
| RISK_BLOCK | INFO | Ticker, signal blocked, reason |
| Alpaca reconciliation | WARNING | Any orphaned positions imported on startup |

---

## EOD Summary

Logged (and optionally emailed) when the monitor stops:

- Total trades, wins/losses, win rate
- Per-trade breakdown: entry/exit price, time, quantity, PnL, exit reason
- Total PnL

---

## Test Suite

Ten integration tests live in `test/`. Run all:

```bash
python test/run_all_tests.py
```

| Test | Coverage |
|------|----------|
| `test_1` | Synthetic bar feed correctness |
| `test_2` | Tradier sandbox connectivity + bar fetch |
| `test_3` | Redpanda event ordering consistency |
| `test_4` | Market-open latency budget |
| `test_5` | Network warmup + connection pool readiness |
| `test_6` | Pipeline integration (feed → signal → order → fill) |
| `test_7` | Risk engine boundary conditions |
| `test_8` | Signal edge cases (RSI extremes, flat VWAP, zero volume) |
| `test_9` | EventBus advanced (backpressure, coalescing, ordering) |
| `test_10` | State persistence + crash recovery round-trip |

---

## Backtesting

Powered by Backtrader. Strategies in `strategies/`:

| Strategy | Description |
|----------|-------------|
| VWAP Reclaim | Dip-and-reclaim with RSI/RVOL filters |
| EMA + RSI Crossover | Fast/slow EMA crossover with RSI filter |
| Trend Following ATR | EMA crossover with ATR-based trailing stop |
| Momentum Breakout | N-bar high breakout with ATR stop |
| Mean Reversion | Bollinger Band lower touch + RSI oversold |

Supports single-ticker backtests and year-by-year compounding. Run via the Streamlit UI or directly via `main.py`.

---

## Requirements

- Python 3.10+
- Alpaca account — free paper trading at [alpaca.markets](https://alpaca.markets)
- Tradier account — free developer sandbox at [tradier.com](https://tradier.com) (recommended for bar data)
- Yahoo Mail app password for email alerts (optional)
- Redpanda or Kafka broker for durable event log (optional — bot runs without it)
- macOS/Linux for cron scheduling
