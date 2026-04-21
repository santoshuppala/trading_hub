# Engine Maturity Comparison — 2026-04-15

## Core vs Options vs Pro vs Pop: Feature Gap Analysis

### Core Engine = Benchmark (most evolved)

The Core engine (VWAP strategy + broker + position management) is the production-grade benchmark. Every other engine should eventually reach this level.

---

## Feature Matrix

| Feature | Core | Pro | Pop | Options |
|---------|:----:|:---:|:---:|:-------:|
| **LIFECYCLE** | | | | |
| EOD Summary Report | YES | NO | NO | NO |
| P&L Reconciliation (Alpaca ground truth) | YES | NO | NO | NO |
| Daily Fees Collection | YES | NO | NO | NO |
| Orphaned Trade Audit | YES | NO | NO | NO |
| Pre-market Connectivity Check | YES | NO | NO | NO |
| Session Equity Baseline | YES | NO | NO | NO |
| Pre-market Order Audit (cancel stale) | YES | NO | NO | NO |
| EOD Position Force-Close | YES | NO | NO | EXISTS but NEVER CALLED |
| Graceful Shutdown with Summary | YES | NO | NO | NO |
| EOD Gate (no new entries) | YES (3:00 PM) | YES (3:45 PM) | YES (3:45 PM) | YES (3:45 PM) |
| | | | | |
| **RISK MANAGEMENT** | | | | |
| Kill Switch (daily loss halt) | YES | NO | NO | NO |
| Force-Sell All on Kill Switch | YES | NO | NO | NO |
| Portfolio Risk Gate (aggregate) | YES | NO | NO | Partial (per-trade) |
| Daily Trade Limit | NO | NO | NO | YES (50) |
| Position Limit | YES (5) | YES (via RiskAdapter) | YES (15) | YES (5) |
| Per-Ticker Cooldown | YES (300s) | YES (300s) | YES (300s) | YES (300s) |
| Sector Concentration Limit | NO | NO | YES (2/sector) | NO |
| R:R Validation | YES | YES | NO | NO |
| Min Stop Distance Floor | YES (0.3%) | YES (0.3%) | NO | NO |
| Bracket Orders (broker-side stop) | YES | YES (via IPC) | NO | NO |
| | | | | |
| **CRASH RECOVERY** | | | | |
| State Persistence (disk) | YES (bot_state.json) | NO | NO | NO |
| Crash Recovery (Redpanda replay) | YES | NO | NO | NO |
| Position Reconciliation on Startup | YES (Alpaca + Tradier) | NO | NO | NO |
| Stale Position Removal | YES | NO | NO | NO |
| Broker Position Scan on Start | YES | NO | NO | NO |
| Cooldown State Recovery | YES | NO | NO | NO |
| | | | | |
| **MONITORING & OBSERVABILITY** | | | | |
| Heartbeat Logging | YES | NO | YES (5 min) | NO |
| Hourly P&L Summary | YES | NO | NO | NO |
| Hourly Position Reconciliation | YES (Alpaca + Tradier) | NO | NO | NO |
| Performance Metrics (win rate, R:R) | YES (in heartbeat) | NO (raw data only) | NO (raw data only) | Partial (daily_pnl) |
| Signal Quality Tracking | NO | NO | NO | NO |
| Execution Quality (slippage) | NO | NO | NO | NO |
| | | | | |
| **ALERTING** | | | | |
| Email Alerts on Position Open | YES | NO | NO | NO |
| Email Alerts on Position Close | YES | NO | NO | NO |
| Email Alerts on Kill Switch | YES | NO | NO | NO |
| Email Alerts on Crash/Error | YES | NO | NO | NO |
| Alert Rate Limiting | YES | N/A | N/A | N/A |
| | | | | |
| **DB PERSISTENCE** | | | | |
| Events to event_store | YES | YES | YES | YES |
| Projection Tables Populated | YES (today) | YES (today) | YES (today) | YES (today) |
| completed_trades | YES | YES (via core) | YES (via core) | NO |
| Broker Tag on Events | YES | YES (via core) | YES (via core) | NO |
| | | | | |
| **DATA PIPELINE** | | | | |
| RVOL Engine | YES | YES | N/A | N/A |
| IV Tracking | NO | NO | NO | YES |
| Sentiment Baselines | NO | NO | YES | NO |
| Data Source Circuit Breaker | NO | NO | YES | NO |
| Smart Persistence (dedup) | NO | NO | YES | NO |

---

## Gap Analysis: What Each Engine Needs

### OPTIONS ENGINE — 14 Critical Gaps

**CRITICAL (must fix for paper trading reliability):**

1. **Crash Recovery** — If options process crashes with 3 open iron condors at 2 PM, those positions remain open at Alpaca, unmonitored. No state reload on restart. Risk gate resets to zero deployed capital. Process thinks it has zero exposure.
   - Fix: Scan Alpaca options positions on startup, rebuild `_positions` dict, recompute `_deployed_capital`
   - Files: `options/engine.py`, `scripts/run_options.py`

