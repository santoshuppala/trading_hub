# 05 Pop Engine Design

Source of truth: `pop_strategy_engine.py`, `pop_screener/` package, `scripts/run_pop.py`

---

## 1. Overview

The Pop engine is a standalone strategy layer (T3.5) that runs in its own OS process. It subscribes to BAR events from shared cache, screens for momentum/catalyst-driven stocks using news and social data, classifies candidates into one of 7+1 strategy types, generates entry signals, and routes orders to the Core SmartRouter via Redpanda IPC.

Pop trades execute on a **dedicated Alpaca account** (`APCA_POPUP_KEY` / `APCA_PUPUP_SECRET_KEY`), fully isolated from the main strategy's capital and position count.

### Position in the 10-layer pipeline

```
T3    BAR events  ->  StrategyEngine (existing Core, unchanged)
T3.5  BAR events  ->  PopStrategyEngine  <-- THIS MODULE
       |-- ingestion + features + screener + classifier + router
            |-- emit POP_SIGNAL (durable, for audit/Redpanda)
            |-- PopExecutor: ORDER_REQ -> IPC Redpanda -> Core SmartRouter
```

---

## 2. Pipeline Flow

```
BAR (from shared cache)
  |
  v
Early Exit Gate  (~90% of tickers rejected here)
  |  bar_range_pct < 0.3%  AND  last_rvol < 1.5 (or ETF_RVOL_MIN)  AND  |gap| < 2%
  |  -> skip: no news/social API calls
  |
  v
Fetch News + Social  (Benzinga, StockTwits)
  |
  v
FeatureEngineer.compute()  ->  EngineeredFeatures  (16 features, stateless)
  |
  v
PopScreener.screen()  ->  PopCandidate | None  (6 rules, first-match-wins)
  |
  v
StrategyClassifier.classify()  ->  StrategyAssignment  (deterministic mapping)
  |
  v
StrategyRouter.route()  ->  List[EntrySignal]  (7+1 engines, primary-first fallback)
  |
  v
PopExecutor.execute_entry()
  |-- Emit ORDER_REQ on local bus (durable)
  |-- Forwarded to Core via IPC (TOPIC_ORDERS)
  |-- Core SmartRouter handles bracket orders + exit monitoring
```

### EOD Gate

No new entries after 15:45 ET. The pop process shuts down at 16:00 ET.

---

## 3. Process Architecture

**Entry point:** `scripts/run_pop.py`

Pop runs as a child process under the supervisor. It:

1. Creates its own `EventBus` instance.
2. Initializes `CacheReader` to consume bars from the shared memory cache.
3. Loads Benzinga and StockTwits adapters (production data sources).
4. Constructs `PopStrategyEngine` with all dependencies injected.
5. Wires `EngineLifecycle` with `PopLifecycleAdapter` for kill-switch, state persistence, and reconciliation.
6. Subscribes `_forward_pop_signal` and `_forward_order` handlers to republish `POP_SIGNAL` and `ORDER_REQ` events to Redpanda topics (`TOPIC_POP`, `TOPIC_ORDERS`).
7. DB persistence is initialized via `init_satellite_db` (POP_SIGNAL, NEWS_DATA, SOCIAL_DATA events written to event_store).

### Main loop

```python
while True:
    bars_cache, rvol_cache = cache_reader.get_bars()
    # Build BarPayload events per ticker
    bus.emit_batch(bar_events)
    lifecycle.tick()
    time.sleep(10)
```

10-second polling cycle. `lifecycle.tick()` enforces the kill switch and max daily loss.

---

## 4. Early Exit Gate

Before making any API calls, the BAR handler checks three conditions. All three must be true to skip:

| Condition | Threshold | Purpose |
|-----------|-----------|---------|
| `bar_range_pct` | < 0.3% | Bar too quiet |
| `last_rvol` | < 1.5 (stocks) or `ETF_RVOL_MIN` (ETFs) | Volume not elevated |
| `abs(gap_size)` | < 2% | No meaningful gap |

This eliminates approximately 90% of API calls. ETFs use a lower RVOL threshold because they rarely reach 1.5x relative volume.

**Code:** `PopStrategyEngine._on_bar()`, lines 572-582.

---

## 5. Data Sources

### 5.1 Benzinga News (`pop_screener/benzinga_news.py`)

