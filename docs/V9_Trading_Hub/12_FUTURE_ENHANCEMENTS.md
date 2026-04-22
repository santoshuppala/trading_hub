# V9 Future Enhancements — Complete Roadmap

## Current State: V9 (Data→Money Efficiency: 5/10)

Steps 1-5 functional. Steps 6-10 are where real alpha lives.
Profit Factor: 1.06 → Target: 1.50+

---

## Priority 1: Profitability Filter Layer (5/10 → 7/10)

### Overview

A hedge-fund-style filter cascade between signal generation and order execution.
Reduces trades from ~20K/month to ~5K/month while improving PF from 1.06 to ~1.50.

### Architecture

```
CURRENT:  BAR → Detectors → Classifier → PRO_STRATEGY_SIGNAL → RiskAdapter → ORDER_REQ

NEW:      BAR → Detectors → Classifier → [PROFITABILITY FILTER] → PRO_STRATEGY_SIGNAL → RiskAdapter → ORDER_REQ
                                              │
                                              ├─ Filter 1: Time-of-Day (ns)
                                              ├─ Filter 2: Regime (μs)
                                              ├─ Filter 3: Quality Score (μs)
                                              ├─ Filter 4: Historical Edge (μs)
                                              └─ Filter 5: Confluence (μs)
```

Same for VWAP path: filter sits between entry checks and SIGNAL emission.

### Filter 1: Time-of-Day

| Window | Allowed | Blocked |
|--------|---------|---------|
| 9:30-9:45 | ORB, gap_and_go only | All others |
| 9:45-10:30 | All strategies | — |
| 10:30-11:30 | All strategies | — |
| 11:30-13:00 | All except momentum | Momentum (lunch lull) |
| 13:00-15:00 | All strategies | — |
| 15:00-15:45 | No new entries | All new entries |
| 15:45-16:00 | No entries | All |

Per-strategy overrides from historical data: if `orb` has PF < 1.0 after 11 AM historically, block it after 11 AM.

Data: precomputed lookup from trade history, updated nightly.

### Filter 2: Market Regime

| Regime | Detection | Strategies Allowed |
|--------|-----------|-------------------|
| BULL_TREND | SPY > 20 EMA + above VWAP | All tiers |
| RANGE_BOUND | SPY between S/R, ADX < 20 | T1 + T2 only |
| BEAR_TREND | SPY < 20 EMA + below VWAP | T1 long only, all short |
| VOLATILE | VIX > 25 or regime score < -0.4 | T1 only, reduced size |

Uses existing `MarketRegime.score()` (cached 30s) + SPY VWAP bias.

### Filter 3: Quality Score

```python
quality = (
    0.35 * confidence +                      # classifier confidence
    0.20 * min(rvol / 3.0, 1.0) +           # volume confirmation
    0.15 * rsi_score +                        # RSI in sweet spot (50-70 for longs)
    0.15 * atr_score +                        # ATR/price 0.5%-3%
    0.15 * detector_agreement                 # % detectors agreeing on direction
)
```

Minimum quality by tier: T1=0.45, T2=0.55, T3=0.65, VWAP=0.50.

### Filter 4: Historical Edge

Rolling 60-day PF lookup per (strategy, regime) combination:
- Strategy-regime PF >= 1.1 AND n_trades >= 15 → PASS
- Ticker-strategy PF < 0.9 AND n_trades >= 10 → BLOCK
- Insufficient data → DEFAULT PASS (fail-open)

Data: aggregated from ml_trade_outcomes, refreshed every 30 min by background thread.

### Filter 5: Confluence

Weighted sum of supporting detectors that agree on direction:

```python
weights = {
    'trend': 1.0, 'vwap': 0.8, 'momentum': 0.7, 'sr': 0.6,
    'liquidity': 0.5, 'fib': 0.5, 'orb': 0.5,
    'volatility': 0.4, 'flag': 0.4, 'gap': 0.4,
    'inside_bar': 0.3, 'sentiment': 0.3, 'news_velocity': 0.3,
}
```

Min confluence: T1=0.3, T2=0.6, T3=1.0.

### New File Structure

