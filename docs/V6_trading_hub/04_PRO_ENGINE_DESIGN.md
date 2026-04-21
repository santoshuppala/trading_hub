# 04 -- ProSetupEngine Design

> Source of truth: `pro_setups/` package.  
> Last verified against code: 2026-04-15.

---

## 1. Pipeline Overview

Every BAR event flows through the following steps for each ticker.
If any step returns `None` or fails validation, the pipeline exits silently
and the BAR continues through the normal T4 StrategyEngine path.

```
BAR (priority=2)
 |
 +-- Guard: len(df) >= 52, not after 15:45 ET
 |
 +-- 11 Detectors  -->  Dict[str, DetectorSignal]
 |
 +-- StrategyClassifier.classify()  -->  ClassificationResult | None
 |       (priority: T3 -> T2 -> T1, first match wins)
 |
 +-- strategy.detect_signal()  -->  'long' | 'short' | None
 |
 +-- strategy.generate_entry()  -->  entry_price
 +-- compute_atr(df)            -->  atr
 +-- strategy.generate_stop()   -->  stop_price
 |       + engine enforces 0.3% min stop distance
 +-- strategy.generate_exit()   -->  (target_1, target_2)
 |
 +-- _levels_valid()  -->  bool
 |       stop < entry < t1 < t2 (long)
 |       R:R >= 1.0 (target_1 to entry vs entry to stop)
 |
 +-- Compute supplementary: VWAP, RSI, RVOL
 |
 +-- Emit PRO_STRATEGY_SIGNAL (non-durable, correlation_id = BAR event_id)
      |
      +-- ProStrategyRouter (priority=1)
           |
           +-- RiskAdapter.validate_and_emit()  -->  9 checks
                |
                +-- ORDER_REQ (durable=True)  -->  AlpacaBroker  -->  IPC Redpanda
```

**File:** `pro_setups/engine.py` -- `ProSetupEngine` class.

---

## 2. DetectorSignal Contract

All 11 detectors inherit from `BaseDetector` (template method pattern).
The public `detect()` method enforces `MIN_BARS` and absorbs exceptions.
Subclasses implement `_detect()`.

```python
@dataclass
class DetectorSignal:
    fired:     bool                    # pattern detected?
    direction: str                     # 'long' | 'short' | 'neutral'
    strength:  float                   # [0.0, 1.0], clamped in __post_init__
    metadata:  Dict[str, Any] = {}     # detector-specific k/v
```

**File:** `pro_setups/detectors/base.py`

---

## 3. The 11 Detectors

| # | Detector | Key | MIN_BARS | Fires when | Strength formula | Direction |
|---|----------|-----|----------|------------|------------------|-----------|
| 1 | **Trend** | `trend` | 52 | EMA9 > EMA20 > EMA50 aligned AND >= 45% HH/HL structure in last 20 bars | `(hh_count + hl_count) / (2 * n)` | EMA alignment |
| 2 | **VWAP** | `vwap` | 5 | Always (above or below VWAP) | `abs(distance) / 3%`, +0.25 on reclaim | above = long |
| 3 | **S/R** | `sr` | 30 | Close within 0.6% of pivot high/low (40-bar lookback, 3-bar pivot window) | `1 - (dist / 0.6%)`, +0.2 on flip | above level = long |
| 4 | **ORB** | `orb` | 17 | Close breaks OR high/low (first 15 bars), OR range >= 0.3% of price | `0.5 + 0.25*(vol_confirm) + min(extension*0.5, 0.25)` | breakout direction |
| 5 | **InsideBar** | `inside_bar` | 22 | `cur_high <= prev_high AND cur_low >= prev_low` | `max(0.3, tightness)` where tightness = `1 - inside_range/mother_range` | EMA20 slope (5-bar) |
| 6 | **Gap** | `gap` | 5 | Gap >= 0.5%, < 15%, unfilled (requires rvol_df for prev-day close) | `abs(gap_pct) / 5%` | gap direction |
| 7 | **Flag** | `flag` | 25 | Pole >= 3x ATR in <= 8 bars, flag range <= 1.5x ATR in 10 bars, breakout >= 0.3x ATR beyond flag | `tightness*0.4 + pole_power*0.6` | pole direction |
| 8 | **Liquidity** | `liquidity` | 22 | Stop-hunt sweep (prev bar pierces 20-bar swing low/high by <= 1.5x ATR), current bar reverses + wick >= 40% of bar range | `0.5 + wick_pct * 0.5` | sweep reversal |
| 9 | **Volatility** | `volatility` | 45 | Close outside BB(20,2) bands; squeeze = band-width < 85% of 20-bar avg width | `0.4 + 0.3*(squeeze) + min(overshoot*0.3, 0.3)` | breakout direction |
| 10 | **Fib** | `fib` | 30 | Close within 0.25% of Fib level (40-bar swing); 38.2%/61.8% get +0.2 bonus | `(1 - dist/0.25%) + primary_bonus` | EMA20 slope |
| 11 | **Momentum** | `momentum` | 25 | Vol >= 3x 20-bar avg AND 3-bar move >= 1.5x ATR AND close in top/bottom 25% of range | `0.5 + vol_score*0.3 + move_score*0.2` | move direction |