- **Class:** `BenzingaNewsSentimentSource`
- **API:** Benzinga Content API v2 (`/api/v2/news`)
- **Rate limit:** 500 requests/min
- **Cache TTL:** 60 seconds per (symbol, window_hours) key
- **Sentiment:** Uses API-provided sentiment score when available; falls back to keyword-based scoring (23 bullish keywords, 22 bearish keywords)
- **Output:** `List[NewsData]` sorted oldest to newest
- **Windows:** Called twice per ticker -- 1-hour and 24-hour windows (for `sentiment_delta`)

### 5.2 StockTwits Social (`pop_screener/stocktwits_social.py`)

- **Class:** `StockTwitsSocialSource`
- **API:** StockTwits API v2 (`/api/2/streams/symbol/{symbol}.json`)
- **Rate limit:** 200 requests/hour (unauthenticated), 400/hour with OAuth token
- **Throttle:** 20-second minimum interval between requests (max 180/hour)
- **Hard cap:** 180 requests/hour counter, returns cached data beyond that
- **Cache TTL:** 15 minutes per symbol (900 seconds)
- **Output:** `SocialData` with mention_count, mention_velocity, bullish_pct, bearish_pct
- **Velocity calculation:** `total_messages / time_span_hours` from the 30-message window

### 5.3 Sentiment Baseline Engine (`pop_screener/sentiment_baseline.py`)

- **Class:** `SentimentBaselineEngine`
- **Purpose:** Replaces hardcoded `headline_baseline=2.0` and `social_baseline=100.0` with per-ticker 7-day averages from the event_store DB.
- **Load:** `load_from_db()` called once at startup; queries `NewsDataSnapshot` and `SocialDataSnapshot` events for tickers with >= 5 samples.
- **Intraday update:** `update_intraday()` applies EMA smoothing (alpha=0.1) for tickers with no DB history.
- **Defaults:** 2.0 headlines/hour, 100.0 mentions/hour when no history exists.

### 5.4 Circuit Breaker

Both news and social sources track consecutive failures (`_news_failures`, `_social_failures`). After **10 consecutive failures** (`_max_data_failures`), the source is disabled and the engine returns early for all subsequent tickers until a success resets the counter.

### 5.5 Ticker Discovery (`pop_screener/ticker_discovery.py`)

- **Class:** `TickerDiscovery`
- **Frequency:** Every 30 minutes during market hours (called by `MomentumScreener.refresh()`)
- **Sources:**
  1. **Benzinga headlines** -- extracts ticker symbols from the `stocks` field and title text; requires >= 2 mentions to qualify.
  2. **StockTwits trending** -- `/api/2/trending/symbols.json`; requires >= 1000 watchers.
  3. **Benzinga movers** -- top gainers, losers, most-active from `/api/v1/market/movers`.
- **Validation:** `^[A-Z]{1,5}$` regex, minimum 2 characters, excludes 170+ false tickers (common English words, abbreviations like CEO, FDA, IPO, SEC, meme terms like HODL, YOLO, FOMO).
- **Cache TTL:** 10 minutes between discovery refreshes.

---

## 6. Feature Engineering (`pop_screener/features.py`)

**Class:** `FeatureEngineer` -- stateless, no I/O, no randomness.

**Method:** `compute(symbol, news_1h, news_24h, social, market, social_baseline_velocity, headline_baseline_velocity) -> EngineeredFeatures`

### 16 Features

| Feature | Computation | Source |
|---------|-------------|--------|
| `sentiment_score` | Mean sentiment of 1-hour headlines [-1, +1] | Benzinga |
| `sentiment_delta` | `sentiment_1h - sentiment_24h` | Benzinga |
| `headline_velocity` | `len(news_1h) / headline_baseline` (7-day avg) | Benzinga |
| `social_velocity` | `mention_velocity / social_baseline` (7-day avg) | StockTwits |
| `social_sentiment_skew` | `bullish_pct - bearish_pct` | StockTwits |
| `mention_velocity` | Raw mentions/hour from SocialData | StockTwits |
| `rvol` | Cumulative intraday volume / time-fraction-adjusted avg daily volume | Market bars + historical |
| `volatility_score` | `ATR(14) / last_price` | Market bars |
| `price_momentum` | `(close_now - close_10_bars_ago) / close_10_bars_ago` | Market bars |
| `gap_size` | `(today_open - prior_close) / prior_close` | Market bars + historical |
| `vwap_distance` | `(last_price - vwap) / vwap` | Market bars |
| `trend_cleanliness_score` | Composite [0, 1] (see below) | Market bars |
| `atr_value` | Raw ATR in dollars (Wilder smoothing, 14-period) | Market bars |
| `float_category` | `LOW_FLOAT` (< 20M shares) or `NORMAL` | Market metadata |
| `last_price` | Most recent close | Market bars |
| `current_vwap` | Current VWAP value | Market bars |
| `earnings_flag` | True if earnings event today/after-hours | Market metadata |