```
profitability_filter/
    __init__.py              # ProfitabilityFilter main class
    filters/
        base.py              # BaseFilter ABC + FilterResult + FilterContext
        time_of_day.py
        regime.py
        quality.py
        historical_edge.py
        confluence.py
    edge_store.py            # EdgeRefresher + in-memory cache
    config.py                # FilterConfig
    shadow_logger.py         # Shadow-mode logging
```

### Existing File Changes

| File | Change |
|------|--------|
| `pro_setups/engine.py` | Insert `filter.evaluate_pro()` after classification (~10 lines) |
| `monitor/strategy_engine.py` | Insert `filter.evaluate_vwap()` after entry checks (~10 lines) |
| `config.py` | Add `PF_*` configuration constants (~20 lines) |
| `scripts/run_core.py` | Pass filter to engines at startup (~5 lines) |

### Deployment

```
Week 1-2: shadow mode  → all trades execute, filter decisions logged
Week 3:   enforce mode → filter actively blocks low-quality signals
Week 4+:  tune thresholds from live data
```

### Expected Impact

| Metric | Before | After |
|--------|:---:|:---:|
| Trades/month | ~20,000 | ~5,000 |
| Win rate | 38.7% | ~48% |
| Profit factor | 1.06 | ~1.50 |
| Daily P&L ($1K budget) | $17 | ~$45 |

---

## Priority 2: Wire Rankings INTO Trade Decisions (7/10 → 8/10)

### Stop/Target from External Data (Step 6)

| Data Source | How to Use | Impact |
|-------------|-----------|--------|
| Finviz analyst target_price | Use as resistance level for target placement | Better targets |
| Polygon prev-day H/L/VWAP | Today's support/resistance levels | Smarter stops |
| UOF institutional flow | Institutions agree with direction → widen target | Bigger wins |
| VIX level | High VIX → wider stops (currently fixed ATR-based) | Fewer stop-outs |
| Prev-day high/low | Feed into ORB detector as key levels | Better ORB entries |

### Strategy Selection from Rankings

The TickerRankingEngine outputs `strategy='earnings_play'` and `entry_urgency='immediate'` but nothing reads these.

Wire into:
- `ranking.strategy → StrategyEngine`: momentum ticker → use momentum_ignition, swing → use sr_flip
- `ranking.catalyst → OptionsEngine`: earnings → straddle not condor, squeeze → directional spread
- `ranking.urgency → RiskAdapter`: immediate → skip cooldown, wait → require full confirmation

### Entry Timing from Urgency

- Immediate: skip cooldown, enter on next BAR regardless of setup quality
- Wait: require full technical confirmation before entry
- Currently: all signals treated equally regardless of urgency

---

## Priority 3: Data Source Upgrades (8/10 → 8.5/10)

### Benzinga: B- → A
- Classify headlines by TYPE (FDA approval, analyst upgrade, M&A, routine PR)
- Breaking news → immediate entry trigger (not just ticker discovery)
- Collect 24h window properly (currently only 1h)
- Detect sector-wide vs ticker-specific catalysts

### StockTwits: B- → A
- Track sentiment-price DIVERGENCE (strongest alt-data signal):
  - Social bearish + price rising = smart money accumulating → BUY
  - Social bullish + price falling = retail bag-holding → SELL
- Track sentiment CHANGE over hours (trend, not snapshot)

### SEC EDGAR: D+ → B
- Parse Form 4 for CEO/CFO buys (strongest insider signal)
- Track 13F institutional accumulation (follow smart money)
- SC13D detection (activist building stake)
- Distinguish insider buys from option exercises

### UOF Institutional Flow: B+ → A
- Match sweep expiry to upcoming catalysts (earnings date)
- Distinguish OTM speculation from ATM hedging
- Track multi-day flow accumulation
- Connect to stop/target: institutions agree = wider target

### Polygon: C → B
- Feed prev-day OHLCV into Pro detectors as reference levels
- Gap + hold analysis (gap up + held above = continuation)
- Compute volume_vs_avg (currently collected but not computed)

### Yahoo Finance: C → B
- Wire earnings calendar to Options strategy selector
- Use before/after open flag for entry timing
- Track earnings surprise history for serial beaters
- Consume analyst consensus data (collected but unused)

