# 06 -- Options Engine Design (T3.7)

Source of truth: `options/` package. This document reflects the code as-shipped.

---

## 1. Architecture Overview

The options subsystem is a self-contained layer that subscribes to three EventBus
event types and manages the full lifecycle of options positions: entry selection,
execution, monitoring, and exit.

```
EventBus
  |
  +-- SIGNAL  (priority=3) --> _on_signal()   --> directional entries
  +-- BAR     (priority=3) --> _on_bar()      --> neutral/vol scan + exit monitoring
  +-- POP_SIGNAL (priority=3) --> _on_pop_signal() --> news/sentiment entries
  |
  v
OptionsEngine (orchestrator)
  |-- OptionStrategySelector   (strategy selection logic)
  |-- IVTracker                (252-day IV rank/percentile)
  |-- EarningsCalendar         (earnings safety gate)
  |-- OptionsRiskGate          (budget + position limits)
  |-- AlpacaOptionChainClient  (chain data, greeks, quotes)
  |-- AlpacaOptionsBroker      (order execution)
  |-- PortfolioGreeksTracker   (aggregate Greeks)
  |-- STRATEGY_REGISTRY        (13 strategy builders)
```

Key files:

| File | Purpose |
|------|---------|
| `options/engine.py` | OptionsEngine orchestrator, OptionsPosition dataclass, exit management |
| `options/selector.py` | OptionStrategySelector -- maps conditions to strategy types |
| `options/iv_tracker.py` | IVTracker -- rolling 252-day IV rank with confidence blending |
| `options/risk.py` | OptionsRiskGate -- budget caps, position limits, TOCTOU prevention |
| `options/broker.py` | AlpacaOptionsBroker -- single-leg and multi-leg execution |
| `options/chain.py` | AlpacaOptionChainClient -- chain fetch, greeks, quotes, contract selection |
| `options/earnings_calendar.py` | EarningsCalendar -- yfinance + Alpaca fallback |
| `options/portfolio_greeks.py` | PortfolioGreeksTracker -- aggregate delta/gamma/theta/vega |
| `options/strategies/` | 13 strategy builders (base, directional, vertical, volatility, neutral, time_based, complex) |

---

## 2. Three Entry Paths

### 2.1 SIGNAL Path (`_on_signal`)

Triggered by Core VWAP signals (BUY, SELL_STOP, SELL_TARGET, SELL_RSI, SELL_VWAP).
Directional trades driven by RVOL tiers and IV rank.

Flow:
1. Extract action, ticker, RVOL, RSI, ATR, spot price from signal payload.
2. Estimate IV via ATM call on the option chain (`_estimate_iv`).
3. Update IVTracker with current reading; compute IV rank.
4. Pass to `OptionStrategySelector.select_from_signal()` -- returns strategy type or None.
5. Earnings safety check: block credit strategies if earnings within 7 days.
6. Execute entry via `_execute_entry()`.

### 2.2 POP_SIGNAL Path (`_on_pop_signal`)

Triggered by Pop screener candidates (news/sentiment-driven).

Direction inference:
- `sentiment_delta >= 0` or `gap_size >= 0` --> bullish
- Otherwise --> bearish

Strategy selection (within handler, not delegated to selector):

| Direction | IV Rank >= 50 | RVOL >= 2.5 | Otherwise |
|-----------|---------------|-------------|-----------|
| Bullish | `bull_call_spread` | `long_call` | `bull_call_spread` |
| Bearish | `bear_put_spread` | `long_put` | `bear_put_spread` |

RSI is extracted from `features_json` (default 50.0). Earnings safety still applies.

### 2.3 BAR Path (`_on_bar`)

Runs on every bar for every ticker. Two phases:

**Phase 1 -- Exit monitoring** (always runs):
- Calls `_monitor_positions(ticker, spot)` to check open positions for exit conditions.

**Phase 2 -- New neutral/volatility entries** (only before 3:45 PM ET):
- Pre-filter: skip chain API call unless ATR ratio > 1.5% OR RSI in 40-60 range OR existing position.
- Compute ATR, RSI, RVOL from bar data.
- Estimate IV, update IVTracker, get IV rank and history days.
- Pass to `OptionStrategySelector.select_from_bar()` -- returns strategy type or None.
- Earnings safety check for credit strategies.
- Reserve ticker atomically via `_risk.reserve()` before spawning execution thread.
- Execute entry in a daemon thread.

