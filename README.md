# Trading Hub

A real-time algorithmic trading system built on an event-driven architecture. Supports live execution via Alpaca, intraday bar data from Tradier or Alpaca, durable event logging via Redpanda, and a VWAP Reclaim momentum strategy with institutional-grade pre-trade risk filters.

> **Disclaimer:** This is for educational purposes only. Trading involves significant risk. Always start with paper trading. Consult a financial advisor before trading with real money.

---

## Architecture

The system is organized as a 9-layer event pipeline. Each layer subscribes to one or more `EventType`s on the shared `EventBus` and emits downstream events.

```
monitor.run() → emit_batch(BAR[])
    └─ StrategyEngine   BAR  → SIGNAL (buy / sell_* / partial_sell / hold)
    └─ RiskEngine        SIGNAL → ORDER_REQ | RISK_BLOCK
    └─ Broker            ORDER_REQ → FILL | ORDER_FAIL
    └─ ExecutionFeedback FILL(buy) → patches stop/target on position record
    └─ PositionManager   FILL → POSITION (opened / partial_exit / closed)
    └─ StateEngine       POSITION → read-only UI snapshot
    └─ HeartbeatEmitter  tick() → HEARTBEAT every 60 s
    └─ EventLogger       * → structured log line for every event
    └─ DurableEventLog   ORDER_REQ/FILL → Redpanda (ACKed before handlers run)
```

### Monitor layers

| Layer | File | Responsibility |
|-------|------|----------------|
| T1 EventBus | `event_bus.py` | Pub/sub backbone; priority queues; backpressure; idempotency; SLA tracking |
| T1.5 Event Schema | `events.py` | Frozen, validated dataclasses; typed enums; PositionSnapshot; read-only numpy arrays on BAR DataFrames |
| T2 DurableEventLog | `event_log.py` | Redpanda producer; write-then-deliver ordering via before-emit hook |
| T3 RiskEngine | `risk_engine.py` | 6 pre-trade checks; blocks on spread fetch failure; price-divergence guard |
| T4 StrategyEngine | `strategy_engine.py` | VWAP Reclaim entry signals; 5 exit conditions |
| T5 PositionManager | `position_manager.py` | Opens/closes positions; computes PnL; persists `bot_state.json`; thread-safe fill handler |
| T6 StateEngine | `state_engine.py` | Maintains read-only portfolio snapshot for UI and heartbeat |
| T7 ExecutionFeedback | `execution_feedback.py` | Patches stop/target prices after a buy fill |
| T8 Observability | `observability.py` | EventLogger, HeartbeatEmitter, EODSummary |
| T9 Monitor | `monitor.py` | Thin orchestrator; data-fetch loop; Alpaca reconciliation; crash-alerting run loop |

### Event Bus (v5.2)

Key capabilities built into `event_bus.py`:

- **Priority queues** — `_BoundedPriorityQueue` (heapq); DROP_OLDEST evicts lowest-urgency items, preserving FILL/ORDER_REQ over BAR
- **Partitioned workers** — `_PartitionedAsyncDispatcher`; N workers per EventType; ticker-based routing (Kafka partition model) preserves per-ticker causal ordering across all EventType dispatchers
- **Causal vs. max-throughput mode** — `causal_partitioning=True` (default) keys on `ticker` only so `BAR→SIGNAL→ORDER→FILL` for the same ticker always share a partition index; `False` keys on `EventType:ticker` for maximum parallelism
- **Round-robin for keyless events** — no-ticker events (HEARTBEAT, SYSTEM) distribute evenly across workers instead of piling on a fixed hot partition
- **Stable partitioning** — `hashlib.md5` instead of Python's `hash()`, which is process-randomized (PEP 456)
- **Split locks** — `_sub_lock`, `_seq_lock`, `_count_lock`, `_idem_lock`; per-handler `_HandlerState._lock` — no global contention
- **`emit_batch()`** — O(4) lock acquisitions for N events vs O(4N) for N×`emit()`; used in the BAR fan-out for 100+ tickers
- **Systemic backpressure** — `_BackpressureMonitor`; 60%/80%/95% thresholds; adaptive micro-sleep; returns `BackpressureStatus` enum to callers
- **LRU stream_seqs** — `OrderedDict` capped at `max_streams=1000`; O(1) eviction
- **Durable write-then-deliver** — `add_before_emit_hook()` + `emit(durable=True)`; Redpanda ACKs before any in-process handler runs
- **SLA tracking** — `event.deadline` field; `_deliver()` counts breaches in `BusMetrics.sla_breaches`
- **Prometheus exporter** — optional `PrometheusExporter` class; delta-based counters + gauges

Default async config:

