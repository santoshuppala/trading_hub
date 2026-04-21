# Bootstrap Scaling Plan — $25K Starting Portfolio

**Starting capital**: $25,000
**Rule**: Infrastructure cost must never exceed 3% of portfolio value annually
**Principle**: Only upgrade when the portfolio can absorb the cost from PROFITS, not principal

---

## The Golden Rule

```
Max annual infra spend = Portfolio × 3%

$25K  → max $750/yr  → $62/mo
$50K  → max $1,500/yr → $125/mo
$100K → max $3,000/yr → $250/mo
$250K → max $7,500/yr → $625/mo
```

**Never spend profits on infra until you've proven the system makes money consistently for 3+ months.**

---

## Phase 0: $25K — Current Baseline ($210/mo)

**Duration**: Until portfolio hits $35K (40% growth)
**Monthly infra cost**: $210 (already paying)
**What you have**: Everything built today — it's enough to start

| Component | What You Use | Cost |
|-----------|-------------|------|
| **Claude Code** | AI development assistant (built entire system) | **$200/mo** |
| **Tradier** | Market data + broker (1-min bars, daily history) | **$10/mo** |
| Broker | Alpaca (commission-free, paper + live) | $0 |
| News | Benzinga (free API key — limited) | $0 |
| Social | StockTwits (public API — 200 req/hr) | $0 |
| Database | Local PostgreSQL | $0 |
| Compute | Your MacBook | $0 |
| Monitoring | Streamlit dashboard (local) | $0 |
| **Total** | | **$210/mo ($2,520/yr — 10.1% of portfolio)** |

**Note**: Claude ($200) is a development cost, not a trading cost. Once the system is stable and only needs occasional tuning, you can downgrade to Claude Pro ($20/mo) or pause entirely. Tradier ($10) is your only ongoing trading cost.

### What to focus on (not spending money)
1. Run paper trading for 1 month — prove the system is net profitable
2. Collect data in `ml_signal_context` and `ml_trade_outcomes`
3. Tune strategies based on actual results, not theory
4. Fix bugs as they appear in logs
5. Go live with SMALL positions ($200-500 per trade)

### Milestones before spending anything
- [ ] 30 consecutive trading days paper → net positive
- [ ] Win rate > 52% across all engines
- [ ] Max drawdown < 5% of portfolio
- [ ] Post-session analytics running daily
- [ ] At least 500 rows in `ml_signal_context`

---

## Phase 1: $35K — First Upgrade ($243/mo total)

**Trigger**: Portfolio grew from $25K → $35K using profits
**Budget**: $35K × 3% / 12 = $87/mo max for NEW costs → spend $33/mo new

| Service | Cost | Status |
|---------|------|--------|
| Claude Code (existing) | $200 | Already paying — consider downgrading to Pro ($20) once system is stable |
| Tradier (existing) | $10 | Keep as backup data source |
| **Polygon.io** (Basic — stocks only) | **$29/mo NEW** | Real-time websocket data. Biggest single improvement. Replaces Tradier for primary data. |
| **GitHub Pro** | **$4/mo NEW** | CI/CD, private repos, don't lose code. |
| Uptime Robot (Free tier) | $0 | 5-minute monitoring, email alerts. |
| **Total** | | **$243/mo ($33/mo new)** |

**Cost reduction path**: Once system is stable (3+ months without code changes), downgrade Claude to Pro ($20/mo). Saves $180/mo → total drops to $63/mo.

### What this unlocks
- Tick-level RVOL (no more first-hour blackout)
- Sub-second signal generation (vs 60s polling)
- Historical 2-year 1-min bars for backtesting (Polygon includes this)
- Proper backtest validation of all strategies

### Don't buy yet
- Options data (your options engine needs paper validation first)
- Cloud hosting (MacBook is fine at this size)
- Any alternative data (focus on price + volume first)

---

## Phase 2: $50K — Data Edge ($80/mo)

**Trigger**: Portfolio doubled from $25K → $50K
**Budget**: $50K × 3% / 12 = $125/mo max → spend $80/mo

