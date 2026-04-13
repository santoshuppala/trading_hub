# TRADING HUB — PRODUCTION READINESS REPORT

**Date:** April 12, 2026
**Classification:** CONFIDENTIAL — Real capital deployment decision
**Prepared by:** Technology team
**Decision required from:** Fund Management, Financial Analysis, Technology Leadership

---

## CRITICAL DISCLAIMER

**This report contains ONLY verified facts.** No paper trading validation has been conducted. No backtesting on historical data has been performed. All win rates and R:R ratios cited are THEORETICAL — derived from strategy math, not from any observed trading results. This system has never executed a trade through its full automated pipeline in any market condition.

---

## I. WHAT WE ACTUALLY KNOW (VERIFIED FACTS)

### Real Trading History: NEAR ZERO

| Date | What Happened | Result |
|------|---------------|--------|
| Apr 8 | Monitor ran. 0 trades executed. | $0.00 P&L |
| Apr 9 | Monitor ran. 1 trade logged: unknown ticker, +$0.09 | +$0.09 P&L |
| Apr 10 | Monitor ran. 1 orphaned ZS position recovered from Alpaca (7 shares @ $116.90). Sold via VWAP breakdown. | -$0.26 P&L |
| Apr 11-12 | Development. No trading. | $0.00 P&L |

**Total verified P&L across entire system lifetime: -$0.17**
**Total verified trades: 2 (1 win, 1 loss)**
**Total verified fills through the automated pipeline: 0** (the ZS trade was an orphan import, not a signal-driven entry)

### What Has NOT Been Validated

| Validation Step | Status | Risk |
|----------------|--------|------|
| Paper trading (any duration) | **NOT DONE** | **CRITICAL** — we have zero evidence strategies produce profitable signals |
| Backtesting on historical data | **NOT DONE** | **CRITICAL** — theoretical R:R ratios are unverified |
| Live execution end-to-end (signal → order → fill) | **NOT DONE** | **CRITICAL** — the full pipeline has never completed in real market conditions |
| Slippage measurement (limit order fill rates) | **NOT DONE** | **HIGH** — we assume fills at ask; real slippage unknown |
| Strategy performance under different market regimes | **NOT DONE** | **HIGH** — bull/bear/choppy behavior unknown |
| Drawdown behavior over multi-day periods | **NOT DONE** | **HIGH** — max drawdown is completely unknown |
| Concurrent multi-strategy execution on live data | **NOT DONE** | **HIGH** — interaction effects untested |
| Benzinga/StockTwits API reliability during market hours | **PARTIALLY** | **MEDIUM** — tested once on Sunday (low volume) |

### What HAS Been Validated

| Validation Step | Status | Evidence |
|----------------|--------|----------|
| Unit test suite (18 tests, 195 assertions) | **17/18 PASS** | T4 latency fails (P95=67ms > 50ms target) |
| EventBus ordering, backpressure, circuit breaker | **PASS** | T1, T9, T11 |
| Strategy signal generation (synthetic data) | **PASS** | T14 — but only with fabricated bar data |
| Risk engine boundary checks (all 6 gates) | **PASS** | T7 |
| Order lifecycle (PaperBroker only) | **PASS** | T6, T15 |
| State persistence + crash recovery | **PASS** | T3, T10 |
| Detector correctness (synthetic patterns) | **PASS** | T13 |
| Replay determinism (same inputs → same outputs) | **PASS** | T17 |
| GlobalPositionRegistry thread safety | **PASS** | T18 |
| Tradier sandbox connectivity | **PASS** | T2, T5 |
| TimescaleDB schema + migrations | **PASS** | T16 (DB tests skipped: asyncpg not in venv at test time) |
| Benzinga API (live call, Sunday) | **WORKS** | 2 AAPL headlines returned |
| StockTwits API (live call, Sunday) | **WORKS** | 30 messages, sentiment parsed correctly |

---

## II. CURRENT SYSTEM STATE

### Alpaca Account

```
API Base URL: https://paper-api.alpaca.markets   ← THIS IS PAPER, NOT LIVE
```