### Shared Compute Functions (`_compute.py`)

| Function | Algorithm |
|----------|-----------|
| `compute_atr(df, 14)` | Wilder-smoothed EWM of true range |
| `compute_vwap(df)` | Cumulative typical-price x volume / cumulative volume |
| `compute_rsi(df, 14)` | Wilder EWM gain/loss ratio |
| `compute_ema(series, span)` | Standard EWM (span-based) |
| `compute_bollinger(df, 20, 2.0)` | Rolling mean +/- std_mult * rolling std |
| `pivot_highs/lows(df, window)` | Local extrema with `window` bars on each side |
| `fib_levels(lo, hi)` | Retracement at 23.6%, 38.2%, 50.0%, 61.8%, 78.6% |
| `compute_rvol(df, rvol_df)` | Canonical `RVOLEngine` when available, else simple ratio fallback |

---

## 4. Classifier

**File:** `pro_setups/classifiers/strategy_classifier.py`

Deterministic, priority-ordered rules. First matching rule wins.
T3 is evaluated first (most selective, highest R:R).

### Rule Table (evaluation order)

| Priority | Strategy | Tier | Required Detectors (min strength) |
|----------|----------|------|-----------------------------------|
| 1 | `momentum_ignition` | 3 | momentum (0.70) |
| 2 | `fib_confluence` | 3 | fib (0.60) |
| 3 | `bollinger_squeeze` | 3 | volatility (0.60) |
| 4 | `liquidity_sweep` | 3 | liquidity (0.60) |
| 5 | `flag_pennant` | 2 | flag (0.50) |
| 6 | `gap_and_go` | 2 | gap (0.50) |
| 7 | `inside_bar` | 2 | inside_bar (0.50) |
| 8 | `orb` | 2 | orb (0.50) |
| 9 | `sr_flip` | 1 | sr (0.60) |
| 10 | `vwap_reclaim` | 1 | vwap (0.50) |
| 11 | `trend_pullback` | 1 | trend (0.50) + vwap (0.30) |

### Confidence Calculation

```
base_conf     = mean(required detector strengths)
optional_bonus = 0.05 per additional fired detector, capped at 0.15
confidence    = min(base_conf + optional_bonus, 1.0)
```

### Direction Agreement

All required detectors must agree on direction (`long` or `short`).
If directions disagree or none provide a valid direction, the rule is rejected.

---

## 5. Strategy Tier System

### Tier Constants (from `BaseProStrategy`)

| Param | Tier 1 | Tier 2 | Tier 3 |
|-------|--------|--------|--------|
| **SL_ATR** | 0.3--1.0 (strategy-specific) | 0.8--1.0 | 1.5--2.0 |
| **PARTIAL_R** (target_1) | 1.0 R | 1.5 R | 3.0 R |
| **FULL_R** (target_2) | 2.0 R | 3.0 R | 6.0--8.0 R |
| **Min Confidence** | 50% | 60% | 70% |
| **Min R:R** (RiskAdapter) | 1.5 | 2.0 | 3.5 |

### 11 Strategies (code-verified)