2. **Daily Loss Circuit Breaker** — No halt if daily_pnl drops below threshold. Options can lose unlimited amounts. The `_daily_pnl` is tracked but never checked against a limit.
   - Fix: Add `MAX_DAILY_LOSS_OPTIONS` check in `_execute_entry()` and `_on_bar()`. On breach: call `close_all_positions()`, disable engine.
   - Files: `options/engine.py`, `options/risk.py`

3. **EOD Position Close NOT CALLED** — `close_all_positions(reason='eod_close')` method exists at line 780 but is NEVER invoked. Process exits at 4 PM without closing positions.
   - Fix: Call `close_all_positions('eod_close')` at 3:55 PM in `run_options.py` main loop
   - Files: `scripts/run_options.py`

4. **Alerts NOT WIRED** — `alert_email` parameter accepted in constructor but `send_alert()` never called anywhere in options/engine.py or options/broker.py.
   - Fix: Add `send_alert()` on entry fill, exit fill, stop-loss trigger, daily loss breach
   - Files: `options/engine.py`, `options/broker.py`

**HIGH (needed for production):**

5. **No EOD Report** — No structured end-of-day summary. Trader must parse logs manually.
   - Fix: Generate EOD summary with: trades opened, trades closed, realized P&L, open positions remaining, Greeks snapshot
   - Files: `options/engine.py` or new `options/observability.py`

6. **No Position Reconciliation** — Engine never checks if its `_positions` dict matches Alpaca's actual options positions.
   - Fix: Periodic (every 30 min) reconciliation: query Alpaca, compare, log mismatches
   - Files: `options/engine.py`, `scripts/run_options.py`

7. **No Greeks EOD Report** — Portfolio delta/theta/vega tracked in memory but never reported.
   - Fix: Log portfolio Greeks in EOD summary and periodic heartbeat
   - Files: `options/engine.py`

8. **No Options completed_trades** — Position closes tracked in event_store but no completed_trades projection for options.
   - Fix: Add options trade records to a dedicated table or tag in completed_trades
   - Files: `db/event_sourcing_subscriber.py`

**MEDIUM:**

9. **No Mark-to-Market Logging** — Current P&L for open positions not logged periodically
10. **No Per-Strategy P&L Breakdown** — Iron condors vs bull call spreads vs butterflies lumped together
11. **No State Persistence** — `_positions` dict not saved to disk
12. **No Heartbeat** — No periodic health/status log
13. **No Signal Quality Metrics** — Can't measure IV rank accuracy or strategy selection quality
14. **No Execution Quality** — No slippage tracking on options fills

---

### PRO ENGINE — 10 Critical Gaps

**CRITICAL:**

1. **No Crash Recovery** — RiskAdapter state (`_positions`, `_last_order`, `_last_signal`) is in-memory only. On restart, all tracking lost. Could attempt duplicate position opens on same ticker.
   - Fix: Persist RiskAdapter state to disk after every mutation, reload on startup
   - Files: `pro_setups/engine.py`, `scripts/run_pro.py`

2. **No EOD Report** — No end-of-day summary of signals generated, strategies used, win rates, P&L.
   - Fix: At 3:55 PM, log summary: signal count by strategy/tier, P&L attribution
   - Files: `pro_setups/engine.py`, `scripts/run_pro.py`

3. **No Alerts** — Pro engine is completely silent on external notification channels. No email on signal generation, position open/close, or errors.
   - Fix: Import and call `send_alert()` on high-confidence signal emission and ORDER_REQ forwarding
   - Files: `pro_setups/engine.py`

**HIGH:**

4. **No Daily Signal Limit** — Unlimited signals can be generated per strategy per day. Only per-ticker cooldown limits frequency.
   - Fix: Add `max_daily_signals_per_strategy` counter in RiskAdapter
   - Files: `pro_setups/risk_adapter.py`

5. **No Kill Switch** — If pro trades accumulate losses, no circuit breaker halts trading
   - Fix: Track pro-originated P&L (via correlation to fills), halt on threshold
   - Files: `pro_setups/engine.py`

6. **No Performance Metrics** — Win rate, average R:R, expectancy, Sharpe not calculated
   - Fix: Aggregate from completed_trades where strategy starts with 'pro:'
   - Files: `pro_setups/engine.py` or new `pro_setups/metrics.py`

7. **No Heartbeat** — No periodic status log showing signal counts, positions, health
   - Fix: Add heartbeat every 5 minutes in `run_pro.py` main loop
   - Files: `scripts/run_pro.py`

**MEDIUM:**

8. **Signal dedup lost on restart** — `_last_signal` dict wiped, allowing duplicate signals in rapid succession after restart
9. **No position detail tracking** — RiskAdapter knows ticker names but not entry prices, stops, or P&L
10. **No shutdown summary** — Process logs "stopped" but no final stats

---