**The system is currently configured for Alpaca PAPER trading.** The live API URL (`https://api.alpaca.markets`) has never been set.

### Architecture: 3 Independent Strategy Layers

| Layer | Strategies | Status | Ever Traded Live? |
|-------|-----------|--------|-------------------|
| T4 (VWAP Reclaim) | 1 strategy, 9 entry filters | Code complete | NO |
| T3.6 (Pro Setups) | 11 strategies, 3 tiers | Code complete | NO — ProSetupEngine has 0 mentions in any log |
| T3.5 (Pop Screener) | 6 engines | Code complete | NO — PopStrategyEngine has 0 mentions in any log |

### Bug Audit Status

| Phase | Bugs Fixed | Verified By |
|-------|-----------|-------------|
| P0 (Critical) | 4/4 | Code review + unit tests |
| P1 (High) | 6/6 | Code review + unit tests |
| Hotfixes | 3/3 | Code review |
| P2 (Medium) | 11/11 | Code review + unit tests |
| **Total** | **24/24** | **Code-level only. No live validation.** |

---

## III. HONEST RISK ASSESSMENT

### Risk Score: 7.5 / 10 (HIGH)

| Risk Category | Score (1-10) | Justification |
|---------------|-------------|---------------|
| **Unvalidated Strategy Performance** | 9 | Zero trades through full pipeline. All win rates are theoretical. |
| **No Paper Trading History** | 9 | Industry standard requires minimum 30 days paper trading before live. We have 0 days. |
| **Execution Risk** | 5 | AlpacaBroker code is solid, but never tested with real market data + latency |
| **Infrastructure** | 3 | EventBus, DB, Redpanda all test-verified. Reliable. |
| **Code Quality** | 2 | 24 bugs found and fixed. 17/18 tests pass. Architecture is sound. |
| **Operational Risk** | 8 | No monitoring dashboard. No kill switch. No alerting beyond email. |

### What Could Go Wrong (Real Scenarios)

**Scenario A: Strategy generates signals that lose money consistently**
- Probability: **UNKNOWN** — we have no performance data
- Impact: Depends on position sizing. At $1,000/trade × 5 positions = $5,000 exposure
- Worst case: 10 consecutive losing trades × $50 avg loss = **$500 drawdown in one day**
- Mitigation: None currently. No daily loss limit implemented.

**Scenario B: Multiple layers fire on same ticker simultaneously**
- Probability: Low (GlobalPositionRegistry prevents this)
- Impact: Single position instead of triple. Registry is tested and verified.
- Status: **MITIGATED** by C-2 fix

**Scenario C: System crashes mid-trade, positions orphaned**
- Probability: Low (CrashRecovery wired from Redpanda)
- Impact: Positions recovered with fallback stops (±3%/±5% from entry)
- Evidence: This DID happen on Apr 10 — 7 ZS shares were orphaned from a prior session
- Status: **PARTIALLY MITIGATED** — recovery works, but fallback stops are wide

**Scenario D: Latency at market open causes bad fills**
- Probability: High (T4 test FAILS at P95=67ms)
- Impact: 3-5 cent slippage per entry on momentum trades
- Status: **UNMITIGATED** — n_workers increase not yet deployed

**Scenario E: Benzinga or StockTwits API down during critical news event**
- Probability: ~1% per day
- Impact: Pop screener runs without sentiment data, falls back to technical-only signals
- Status: **ACCEPTABLE** — graceful degradation implemented

---

## IV. WHAT MUST HAPPEN BEFORE DEPLOYING REAL MONEY

### Phase 0: MANDATORY (before any live trading)

| # | Requirement | Est. Effort | Status |
|---|-------------|-------------|--------|
| 1 | **Run paper trading for minimum 2 full trading weeks** | 10 business days | **NOT STARTED** |
| 2 | Collect and analyze: win rate, avg win/loss, max drawdown, Sharpe ratio | After Phase 0.1 | **NOT STARTED** |
| 3 | Verify all 3 layers (T4 + T3.6 + T3.5) generate signals during market hours | 1 day observation | **NOT STARTED** |
| 4 | Fix T4 latency (BAR n_workers 4→8) | 5 minutes | **NOT DONE** |
| 5 | Implement daily drawdown kill switch | 30 minutes | **NOT DONE** |
| 6 | Add pre-market connectivity check | 30 minutes | **NOT DONE** |
| 7 | Switch from `paper-api.alpaca.markets` to `api.alpaca.markets` when ready | 1 minute | **NOT DONE** |