| EventType | Queue size | Overflow | Workers |
|-----------|-----------|----------|---------|
| BAR | 500 | DROP_OLDEST | 4 |
| ORDER_REQ, FILL | 100 | BLOCK | 1 (strict ordering) |
| HEARTBEAT | 10 | DROP_OLDEST | 1 |

---

## Project Structure

```
trading_hub/
├── monitor/
│   ├── event_bus.py          # T1  — EventBus v5.2 (priority queues, causal partitioning, backpressure)
│   ├── events.py             # T1.5 — Frozen payload dataclasses; Enums; PositionSnapshot
│   ├── event_log.py          # T2  — Redpanda durable event log
│   ├── risk_engine.py        # T3  — Pre-trade risk checks (6 filters)
│   ├── strategy_engine.py    # T4  — VWAP Reclaim signal generation
│   ├── position_manager.py   # T5  — Position lifecycle + bot_state.json persistence
│   ├── state_engine.py       # T6  — Read-only portfolio snapshot (seeded on restart)
│   ├── execution_feedback.py # T7  — Stop/target patch after buy fill
│   ├── observability.py      # T8  — EventLogger, HeartbeatEmitter, EODSummary
│   ├── monitor.py            # T9  — Orchestrator; run loop; Alpaca reconciliation
│   ├── brokers.py            # AlpacaBroker (limit+retry buy, market sell) + PaperBroker
│   ├── data_client.py        # Factory: TradierDataClient | AlpacaDataClient
│   ├── tradier_client.py     # Tradier REST API: bars, quotes
│   ├── alpaca_data_client.py # Alpaca data API: bars, quotes, screener
│   ├── screener.py           # MomentumScreener — refreshes watchlist every 30 min
│   ├── signals.py            # VWAP Reclaim signal math (used by StrategyEngine)
│   ├── state.py              # load_state() / save_state() — atomic bot_state.json I/O
│   ├── alerts.py             # Email alerts via Yahoo SMTP
│   └── orders.py             # (legacy) direct order helpers
├── app.py                    # Streamlit UI — live monitor + backtest tabs
├── config.py                 # Shared settings: tickers, strategy params, credentials
├── main.py                   # Backtrader backtesting engine
├── run_monitor.py            # Headless launcher (no UI)
├── vwap_utils.py             # VWAP computation utilities
├── start_monitor.sh          # Shell launcher for cron
├── bot_state.json            # Intraday state — positions, reclaimed tickers, trade log
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

Python 3.10+ required (uses `match` statement).

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

# Redpanda / Kafka (optional — for durable event log)
REDPANDA_BROKERS=127.0.0.1:9092
```

### `config.py`

All tunable strategy values live in `config.py` — tickers, per-ticker params, max positions, trade budget, order cooldown, etc. Both the UI and headless launcher read from it.

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

### Cron (auto-start daily)

```
0 6 * * 1-5 /path/to/trading_hub/start_monitor.sh >> /path/to/logs/cron.log 2>&1
```

Runs at 6:00 AM PST (9:00 AM ET) on weekdays.

### Single-instance enforcement

Only one monitor can run at a time. A second start attempt (from UI or CLI) is refused with the PID of the running process. The lock file is `.monitor.lock` in the project root; cleaned up automatically on stop.

---

## Trading Strategy: VWAP Reclaim

### Entry — all 9 conditions must hold simultaneously

| # | Filter | Detail |
|---|--------|--------|
| 1 | **2-bar VWAP reclaim** | Was above VWAP → dipped below → closed above VWAP for 2 consecutive bars |
| 2 | **Opened above VWAP** | Day bias is bullish |
| 3 | **RSI 50–70** | Momentum without being overbought |
| 4 | **RVOL ≥ 2×** | Twice the usual volume at this time of day |
| 5 | **SPY above VWAP** | Market tailwind |
| 6 | **Bid/ask spread ≤ 0.2%** | Spread not wider than target profit margin |
| 7 | **Not already traded today** | No re-entry on same ticker intraday |
| 8 | **Trading hours 9:45–3:00 PM ET** | Avoid noisy open and illiquid close |
| 9 | **Max positions not exceeded** | Configurable; default 5 |

### Exit conditions (first triggered wins)

| Condition | Description |
|-----------|-------------|
| **Trailing stop** | Price drops 1×ATR below entry |
| **Full target** | Price reaches 2×ATR above entry |
| **Partial exit** | Sell half at 1×ATR profit; trail remainder |
| **RSI overbought** | RSI crosses above 75 |
| **VWAP breakdown** | Price closes below VWAP |
| **EOD force-close** | All positions closed at 3:00 PM ET |

### Order execution

