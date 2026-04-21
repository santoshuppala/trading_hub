# V8 Architecture — Merged Core + Pro + Pop

## Overview

V8 merges Pro and Pop engines into Core, reducing processes from 5 to 3. Options stays separate (different instruments/broker). Data Collector stays separate (independent pipeline).

```
BEFORE (V7 — 5 processes):          AFTER (V8 — 3 processes):
  Core (VWAP)                         Core (VWAP + Pro + Pop scan)
  Pro (11 strategies)        -->        |_ ProSetupEngine (in-process)
  Pop (momentum scanner)     -->        |_ Pop scanner (periodic, no trading)
  Options (13 strategies)              Options (unchanged)
  Data Collector (8 sources)           Data Collector (unchanged)
```

## Why V8

Every major bug on 2026-04-16 traced to cross-process complexity:
- 13 missing positions after restart (state sync failure)
- 14 SIGKILL force-kills (heartbeat starvation from IPC processing)
- 115 consecutive alert delivery failures
- Phantom positions from stale broker maps
- SmartRouter broker_map out of sync after reconciliation
- POP_SIGNAL 7x duplication across IPC

## Architecture

```
Data Collector (separate, every 10 min)
  +-- alt_data_cache.json
        +-- Fear & Greed, Finviz, SEC EDGAR, Yahoo
        +-- Polygon, Benzinga, StockTwits
        +-- Unusual Options Flow

Core Process (single process):
  |
  +-- Ticker Universe Manager
  |     +-- Base tickers (config)
  |     +-- Momentum screener (every 30 min)
  |     +-- Discovery from Data Collector (th-discovery)
  |     +-- Pop full scan (every 10 min, reads alt_data_cache.json)
  |     +-- Pop headline check (every 2 min, breaking news)
  |
  +-- VWAP StrategyEngine (per BAR)
  |     +-- Sentiment deferral (defers to Pro when sentiment > 0.3)
  |     +-- Sentiment threshold relaxation (RVOL 2.0->1.5, RSI 40-75->35-80)
  |
  +-- ProSetupEngine (per BAR, merged in-process)
  |     +-- 11 existing detectors (unchanged)
  |     +-- SentimentDetector (8 alt data sources, dual stock/options weights)
  |     +-- NewsVelocityDetector (Benzinga + Yahoo headlines)
  |     +-- Classifier: highest confidence wins (not first tier match)
  |     +-- Sentiment lowers detector thresholds by up to 25%
  |     +-- Precomputed shared indicators (VWAP/ATR/RSI/EMAs once per ticker)
  |
  +-- RiskEngine (VWAP rules — separate)
  |     +-- V8: Per-strategy position count (Pro positions don't block VWAP)
  |     +-- V8: Sentiment threshold relaxation
  |     +-- V8: Beta-weighted exposure check
  |
  +-- RiskAdapter (Pro rules — separate)
  |     +-- V8: Cross-engine sector concentration (shared positions dict)
  |     +-- V8: Earnings calendar block for T2/T3
  |     +-- V8: ORDER_FAIL cleanup (prevents permanent ticker lock)
  |
  +-- PortfolioRiskGate (shared aggregate limits)
  |     +-- V8: Unrealized P&L tracking (BAR subscription for live prices)
  |     +-- V8: Buying power cache (60s, prevents 12s blocking)
  |     +-- V8: Un-halt hysteresis (20% buffer above drawdown threshold)
  |     +-- V8: Overnight position re-seed on reset_day
  |
  +-- PerStrategyKillSwitch (V8, per-strategy daily loss tracking)
  +-- RegistryGate (FIFO blocked_ids, stale entry cleanup)
  +-- SmartRouter -> Alpaca/Tradier
  +-- Single PositionManager -> single bot_state.json
  +-- POP_SIGNAL publisher -> Redpanda -> Options

Options (separate process):
  +-- Receives SIGNAL + POP_SIGNAL via Redpanda

Data Collector (separate process):
  +-- Collects all 8 sources every 10 min
  +-- V8: Writes ALL sources to data_source_snapshots DB table
```

## Event Flow