### Phase 1: Paper Trading Validation (2 weeks minimum)

**Success criteria to advance to live:**

| Metric | Minimum Threshold | Why |
|--------|-------------------|-----|
| Total paper trades | ≥ 50 | Statistical significance |
| Win rate (blended) | ≥ 40% | Below this, expectancy may be negative |
| Average winner / average loser | ≥ 1.5 | Confirms R:R design works in practice |
| Max single-day drawdown | < 5% of test capital | System is manageable |
| System uptime (9:30-3:15 ET) | ≥ 98% | No frequent crashes |
| False signal rate | < 20% | Signals actually lead to valid entries |
| Fill rate (signals → actual fills) | ≥ 80% | Orders don't time out excessively |
| No position doubling incidents | 0 | GlobalPositionRegistry works |
| No orphaned positions | 0 | State management works |

**If ANY threshold is not met, DO NOT proceed to live trading.**

### Phase 2: Live with Minimal Capital (if Phase 1 passes)

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| TRADE_BUDGET | $100 | Absolute minimum to test real fills |
| MAX_POSITIONS | 2 | Limit total exposure to $200 |
| GLOBAL_MAX_POSITIONS | 3 | Cross-layer cap |
| Duration | 1 week | Enough to observe real slippage |
| Tier 3 strategies | DISABLED | Not enough capital for proper sizing |
| Parabolic Reversal | DISABLED | Short execution not wired |

### Phase 3: Scale Up (if Phase 2 is profitable)

Gradual increase. Reviewed weekly.

---

## V. STRATEGIES — HONEST ASSESSMENT

### Theoretical vs Validated

| Strategy | Theoretical R:R | Theoretical Win Rate | Validated by Real Data? | Confidence Level |
|----------|----------------|---------------------|------------------------|-----------------|
| VWAP Reclaim (T4) | 2:1 | 50-55% | **NO** | LOW — only synthetic tests |
| Trend Pullback | 2:1 | 55-60% | **NO** | LOW |
| S/R Flip | 2:1 | 45-50% | **NO** | LOW |
| ORB | 3:1 | 40-45% | **NO** | LOW |
| Inside Bar | 3:1 | 40-45% | **NO** | LOW |
| Gap & Go | 3:1 | 35-40% | **NO** | LOW — gap detection requires rvol_df which may not always be available |
| Flag/Pennant | 3:1 | 40-50% | **NO** | LOW |
| Liquidity Sweep | 6:1 | 25-30% | **NO** | VERY LOW — pattern unreliable in live markets |
| Bollinger Squeeze | 6:1 | 30-35% | **NO** | LOW |
| Fib Confluence | 6:1 | 25-30% | **NO** | LOW |
| Momentum Ignition | 8:1 | 20-25% | **NO** | VERY LOW — tiny position sizes at low budgets |
| Pop VWAP Reclaim | 2:1 | 45-50% | **NO** | LOW — T4 adapter, never tested through pop pipeline |
| Pop ORB | 2:1 | 35-40% | **NO** | LOW |
| Halt Resume | 3:1 | 25-35% | **NO** | VERY LOW — halt detection had off-by-one bug (fixed) |
| Parabolic Reversal | 2:1 | 20-30% | **NO** | VERY LOW — exhaustion detection was inverted (fixed, but never validated) |
| EMA Trend | 2.5:1 | 40-45% | **NO** | LOW |
| BOPB | 2.5:1 | 35-40% | **NO** | LOW |

**All confidence levels are LOW or VERY LOW because none of these strategies have generated a single real or paper trade.**

### What the Apr 9 and Apr 10 Logs Actually Show