#### Tier 1 -- High Win Rate, Tight Stops

| Strategy | Class | SL_ATR | Targets | Entry Logic | Stop Override |
|----------|-------|--------|---------|-------------|---------------|
| **VWAPReclaim** | `VWAPReclaim` | 1.0 | 1R / 2R | Delegates to T4 `SignalAnalyzer`: 2-bar VWAP reclaim + opened above VWAP + RSI 50--70 | `max(atr_stop, reclaim_candle_low - 0.01)` |
| **TrendPullback** | `TrendPullback` | 0.4 | 1R / 2R | Trend detector strength >= 0.50, close within 1% of EMA9 or 1.5% of EMA20, bullish bar (close > open), price above VWAP | `max(0.4*ATR stop, 5-bar swing low)` |
| **SRFlip** | `SRFlip` | 0.3 | 1R / 2R | SR detector strength >= 0.60, flip=True, bar confirms direction, no strong counter-trend | Uses `BaseProStrategy.generate_stop()` (S/R pivot scan) |

#### Tier 2 -- Moderate, Higher R:R

| Strategy | Class | SL_ATR | Targets | Entry Logic | Stop Override |
|----------|-------|--------|---------|-------------|---------------|
| **GapAndGo** | `GapAndGo` | 1.0 | 1.5R / 3R | Gap strength >= 0.40, close in top 40% of session (long), VWAP alignment | `max(1.0*ATR stop, VWAP - 0.01)` |
| **InsideBar** | `InsideBar` | 1.0 | 1.5R / 3R | Inside bar fired, close must breach mother bar boundary (high for long, low for short) | `max(1.0*ATR stop, mother_bar_low - 0.01)` |
| **ORB** | `ORB` | 1.0 | 1.5R / 3R | ORB strength >= 0.50, volume confirmation required | `min(1.0*ATR stop, OR_high - 0.01)` -- wider stop |
| **FlagPennant** | `FlagPennant` | 0.8 | 1.5R / 3R | Flag strength >= 0.45, direction from detector | `max(flag_low - 0.01, 0.8*ATR stop)` |

#### Tier 3 -- Low Win Rate, High Expectancy

| Strategy | Class | SL_ATR | Targets | Entry Logic | Stop Override |
|----------|-------|--------|---------|-------------|---------------|
| **MomentumIgnition** | `MomentumIgnition` | 1.5 | 3R / 8R | Momentum strength >= 0.65, vol_ratio >= 2.5, no strong opposing trend | `min(1.5*ATR stop, 3-bar low - 0.01)` |
| **FibConfluence** | `FibConfluence` | 2.0 | 3R / 6R | Fib strength >= 0.70, label in {38.2%, 61.8%}, >= 2 confirming signals (S/R, trend; VWAP only counts with 1+ real confirm), volume >= 1.1x avg, close in strong 30% of bar | Entry at fib_price if within 0.3% of close |
| **BollingerSqueeze** | `BollingerSqueeze` | 1.5 | 3R / 6R | Volatility strength >= 0.55, squeeze=True, volume >= 1.2x avg | ATR-based stop |
| **LiquiditySweep** | `LiquiditySweep` | 2.0 | 3R / 6R | Liquidity strength >= 0.55, wick_pct >= 0.35, volume >= 0.8x avg | `min(2.0*ATR stop, sweep_low - 0.01)` |

---

## 6. Stop Generation

**File:** `pro_setups/strategies/base.py` -- `BaseProStrategy.generate_stop()`

Three-tier approach (used by strategies that do not override):

### Tier A: Full-Day S/R Pivot Scan

1. Scan all bars for pivot lows/highs (3-bar window on each side).
2. Cluster nearby levels within 0.2% proximity band.
3. Score each cluster: `len(cluster) + touches + bounces * 2`.
4. Select strongest level below entry (long) with >= 2 touches.
5. Place stop 0.01 below that level.

### Tier B: ATR Fallback

When no qualifying S/R level exists:
- Compute daily ATR from all available bars (full day, manual TR calculation).
- Stop = entry - 1.5 x daily_ATR (long).

### Tier C: Safety Caps

