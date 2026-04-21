# V8 Future Enhancement Roadmap

## Current State: Data → Money Efficiency = 4/10

### What We Do vs What Hedge Funds Do

| Step | What HFs Do | Our Status | Score |
|------|-------------|------------|-------|
| 1. SELECT tickers | Pre-market news/social scanning | ✓ Working (7 sources) | 7/10 |
| 2. DECIDE direction | Long + short based on all signals | Long only, ignore direction votes | 3/10 |
| 3. CHOOSE strategy | Data-driven (earnings→straddle, squeeze→momentum) | Ranking suggests but nothing reads it | 2/10 |
| 4. SIZE positions | Conviction-weighted | ✓ Just added (0.5-2.0x multiplier) | 5/10 |
| 5. TIME entries | Breaking news → immediate, otherwise wait for setup | Urgency flag exists but not acted on | 2/10 |
| 6. SET stops/targets | From analyst targets, prev-day levels, UOF strikes | ATR-based only — data ignored | 1/10 |
| 7. ROTATE sectors | Fed/macro → sector preferences | Zero | 0/10 |
| 8. HEDGE portfolio | Puts, VIX calls, sector balance | Zero | 0/10 |

---

## Priority 1: Wire Rankings INTO Trade Decisions (4/10 → 7/10)

### A. Stop/Target from Data (Step 6)
Currently stops are purely ATR-based. Data should inform levels:
- **Finviz analyst target** → use as resistance/target level
- **Polygon prev-day close/VWAP** → today's support/resistance
- **UOF direction agrees** → widen target (let institutional flow run)
- **High VIX** → wider stops (more volatile environment)
- **Prev-day high/low** → feed into Pro's ORB detector as key levels

### B. Strategy Selection Actually Used
TickerRankingEngine outputs `strategy` but nothing reads it:
- `strategy='earnings_play'` → Options should pick straddle, not condor
- `strategy='squeeze'` → Pro should use momentum_ignition detector
- `strategy='mean_reversion'` → VWAP reclaim is the right strategy
- `strategy='swing'` → Pro T2/T3 with wider stops

### C. Entry Timing from Urgency
TickerRankingEngine outputs `entry_urgency` but no different behavior:
- `immediate` → skip cooldown, enter on next BAR if any setup detected
- `wait_for_setup` → require full technical confirmation
- `watch_only` → add to universe but don't enter until conviction rises

### D. Sector Rotation (Step 7)
Map FRED macro regime to sector preferences:
- **Fed hiking** → avoid utilities/REIT, favor banks/energy
- **Yield curve inverted** → favor defensive (healthcare, staples, utilities)
- **VIX > 30** → reduce growth/tech exposure, favor low-beta
- **Fed cutting** → favor growth/tech, avoid defensive
- Implementation: filter ticker universe by preferred sectors based on regime

### E. Portfolio Hedging (Step 8)
- **F&G > 80** → buy SPY puts (extreme greed protection)
- **Portfolio delta > threshold** → add VIX calls
- **Sector concentration > 40%** → stop adding to that sector
- **Intraday drawdown > -$2K** → reduce all new entries by 50%

---

## Priority 2: Upgrade Individual Source Usage

### Benzinga: B- → A
- Classify headlines by TYPE using keyword matching:
  - "FDA approved/cleared" → `catalyst='fda'` (binary event, trade options)
  - "upgraded/downgraded by" → `catalyst='upgrade'` (pullback entry)
  - "beats/misses estimates" → `catalyst='earnings'` (momentum play)
  - "CEO/CFO resigns" → `catalyst='negative'` (avoid)
  - "acquires/merger/buyout" → `catalyst='ma'` (gap and hold)
- Breaking news (>5 headlines in <30 min) → immediate entry trigger
- Collect 24h window properly to compute proper sentiment DELTA
- Detect sector-wide event: if >3 tickers in same sector have headlines → sector move, not ticker-specific