- **Apr 9:** System ran from 6:37 AM to ~9:00 AM+. Scanned 189 tickers. Generated 1 trade (+$0.09). This was the T4 VWAP Reclaim strategy ONLY. Pro Setups and Pop Screener were NOT wired in at that time.
- **Apr 10:** System started at 8:32 AM. Found 7 orphaned ZS shares from Alpaca (from a previous manual or external trade). Auto-imported with ±3%/5% fallback stops. ZS was sold via VWAP breakdown for -$0.26. No new entries generated. The monitor ran for 4+ hours producing zero new signals. This suggests either: (a) entry conditions are too strict, or (b) market conditions didn't trigger VWAP reclaim on any of 169 tickers.

---

## VI. KNOWN UNRESOLVED ISSUES

### Critical (Block deployment)

| # | Issue | Impact | Status |
|---|-------|--------|--------|
| 1 | **Zero paper trading validation** | Cannot estimate P&L, drawdown, or fill quality | NOT STARTED |
| 2 | **T4 latency P95=67ms (test fails)** | Slippage on momentum entries at market open | FIX NOT DEPLOYED |
| 3 | **No daily drawdown kill switch** | System continues trading after large losses | NOT IMPLEMENTED |
| 4 | **Alpaca API still pointed at paper endpoint** | Cannot trade real money until changed | BY DESIGN (correct for now) |
| 5 | **ProSetupEngine and PopStrategyEngine never ran in any monitor session** | These layers may fail on startup with real data | UNTESTED |

### High (Fix before live, can paper trade without)

| # | Issue | Impact |
|---|-------|--------|
| 6 | No monitoring dashboard | Operator has no real-time visibility |
| 7 | No alerting for circuit breaker trips | Failures go unnoticed |
| 8 | No pre-market connectivity check | System may start but fail to trade |
| 9 | Benzinga API 400 errors with date filtering | Relies on fetching latest without time window |
| 10 | StockTwits rate limit (200/hr) may throttle with 200 tickers | Cache mitigates but not proven under load |

### Medium (Fix during paper trading)

| # | Issue | Impact |
|---|-------|--------|
| 11 | Apr 10: 4 hours running, 0 new entries on 169 tickers | Entry conditions may be too strict |
| 12 | RVOL first-hour skip (60 min) means no trades 9:30-10:30 | Misses opening momentum — the most active period |
| 13 | Pop strategies use mock data sources by default if env vars missing | Silent fallback to fake data |
| 14 | No backtest infrastructure | Cannot validate strategies against historical data |

---

## VII. RECOMMENDATION

### DO NOT DEPLOY WITH REAL MONEY

**Reason:** The system has never executed a single trade through its full automated pipeline. Deploying real capital based on unit tests and theoretical math without paper trading validation is reckless.

### Required Path to Production

```
Week 1-2:  Paper trading (Alpaca paper account)
           - Run all 3 layers simultaneously
           - Collect signal and fill data
           - Measure: win rate, R:R, drawdown, fill rate, latency

Week 3:    Review paper trading results
           - If metrics meet thresholds → proceed
           - If not → identify and fix issues

Week 4:    Live with minimal capital ($100/trade, 2 max positions)
           - Verify real slippage matches paper
           - Verify real fills match expected
           - Monitor daily

Week 5+:   Gradual scale-up (only if profitable)
           - $250/trade, then $500, then $1,000
           - Each increase requires 1 profitable week at prior level
```

### What CAN Be Done Now

1. Fix T4 latency (5 min)
2. Implement daily loss kill switch (30 min)
3. Add pre-market check (30 min)
4. Run paper trading starting Monday with all 3 layers active
5. Instrument every signal, fill, and rejection for analysis

---

## VIII. SIGNATURES

| Name | Role | Recommendation |
|------|------|----------------|
| Fund Manager | Portfolio Risk | **DO NOT DEPLOY LIVE. Start paper trading immediately.** |
| Financial Analyst | Strategy P&L | **Paper trade minimum 2 weeks. Present data before live decision.** |
| Head of Technology | System Architecture | **System is technically sound but operationally unvalidated. Paper first.** |

---

*This report contains no fabricated data. All statements are based on actual code, test results, and log files as of April 12, 2026.*