### Trend Cleanliness Score

Composite score [0, 1] with four weighted components over the last 10 bars:

| Component | Weight | Calculation |
|-----------|--------|-------------|
| Higher-highs fraction | 0.35 | Fraction of consecutive bar pairs where `high_i > high_{i-1}` |
| Higher-lows fraction | 0.35 | Fraction of consecutive bar pairs where `low_i > low_{i-1}` |
| Trend/noise ratio | 0.15 | `abs(close[-1] - close[-N]) / (N * ATR)`, capped [0, 1] |
| EMA20 proximity | 0.15 | `1 - (|close - EMA20| / close) / 0.02`, capped [0, 1] |

Returns 0.5 (neutral) when fewer than 3 bars are available.

### ATR Calculation

Wilder's smoothing: seed with simple mean of first `N` true ranges, then apply `atr = atr * (1 - 1/N) + tr * (1/N)` for subsequent values. Falls back to simple average when fewer bars than the period are available.

---

## 7. Screening Rules (`pop_screener/screener.py`)

**Class:** `PopScreener` -- six deterministic rules evaluated in priority order. First match wins.

### Rule 1: HIGH_IMPACT_NEWS

```
sentiment_delta   > 0.40     (SENTIMENT_DELTA_HIGH)
headline_velocity > 8.0x     (HEADLINE_VELOCITY_HIGH)
abs(gap_size)     >= 4%      (GAP_SIZE_HIGH_IMPACT_MIN)
```

All three conditions must be true. Strongest catalyst -- checked first.

### Rule 2: EARNINGS

```
earnings_flag  == True
abs(gap_size)  > 5%          (GAP_SIZE_EARNINGS_MIN)
rvol           > 2.0         (stocks) or ETF_RVOL_MIN (ETFs)
```

### Rule 3: LOW_FLOAT

```
float_category == LOW_FLOAT  (< 20M shares)
rvol           > 4.0x        (RVOL_LOW_FLOAT_THRESHOLD)
abs(gap_size)  > 5%          (GAP_SIZE_LOW_FLOAT_MIN)
```

### Rule 4: MODERATE_NEWS

```
sentiment_delta   > 0.20     (SENTIMENT_DELTA_MODERATE)
headline_velocity in [1.5x, 8.0x]
abs(gap_size)     < 4%       (GAP_SIZE_MODERATE_MAX)
```

### Rule 5: SENTIMENT_POP

```
social_velocity       > 3.0x  (SOCIAL_VELOCITY_THRESHOLD)
social_sentiment_skew > 0.20  (SOCIAL_SKEW_THRESHOLD)
```

### Rule 6: UNUSUAL_VOLUME

```
rvol            > 3.0x       (RVOL_UNUSUAL_THRESHOLD, or ETF_RVOL_MIN for ETFs)
price_momentum  > 2%         (MOMENTUM_MIN)
```

All thresholds are centralized in `pop_screener/config.py`.

---

## 8. Classifier (`pop_screener/classifier.py`)

**Class:** `StrategyClassifier` -- deterministic mapping from `PopCandidate` to `StrategyAssignment`.

Dispatches by `pop_reason` to per-reason classification functions. Outputs a primary strategy, ordered secondary strategies, VWAP compatibility score [0, 1], and overall confidence [0, 1].

### Classification Rules