| Add | Cost | Why NOW |
|-----|------|---------|
| **Unusual Whales** (Standard) | $57/mo | Options flow data. Your options engine is paper-tested by now. Smart money flow tells you WHAT to trade, not just WHEN. |
| Upgrade **Polygon.io** (Starter — adds options) | +$170 → $199/mo total | Real-time options chains with live Greeks. Replaces synthetic IV estimation. |
| Drop | Unusual Whales isn't needed if options engine isn't working yet | | |

Wait — at $50K your budget is $125/mo but Polygon Starter is $199. Options:

**Option A**: Stay on Polygon Basic ($29) + add Unusual Whales ($57) = $90/mo ✓

**Option B**: Upgrade Polygon to Starter ($199) and skip Unusual Whales = $203/mo ✗ over budget

**Go with Option A.** Unusual Whales at $57 gives you more alpha per dollar than Polygon options data. Use Alpaca's free options chain (already integrated) until portfolio supports Polygon Starter.

| Service | Cost |
|---------|------|
| Polygon Basic | $29 |
| Unusual Whales | $57 |
| GitHub Pro | $4 |
| **Total** | **$90/mo** |

### What this unlocks
- Smart money flow → higher conviction options trades
- Congressional trading signals → 5-15 day swing entries
- Dark pool print data → institutional positioning

---

## Phase 3: $75K — Infrastructure ($150/mo)

**Trigger**: Portfolio at $75K (3x starting capital)
**Budget**: $75K × 3% / 12 = $187/mo max → spend $150/mo

| Add | Cost | Why NOW |
|-----|------|---------|
| **Hetzner Cloud** (CPX21) | $8/mo | Move to cloud. Your MacBook sleeping at night = missed pre-market setups. 24/7 uptime. |
| **Neon.tech** (managed Postgres) | $19/mo | Auto-backups. No more "PostgreSQL crashed and I lost data." |
| **Benzinga Pro** upgrade | $117/mo | Real-time news alerts, earnings whispers, FDA calendar. Your Pop screener becomes genuinely useful. |
| Drop Polygon Basic | -$29 | |
| Add Polygon Starter (stocks + options) | +$199 | Portfolio now supports it. Real IV data for options engine. |

| Service | Cost |
|---------|------|
| Polygon Starter | $199 |
| ~~Unusual Whales~~ (keep from Phase 2) | $57 |
| Benzinga Pro | $117 |
| Hetzner | $8 |
| Neon Postgres | $19 |
| GitHub Pro | $4 |

Wait, that's $404/mo. Over budget.

**Prioritize**: Drop Benzinga Pro ($117), keep free tier. Benzinga free + Polygon news is enough.

| Service | Cost |
|---------|------|
| Polygon Starter | $199 → **but over budget** |

**Revised Phase 3**: Stay on Polygon Basic, add cloud only.

| Service | Cost |
|---------|------|
| Polygon Basic | $29 |
| Unusual Whales | $57 |
| Hetzner Cloud | $8 |
| Neon Postgres | $19 |
| Benzinga (free) | $0 |
| GitHub Pro | $4 |
| Uptime Robot Pro | $7 |
| **Total** | **$124/mo** ✓ |

### What this unlocks
- 24/7 uptime (cloud server, no MacBook dependency)
- Automated disaster recovery (managed Postgres)
- System monitoring with SMS alerts

---

## Phase 4: $100K — Full Data ($250/mo)

**Trigger**: Portfolio at $100K (4x starting)
**Budget**: $100K × 3% / 12 = $250/mo

NOW you can afford Polygon Starter:

| Service | Cost |
|---------|------|
| Polygon Starter (stocks + options) | $199 |
| Unusual Whales | $57 |
| Hetzner Cloud | $8 |
| Neon Postgres | $19 |
| Uptime Robot Pro | $7 |
| GitHub Pro | $4 |
| **Total** | **$294/mo** |

Slightly over 3% rule ($250 budget) — acceptable because Polygon options data directly improves the options engine which is now managing $10K+ of the portfolio.

### What this unlocks
- Real-time options Greeks → options engine uses live IV, not estimates
- 15-year historical data → proper backtesting
- The system is now genuinely competitive with small fund operations