| Cap | Value | Effect |
|-----|-------|--------|
| Minimum floor | 0.3% of entry | `entry * 0.003` -- prevents trivially tight stops |
| Maximum ceiling | 3% of entry | `entry * 0.03` -- prevents unreasonably wide stops |
| Absolute floor | 0.01 | Stop can never go below $0.01 |

### Engine-Level Override

After `strategy.generate_stop()` returns, the engine enforces a 0.3% minimum
distance between entry and stop for ALL strategies, regardless of what the
strategy computed.

---

## 7. ProStrategyRouter

**File:** `pro_setups/router/strategy_router.py`

Thin subscriber on `PRO_STRATEGY_SIGNAL` (priority=1).
Logs the signal and forwards all fields to `RiskAdapter.validate_and_emit()`.
No filtering or transformation -- purely a routing layer.

---

## 8. RiskAdapter (9 Checks)

**File:** `pro_setups/risk/risk_adapter.py`

All checks run sequentially. First failure blocks the order and logs the reason.

| # | Check | Threshold | Notes |
|---|-------|-----------|-------|
| 0 | Cross-layer dedup | `position_registry.try_acquire(ticker, layer="pro_setups")` | Prevents same ticker held by both pro_setups and T4 layers. Released on failure. |
| 1 | Direction filter | `direction == 'long'` only | Short execution not wired yet. |
| 1b | Min confidence | T1: 0.50, T2: 0.60, T3: 0.70 | Per-tier threshold from `_MIN_CONFIDENCE`. |
| 1c | Signal dedup | Same `ticker:strategy_name` within 60s | Uses `time.monotonic()` timestamps. |
| 2 | Max positions | Configurable (default 3) | Counts `_positions` set. |
| 2b | Sector concentration | Max 2 per sector | Uses `monitor.sector_map` for sector lookup. |
| 3 | Per-ticker cooldown | 300s default | `_last_order` monotonic timestamp dict. |
| 4 | No duplicate ticker | Ticker not in `_positions` | Prevents double-up. |
| 5 | Min R:R | T1: 1.5, T2: 2.0, T3: 3.5 | `reward = target_2 - entry`, `risk = entry - stop`. |
| 6 | ATR sanity | `atr_value > 0` | Rejects invalid ATR. |

### Position Sizing

```
max_dollar_risk = trade_budget * 2%          (RISK_PCT = 0.02)
qty_by_risk     = int(max_dollar_risk / risk)
qty_by_budget   = int(trade_budget / entry_price)
qty             = min(max(qty_by_risk, 1), max(qty_by_budget, 1))
```

### ORDER_REQ Emission

```python
OrderRequestPayload(
    ticker            = ticker,
    side              = Side.BUY,
    qty               = qty,
    price             = round(entry_price, 2),
    reason            = "pro:{strategy_name}:T{tier}:long",
    needs_ask_refresh = True,
    stop_price        = round(stop_price, 4),
    target_price      = round(target_2, 4),       # full exit target
    atr_value         = round(atr_value, 4),
)
```

Emitted with `durable=True` and `correlation_id` from the source PRO_STRATEGY_SIGNAL event.

### State Tracking

- `_on_fill()`: removes ticker from `_positions` on SELL fills with `pro:` reason prefix.
- `_on_position()`: removes ticker from `_positions` on `CLOSED` action.

---

## 9. Event Flow Diagram

```
                            BAR event (priority=2)
                                 |
                          ProSetupEngine._on_bar()
                                 |
                     +-----------+-----------+
                     |  11 Detectors (parallel dict) |
                     +-----------+-----------+
                                 |
                     StrategyClassifier.classify()
                                 |
                         ClassificationResult
                     {strategy_name, tier, direction, confidence}
                                 |
                     STRATEGY_REGISTRY[name]()
                          .detect_signal()
                          .generate_entry()
                          .generate_stop()
                          .generate_exit()
                                 |
                         _levels_valid()
                                 |
                    PRO_STRATEGY_SIGNAL (non-durable)
                                 |
                      ProStrategyRouter (priority=1)
                                 |
                   RiskAdapter.validate_and_emit()
                      [9 sequential checks]
                                 |
                      ORDER_REQ (durable=True)
                                 |
                           AlpacaBroker
```

