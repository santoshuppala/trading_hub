# Trading Hub (V8)

A real-time algorithmic trading system built on an event-driven architecture. V8 merges Pro and Pop engines into Core for a unified 3-process architecture with 13 technical strategies, 8 alt data sources, multi-broker execution (Alpaca + Tradier), and a 20+ check risk stack.

> **Disclaimer:** This is for educational purposes only. Trading involves significant risk. Always start with paper trading. Consult a financial advisor before trading with real money.

---

## Architecture (V8)

3 processes under supervisor. Pro and Pop merged into Core.

```
Supervisor
  +-- Core (VWAP + Pro 11 strategies + Pop scanner)
  |     +-- 13 detectors (11 technical + SentimentDetector + NewsVelocityDetector)
  |     +-- Classifier (highest confidence wins, sentiment boosts thresholds)
  |     +-- Tier-based stops/EOD (T1 intraday close, T2/T3 swing with trailing)
  |     +-- Per-strategy risk engines (RiskEngine for VWAP, RiskAdapter for Pro)
  |     +-- PortfolioRiskGate (unrealized P&L, buying power, drawdown)
  |     +-- PerStrategyKillSwitch (per-strategy daily loss halt)
  |     +-- SmartRouter -> Alpaca + Tradier (failover, circuit breaker)
  |     +-- Pop periodic scan (10 min full, 2 min headline check)
  |     +-- Publishes SIGNAL + POP_SIGNAL to Options via Redpanda
  |
  +-- Options (13 multi-leg strategies, separate Alpaca account)
  |
  +-- Data Collector (8 alt data sources, writes to DB + alt_data_cache.json)

Event Flow:
  BAR -> VWAP StrategyEngine -> SIGNAL -> RiskEngine -> ORDER_REQ
  BAR -> ProSetupEngine (13 detectors) -> PRO_STRATEGY_SIGNAL -> RiskAdapter -> ORDER_REQ
  ORDER_REQ -> PortfolioRiskGate -> RegistryGate -> SmartRouter -> Broker -> FILL
  FILL -> PositionManager -> POSITION -> bot_state.json + event_store DB
```

### Process Map

| Process | Strategies | IPC |
|---------|-----------|-----|
| **Core** | VWAP (1) + Pro (11) + Pop scan | Publishes to th-signals, th-pop-signals |
| **Options** | 13 multi-leg strategies | Consumes th-signals, th-pop-signals |
| **Data Collector** | N/A (data pipeline) | Publishes to th-discovery |

### Key V8 Changes

| Change | V7 | V8 |
|--------|----|----|
| Processes | 5 (Core, Pro, Pop, Options, DataCol) | 3 (Core+Pro+Pop, Options, DataCol) |
| Pro execution | Separate process via IPC | In-process on Core's EventBus |
| Pop execution | Separate process, trades independently | Periodic scanner, no trading |
| State files | 3 (bot_state, pro_state, pop_state) | 1 (bot_state.json) |
| Position tracking | Cross-process sync issues | Single PositionManager |
| Classifier | First tier match wins | Highest confidence wins |
| Stops | Fixed per strategy | Tier-aligned (T1 tight, T2/T3 wide) |
| Trailing | VWAP ATR-based only | T2/T3: breakeven at +1R, lock at +2R |
| Sentiment | Not integrated | 8-source SentimentDetector, threshold relaxation |
| EOD | All Pro partial sell | T1 full close, T2/T3 partial sell |
| Kill switch | Per-process | Per-strategy (VWAP and Pro independent) |
| Drawdown check | Realized P&L only | Realized + unrealized (BAR price updates) |

---

## Data Sources (10 total)

| # | Source | API Key | Data | DB Table |
|---|--------|---------|------|----------|
| 1 | Benzinga | Yes | Headlines, sentiment | `event_store` (NewsDataSnapshot) |
| 2 | StockTwits | Optional | Social mentions, sentiment | `event_store` (SocialDataSnapshot) |
| 3 | Fear & Greed | No | Market sentiment index | `data_source_snapshots` |
| 4 | Finviz | No | Screener, insider, analyst | `data_source_snapshots` |
| 5 | SEC EDGAR | No | Insider filings | `data_source_snapshots` |
| 6 | Yahoo Finance | No | Earnings, recommendations | `data_source_snapshots` |
| 7 | Polygon.io | Yes | Prev-day movers, options | `data_source_snapshots` |
| 8 | Alpha Vantage | Yes | Technicals | `data_source_snapshots` |
| 9 | FRED | Yes | Macro indicators (rates, VIX) | `data_source_snapshots` |
| 10 | UOF Scanner | Via chain API | Unusual options flow | `data_source_snapshots` |

All sources collected by Data Collector every 10 min. Written to `alt_data_cache.json` (file) and `data_source_snapshots` (DB).

---

## Risk Stack (20+ Checks)