### Fear & Greed: C+ → B
- Track F&G TREND (dropping vs stable vs rising) not just level
- F&G weekly delta for regime transition detection
- Map to stop width: extreme fear = wider stops

### FRED Macro: D → C+
- VIX regime → stop width multiplier (>30 = double stops)
- Drop unused unemployment/CPI collection (noise)
- Rate trend → sector preferences in ticker universe

---

## Priority 4: Sector Rotation (Step 7)

Map macro regime to sector allocation:

| Macro Signal | Favor | Avoid |
|-------------|-------|-------|
| Fed hiking | Banks (XLF), Energy (XLE) | Utilities (XLU), REITs (XLRE) |
| Yield curve inverted | Healthcare (XLV), Staples (XLP) | Growth tech, speculative |
| VIX > 30 | Low-beta, defensive | High-beta growth |
| Dollar strengthening | Domestic small-cap | International (BABA, JD, PDD) |
| Oil rising | Energy producers | Airlines, transport |

Implementation: weight ticker universe by sector preference. Don't hard-block, but reduce allocation to disfavored sectors by 50-70%.

---

## Priority 5: Portfolio Hedging (Step 8)

| Trigger | Action | Purpose |
|---------|--------|---------|
| F&G > 80 (extreme greed) | Buy SPY puts (2% of portfolio) | Crash protection |
| Portfolio delta > threshold | Buy VIX calls | Volatility hedge |
| Single sector > 40% | Reduce exposure | Concentration risk |
| 3+ consecutive loss days | Reduce position sizes 50% | Drawdown control |
| Kill switch within 20% of limit | Auto-reduce new entries | Pre-emptive protection |

---

## Priority 6: Enable Short Selling

Pro generates **69.6% short signals** that are all discarded today.

### Requirements
- Short execution wiring in AlpacaBroker and TradierBroker
- Inverse position management (stop above entry, target below)
- Margin/borrow availability check before entry
- Short-specific risk limits (separate max positions, separate kill switch)
- Uptick rule compliance check

### Risk
Biggest single alpha unlock but also biggest risk. Short squeezes, unlimited loss potential, margin calls. Start with only T1 shorts (tight stops) on high-liquidity names.

---

## Priority 7: ML / Optimization (9/10 → 9.5/10)

### Conviction Weight Training
- **Data needed:** 1 month of live P&L per strategy
- **Method:** Logistic regression on (detector_strengths, rvol, rsi, atr, regime) → trade_profitable
- **Output:** Learned quality score weights (replace hand-tuned 0.35/0.20/0.15/0.15/0.15)
- **Expected impact:** +10-15% signal quality

### Optimal R-Level for Trailing Stops
- **Data needed:** 2 months of trade data with R-levels at each exit point
- **Method:** Grid search over R-level thresholds per strategy
- **Output:** Per-strategy optimal partial exit R and full exit R
- **Expected impact:** Fewer premature exits, larger winners

### Sector Rotation Backtesting
- **Data needed:** 2+ years of FRED + sector ETF data
- **Method:** Backtest macro regime → sector weight rules against historical returns
- **Output:** Validated rotation rules with confidence intervals

### Signal Quality Scoring
- **Data needed:** Correlation of detector strength values vs trade P&L
- **Method:** Random forest feature importance on all detector/indicator features
- **Output:** Drop low-importance detectors, upweight high-importance ones

### Entry Timing Model
- **Data needed:** Intraday P&L by entry time + volatility regime
- **Method:** Per-strategy optimal entry window from historical data
- **Output:** Dynamic time-of-day filter (replaces static windows)

---

## Priority 8: Scaling (9.5/10 → Production)

### Live Trading (Real Money)
- Graduate from paper after 3+ months consistently profitable
- Start with 10% of target capital
- Scale up 25% per month if PF stays > 1.3
- Automated position sizing from Kelly criterion

### Multi-Account
- Separate accounts for VWAP vs Pro vs Options
- Dedicated margin for each strategy type
- Independent kill switches and risk limits

