# Infrastructure & Subscription Cost Plan — $100K Portfolio

**Last updated**: 2026-04-14
**Portfolio target**: $100,000
**Goal**: Hedge-fund-grade data quality for maximum trade profitability

---

## Current Costs (Already Paying)

| Service | Monthly | Annual | What It Does |
|---------|---------|--------|-------------|
| **Claude Code** | $200 | $2,400 | AI development assistant — built the entire system. Can downgrade to Pro ($20/mo) once stable. |
| **Tradier** | $10 | $120 | Market data (1-min bars, daily history) + brokerage |
| **Current Total** | **$210** | **$2,520** | **10.1% of $25K portfolio** |

**Note**: Claude is a development cost. Once the system needs only occasional maintenance, downgrade to Claude Pro ($20/mo). Effective trading cost then = $30/mo ($360/yr = 1.4% of $25K).

---

## Additional Cost Summary (on top of current $210/mo)

| Tier | Additional Monthly | Total Monthly | Annual | % of $100K | Priority |
|------|-------------------|--------------|--------|-----------|----------|
| **Current** | $0 | $210 | $2,520 | 2.5% | Now |
| **T1: Essential Data** | +$285 | $495 | $5,940 | 5.9% | When $50K+ |
| **T2: Edge Data** | +$190 | $685 | $8,220 | 8.2% | When $100K+ |
| **T3: Infrastructure** | +$46 | $731 | $8,772 | 8.8% | When $75K+ |
| **T4: ML & Analytics** | +$20 | $751 | $9,012 | 9.0% | When $250K+ |

**With Claude downgraded** (system stable):

| Tier | Total Monthly | Annual | % of $100K |
|------|-------------|--------|-----------|
| **Current (stable)** | $30 | $360 | 0.4% |
| **+ T1** | $315 | $3,780 | 3.8% |
| **+ T1 + T2** | $505 | $6,060 | 6.1% |
| **+ T1 + T2 + T3** | $551 | $6,612 | 6.6% |
| **Full stack** | $571 | $6,852 | 6.9% |

**Breakeven**: System must generate >7% annual return to cover full stack costs at $100K.

---

## Tier 1: Essential Data (Non-Negotiable)

| Service | Plan | Monthly | What You Get | Integration Point |
|---------|------|---------|-------------|-------------------|
| **Polygon.io** | Starter | $199 | Real-time tick-by-tick stocks + options, 15-year history, websocket streaming, unlimited API | Replace `TradierDataClient` with `PolygonDataClient`. Feeds RVOL engine with tick-level volume, VWAP from actual trades, L2 snapshots. |
| **Alpaca** | Free (commission-free) | $0 | Order execution, paper trading, options | Already integrated. Keep as broker. |
| **Unusual Whales** | Standard | $57 | Real-time options flow, dark pool prints, whale alerts, congress trades | New: `pop_screener/unusual_whales.py` adapter. Feeds options engine with smart money positioning. Enhances ticker discovery. |
| **Benzinga** | Pro | $117 | Real-time news, analyst ratings, FDA calendar, earnings whispers, pre-market movers | Already integrated (`benzinga_news.py`). Upgrade unlocks: earnings estimates, real-time alert webhooks, sector news filtering. |

**Tier 1 Total: $373/mo**

### Polygon.io Integration Spec
```
Current:  Tradier REST → 1-min bars → 60s delay → signals
Upgrade:  Polygon WS → tick stream → <100ms → real-time signals

Files to create/modify:
  - NEW: monitor/polygon_client.py (PolygonDataClient)
  - MODIFY: monitor/data_client.py (add polygon as source option)
  - MODIFY: config.py (POLYGON_API_KEY, DATA_SOURCE='polygon')
  - MODIFY: monitor/rvol.py (accept tick-level volume updates)

Polygon endpoints to use:
  - wss://socket.polygon.io/stocks (real-time trades + quotes)
  - /v2/aggs/ticker/{ticker}/range/1/minute (historical 1-min bars)
  - /v3/snapshot/options/{underlyingAsset} (options chain with Greeks)
  - /v2/last/trade/{ticker} (last trade for VWAP)
```

### Unusual Whales Integration Spec
```
API: https://api.unusualwhales.com/api/
Key endpoints:
  - /flow/live (real-time options flow)
  - /darkpool/recent (dark pool transactions)
  - /congress/recent (congress member trades)

Files to create:
  - NEW: pop_screener/unusual_whales.py (OptionsFlowSource)
  - MODIFY: options/selector.py (use flow data for strategy selection)
  - MODIFY: pop_screener/ticker_discovery.py (add UW trending tickers)
```

---

## Tier 2: Competitive Edge Data