---

## 10. File Map

```
pro_setups/
  engine.py                          # ProSetupEngine -- BAR subscriber, orchestrator
  __init__.py
  detectors/
    __init__.py                      # Re-exports all 11 detectors + DetectorSignal
    _compute.py                      # Shared indicators: ATR, VWAP, RSI, EMA, BB, Fib, pivots
    base.py                          # BaseDetector (ABC) + DetectorSignal dataclass
    trend_detector.py                # EMA alignment + HH/HL structure
    vwap_detector.py                 # VWAP position + reclaim
    sr_detector.py                   # Pivot S/R + flip detection
    orb_detector.py                  # Opening Range Breakout (15-bar OR)
    inside_bar_detector.py           # Harami pattern + tightness
    gap_detector.py                  # Gap-up/down, unfilled gap-and-go
    flag_detector.py                 # Bull/bear flag: pole + consolidation + breakout
    liquidity_detector.py            # Stop-hunt sweep + reversal
    volatility_detector.py           # Bollinger Band squeeze + breakout
    fib_detector.py                  # Fibonacci retracement proximity
    momentum_detector.py             # Volume thrust + price expansion
  classifiers/
    __init__.py
    strategy_classifier.py           # Priority-ordered rule matcher (T3 -> T2 -> T1)
  strategies/
    __init__.py                      # STRATEGY_REGISTRY dict (11 entries)
    base.py                          # BaseProStrategy (ABC) + S/R pivot stop generator
    tier1/
      vwap_reclaim.py                # T4 SignalAnalyzer adapter
      trend_pullback.py              # EMA pullback + bullish bar
      sr_flip.py                     # Support/resistance flip
    tier2/
      gap_and_go.py                  # Unfilled gap continuation
      inside_bar.py                  # Mother bar breakout
      orb.py                         # Opening range breakout (volume-confirmed)
      flag_pennant.py                # Flag/pennant channel breakout
    tier3/
      momentum_ignition.py           # Volume thrust + price expansion
      fib_confluence.py              # Fibonacci + multi-signal confluence
      bollinger_squeeze.py           # BB squeeze breakout
      liquidity_sweep.py             # Smart-money sweep reversal
  router/
    __init__.py
    strategy_router.py               # PRO_STRATEGY_SIGNAL -> RiskAdapter bridge
  risk/
    __init__.py
    risk_adapter.py                  # 9-check risk gate + position sizing + ORDER_REQ emit
```

---

## 11. Key Design Decisions

1. **Non-invasive integration.** ProSetupEngine subscribes to BAR at priority=2.
   It emits ORDER_REQ through the same durable channel as T4. AlpacaBroker
   does not distinguish between sources.

2. **Template method pattern for detectors.** `BaseDetector.detect()` handles
   MIN_BARS guard and exception absorption. Subclasses only implement `_detect()`.

3. **First-match classifier.** T3 rules are checked before T2 before T1.
   This ensures the most selective, highest-R:R setups are preferred when
   multiple detectors fire simultaneously.

4. **Structure-aware stops.** The base stop generator scans full-day price
   action for real support levels (pivot clustering + touch/bounce scoring)
   before falling back to ATR. Individual strategies can override with
   pattern-specific stops (e.g., mother bar low for InsideBar).

5. **Cross-layer position dedup.** The `position_registry` prevents the same
   ticker from being held by both pro_setups and the T4 VWAP Reclaim layer.
   The registry lock is acquired at check 0 and released on any subsequent failure.

6. **Reason tagging.** Every ORDER_REQ carries `reason = "pro:{name}:T{tier}:long"`
   which flows through FILL events for attribution, P&L tracking, and the
   RiskAdapter's own state management (`_on_fill` filters on `pro:` prefix).

7. **EOD gate.** No new signals after 15:45 ET. Prevents late-day entries
   with insufficient time for targets to work.

8. **VWAPReclaim adapter.** Rather than duplicating the T4 signal logic,
   the Tier-1 VWAPReclaim strategy delegates to the existing `SignalAnalyzer`
   and wraps the result in the BaseProStrategy interface.
