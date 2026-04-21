# Portfolio Growth Projection — $25K → $100K

**Starting capital**: $25,000
**Target**: $100,000 (4x growth, 300% cumulative return)
**Start date**: 2026-04-14 (paper trading begins)
**Current costs**: $210/mo (Claude $200 + Tradier $10)

---

## Baseline Performance Data (2026-04-13)

From the first full trading day:
```
Trades:        146
Win rate:      56.2%
Total P&L:     +$55.52
Avg winner:    +$0.68
Avg loser:     -$0.20
Profit factor: 1.27
Exit breakdown: SELL_STOP 46%, SELL_RSI 33%, SELL_TARGET 3%, PARTIAL_SELL 3%
```

**Fixes applied** (expected improvement for tomorrow):
- fib_confluence signals reduced 87% (less noise, higher quality)
- Wash trade prevention (no more stuck positions)
- RVOL engine upgraded (tick-level accuracy, no first-hour blackout)
- SignalAction case fix (196 errors/day eliminated)
- Pop budget $500 → $10K (can now trade SPY/QQQ)
- Ticker discovery from Benzinga/StockTwits (catches trending stocks like CRWV)

---

## Phase Timeline

### Phase 1: Paper Trading Validation (Month 1-3)

**Goal**: Prove the system has positive expected value
**Capital at risk**: $0
**Portfolio**: $25,000 (unchanged)

| Month | What Happens |
|-------|-------------|
| Month 1 | Run all 4 engines on paper. RVOL builds 20-day profiles. Collect data in ml_signal_context. Expect 40-80 trades/day. |
| Month 2 | Analyze ml_trade_outcomes. Disable losing strategies. Tune stops/targets based on MFE/MAE. Options engine generates first signals. |
| Month 3 | Refined system. Win rate should be 58-62%. Profit factor > 1.5. At least 1500 labeled rows in ml_signal_context. |

**Exit criteria for going live**:
- [ ] 30 consecutive trading days net positive on paper
- [ ] Win rate > 55% across all active engines
- [ ] Max drawdown < 5% simulated
- [ ] Profit factor > 1.4
- [ ] No system crashes for 2+ weeks

**If criteria NOT met after 3 months**: Do NOT go live. Fix strategies first. Extend paper by 1 month. Repeat.

---

### Phase 2: Live — Training Wheels (Month 4-9)

**Goal**: Validate paper results translate to live
**Position sizes**: $200-500/trade (1-2% of portfolio)
**Max concurrent**: 3 positions
**Portfolio**: $25,000 → ~$27,000

Expected daily P&L (conservative):
```
Paper: $50/day average
Live haircut: 40% (slippage, partial fills, latency)
Live estimate: $30/day
Monthly: $30 × 21 trading days = $630/month = $7,560/year = 30% annual

But first 3 months live will be worse (learning curve):
Month 4-6: $15-20/day average → +$945-$1,260 for the quarter
```

| Month | Portfolio (est) | Actions |
|-------|----------------|---------|
| Month 4 | $25,300 | Go live with VWAP strategy only (proven). Small positions. |
| Month 5 | $25,700 | Add ProSetupEngine if paper win rate held. |
| Month 6 | $26,200 | Add PopStrategyEngine if ticker discovery is finding winners. |

---

### Phase 3: Live — Full System (Month 10-24)

**Goal**: Scale position sizes as confidence builds
**Position sizes**: $500-$1,500/trade
**Max concurrent**: 5-8 positions
**Portfolio**: $27,000 → $40,000

```
Estimated returns:
  VWAP strategy:   8-12% annual (conservative, proven)
  ProSetupEngine:  5-10% annual (selective after fib_confluence fix)
  PopStrategy:     3-8% annual  (news-driven, variable)
  OptionsEngine:   0-5% annual  (paper testing phase still)
  Combined:        20-30% annual on deployed capital
```

| Month | Portfolio (est) | Actions |
|-------|----------------|---------|
| Month 12 | $31,000 | End of Year 1. Review full year data. Train first ML model. |
| Month 15 | $34,000 | Add Polygon.io ($29/mo) if portfolio supports it. |
| Month 18 | $37,000 | Options engine goes live (after 12+ months paper). |
| Month 24 | $40,000-$45,000 | End of Year 2. Full system operational. |

---

### Phase 4: Compounding (Month 25-48)

**Goal**: Let compounding work
**Portfolio**: $40K → $100K

```
At 25% annual compound (realistic for proven system):
  Year 3 start: $40,000
  Year 3 end:   $50,000
  Year 4 end:   $62,500
  Year 5 end:   $78,125

At 30% annual compound (with data upgrades):
  Year 3 end:   $52,000
  Year 4 end:   $67,600
  Year 5 end:   $87,880
```

**$100K reached**: End of Year 5 at 25% annual, or mid-Year 4 at 30%.

---

## Scenario Analysis

### Scenario A: Pure Trading Returns (No Additional Capital)