| Service | Plan | Monthly | What You Get | Integration Point |
|---------|------|---------|-------------|-------------------|
| **Quiver Quantitative** | Standard | $25 | Congress trades, insider filings, SEC filings, Reddit/WSB sentiment, patent data | New: `pop_screener/quiver.py`. Congress + insider trades as high-conviction Pop signals. 60%+ accuracy on 5-15 day moves. |
| **CBOE LiveVol** | Data API | $100 | Real IV surfaces, historical IV (1yr+), skew curves, term structure | Replace synthetic IV in `options/chain.py` with real market IV. Makes IV rank accurate. Feeds options selector. |
| **Alpha Vantage** | Premium | $50 | Fundamentals, earnings estimates, economic indicators, sector ETF performance | Replace yfinance earnings calendar. Add sector rotation data to `monitor/sector_map.py`. Economic calendar for risk events. |
| **TradingView** | Pro | $15 | Real-time screener API, community signals, alerts | Replace StockTwits trending (200/hr rate limit) with TradingView screener (unlimited). Better ticker discovery. |

**Tier 2 Total: $190/mo**

### CBOE LiveVol Integration Spec
```
Replaces: options/iv_tracker.py (in-memory, self-computed IV rank)
With: Real market IV surfaces from CBOE

Files to modify:
  - MODIFY: options/chain.py (use LiveVol for Greeks instead of Alpaca)
  - MODIFY: options/iv_tracker.py (seed from LiveVol historical IV)
  - MODIFY: options/selector.py (use real IV percentile, not estimated)

Impact: IV rank becomes accurate from day 1 (no 20-day warmup needed)
```

---

## Tier 3: Infrastructure

| Service | Plan | Monthly | What You Get | Why |
|---------|------|---------|-------------|-----|
| **Hetzner Cloud** | CPX31 | $15 | 4 vCPU, 8GB RAM, 160GB SSD, US datacenter | 24/7 uptime. No sleeping laptop. Low latency to Alpaca/Polygon. |
| **Neon.tech** | Pro | $19 | Managed PostgreSQL, auto-scaling, branching, PITR backup | No more local PostgreSQL. Auto-backups. Zero admin. |
| **Uptime Robot** | Pro | $7 | 1-minute checks, SMS + email alerts, status page | Know within 60s if system is down. |
| **GitHub** | Pro | $4 | Private repos, CI/CD Actions, code scanning | Automated tests before deploy. |
| **Backblaze B2** | Pay-as-you-go | $1 | Cloud backup for event_store, logs, models | Offsite disaster recovery. |

**Tier 3 Total: $46/mo**

### Cloud Deployment Spec
```
Hetzner CPX31 (Ashburn, VA datacenter — near NYSE)
  - OS: Ubuntu 22.04
  - Python 3.14 + venv
  - PostgreSQL 16 (or use Neon managed)
  - systemd service for run_monitor.py
  - Cron: post_session_analytics.py at 4:30 PM ET daily

Network latency:
  - Hetzner Ashburn → Alpaca: ~5ms (vs MacBook WiFi ~50ms)
  - Hetzner Ashburn → Polygon: ~3ms
  - Hetzner Ashburn → NYSE: ~1ms

Deployment:
  git clone → pip install → systemctl enable trading-hub
  Auto-restart on crash via systemd Restart=always
```

---

## Tier 4: ML & Analytics (Phase 2)

| Service | Plan | Monthly | What You Get | Why |
|---------|------|---------|-------------|-----|
| **Modal.com** | Pay-per-use | ~$20 | GPU compute for ML training | Train signal classifier monthly. 10x faster than CPU. |
| **Weights & Biases** | Free | $0 | Experiment tracking, model registry | Track model versions, compare experiments. |
| **Grafana Cloud** | Free | $0 | Dashboards, alerting, log aggregation | Always-on monitoring (Streamlit needs browser open). |

**Tier 4 Total: $20/mo**

---

## ROI Analysis

### Current System (No Upgrades)
```
Revenue: +10-15% annually ($10K-$15K)
Costs:   $0
Net:     +$10K-$15K
```

### With T1 Data Only ($373/mo)
```
Revenue: +18-25% annually ($18K-$25K)
  - Polygon tick data improves RVOL accuracy → fewer false entries
  - Unusual Whales flow data → better options timing
  - Benzinga Pro → earlier news signals

Costs:   -$4,476/yr
Net:     +$13.5K-$20.5K
Delta:   +$3.5K-$5.5K more than current system
```

### Full Hedge Fund Grade ($629/mo)
```
Revenue: +25-35% annually ($25K-$35K)
  - All data advantages above
  - Real IV surfaces → options win rate improves 10%+
  - Congress/insider data → 5-15 day conviction trades
  - Cloud infra → zero downtime, no missed trades

Costs:   -$7,548/yr
Net:     +$17.5K-$27.5K
Delta:   +$7.5K-$12.5K more than current system
```