### StockTwits: B- → A
- **Sentiment-Price Divergence** (the strongest retail contrarian signal):
  - Social bearish + price RISING = smart money accumulating, retail scared → STRONG BUY
  - Social bullish + price FALLING = retail bag-holding, smart money exiting → STRONG SELL
  - Requires comparing current price trend with social sentiment direction
- Track sentiment CHANGE over hours (rising bullish differs from stable bullish)
- Velocity spike detection: 10x normal mentions = potential catalyst happening NOW

### SEC EDGAR: D+ → B
- Parse filing TYPE (not just count):
  - **Form 4** (insider trade): extract buy/sell, shares, dollar amount, insider title
  - **13F** (institutional quarterly): track position changes by major funds
  - **SC 13D** (>5% stake): activist investor building position → STRONG signal
  - **8-K** (material event): corporate event disclosure
- CEO/CFO buying > $1M of own stock = highest conviction long signal in finance
- Distinguish open-market buys from stock option exercises (very different signals)

### UOF: B+ → A
- Match sweep EXPIRY to upcoming catalysts:
  - Sweeps expiring before earnings → directional bet on earnings
  - Sweeps with no catalyst → less meaningful, might be hedging
- Distinguish OTM speculation (small premium, big potential) from ATM hedging (large premium, protective)
- Track multi-day flow accumulation (3 days of call sweeps = institutional accumulation)
- Connect to stop/target: when institutions agree with our direction, widen target (asymmetric risk)

### Polygon: C → B
- Feed prev-day OHLCV as reference levels into Pro detectors
- Gap analysis: gap up + held above gap low = bullish continuation
- Compute `volume_vs_average` field (currently missing)
- Multi-day pattern: 3 consecutive gap-ups = potential exhaustion

### Yahoo: C → B
- Wire earnings proximity to Options engine strategy selector
- Use `earnings_before_open` flag to time entries (pre-market vs open)
- Track earnings surprise history: serial beaters = directional bet
- Consume analyst consensus data for upside/downside percentages

### F&G: C+ → B
- Track F&G TREND (weekly delta):
  - Dropping 70→30 = active sell-off, tighten ALL stops
  - Rising 20→50 = recovery starting, go aggressive on beaten-down names
- Map extreme levels to stop width:
  - F&G < 25 → widen stops 30% (more volatile, need room)
  - F&G > 75 → tighten stops 20% (protect gains before reversal)

### FRED: D → C+
- VIX regime → stop width adjustment (VIX > 30 = double stops)
- Rate trend → sector preferences (hiking = banks, cutting = tech)
- Drop unemployment/CPI collection (fetched but never used)

---

## Priority 3: Enable Short Selling

Pro generated 12,716 short signals on 2026-04-16 (69.6% of all signals). ALL were discarded because `direction != 'long'` → blocked.

Enabling shorts requires:
- Short execution wiring in AlpacaBroker + TradierBroker
- Inverse position management (negative qty, inverted P&L)
- Margin/locate checks before shorting
- Different stop logic (stop ABOVE entry for shorts)
- Hard-to-borrow fee checks
- Short squeeze risk detection (existing in TickerRankingEngine)

**Risk:** Shorting has unlimited loss potential. Requires strong risk gates.
**Alpha potential:** Significant — Pro's short signals are based on the same 13 detectors as longs.

---

## Priority 4: Post-V8 Deployment Monitoring

### Runtime Checks (after first V8 trading session)
Run `python scripts/v8_post_run_check.py` after market close.

Items that can't be verified without live market:
- EventBus handler exception propagation across priorities
- RVOL dedup timestamp precision (compare values for same ticker in Pro vs VWAP signals)
- Trailing stop R-level calibration (collect [Trail] log data for 2-3 weeks)
- SentimentDetector weight calibration (correlate composite score with P&L after 1 month)
- Quick filter false negatives at 500 tickers (run full analysis for 1 week, compare)
- Alpaca batch 200-symbol limit (check if partial data returned)
- Sentiment deferral timing gap (10:00-10:22 AM — do VWAP entries appear?)
- Buying power cache correctness (compare broker rejections to PRG approvals)