- **Buys**: marketable limit order at ask price; polls for fill every 250 ms up to 2 s; cancel-and-retry with fresh ask (via injected `quote_fn`) up to 3 times before abandoning; **0.5% max slippage cap** — abandons retry if ask drifts > 0.5% from original signal price
- **Sells**: market order for guaranteed exit speed
- **Slippage model**: 0.01% applied to entry price for accounting; Alpaca is commission-free

---

## Stock Scanning

**Base watchlist**: ~165 liquid stocks across mega-cap tech, semiconductors, software, fintech, financials, energy, healthcare, consumer, and sector ETFs — defined in `config.py`.

**Dynamic momentum additions**: refreshed every 30 minutes by `MomentumScreener`:
- Top 50 most active stocks by volume
- Top 20 gainers

Filtered by **Relative Strength**: only stocks outperforming SPY over the last 5 trading days are added.

**Data fetching**: all tickers fetched in a single batch API call per cycle; fan-out via `emit_batch()` (O(4) lock acquisitions regardless of ticker count).

---

## State Persistence

`bot_state.json` is written atomically (tmp → replace) after every position change:

```json
{
  "date": "2026-04-10",
  "positions": {
    "AAPL": {"entry_price": 175.0, "qty": 5, "stop": 172.0, "target": 178.5, ...}
  },
  "reclaimed_today": ["ZS"],
  "trade_log": [
    {"ticker": "ZS", "entry_price": 116.9, "exit_price": 116.64, "qty": 1, "pnl": -0.26, ...}
  ]
}
```

On restart, `load_state()` validates the date and restores positions and trade history. Then `RealTimeMonitor.__init__` runs **Alpaca reconciliation**: any position that Alpaca holds but `bot_state.json` doesn't know about is imported automatically, preventing orphaned positions from being ignored.

---

## Logging

Both run modes write to `logs/monitor_YYYY-MM-DD.log`:

| Event | Level | What's logged |
|-------|-------|---------------|
| Monitor start/stop | INFO | Strategy, tickers, broker, data source, PID |
| Heartbeat (60 s) | INFO | Tickers, open positions, trades, win rate, running PnL |
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

## Backtesting

Powered by Backtrader. Strategies in `strategies/`:

| Strategy | Description |
|----------|-------------|
| EMA + RSI Crossover | Fast/slow EMA crossover with RSI filter |
| Trend Following ATR | EMA crossover with ATR-based trailing stop |
| Momentum Breakout | N-bar high breakout with ATR stop |
| Mean Reversion | Bollinger Band lower touch + RSI oversold |

Supports single-ticker backtests and year-by-year compounding (reinvest returns each year). Run via the Streamlit UI or directly via `main.py`.

---

## Test Suite

Run the full 10-test validation suite:

```bash
python test/run_all_tests.py
# Report written to: test/logs/test_report.md
```

| Test | Coverage | Status |
|------|----------|--------|
| T1 Synthetic Feed | 200 tickers × 100 bars via ASYNC EventBus at 10ms intervals | ✅ |
| T2 Tradier Sandbox | Auth + quotes + history + order submission via sandbox.tradier.com | ✅ |
| T3 Redpanda Consistency | Serialisation round-trip + (optional) live Redpanda replay ordering | ✅ |
| T4 Market Open Latency | 200 BAR burst; P95 < 50ms; 0 drops | ⚠️ P95 ≈ 55ms on dev Mac |
| T5 Network Warm-up | Parallel RVOL fetch 200 tickers in < 60s | ✅ |
| T6 Full Pipeline | BAR→SIGNAL→ORDER_REQ→FILL→POSITION end-to-end; max-positions gate; EOD close | ✅ |
| T7 Risk Boundaries | All 6 pre-trade checks at exact boundaries; partial-sell qty=1; concurrent fills | ✅ |
| T8 Signal Edge Cases | NaN/zero-volatility/insufficient bars; RVOL time gate; SignalPayload invariants; 1000-frame stress | ✅ |
| T9 EventBus Advanced | Circuit-breaker; DLQ; TTL expiry; priority ordering; coalescing; retry; dedup; stream_seq | ✅ |
| T10 State Persistence | Corrupt JSON backup; concurrent saves; StateEngine; HeartbeatEmitter | ✅ |

> T4 passes on production Linux servers; the P95 slightly exceeds 50ms on a dev MacBook due to Python GIL + OS scheduling variance.

---

## Requirements

- Python 3.10+
- Alpaca account — free paper trading at [alpaca.markets](https://alpaca.markets)
- Tradier account — free developer sandbox at [tradier.com](https://tradier.com) (recommended for bar data)
- Yahoo Mail app password for email alerts (optional)
- Redpanda or Kafka broker for durable event log (optional — bot runs without it)
- macOS/Linux for cron scheduling