---

## Rollout Schedule

| Month | Action | Added Cost | Total |
|-------|--------|-----------|-------|
| **1** | Polygon.io + Unusual Whales + Benzinga Pro upgrade | $373/mo | $373/mo |
| **2** | Hetzner cloud + managed Postgres + Uptime Robot | +$41/mo | $414/mo |
| **3** | CBOE LiveVol (real IV for options engine) | +$100/mo | $514/mo |
| **4** | Quiver Quantitative + Alpha Vantage | +$75/mo | $589/mo |
| **5** | TradingView Pro + Modal.com ML compute | +$35/mo | $624/mo |
| **6+** | Steady state | $0 | $629/mo |

### Decision Gates (Don't Advance Without)

| Gate | Condition | What It Proves |
|------|-----------|---------------|
| Month 1 → 2 | Win rate improved ≥3% with Polygon data | Data quality → signal quality confirmed |
| Month 2 → 3 | System ran 30 days on cloud with <1hr total downtime | Infrastructure stable |
| Month 3 → 4 | Options engine generated ≥10 signals/day on paper | Options strategy viable with real IV |
| Month 4 → 5 | `ml_signal_context` has ≥5000 labeled rows | Enough data to train first ML model |
| Month 5 → 6 | ML classifier improves signal accuracy ≥5% | ML adds measurable value |

---

## Single Highest-ROI Upgrade

**Polygon.io at $199/mo** replaces the entire data layer:

| Metric | Current (Tradier) | With Polygon |
|--------|-------------------|-------------|
| Data latency | 60 seconds (REST polling) | <100ms (websocket) |
| Data granularity | 1-minute bars | Tick-by-tick |
| Order book | None | L2 snapshots |
| RVOL accuracy | ±20% (daily time-fraction) | ±2% (tick-level cumulative) |
| VWAP accuracy | Computed from 1-min OHLC | Computed from every trade |
| Pre-market | None | Full pre-market ticks |
| Historical depth | 14 days | 15 years |
| Rate limits | 200 req/min | Unlimited streaming |
| Options chains | Alpaca (delayed Greeks) | Real-time Greeks + IV surface |

**Expected impact**: 5-8% improvement in annual return from better signal timing alone.

---

## What NOT to Buy

| Service | Monthly | Why Skip It |
|---------|---------|-------------|
| Bloomberg Terminal | $2,000 | Overkill for $100K. Polygon + Benzinga cover 90% of what you need. |
| Refinitiv Eikon | $1,500 | Same — enterprise pricing for enterprise scale. |
| QuantConnect | $40-200 | You already have a backtest framework. Use your own. |
| Trade Ideas | $170 | Scanner — your Pop screener + ticker discovery already does this. |
| Multiple brokers (IB + Alpaca) | $0-10 | Not needed until $500K+. Alpaca execution is fine for this size. |
| Co-location | $1,000+ | Microsecond latency irrelevant for 1-min+ holding periods. |

---

## Annual Budget Summary

```
ALREADY PAYING:
  Claude Code (development) $2,400/yr  → $240/yr after downgrade to Pro
  Tradier (data + broker) . $  120/yr
  ─────────────────────────────────
  Subtotal                  $2,520/yr  → $360/yr when stable

ESSENTIAL (add when portfolio supports it):
  Polygon.io .............. $2,388/yr
  Unusual Whales .......... $  684/yr
  Benzinga Pro ............ $1,404/yr
  Hetzner Cloud ........... $  180/yr
  Managed Postgres ........ $  228/yr
  Monitoring .............. $   84/yr
  ─────────────────────────────────
  Subtotal                  $4,968/yr  (5.0% of $100K portfolio)

COMPETITIVE EDGE (nice to have):
  CBOE LiveVol ............ $1,200/yr
  Quiver Quantitative ..... $  300/yr
  Alpha Vantage ........... $  600/yr
  TradingView Pro ......... $  180/yr
  ML Compute .............. $  240/yr
  ─────────────────────────────────
  Subtotal                  $2,520/yr  (2.5% of $100K portfolio)

TOTAL (everything):
  With Claude Code ........ $9,948/yr  (10.0% of $100K)
  With Claude Pro ......... $7,848/yr  ( 7.8% of $100K)
```

**Rule of thumb**: If the system doesn't generate >10% annually, fix the strategies before buying more data. Data amplifies edge — it doesn't create it.

**Immediate cost reduction**: Once system is stable, Claude Code → Claude Pro saves $2,160/yr. This is the single biggest cost optimization available today.