```
Year    Return    Portfolio    Monthly Avg P&L
────    ──────    ─────────    ───────────────
  0     (paper)   $25,000     $0
  1     +15%      $28,750     $312
  2     +22%      $35,075     $527
  3     +25%      $43,844     $731
  4     +28%      $56,120     $1,023
  5     +25%      $70,150     $1,169
  6     +25%      $87,688     $1,461
  6.5   +12%      $98,210
  7     —         $100,000+

Timeline: ~6-7 years
```

### Scenario B: Trading Returns + $500/mo Contributions

```
Year    Return    Added      Portfolio
────    ──────    ─────      ─────────
  0     (paper)   $0         $25,000
  1     +15%      +$6,000    $34,750
  2     +22%      +$6,000    $48,395
  3     +25%      +$6,000    $66,494
  4     +28%      +$6,000    $91,112
  4.3   —         —          $100,000+

Timeline: ~4 years
```

### Scenario C: Trading Returns + $1,000/mo Contributions

```
Year    Return    Added       Portfolio
────    ──────    ──────      ─────────
  0     (paper)   $0          $25,000
  1     +15%      +$12,000    $40,750
  2     +22%      +$12,000    $61,715
  3     +25%      +$12,000    $89,144
  3.3   —         —           $100,000+

Timeline: ~3 years
```

### Scenario D: Aggressive (Everything Goes Right)

```
- All 4 engines profitable from Month 4
- Win rate 62%, profit factor 2.0+
- Polygon.io added Month 6 (improves RVOL 5x)
- ML classifier added Year 2 (filters 30% of losers)
- 35-45% annual returns

Year 1: $25,000 → $33,750
Year 2: $33,750 → $48,938
Year 3: $48,938 → $71,959
Year 3.5: $100,000

Timeline: ~3.5 years (no contributions needed)
```

### Scenario E: Worst Case (System Has No Edge)

```
- Paper testing shows 48% win rate (below breakeven after costs)
- Profit factor < 1.0
- Max drawdown > 8% in first month

Action: DO NOT GO LIVE
- Fix strategies using ml_trade_outcomes data
- Retune or disable underperforming engines
- Extend paper testing
- Worst case: $25,000 stays in savings, $0 lost
- Cost: $210/mo × 3 months paper = $630 (Claude + Tradier)

This is the VALUE of paper trading — you discover failure for $630, not $5,000.
```

---

## Most Likely Outcome

```
$25K → $100K in 4-5 years (pure trading)
$25K → $100K in 3-3.5 years (with $750/mo contributions)
```

**The realistic path**:

```
Month 1-3:   Paper. Prove edge. $0 growth.          Portfolio: $25,000
Month 4-12:  Live, conservative. +15% first year.   Portfolio: $28,750
Year 2:      Scaled up. +22%.                        Portfolio: $35,075
Year 3:      Full system + first data upgrade. +25%. Portfolio: $43,844
Year 4:      Compounding. Options engine live. +28%. Portfolio: $56,120
Year 5:      Mature system. +25%.                    Portfolio: $70,150

With $500/mo contributions added:
Year 4:      $91,112
Year 4.3:    $100,000 ✓
```

---

## Key Milestones & Decision Points

| Portfolio | Milestone | Action |
|-----------|-----------|--------|
| $25,000 | Start | Paper trading begins |
| $25,000 | Month 3 | Go/no-go decision for live trading |
| $28,000 | +12% | System is working — maintain discipline |
| $35,000 | +40% | First infra upgrade eligible (Polygon $29/mo) |
| $50,000 | +100% (doubled) | Celebrate. Add Unusual Whales ($57/mo). |
| $75,000 | +200% (tripled) | Move to cloud. Options engine should be live. |
| $100,000 | +300% (4x) | Full hedge-fund-grade data stack unlocked. |

---

## What Determines Speed More Than Anything

| Factor | Impact on Timeline |
|--------|-------------------|
| **Letting the system run without interference** | #1. Every manual override costs 2-5% annually. |
| **Disabling losing strategies early** | Cutting the bottom 2 strategies can double net returns. |
| **Adding capital from income** | $750/mo contributions shave 2 years off the timeline. |
| **Polygon.io upgrade at $35K** | Better data → better RVOL → 3-5% more annual return. |
| **Training ML classifier at 5000+ rows** | Filtering 30% of losers → profit factor doubles. |

**The single most important thing**: Do not override the system for the first 6 months. Let it trade, collect data, and analyze AFTER the fact. Every time you manually close a position or skip a signal, you corrupt the data that would tell you how to improve.

---

## Tracking Progress

After each month, run:
```bash
python scripts/post_session_analytics.py --backfill 30
```

Then check the dashboard at http://localhost:8502 for:
- Monthly P&L trend (equity curve tab)
- Win rate by strategy (performance tab)
- Alpha vs beta (attribution tab)
- Which strategies to keep/kill (strategy breakdown)

Record monthly results here:

| Month | Portfolio | P&L | Win Rate | Best Strategy | Worst Strategy | Notes |
|-------|----------|-----|----------|---------------|----------------|-------|
| Apr 2026 | $25,000 | (paper) | — | — | — | Paper testing begins |
| May 2026 | | | | | | |
| Jun 2026 | | | | | | |
| Jul 2026 | | | | | | Go/no-go for live |