### API Rate Limit Monitoring
- Benzinga: 50/day free — using ~45 (tight, consider paid tier if discovery valuable)
- StockTwits: 200/hr — using ~30/hr (comfortable)
- Polygon: 5/min — using ~0.5/min (very comfortable)
- Alpha Vantage: 25/day — currently disabled (0 usage)

---

## Priority 4b: Items Deferred to "See How System Performs Tomorrow"

These were identified during the 9-layer audit but deferred to see real-world behavior first:

### Trailing Stop R-Level Optimization
- Current: breakeven at +1R, lock +1R at +2R (T3), lock +0.5R at +2R (T2)
- Monitor: are we getting trailed out too early? Or letting winners give back gains?
- Data: collect `[Trail]` log entries for 2-3 weeks, analyze R distribution
- Then: adjust thresholds based on actual trade outcomes

### SentimentDetector Weight Calibration
- Current weights: UOF 0.25, Benzinga 0.20, StockTwits 0.15, etc.
- After 1 month: correlate composite sentiment score at entry with trade P&L
- If UOF-heavy signals correlate better → increase UOF weight
- If social signals are noise → decrease StockTwits weight

### Quick Filter False Negatives (Option D Tiered Scanning)
- Currently: quick filter at 500 tickers skips ~60% as "cold"
- Risk: profitable setups in cold tickers are missed
- Test: run full 13-detector analysis on ALL tickers for 1 week, compare
- Target: false negative rate < 5%

### EventBus Priority + Handler Exception Interaction
- Question: if PortfolioRiskGate (priority=3) throws, does SmartRouter (priority=1) still run?
- Check: search logs for `handler.*exception` on ORDER_REQ events after first session
- Risk: ORDER_REQ could bypass risk gate

### Buying Power Cache Timing
- Cache window: 60 seconds
- Risk: between cached check and broker execution, buying power drops
- Monitor: compare ORDER_REQ approved count vs broker rejections for "insufficient buying power"
- If > 5% rejection rate → reduce cache window to 30s

### Alpaca Batch Symbol Limit
- Undocumented limit: ~200 symbols per batch request
- If using Alpaca as data source at 300+ tickers, may need chunking
- Monitor: check `fetched bars for X/Y` — if X consistently < Y, chunking needed

### options_greeks.json Corruption
- All 3 copies were corrupt today (20+ SafeState ERROR logs)
- Root cause unknown — Options engine never ran properly
- Monitor: if errors continue tomorrow, investigate writer (Options engine)

### Sell Storm Prevention
- V8 added phantom cleanup (ORDER_FAIL → remove position)
- V7.2 added background heartbeat (prevents false SIGKILL)
- Monitor: do sells still queue up sequentially? If so, consider async sell execution

### Cross-Engine Sector Concentration
- Added shared_positions to RiskAdapter for cross-engine sector counting
- Monitor: are we still getting 4+ positions in same sector?
- If yes: the sector mapping may be incomplete

## Priority 5: ML/Optimization (After 1 Month of Data)

- Train conviction score weights on historical trade P&L
- Optimize trailing stop R-level thresholds per tier
- Backtest sector rotation against historical FRED data
- A/B test contrarian vs momentum social signals
- Feature importance analysis: which source contributes most to profitable trades
- Calibrate F&G multiplier on real equity curve data

---

## Source Efficiency Grades (Current → Target)

| Source | Current | Target | Key Upgrade |
|--------|---------|--------|-------------|
| UOF | B+ | A | Match flow to catalysts, set asymmetric risk |
| Benzinga | B- | A | News TYPE classification, immediate triggers |
| StockTwits | B- | A | Sentiment-price divergence (contrarian) |
| F&G | C+ | B | Trend tracking, stop width adjustment |
| Polygon | C | B | Prev-day levels as today's S/R |
| Yahoo | C | B | Wire to Options strategy selection |
| Finviz | C- | B | Analyst target as price level, squeeze detection |
| SEC EDGAR | D+ | B | Parse filing types, insider buy/sell |
| FRED | D | C+ | VIX→stops, rate→sectors |
| Alpha Vantage | F | N/A | Disabled (no consumer) |