| PopReason | Primary | Condition | Secondaries |
|-----------|---------|-----------|-------------|
| MODERATE_NEWS | VWAP_RECLAIM | vwap_compat >= 0.60 AND trend_clean >= 0.55 | EMA_TREND_CONTINUATION |
| MODERATE_NEWS | EMA_TREND_CONTINUATION | fallback | BREAKOUT_PULLBACK |
| HIGH_IMPACT_NEWS | ORB | always | HALT + PARABOLIC (if extreme vol/gap), else EMA_TREND |
| SENTIMENT_POP | VWAP_RECLAIM | vwap_compat >= 0.60 AND clean trend AND near VWAP | EMA_TREND_CONTINUATION |
| SENTIMENT_POP | EMA_TREND_CONTINUATION | clean trend but low VWAP compat | BREAKOUT_PULLBACK |
| UNUSUAL_VOLUME | VWAP_RECLAIM | trend_clean >= 0.55 AND vwap_compat >= 0.60 | EMA_TREND_CONTINUATION |
| UNUSUAL_VOLUME | EMA_TREND_CONTINUATION | moderate trend cleanliness | BREAKOUT_PULLBACK |
| UNUSUAL_VOLUME | BREAKOUT_PULLBACK | messy trend | EMA_TREND_CONTINUATION |
| LOW_FLOAT | HALT_RESUME_BREAKOUT | gap >= 15% or extreme volatility | PARABOLIC_REVERSAL |
| LOW_FLOAT | PARABOLIC_REVERSAL | moderate volatility | HALT_RESUME_BREAKOUT |
| EARNINGS | VWAP_RECLAIM | gap < 8% | EMA_TREND (if trend clean) |
| EARNINGS | ORB | gap >= 8% | EMA_TREND (if trend clean) |

### VWAP Compatibility Score

Three-component weighted score [0, 1]:

| Component | Weight | Calculation |
|-----------|--------|-------------|
| VWAP distance | 0.35 | Linear decay from 0% to 3% distance |
| Trend cleanliness | 0.35 | Direct use of trend_cleanliness_score |
| RVOL in-band [1.5, 5.0] | 0.30 | Ramp up below 1.5x, 1.0 in band, decay above 5.0x |

### Confidence

Equal-weight average (`_blend`) of relevant feature scores, clamped to [0, 1]. Inputs vary by pop_reason (e.g., gap magnitude, rvol, trend cleanliness, social velocity).

---

## 9. Strategy Router (`pop_screener/strategy_router.py`)

**Class:** `StrategyRouter`

### Routing Logic

1. Run the **primary strategy** engine's `generate_signals()`.
2. If no entry signals and `try_secondaries=True`, try each **secondary strategy** in order.
3. If still no entry signals and `MOMENTUM_ENTRY` was not already tried, run it as a **last resort** (7+1 pattern).
4. Return the first strategy that produces entry signals. Exit signals accumulate from all attempted engines.

### 7+1 Strategy Engines

All engines are stateless and re-used across calls.

| Strategy Type | Engine Class | File |
|---------------|-------------|------|
| VWAP_RECLAIM | `VWAPReclaimEngine` | `strategies/vwap_reclaim_engine.py` |
| ORB | `ORBEngine` | `strategies/orb_engine.py` |
| HALT_RESUME_BREAKOUT | `HaltResumeEngine` | `strategies/halt_resume_engine.py` |
| PARABOLIC_REVERSAL | `ParabolicReversalEngine` | `strategies/parabolic_reversal_engine.py` |
| EMA_TREND_CONTINUATION | `EMATrendEngine` | `strategies/ema_trend_engine.py` |
| BREAKOUT_PULLBACK | `BOPBEngine` | `strategies/bopb_engine.py` |
| MOMENTUM_ENTRY | `MomentumEntryEngine` | `strategies/momentum_entry_engine.py` |

The `+1` is MOMENTUM_ENTRY, which acts as the universal fallback.

### Engine Interface

Every engine implements:

```python
def generate_signals(
    symbol: str,
    bars: List[OHLCVBar],
    vwap_series: List[float],
    features: EngineeredFeatures,
    assignment: StrategyAssignment,
) -> Tuple[List[EntrySignal], List[ExitSignal]]
```

---

## 10. PopExecutor (`pop_strategy_engine.py`)

**Class:** `PopExecutor` -- lightweight order executor with its own risk gate.

### Configuration

| Parameter | Default | Env var |
|-----------|---------|---------|
| `max_positions` | 3 | `POP_MAX_POSITIONS` |
| `trade_budget` | $500 | `POP_TRADE_BUDGET` |
| `order_cooldown` | 300s | `POP_ORDER_COOLDOWN` |
| `paper` | True | `POP_PAPER_TRADING` |
| Max sector concentration | 2 | Hardcoded `_MAX_PER_SECTOR` |

### Execution Path (Current)

PopExecutor emits `ORDER_REQ` on the local bus. The `_forward_order` handler in `run_pop.py` publishes it to Redpanda `TOPIC_ORDERS`. Core consumes the message and routes through SmartRouter -- the same execution path as Pro engine orders.