### Cloud Deployment
- Move from local Mac to AWS/GCP for 99.9% uptime
- EC2/GCE with auto-restart on failure
- CloudWatch/Stackdriver for monitoring (replaces local watchdog)
- RDS for TimescaleDB (managed, backed up)

### IBKR Integration
- BrokerRegistry makes this plug-and-play
- Register IBKR broker → SmartRouter routes automatically
- Better margin rates, more instruments, global markets

### Crypto Extension
- Same detector architecture, different data source
- Coinbase/Binance WebSocket for price data
- 24/7 trading (no market hours restriction)
- Higher volatility = wider stops, more frequent signals

### Options Intelligence
- Real-time IV surface modeling (not static IV rank)
- Greeks-based position management (delta-neutral adjustments)
- Earnings straddle timing from historical IV crush patterns
- Calendar spread detection from term structure

---

## Priority 9: Backtest Improvements

### Performance Optimization
- Current: 30 tickers × 33 days = ~90 min (too slow for 183 tickers)
- Target: 183 tickers × 40 days in <15 min
- Methods: batch detector calls, vectorized indicators, reduce per-bar logging
- Consider: Rust/C extension for hot path (detector computation)

### VWAP Exit Wiring
- Current gap: VWAP SELL signals (SELL_STOP, SELL_RSI, SELL_VWAP) not routed to FillSimulator
- Fix: StrategyEngine SELL signals → close positions in FillSimulator
- Impact: Accurate VWAP P&L in backtests

### Walk-Forward Testing
- Current: single backtest period
- Need: rolling 4-week train / 1-week test windows
- Validates that strategy parameters aren't overfit to specific period

### Monte Carlo Simulation
- Shuffle trade order 1000x, compute PF distribution
- Answers: "Is PF 1.06 statistically significant or just luck?"
- If 95th percentile PF < 1.0, the edge isn't real

### Slippage Model
- Current: fixed 2bp spread factor
- Need: volume-dependent slippage (low liquidity = more slippage)
- Need: market impact for larger position sizes

---

## Priority 10: Monitoring & Observability

### Real-Time Dashboard
- Current: Streamlit backtest dashboard (post-hoc analysis)
- Need: Live Grafana dashboard showing:
  - Real-time P&L curve
  - Open positions with unrealized P&L
  - Signal rate per detector
  - Filter pass/block rates
  - Latency percentiles (data fetch, signal generation, order execution)

### Trade Journal
- Automated daily trade journal with:
  - Each trade: why entered, what detectors fired, quality score, outcome
  - Best/worst trades with charts
  - Strategy performance summary
  - Lessons (patterns in losses)

### API Rate Limit Monitoring
| Source | Limit | Current Usage | Risk |
|--------|-------|---------------|------|
| Tradier | 120/min | ~60/min | Low |
| Benzinga | 50/day free | ~45/day | High — upgrade if discovery proves valuable |
| StockTwits | 200/hr | ~30/hr | Low |
| Polygon | 5/min | ~0.5/min | Low |
| Yahoo | Unofficial | ~20/day | Medium — could be blocked |

---

## Implementation Timeline

```
WEEK 1-2:   Profitability Filter (Quality + Confluence + Time-of-Day)
WEEK 3-4:   Profitability Filter (Regime + Historical Edge + shadow mode)
MONTH 2:    Wire rankings into strategy selection
MONTH 2:    Enable short selling (T1 only)
MONTH 3:    Sector rotation from FRED macro
MONTH 3:    Data source upgrades (Benzinga classify, StockTwits divergence)
MONTH 4-6:  ML conviction weights from accumulated live data
MONTH 6+:   Scale to real money (if PF > 1.3 for 3+ months)
```

---

## How We Track Progress

| Milestone | Metric | Target |
|-----------|--------|--------|
| Filter shadow mode | Blocked trades PF < 1.0 | Validates filter removes losers |
| Filter enforce mode | Overall PF | > 1.30 |
| Rankings wired | Strategy match rate | > 60% match ranking.strategy |
| Short selling | Short trade PF | > 1.2 |
| ML weights | Quality score correlation with P&L | r > 0.3 |
| Real money ready | 3-month rolling PF | > 1.3, max DD < 10% |