### POP ENGINE — 9 Critical Gaps

**CRITICAL:**

1. **No PopExecutor State Recovery** — `_positions` (open pop positions) and `_last_order_time` (cooldowns) reset to empty on every restart. Orphaned pop positions at Alpaca not tracked.
   - Fix: Save PopExecutor._positions to bot_state.json or separate file. Reload on start.
   - Files: `pop_strategy_engine.py`, `scripts/run_pop.py`

2. **No EOD Report** — No end-of-day summary for pop trades: signals generated, filled, profitable, P&L by pop reason.
   - Fix: At 3:55 PM, generate: "Pop session: X scans, Y candidates, Z filled, W profitable, P&L by reason"
   - Files: `pop_strategy_engine.py`, `scripts/run_pop.py`

3. **No Alerts** — `alert_email` accepted but never used. No notifications on fills, failures, or critical events.
   - Fix: Call `send_alert()` on order fill, order failure, data source disabled
   - Files: `pop_strategy_engine.py`

**HIGH:**

4. **No Daily Loss Limit** — Pop trades can accumulate losses all day without circuit breaker
   - Fix: Track pop-originated P&L, halt on threshold
   - Files: `pop_strategy_engine.py`

5. **No Data Source Recovery** — Benzinga/StockTwits disabled after 10 consecutive failures, stays disabled for entire session. No automatic reconnection.
   - Fix: Add exponential backoff reconnection (retry after 5 min, 10 min, 30 min)
   - Files: `pop_strategy_engine.py`

6. **No Kill Switch Integration** — Pop engine has no connection to the core's kill switch
   - Fix: Listen for kill switch event or check shared flag
   - Files: `pop_strategy_engine.py`

7. **No Performance Metrics** — No intraday pop-specific win rate, P&L, or signal quality tracking
   - Fix: Aggregate metrics, log in heartbeat
   - Files: `pop_strategy_engine.py`

**MEDIUM:**

8. **No Position Reconciliation** — PopExecutor never checks Alpaca for pop-originated positions
9. **No Shutdown Summary** — Logs "stopped" with no final position/P&L report

---

## Implementation Priority Matrix

### Phase 1: Safety (Week 1)
These prevent money loss on paper account:

| Task | Engine | Effort | Impact |
|------|--------|--------|--------|
| Options crash recovery (scan broker positions) | Options | Medium | CRITICAL |
| Options daily loss circuit breaker | Options | Small | CRITICAL |
| Options EOD force-close positions | Options | Small | CRITICAL |
| Pro crash recovery (persist RiskAdapter state) | Pro | Medium | CRITICAL |
| Pop crash recovery (persist PopExecutor positions) | Pop | Small | CRITICAL |
| Kill switch for all engines (shared flag) | All | Medium | CRITICAL |

### Phase 2: Visibility (Week 2)
These let you see what's happening:

| Task | Engine | Effort | Impact |
|------|--------|--------|--------|
| EOD report for Options | Options | Medium | HIGH |
| EOD report for Pro | Pro | Medium | HIGH |
| EOD report for Pop | Pop | Medium | HIGH |
| Heartbeat for Options | Options | Small | HIGH |
| Heartbeat for Pro | Pro | Small | HIGH |
| Wire alerts on all engines | All | Medium | HIGH |
| Position reconciliation (periodic) | Options | Medium | HIGH |
| Performance metrics aggregation | All | Medium | MEDIUM |

### Phase 3: Intelligence (Week 3+)
These make the system smarter:

| Task | Engine | Effort | Impact |
|------|--------|--------|--------|
| Per-strategy P&L breakdown | Pro/Pop | Medium | MEDIUM |
| Signal quality tracking | All | Large | MEDIUM |
| Execution quality (slippage) | All | Medium | MEDIUM |
| Feature importance feedback loop | Pop | Large | LOW |
| Data source auto-recovery | Pop | Small | MEDIUM |
| Greeks dashboard | Options | Medium | MEDIUM |
| Daily signal limits (Pro) | Pro | Small | MEDIUM |

---

## Architecture Recommendation

The biggest structural gap is that **Pro, Pop, and Options are "fire-and-forget" signal generators** while Core is a **full lifecycle manager**. The fix is to give each engine its own lightweight lifecycle module:

```
options/lifecycle.py   — EOD report, crash recovery, position reconciliation, kill switch
pro_setups/lifecycle.py — EOD report, crash recovery, state persistence, metrics
pop_screener/lifecycle.py — EOD report, crash recovery, state persistence, data source health
```

Each lifecycle module would:
1. **Load state on startup** (positions, cooldowns, daily counters)
2. **Persist state on every mutation** (atomic writes)
3. **Reconcile with broker periodically** (every 30 min)
4. **Check kill switch on every P&L update**
5. **Generate EOD report at 3:55 PM**
6. **Send alerts on critical events**

This mirrors what Core already does via `run_monitor.py` (lines 179-1210) but in a modular, reusable way.