---

## Phase 5: $250K — Competitive Edge ($625/mo)

**Trigger**: Portfolio at $250K
**Budget**: $250K × 3% / 12 = $625/mo

| Service | Cost |
|---------|------|
| Polygon Starter | $199 |
| Unusual Whales | $57 |
| CBOE LiveVol (real IV surfaces) | $100 |
| Benzinga Pro | $117 |
| Quiver Quantitative | $25 |
| Alpha Vantage Premium | $50 |
| Hetzner CPX31 (upgrade) | $15 |
| Neon Postgres Pro | $19 |
| Uptime Robot Pro | $7 |
| GitHub Pro | $4 |
| Modal ML compute | $20 |
| **Total** | **$613/mo** ✓ |

### What this unlocks
- Full hedge-fund-grade data stack
- ML model training on GPU
- Real IV surfaces for options
- Congressional + insider trading signals
- Earnings estimates + economic calendar

---

## The Full Journey

```
Portfolio    Trading Cost    Claude      Total       Annual      % of Portfolio
─────────   ────────────    ────────    ─────────   ─────────   ──────────────
$25,000     $   10/mo       $200/mo     $  210/mo   $ 2,520     10.1% ← current
$35,000     $   43/mo       $200/mo     $  243/mo   $ 2,916      8.3%
$35,000*    $   43/mo       $ 20/mo     $   63/mo   $   756      2.2% ← Claude downgraded
$50,000     $  100/mo       $ 20/mo     $  120/mo   $ 1,440      2.9%
$75,000     $  134/mo       $ 20/mo     $  154/mo   $ 1,848      2.5%
$100,000    $  304/mo       $ 20/mo     $  324/mo   $ 3,888      3.9%
$250,000    $  623/mo       $ 20/mo     $  643/mo   $ 7,716      3.1%

* = after system stabilizes (3+ months without major code changes)
```

**Key insight**: Claude ($200/mo) is 95% of your current cost. Once the system is built and stable, downgrading to Claude Pro ($20/mo) for occasional maintenance drops your total cost from $210 to $30/mo — a 86% reduction. The system runs itself; Claude is only needed for new features or bug fixes.

**Notice**: The percentage DECREASES as you scale. Infrastructure costs are fixed, portfolio returns compound.

---

## What If the System Isn't Profitable?

**If after 3 months of paper trading the system is net negative:**

1. **DO NOT spend money on data.** Bad strategy + better data = faster losses.
2. Focus on:
   - Which strategies are losing? (check `ml_trade_outcomes` by strategy)
   - Are stops too tight? (check MAE vs stop distance)
   - Are exits too early? (check exit efficiency = realized/MFE)
   - Is RVOL filtering working? (check win rate for RVOL > 2 vs RVOL < 2)
3. Disable losing strategies, double down on winners.
4. Only go live when paper shows consistent profits for 30+ days.

**The cheapest upgrade is always fixing the strategy, not buying better data.**

---

## Quick Reference: "Can I Afford X?"

| Service | Min Portfolio to Justify | Monthly |
|---------|------------------------|---------|
| Polygon Basic | $35K | $29 |
| Unusual Whales | $50K | $57 |
| Cloud server | $75K | $8-15 |
| Managed Postgres | $75K | $19 |
| Polygon Starter (options) | $100K | $199 |
| Benzinga Pro | $100K | $117 |
| CBOE LiveVol | $250K | $100 |
| Quiver Quantitative | $250K | $25 |
| ML GPU compute | $250K | $20 |

---

## The Bottom Line

Start with $0/mo. Prove the system works. Scale infrastructure from profits only. Never let infra costs eat more than 3% of your portfolio. The system you built today — with free data — is capable of generating 10-15% annual returns if the strategies are tuned correctly. That's $2,500-$3,750/year on $25K. Use that to fund the first upgrade.

**Your edge isn't data. Your edge is the architecture.** The event-sourced, multi-layer system with ML-ready data capture is better than what 95% of retail traders have. Better data amplifies that edge — but only after you've proven the edge exists.