| # | Check | Component | Action |
|---|-------|-----------|--------|
| 1 | Max positions (per-strategy) | RiskEngine/RiskAdapter | VWAP max 5, Pro max 15 |
| 2 | Cooldown (300s per ticker) | RiskEngine/RiskAdapter | Block re-entry |
| 3 | RVOL threshold | RiskEngine | >= 2.0 (1.5 with sentiment) |
| 4 | RSI range | RiskEngine | 40-75 (35-80 with sentiment) |
| 5 | Spread width | RiskEngine | <= 0.2% |
| 6 | Price divergence | RiskEngine | Block if ask moved > 0.5% |
| 7 | Correlation groups (14) | RiskSizer | Max 3 per group |
| 8 | Beta-weighted exposure | RiskSizer | Max $200K beta-weighted |
| 9 | Beta-adjusted sizing | RiskSizer | TQQQ: 10 -> 3 shares |
| 10 | ATR-scaled sizing | RiskSizer | Volatility-based |
| 11 | Confidence threshold (tier) | RiskAdapter | T1: 0.50, T2: 0.60, T3: 0.70 |
| 12 | R:R ratio (tier) | RiskAdapter | T1: 1.5, T2: 2.0, T3: 3.5 |
| 13 | Sector concentration | RiskAdapter | Max 2 per sector (cross-engine) |
| 14 | Earnings block (T2/T3) | RiskAdapter | Block overnight holds near earnings |
| 15 | Drawdown halt | PortfolioRiskGate | Block at -$5K (un-halts at -$4K) |
| 16 | Notional cap | PortfolioRiskGate | Block > $100K exposure |
| 17 | Buying power | PortfolioRiskGate | Fail-closed if unavailable |
| 18 | Greeks limits | PortfolioRiskGate | Delta/gamma caps |
| 19 | Per-strategy kill switch | PerStrategyKillSwitch | VWAP: -$10K, Pro: -$2K |
| 20 | Cross-layer dedup | RegistryGate | One engine per ticker |
| 21 | Sentiment-based priority | StrategyEngine | Defer to Pro when sentiment present |

---

## Strategies

### VWAP Reclaim (Core)

Entry: 2-bar VWAP reclaim + opened above VWAP + RSI 50-70 + RVOL >= 2x + SPY above VWAP.
Exit: Trailing stop (1x ATR), full target (2x ATR), partial at 1x ATR, RSI > 75, VWAP breakdown, EOD close.

### Pro Setups (13 detectors, 11 strategies, 3 tiers)

| Tier | Strategies | Stop | Targets | EOD |
|------|-----------|------|---------|-----|
| T1 | sr_flip, trend_pullback, vwap_reclaim | 0.4-0.5 ATR | 1R/2R | Full close |
| T2 | orb, gap_and_go, inside_bar, flag_pennant | 1.5-2.0 ATR | 1.5R/3R | Partial sell |
| T3 | momentum_ignition, fib_confluence, bollinger_squeeze, liquidity_sweep | 2.0-2.5 ATR | 3R/6-8R | Partial sell |

### Pop Scanner (No Independent Trading)

Scans for momentum every 10 min. Adds tickers to VWAP/Pro universe. Sends POP_SIGNAL to Options.

### Options (13 strategies, separate process)

Iron condor, iron butterfly, bull/bear call/put spreads, long call/put, calendar, diagonal, straddle, strangle, collar.

---

## Quick Start

### 1. Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Python 3.10+ required.

### 2. Configure `.env`

```
# Alpaca (main account)
APCA_API_KEY_ID=PKxxxxxxxxxxxxxxxx
APCA_API_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Tradier (bar data + broker)
TRADIER_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx
TRADIER_SANDBOX_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxx
TRADIER_ACCOUNT_ID=xxxxxxxx

# Broker mode
BROKER=alpaca
BROKER_MODE=smart
MONITOR_MODE=supervisor
DATA_SOURCE=tradier

# Email alerts (Yahoo app password)
ALERT_EMAIL_USER=you@yahoo.com
ALERT_EMAIL_PASS=your16charapppassword
ALERT_EMAIL_FROM=you@yahoo.com
ALERT_EMAIL_TO=you@yahoo.com

# Options account
APCA_OPTIONS_KEY=PKxxxxxxxxxxxxxxxx
APCA_OPTIONS_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Pop account (V8: merged into Core, credentials still needed for buying power check)
APCA_POPUP_KEY=PKxxxxxxxxxxxxxxxx
APCA_POPUP_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Pro config
PRO_MAX_POSITIONS=15
PRO_TRADE_BUDGET=1000
PRO_ORDER_COOLDOWN=300

# External data
BENZINGA_API_KEY=bz.xxxxxxxxxxxxxxxxxxxx
FRED_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ALPHA_VANTAGE_KEY=xxxxxxxxxxxxxxxx
POLYGON_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Database
DB_ENABLED=true
DATABASE_URL=postgresql://postgres:trading_secret@localhost:5432/tradinghub
REDPANDA_BROKERS=127.0.0.1:9092

# Risk
MAX_DAILY_LOSS=-10000
GLOBAL_MAX_POSITIONS=75
```

### 3. Start infrastructure

```bash
cd docker && docker compose up -d
python -m db.migrations.run
```

### 4. Run

```bash
bash start_monitor.sh
```

### 5. Cron

```
0 6 * * 1-5 /path/to/trading_hub/start_monitor.sh >> /path/to/logs/cron.log 2>&1
```

---

## Database (TimescaleDB)

All events persisted to TimescaleDB. 13 ordered migrations.

Key tables:
- `event_store` — immutable event log (all trading events)
- `data_source_snapshots` — alt data from all 8 sources (V8)
- `bar_events`, `signal_events`, `fill_events`, `position_events` — projections
- `completed_trades` — trade summaries with strategy attribution

---

## Logging

Daily logs in `logs/YYYYMMDD/core.log`. EOD summary emailed automatically.

---

## Docs

- `docs/V8_trading_hub/V8_ARCHITECTURE.md` — full V8 architecture
- `docs/V8_trading_hub/V8_POST_RUN_CHECKLIST.md` — post-deployment verification
- `docs/V7_trading_hub/` — previous architecture (reference)
- `docs/V6_trading_hub/` — V6 design docs

---

## Requirements

- Python 3.10+
- Docker (TimescaleDB 2.26+ / PostgreSQL 16 + Redpanda)
- Alpaca account (paper trading free)
- Tradier account (sandbox free)
- Yahoo Mail app password for alerts (optional)
- macOS/Linux