```
PopExecutor.execute_entry()
  -> bus.emit(ORDER_REQ, durable=True)
  -> _forward_order handler
  -> publisher.publish(TOPIC_ORDERS, ...)
  -> Core SmartRouter handles bracket orders + exit monitoring
```

### Risk Gate (per-entry, independent of Core RiskEngine)

Checks run in this order under a single lock acquisition:

1. **Cross-layer dedup:** `position_registry.try_acquire(symbol, layer="pop")` -- rejects if held by another strategy layer (Core, Pro, Options).
2. **Max pop positions:** `len(positions) >= max_positions` (default 3).
3. **Sector concentration:** Max 2 positions per sector (`count_sector_positions`).
4. **Ticker cooldown:** Not ordered within the last `order_cooldown` seconds (default 300s).
5. **Duplicate position:** Symbol not already in open pop positions.
6. **Position sizing:** `qty = floor(trade_budget / entry_price)`; reject if qty <= 0.

If all checks pass, the cooldown timestamp and position set are updated atomically while still holding the lock.

### Paper Mode

If `POP_PAPER_TRADING=true` or Alpaca keys are absent, `PopExecutor._paper_fill()` simulates an instant fill at the entry price and emits a `FILL` event on the bus.

### Live Mode

`_live_buy()` submits a marketable limit order to the pop Alpaca account with:

- **Fill timeout:** 2.0 seconds per attempt
- **Poll interval:** 0.25 seconds
- **Max retries:** 3
- **Slippage guard:** Abandons if ask drifts > 0.5% from original ask

On fill, emits `FILL` event. On abandonment, emits `ORDER_FAIL` event.

### Heartbeat

Every 5 minutes, the engine logs a heartbeat with scan count, news hit count, and social hit count.

---

## 11. Data Models (`pop_screener/models.py`)

### Enums

| Enum | Values |
|------|--------|
| `StrategyType` | VWAP_RECLAIM, ORB, HALT_RESUME_BREAKOUT, PARABOLIC_REVERSAL, EMA_TREND_CONTINUATION, BREAKOUT_PULLBACK, MOMENTUM_ENTRY |
| `PopReason` | MODERATE_NEWS, HIGH_IMPACT_NEWS, SENTIMENT_POP, UNUSUAL_VOLUME, LOW_FLOAT, EARNINGS |
| `FloatCategory` | LOW_FLOAT, NORMAL |
| `ExitReason` | STOP, TARGET_1, TARGET_2, VWAP_BREAK, RSI_OVERBOUGHT, EOD, MOMENTUM_FADE, TREND_BREAK, REVERSAL, BELOW_EMA, BELOW_PRIOR_HIGH |

### Stage Flow

```
Ingestion    ->  NewsData, SocialData, MarketDataSlice
Features     ->  EngineeredFeatures
Screener     ->  PopCandidate
Classifier   ->  StrategyAssignment
Router       ->  EntrySignal, ExitSignal
```

### Key Dataclasses

- **`MarketDataSlice`:** bars, vwap_series, rvol_series, gap_size, float_category, earnings_flag, prior_close
- **`PopCandidate`:** symbol, features, pop_reason, raw_scores (dict of key metrics that triggered the rule)
- **`StrategyAssignment`:** symbol, primary_strategy, secondary_strategies, vwap_compatibility_score, strategy_confidence, notes
- **`EntrySignal`:** symbol, side (buy/sell), entry_price, stop_loss, target_1, target_2, strategy_type, size_hint, metadata
- **`ExitSignal`:** symbol, side, exit_price, reason (ExitReason), strategy_type, metadata

---

## 12. Lifecycle and Supervision

**Adapter:** `lifecycle/adapters/pop_adapter.py` -- `PopLifecycleAdapter`

Provides the lifecycle framework with:

- **`get_state()` / `restore_state()`:** Serializes/restores open positions, cooldown offsets, trade log, daily PnL across process restarts.
- **`get_broker_positions()`:** Queries Pop's Alpaca account for actual open positions (for reconciliation).
- **`force_close_all()`:** Emergency close all pop positions via Alpaca API.
- **`cancel_stale_orders()`:** Cancel all open orders on the pop account.
- **`verify_connectivity()`:** Health check against pop Alpaca account.

The lifecycle framework enforces `POP_MAX_DAILY_LOSS` -- if breached, the pop engine is halted via kill switch.

---