```
Market Data -> RealTimeMonitor (bars + rvol cache)
                    |
              emit_batch(BAR)
                    |
    +---------------+----------------+
    |               |                |
VwapStrategy   ProSetupEngine   Pop scan (periodic)
    |          +-------------+       |
    |          | 13 detectors|       | 6 pop rules ->
    |          | + Sentiment |<-- alt_data_cache.json
    |          | + NewsVel   |       |
    |          +------+------+       |
    |                 |              |
  SIGNAL     PRO_STRATEGY_SIGNAL     POP_SIGNAL
    |                 |              |
    v                 v              +-> Redpanda -> Options
 RiskEngine      RiskAdapter
 (VWAP rules)    (Pro rules)
    |                 |
    +--------+--------+
             |
       ORDER_REQ (shared bus)
             |
       PortfolioRiskGate
             |
       PerStrategyKillSwitch
             |
       RegistryGate
             |
       SmartRouter -> Alpaca/Tradier
             |
           FILL
             |
       PositionManager -> bot_state.json
```

## Key Design Decisions

### 1. SentimentDetector (8 sources, dual weight profiles)

| Source | Stock Weight | Options Weight |
|--------|-------------|----------------|
| Benzinga headlines | 0.20 | 0.15 |
| StockTwits social | 0.15 | 0.10 |
| **UOF** | **0.25** | **0.35** |
| Finviz insider | 0.05 | 0.05 |
| SEC EDGAR | 0.05 | 0.05 |
| Yahoo earnings | 0.10 | 0.15 |
| Polygon volume | 0.10 | 0.05 |
| **F&G** | **multiplier** | **multiplier** |

F&G multiplier: `0.7 + (fear_greed_value / 100) * 0.6` (range 0.7x to 1.3x)
UOF threshold: relative (premium / avg daily options volume), not fixed dollar.

### 2. Classifier — Highest Confidence Wins

V7: first tier match wins (T3 checked before T2 before T1).
V8: ALL rules evaluated, highest confidence returned. Tie-break by tier.

Sentiment boost: `adjusted_threshold = base * (1.0 - sentiment * 0.25)` (max 25% reduction).

### 3. Tier-Based EOD + Stops + Trailing

| Tier | EOD Action | Stop Width | Trailing |
|------|-----------|-----------|----------|
| T1 (sr_flip, trend_pullback) | Full close | 0.4-0.5 ATR (tight) | None (closes at EOD) |
| T2 (orb, gap_and_go, inside_bar, flag_pennant) | Partial sell | 1.5-2.0 ATR (overnight) | Breakeven at +1R, lock +0.5R at +2R |
| T3 (momentum, fib, bollinger, liquidity) | Partial sell | 2.0-2.5 ATR (multi-day) | Breakeven at +1R, lock +1R at +2R |

T2/T3 use the WIDER of ATR and structural stop (not tighter).

### 4. Pop as Scanner (No Independent Trading)

Pop no longer trades. It:
1. Scans alt_data_cache.json every 10 min for momentum tickers
2. Quick headline check every 2 min for breaking news
3. Adds qualifying tickers to scan universe (VWAP + Pro benefit)
4. Emits POP_SIGNAL to Options for directional entries

### 5. Three-Tier State Recovery

```
Restart:
  Tier 1: bot_state.json (fast, local)
  Tier 2: DB event_store (durable, survives any crash)
  Tier 3: Broker APIs (absolute truth for qty)

Merge: richest source wins for stop/target/strategy.
Broker wins for qty/entry_price.
```

### 6. Per-Strategy Budgets and Kill Switches

| Strategy | Budget | Max Positions | Kill Switch |
|----------|--------|--------------|-------------|
| VWAP | $1,000 | 5 | -$10,000/day |
| Pro | $1,000 | 15 | -$2,000/day |
| Options | $500 | 5 | -$3,000/day (separate process) |

### 7. Scalability (Option D Tiered Scanning)

At 500 tickers, quick filter + cold rotation:
- Quick filter: all tickers, ~20ms each (RVOL, VWAP proximity, sentiment)
- Hot tickers (~75): full 13-detector analysis every cycle
- Cold rotation (1/5 of ~420): full analysis, rotates each cycle

## Files Created in V8

| File | Purpose |
|------|---------|
| `pro_setups/detectors/sentiment_detector.py` | 8-source composite sentiment |
| `pro_setups/detectors/news_velocity_detector.py` | Headline velocity + earnings |
| `monitor/kill_switch.py` | Per-strategy daily loss halt |
| `monitor/ticker_scanner.py` | Tiered scanning for 500+ tickers |

## Files Modified in V8

42 files modified across all 9 layers. See plan file for complete list.

## 104 Issues Fixed

9-layer audit found 104 issues (11 critical, 36 high, 30 medium, 27 low).
All critical and high issues implemented and verified.

## Post-Deployment Monitoring

See `V8_POST_RUN_CHECKLIST.md` for runtime verification items.