Minimum bars required: 52 (`_MIN_BARS`).

---

## 3. IV Rank (Critical Design Choice)

**File:** `options/iv_tracker.py`

### 3.1 Core Formula

```
raw_rank = (current_iv - min_iv) / (max_iv - min_iv) * 100
```

Rolling window: 252 trading days (1 year). One reading per calendar day per ticker
(intraday updates overwrite the same day's entry).

### 3.2 Confidence Blending

With fewer than 20 data points, the raw rank is blended toward neutral (50):

```
confidence = min(1.0, (num_days - 2 + 1) / 20)
blended = confidence * raw_rank + (1.0 - confidence) * 50.0
```

- `< 2 days`: returns 50.0 (no meaningful rank).
- `2-21 days`: linearly blended toward neutral.
- `>= 21 days`: full confidence, no blending.
- `max_iv == min_iv` (zero range): returns 50.0.

### 3.3 IV Percentile

Separate metric: percentage of historical days where IV was lower than current IV.
Same confidence blending logic.

### 3.4 Regime Thresholds (used by selector)

| IV Rank | Regime | Action |
|---------|--------|--------|
| >= 70 | Very rich | Aggressive credit selling |
| >= 50 | Rich | Sell premium (credit strategies) |
| < 30 | Cheap | Buy premium (debit strategies) |

### 3.5 Persistence

- Storage: `data/iv_history.json` (atomic write via `.tmp` + `os.replace`).
- Auto-save: hourly (3600s interval), runs in daemon thread.
- Load on startup: trims to lookback window.
- Seeding: `seed_from_chain()` bootstraps from option chain term structure when no prior data exists.

### 3.6 Raw IV Fallback

When IV history is thin (< 2 days), the selector falls back to raw IV thresholds:
- Rich: IV >= 0.25
- Moderate: IV >= 0.20
- Cheap: IV <= 0.18
- Very rich: IV >= 0.35

---

## 4. Strategy Selection

**File:** `options/selector.py`

### 4.1 Signal Path: `select_from_signal()`

Uses RVOL tiers crossed with IV regime. ETFs get scaled-down RVOL thresholds
(they structurally never reach 2.5x RVOL).

**RVOL Thresholds:**

| Threshold | Stocks | ETFs (0.35x scale) |
|-----------|--------|---------------------|
| High | 2.5 | 0.875 |
| Moderate | 1.2 | 0.6 |
| Low | 0.8 | 0.4 |

**Selection Matrix (BUY action shown; SELL mirrors with puts):**

| RVOL Tier | IV Cheap/Neutral | IV Rich (>= 50) |
|-----------|------------------|-----------------|
| Tier 1: >= high | `long_call` | `bull_call_spread` |
| Tier 2: >= moderate | `bull_call_spread` | `bull_put_spread` (credit) |
| Tier 3: >= low | None | `bull_put_spread` (if very rich: also allowed) |
| Tier 4: < low | None | `bull_put_spread` (only if IV rank >= 70) |

### 4.2 Bar Path: `select_from_bar()`

Uses ATR ratio, RSI range, and IV regime.

**ATR Thresholds:**
- Spike: > 3.5% of spot
- Moderate: 1.5% - 3.5% of spot

**Selection Logic:**

| Condition | Strategy |
|-----------|----------|
| ATR spike > 3.5% + IV not rich | `long_straddle` |
| ATR moderate 1.5-3.5% + IV cheap | `long_strangle` |
| RSI 40-60 + IV very rich/rich + no existing position + RSI 42-58 | `iron_butterfly` |
| RSI 40-60 + IV very rich/rich + no existing position | `iron_condor` |
| RSI 40-60 + IV rich + no existing position + ATR < 1.5% | `butterfly_spread` |
| Existing position + IV rich + RSI 40-60 | `calendar_spread` |

---

## 5. The 13 Strategies

All strategies implement `BaseOptionsStrategy.build()` and return an
`OptionsTradeSpec` (or None). Registered in `STRATEGY_REGISTRY` dict.

### 5.1 Data Structures

```python
@dataclass
class OptionLeg:
    symbol:      str    # OCC symbol (e.g. AAPL240119C00180000)
    side:        str    # 'buy' | 'sell'
    qty:         int    # number of contracts
    ratio:       int    # leg ratio in combo (1 for most, 2 for butterfly body)
    limit_price: float  # mid-price for execution

@dataclass
class OptionsTradeSpec:
    strategy_type:  str
    ticker:         str
    expiry_date:    str             # 'YYYY-MM-DD'
    legs:           List[OptionLeg]
    net_debit:      float           # positive=cost, negative=credit received
    max_risk:       float           # maximum dollar loss (always > 0)
    max_reward:     float           # maximum dollar gain (always > 0)
    reason:         str
```

### 5.2 Strategy Catalog

**Directional** (`strategies/directional.py`):

| Strategy | Legs | Construction | Risk/Reward |
|----------|------|-------------|-------------|
| `long_call` | 1 | Buy call at delta ~0.35 | Risk = premium x 100; Reward = 3x premium (conservative estimate) |
| `long_put` | 1 | Buy put at delta ~0.35 | Same as long_call |

**Vertical Spreads** (`strategies/vertical.py`):

| Strategy | Type | Legs | Construction |
|----------|------|------|-------------|
| `bull_call_spread` | Debit | 2 | Buy call at delta ~0.35, sell call at delta ~0.175 |
| `bear_put_spread` | Debit | 2 | Buy put at delta ~0.35, sell put at delta ~0.175 |
| `bull_put_spread` | Credit | 2 | Sell put at delta ~0.175, buy put at delta ~0.10 (protection) |
| `bear_call_spread` | Credit | 2 | Sell call at delta ~0.175, buy call at delta ~0.10 (protection) |

Spread width limits: $1.00 minimum, $20.00 maximum. Liquidity filter: both legs must have bid > 0 and ask > 0.

Credit spread risk: `max_risk = (spread_width - net_credit) x 100`. The `net_debit` field is negative for credits.

**Volatility** (`strategies/volatility.py`):

| Strategy | Legs | Construction |
|----------|------|-------------|
| `long_straddle` | 2 | Buy ATM call + ATM put (same strike). If expiries differ, filter to closest-to-30-DTE expiry. |
| `long_strangle` | 2 | Buy 3% OTM call + 3% OTM put. Same expiry alignment as straddle. |

Max reward estimated at 5x premium (theoretical unlimited).

**Neutral** (`strategies/neutral.py`):

Skew-aware: uses OTM percentage from spot instead of raw delta to avoid
asymmetric risk from put skew.

| Strategy | Legs | Strike Placement |
|----------|------|-----------------|
| `iron_condor` | 4 | Short strikes at spot +/- 5% OTM. Wings at +/- 2.5% beyond short strikes (7.5% OTM total). Spread widths equalized within 20% tolerance. Single expiry closest to 30 DTE. |
| `iron_butterfly` | 4 | Short body at spot +/- 0.5% (slightly OTM for better fills). Wings at spot +/- 5%. Width equalization same as iron condor. |

**Time-Based** (`strategies/time_based.py`):

| Strategy | Legs | Construction |
|----------|------|-------------|
| `calendar_spread` | 2 | Sell near-term ATM call (min_dte to min_dte+10), buy far-term ATM call (max_dte-5 to max_dte+10). Same strike (within $1). Profits from theta differential. |
| `diagonal_spread` | 2 | Buy deep ITM LEAPS call (delta ~0.80, 300+ DTE), sell near-term OTM call (delta ~0.30). Synthetic covered call. |

**Complex** (`strategies/complex.py`):

| Strategy | Legs | Construction |
|----------|------|-------------|
| `butterfly_spread` | 3 (1-2-1) | Buy 1 ITM call (lower), sell 2 ATM calls (middle, ratio=2), buy 1 OTM call (upper). Wing width ~3% of underlying. Wings must be symmetric within $1. |

---

## 6. Exit Management

**Location:** `options/engine.py`, methods `_monitor_positions()`, `_evaluate_exit()`,
`_evaluate_greeks_exit()`.

Checked on every BAR event via `_monitor_positions()`. A 30-second cooldown
(`_MONITOR_COOLDOWN`) prevents re-checking the same ticker faster than 30s.

### 6.1 Exit Categories

Strategy categories determine profit target percentages:

| Category | Strategies | Profit Target |
|----------|-----------|--------------|
| Credit | bull_put_spread, bear_call_spread, iron_condor, iron_butterfly | 50% of max_reward |
| Debit | long_call, long_put, bull_call_spread, bear_put_spread, butterfly_spread | 80% of max_reward |
| Volatility | long_straddle, long_strangle | 60% of max_reward |
| Time | calendar_spread, diagonal_spread | (no special target) |

### 6.2 Exit Conditions (evaluated in order, first match wins)

**1. DTE Expiry** -- `DTE <= 10`
- Reason: gamma risk near expiration.
- Action: close immediately.

**2. Profit Target** -- `current_pnl >= max_reward x target_fraction`
- Fraction varies by category (see table above).
- P&L calculation: `close_value - entry_cost`. For credit positions, entry_cost is negative (credit received), close_value is negative (cost to buy back).

**3. Stop Loss** -- `current_pnl <= -(max_risk x 50%)`
- Triggers at 50% of max risk (was 80%, changed because 80% was too late).

**4. Theta Bleed** -- debit positions only
- Condition: position lost 60% of entry value AND DTE >= 15.
- Formula: `remaining_value / entry_cost <= 0.40`.
- Rationale: position decaying without recovery while still far from expiry.

**5. DTE Roll Profit** -- credit strategies only
- Condition: DTE <= 14 AND current_pnl > 0.
- Captures remaining credit profit before gamma risk zone.

**6. Greeks-based exits** (3 sub-conditions):

| Trigger | Condition | Rationale |
|---------|-----------|-----------|
| Gamma spike | gamma > 0.10 at DTE <= 14 | Position becomes unmanageable |
| Theta bleed fast | abs(theta) / position_value > 5% per day | Bleeding too fast |
| Delta drift | abs(delta) > 0.30 for neutral strategies | Neutral position has become directional |

Neutral strategies for delta drift: iron_condor, iron_butterfly, long_straddle,
long_strangle, butterfly_spread.

### 6.3 Position Mark-to-Market

`_get_position_mark()` computes close value per leg:
- BUY legs: sell at bid (we own it).
- SELL legs: buy at ask (we're short it, costs to close).

### 6.4 Position Close Flow

1. Log intent with strategy type, reason, P&L, hold time, DTE.
2. Execute close via broker (`close_position`).
3. On success: update daily stats, release from risk gate, remove from positions dict, recalculate portfolio Greeks.
4. On failure: log error, position remains open.

`close_all_positions()` iterates all open positions and closes each (used for EOD).

---

## 7. Risk Gate

**File:** `options/risk.py`

### 7.1 Limits

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_positions` | 5 | Maximum concurrent open options positions |
| `trade_budget` | $500 | Maximum risk per single trade |
| `total_budget` | $10,000 | Total capital at risk ceiling |
| `order_cooldown` | 300s | Per-ticker cooldown between trades |
| `max_daily_trades` | 50 | Daily trade count limit |

### 7.2 Capital Tracking

Tracks **max_risk** (not net_debit) against the budget. A credit spread that
costs $100 net debit but has $500 max risk deploys $500 against the total budget.

```python
risk_amount = max_risk if max_risk > 0 else max(cost, 0.0)
```

### 7.3 TOCTOU Prevention

The risk gate has a three-phase protocol to prevent time-of-check-to-time-of-use
race conditions when BAR path spawns execution threads:

1. **`reserve(ticker)`** -- atomically add ticker to `_pending_tickers` set before
   spawning execution thread. Fails if ticker already pending/open or position
   limits reached. Counts pending tickers toward `max_positions`.
2. **`unreserve(ticker)`** -- release pending reservation on build failure or
   risk rejection.
3. **`acquire(ticker, cost, max_risk)`** -- convert reservation to open position.
   Records max_risk in `_open_positions`, updates deployed capital, increments
   daily trade count, discards from pending set.

### 7.4 Check Method

`check(ticker, max_risk)` runs all gates without mutating state:
1. Already open position for ticker?
2. Already pending execution for ticker?
3. Max positions reached?
4. Daily trade limit reached?
5. Cooldown elapsed?
6. Per-trade max_risk exceeds trade_budget?
7. Available capital sufficient?

Returns `None` on pass, or a rejection reason string.

---

## 8. Broker

**File:** `options/broker.py`

### 8.1 Execution Modes

The broker does NOT subscribe to EventType.ORDER_REQ. It receives direct method
calls from OptionsEngine to avoid polluting the main equity order channel.

**Single-leg** (LongCall, LongPut):
- Uses alpaca-py `LimitOrderRequest` with the OCC option symbol.
- Limit price at mid-market.
- `TimeInForce.DAY`.
- Poll for fill: 15s timeout (`FILL_TIMEOUT_SEC`), 1s poll interval.
- On timeout: cancel, widen by $0.05, retry once (`MAX_RETRIES = 1`, 2 total attempts).
- Widen direction: buy orders increase price, sell orders decrease price.

**Multi-leg** (spreads, condors, butterflies):
- POST to `/v2/orders` with `order_class: "mleg"`.
- Top-level `qty` = number of spreads. Each leg uses `ratio_qty` (not `qty`).
- Entry orders use `type: "limit"` with net price. Close orders use `type: "market"`.
- Submit method: tries `_client._request("POST", "/orders", body)`, falls back to
  `_client.post()`, falls back to direct httpx POST with extracted credentials.
- Same 15s timeout, cancel-and-retry with $0.05 widen.

### 8.2 Cancel Race Handling

If cancel fails on a multi-leg order:
1. Re-check order status.
2. If status is "filled" -- return True (trade succeeded).
3. If not filled -- abort (do not retry to avoid duplicates).

### 8.3 Position Close

`close_position(ticker, legs)`:
- Reverses all leg sides (buy becomes sell, sell becomes buy).
- Builds a close `OptionsTradeSpec` with `strategy_type="close"`.
- Single reversed leg uses `_execute_single_leg`, multi-leg uses `_execute_multi_leg`.
- Close orders use market type (not limit).

---

## 9. Earnings Calendar

**File:** `options/earnings_calendar.py`

### 9.1 Data Sources

1. **yfinance** (primary): `yf.Ticker(symbol).calendar`. Handles both DataFrame and
   dict return formats. Extracts next and last earnings dates.
2. **Alpaca** (fallback): GET `/v2/corporate_actions/announcements` with
   `ca_types=Earnings`, 90-day forward window.

### 9.2 Cache

- TTL: 24 hours.
- Per-ticker: `(fetch_timestamp, next_earnings_date, last_earnings_date)`.
- Thread-safe with lock.
- Lazy fetch on first access (`_ensure_cached`).

### 9.3 Safety Rules

| Check | Min Days | Used By |
|-------|----------|---------|
| `is_earnings_safe(ticker, min_days=7)` | 7 | Credit strategy gate |
| `debit_strategy_allowed(ticker, min_days=3)` | 3 | Debit strategy gate |
| Unknown earnings date | N/A | Treated as safe (risk gate already caps exposure) |

### 9.4 Post-Earnings Detection

`is_post_earnings(ticker, within_days=3)` / `iv_crush_opportunity()`:
Flags tickers where earnings happened within the last 3 days -- premium selling
window due to IV crush.

---

## 10. Option Chain Client

**File:** `options/chain.py`

### 10.1 Data Pipeline

1. **Contract discovery**: `TradingClient.get_option_contracts()` with DTE window
   and `AssetStatus.ACTIVE` filter.
2. **Snapshot enrichment**: `OptionHistoricalDataClient.get_option_snapshot()` in
   batches of 100. Extracts Greeks (delta, gamma, theta, vega, mid_iv) and quotes
   (bid, ask). Retry on 429 with exponential backoff (2s, 4s).
3. **Liquidity filtering**: bid > 0, ask > 0, spread < 20% of mid, bid >= $0.05.
4. **Caching**: 60-second TTL per `(ticker, min_dte, max_dte)` key.
5. **Rate limiting**: token-bucket at 3 requests/second.

### 10.2 Contract Selection Methods

| Method | Purpose | Parameters |
|--------|---------|-----------|
| `find_by_delta()` | Closest to target delta within tolerance | option_type, target_delta, tolerance=0.05 |
| `find_atm()` | Strike closest to underlying price | option_type, underlying_price |
| `find_otm()` | Strike at OTM percentage from spot | option_type, underlying_price, otm_pct=0.03 |
| `find_leaps()` | LEAPS contract for diagonal spread | option_type, target_delta, min_dte=300 (tolerance=0.10) |

### 10.3 OptionContract Dataclass

```python
@dataclass
class OptionContract:
    symbol, underlying, expiry_date, strike, option_type,
    delta, gamma, theta, vega, iv, bid, ask, dte
```

---

## 11. Portfolio Greeks

### 11.1 Engine-Level Tracking

`OptionsEngine` maintains portfolio-level delta, theta, vega aggregated across
all `OptionsPosition` objects. Updated via `_recalculate_portfolio_greeks()`
on every entry and exit.

Per-position Greeks are updated on each monitoring pass via `_update_position_greeks()`,
which fetches fresh chain data for each leg.

Sign convention:
- BUY legs: `+1` multiplier (long delta, long vega, long theta decay)
- SELL legs: `-1` multiplier
- All multiplied by `qty x 100` (contract multiplier)

### 11.2 PortfolioGreeksTracker

**File:** `options/portfolio_greeks.py`

Standalone tracker used for dashboard/monitoring. Aggregates across arbitrary
position lists.

Sign convention:
- `long` side: `+1`
- `short` side: `-1`
- Gamma: always absolute value (`abs(gamma * qty)`)

Outputs:
- `total_delta`, `total_gamma`, `total_theta`, `total_vega`
- `delta_dollars`: `total_delta x 100` (dollar exposure per $1 underlying move)
- `per_position`: breakdown by ticker

Caching: 60-second TTL per contract symbol. Fetches via
`AlpacaOptionChainClient.get_greeks_for_symbol()`.

---

## 12. Entry Execution Pipeline

`_execute_entry()` runs the full pipeline:

1. **Risk pre-check**: `_risk.check(ticker, trade_budget)` -- fast rejection.
2. **Strategy build**: instantiate from `STRATEGY_REGISTRY`, call `.build()` with
   chain client, DTE window (default 20-45), delta targets (directional=0.35,
   spread=0.175).
3. **Risk verify**: `_risk.check(ticker, actual_max_risk)` -- with real numbers from build.
4. **Emit durable event**: `OPTIONS_SIGNAL` via EventBus with `durable=True`.
   Payload includes all legs as JSON, net_debit, max_risk, max_reward, source.
5. **Acquire position**: `_risk.acquire(ticker, cost, max_risk)`.
6. **Execute via broker**: `_broker.execute(trade_spec, source_event)`.
7. **On failure**: release risk gate position, unreserve ticker.
8. **On success**: create `OptionsPosition` dataclass, add to positions dict,
   increment daily entries, recalculate portfolio Greeks.

Failure at any step releases all held resources (reserve, acquisition).

---

## 13. OptionsPosition Lifecycle

```python
@dataclass
class OptionsPosition:
    ticker, strategy_type, spec, entry_time, entry_cost,
    max_risk, max_reward, expiry_date,
    current_value, current_pnl, last_check_time,
    portfolio_delta, portfolio_theta, portfolio_vega
```

Properties:
- `is_credit`, `is_debit`, `is_volatility` -- category checks for exit logic.
- `dte` -- computed from `expiry_date` vs today.
- `profit_target` -- returns category-specific target fraction.
- `holding_minutes` -- elapsed time since entry.

---

## 14. EOD and Daily Reset

- **EOD gate**: no new entries after 3:45 PM ET. Exit monitoring continues.
- **`close_all_positions(reason)`**: closes all open positions (used for EOD or emergency).
- **`reset_daily()`**: zeroes daily_entries, daily_exits, daily_pnl counters.

---

## 15. Configuration Defaults

| Parameter | Value | Location |
|-----------|-------|----------|
| min_dte | 20 | engine.py constructor |
| max_dte | 45 | engine.py constructor |
| leaps_dte | 365 | engine.py constructor |
| delta_directional | 0.35 | engine.py constructor |
| delta_spread | 0.175 | engine.py constructor |
| paper mode | True | engine.py constructor |
| IV lookback | 252 days | iv_tracker.py |
| IV auto-save interval | 3600s | iv_tracker.py |
| Chain cache TTL | 60s | chain.py |
| Chain rate limit | 3 req/s | chain.py |
| Chain liquidity: max spread | 20% of mid | chain.py |
| Chain liquidity: min bid | $0.05 | chain.py |
| Snapshot batch size | 100 | chain.py |
| Earnings cache TTL | 24h | earnings_calendar.py |
| Greeks cache TTL | 60s | portfolio_greeks.py |