## 13. IPC and Event Flow

### Events Emitted by Pop

| Event | Destination | Durable | Purpose |
|-------|-------------|---------|---------|
| `POP_SIGNAL` | Redpanda `TOPIC_POP` | Yes | Audit trail + consumed by Options engine |
| `ORDER_REQ` | Redpanda `TOPIC_ORDERS` | Yes | Forwarded to Core SmartRouter for execution |
| `FILL` | Local bus | Yes | PositionManager, StateEngine, DurableEventLog |
| `ORDER_FAIL` | Local bus | No | Logging and alerting |
| `NEWS_DATA` | Local bus -> DB | No | Persisted via satellite DB handler |
| `SOCIAL_DATA` | Local bus -> DB | No | Persisted via satellite DB handler |

### Smart Persistence

News and social data events use `SmartPersistence` with change-detection functions (`benzinga_changed`, `stocktwits_changed`) to avoid writing duplicate snapshots to the event store.

---

## 14. Configuration Reference

All numeric thresholds are in `pop_screener/config.py`. Key groups:

### Screener Thresholds

```
FLOAT_LOW_THRESHOLD_SHARES   = 20,000,000
SENTIMENT_DELTA_MODERATE     = 0.20
SENTIMENT_DELTA_HIGH         = 0.40
HEADLINE_VELOCITY_MIN        = 1.5
HEADLINE_VELOCITY_HIGH       = 8.0
GAP_SIZE_MODERATE_MAX        = 0.04  (4%)
GAP_SIZE_HIGH_IMPACT_MIN     = 0.04  (4%)
GAP_SIZE_EARNINGS_MIN        = 0.05  (5%)
GAP_SIZE_LOW_FLOAT_MIN       = 0.05  (5%)
SOCIAL_VELOCITY_THRESHOLD    = 3.0
SOCIAL_SKEW_THRESHOLD        = 0.20
RVOL_UNUSUAL_THRESHOLD       = 3.0
RVOL_LOW_FLOAT_THRESHOLD     = 4.0
MOMENTUM_MIN                 = 0.02  (2%)
```

### Classifier Thresholds

```
RVOL_VWAP_RECLAIM_MIN        = 1.5
RVOL_VWAP_RECLAIM_MAX        = 5.0
TREND_CLEAN_VWAP_MIN         = 0.55
TREND_CLEAN_EMA_MIN          = 0.60
VWAP_DISTANCE_EMA_MAX        = 0.03  (3%)
GAP_SIZE_EARNINGS_VWAP_MAX   = 0.08  (8%)
VOLATILITY_EXTREME_THRESHOLD = 0.25
GAP_EXTREME_THRESHOLD        = 0.15  (15%)
```

### Feature Engineering

```
PRICE_MOMENTUM_BARS          = 10
TREND_CLEAN_LOOKBACK_BARS    = 10
ATR_PERIOD                   = 14
RSI_PERIOD                   = 14
```

---

## 15. File Map

```
pop_strategy_engine.py                  PopStrategyEngine, PopExecutor
pop_screener/
  __init__.py
  config.py                             All thresholds and parameters
  models.py                             Dataclasses and enums
  ingestion.py                          Mock data sources (NewsSentimentSource, SocialSentimentSource, MarketBehaviorSource, MomentumSource)
  features.py                           FeatureEngineer (16 features)
  screener.py                           PopScreener (6 rules)
  classifier.py                         StrategyClassifier (deterministic mapping)
  strategy_router.py                    StrategyRouter (7+1 fallback chain)
  benzinga_news.py                      BenzingaNewsSentimentSource (production)
  stocktwits_social.py                  StockTwitsSocialSource (production)
  sentiment_baseline.py                 SentimentBaselineEngine (per-ticker 7-day baselines)
  ticker_discovery.py                   TickerDiscovery (Benzinga + StockTwits trending)
  strategies/
    vwap_reclaim_engine.py              VWAPReclaimEngine
    orb_engine.py                       ORBEngine
    halt_resume_engine.py               HaltResumeEngine
    parabolic_reversal_engine.py        ParabolicReversalEngine
    ema_trend_engine.py                 EMATrendEngine
    bopb_engine.py                      BOPBEngine (Breakout-Pullback)
    momentum_entry_engine.py            MomentumEntryEngine (last resort)
scripts/run_pop.py                      Process entry point
lifecycle/adapters/pop_adapter.py       PopLifecycleAdapter
```
