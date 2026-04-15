# Session Test Plan — 2026-04-15 (Tuesday)

**Changes deployed**: Pop screener RVOL/gap fix, Benzinga/StockTwits persistence, sentiment baselines, ETF thresholds, options↔pop integration

**Risk**: MEDIUM — pop screener was dead yesterday, these changes bring it online with real data for the first time

---

## Pre-Market Checks (Before 9:30 AM ET)

### 1. Verify preflight logs
```bash
tail -100 logs/monitor_2026-04-15.log | grep -i "preflight\|baseline\|equity\|PopStrategy\|SentimentBaseline"
```
**Expect**:
- [ ] `SESSION EQUITY BASELINE` block with previous close equity
- [ ] `PREFLIGHT POSITION & ORDER AUDIT` — clean slate or swing positions listed
- [ ] `PopStrategyEngine ready | paper=True | max_positions=25`
- [ ] `SentimentBaseline ready | headline_tickers=0 | social_tickers=0 | from_db=False` (first day, no history yet)
- [ ] NO `permanently failed` errors for PopStrategyEngine

### 2. Verify no startup crashes
```bash
grep -i "error\|failed\|traceback" logs/monitor_2026-04-15.log | head -20
```
**Expect**: No errors related to `rvol`, `MarketDataSlice`, `sector_map`, `sentiment_baseline`

---

## During Market Hours (9:30 AM - 4:00 PM ET)

### 3. Pop Screener — Is it alive?
**Check every 30 minutes:**
```bash
# Are BARs being processed (not crashing)?
grep "POP skip\|POP data\|POP features\|POP CANDIDATE\|POP no candidate" logs/monitor_2026-04-15.log | tail -10
```
**Expect**:
- [ ] `POP skip` messages with real RVOL values (NOT all 1.0) and real gap values (NOT all 0.0)
- [ ] Some `POP data` messages showing Benzinga/StockTwits fetches (headline counts, bull/bear %)
- [ ] Some `POP features` messages showing computed features
- [ ] `POP no candidate` is fine — means screener evaluated but thresholds not met
- [ ] `POP CANDIDATE` = a signal was generated (may or may not happen on a quiet day)

### 4. RVOL values — are they real?
```bash
grep "POP skip\|POP features" logs/monitor_2026-04-15.log | grep -o "rvol=[0-9.]*" | sort | uniq -c | sort -rn | head -10
```
**Expect**:
- [ ] Distribution of RVOL values (NOT all 1.0)
- [ ] Most values between 0.3 and 3.0
- [ ] Some ETFs (QQQ, SPY) showing values around 0.5-1.5

**RED FLAG**: If all rvol=1.0 → `payload.rvol_df` is empty or not being passed correctly

### 5. Gap size — is it real?
```bash
grep "POP skip\|POP features" logs/monitor_2026-04-15.log | grep -o "gap=[0-9.-]*" | sort | uniq -c | sort -rn | head -10
```
**Expect**:
- [ ] Variety of gap values (positive and negative)
- [ ] NOT all 0.0000

**RED FLAG**: If all gap=0.0 → `payload.rvol_df` has no close column or is empty

### 6. ETF RVOL thresholds — are ETFs passing through?
```bash
grep -E "\b(QQQ|SPY|IWM|TQQQ|XLK)\b" logs/monitor_2026-04-15.log | grep -v "chain\|permanently" | head -20
```
**Expect**:
- [ ] ETF BARs should NOT be blocked by RVOL (threshold is 0.7 not 2.0)
- [ ] Some ETF signals or at least `POP features` for ETFs
- [ ] If VWAP SIGNAL for an ETF: risk gate should pass RVOL check at 0.7

### 7. Benzinga/StockTwits data persistence
```bash
# Check if events are being written to DB
psql -d tradinghub -c "SELECT COUNT(*), event_type FROM event_store WHERE event_type IN ('NewsDataSnapshot', 'SocialDataSnapshot') AND event_time >= CURRENT_DATE GROUP BY event_type;"
```
**Expect**:
- [ ] `NewsDataSnapshot` count growing (should be 20-50+ by midday)
- [ ] `SocialDataSnapshot` count growing (same range)
- [ ] If both are 0 → check if DB is connected and EventSourcingSubscriber registered

### 8. Benzinga data quality
```bash
psql -d tradinghub -c "
SELECT event_payload->>'ticker' as ticker,
       event_payload->>'headlines_1h' as hl_1h,
       event_payload->>'avg_sentiment_1h' as sent,
       event_payload->>'top_headline' as headline,
       event_payload->>'news_fetched_at' as fetched,
       event_payload->>'latest_headline_time' as news_time
FROM event_store
WHERE event_type = 'NewsDataSnapshot'
  AND event_time >= CURRENT_DATE
ORDER BY event_time DESC LIMIT 10;
"
```
**Expect**:
- [ ] Real headline text (not empty)
- [ ] Sentiment scores between -1.0 and 1.0
- [ ] `news_fetched_at` timestamps present
- [ ] `latest_headline_time` timestamps present (for latency analysis)

### 9. StockTwits data quality
```bash
psql -d tradinghub -c "
SELECT event_payload->>'ticker' as ticker,
       event_payload->>'mention_count' as mentions,
       event_payload->>'mention_velocity' as velocity,
       event_payload->>'bullish_pct' as bull,
       event_payload->>'bearish_pct' as bear,
       event_payload->>'social_fetched_at' as fetched
FROM event_store
WHERE event_type = 'SocialDataSnapshot'
  AND event_time >= CURRENT_DATE
ORDER BY event_time DESC LIMIT 10;
"
```
**Expect**:
- [ ] Mention counts > 0 for popular tickers
- [ ] Bullish/bearish percentages that aren't all 0.50/0.50
- [ ] `social_fetched_at` timestamps present

### 10. Sentiment baseline building (intraday)
```bash
grep "SentimentBaseline\|baseline" logs/monitor_2026-04-15.log | tail -5
```
**Expect**:
- [ ] Initial load shows 0 tickers (first day)
- [ ] No errors from baseline engine

### 11. Options engine — POP_SIGNAL integration
```bash
grep "OptionsEngine.*POP_SIGNAL\|OPTIONS_SIGNAL.*pop_signal" logs/monitor_2026-04-15.log | head -10
```
**Expect**:
- [ ] If any pop candidates are found, options engine should log `POP_SIGNAL → TICKER strategy_type`
- [ ] If no pop candidates today, this will be empty (that's OK)

### 12. Intraday reconciliation
```bash
grep "Intraday reconciliation" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] Runs at :30 of each hour (10:30, 11:30, 12:30, etc.)
- [ ] `Intraday reconciliation OK: N positions match`
- [ ] If MISMATCH appears → investigate immediately

### 13. Hourly summary — equity P&L included
```bash
grep "Hourly summary" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] Shows both trade P&L and Equity PnL (Alpaca)
- [ ] Format: `Hourly summary: N trades | WW/LL | PnL: $X.XX | Equity PnL: $Y.YY (Alpaca)`

### 14. Kill switch — immediate on FILL
```bash
grep "KillSwitchGuard\|KILL SWITCH" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] No kill switch activation (hopefully!)
- [ ] If it fires, should show `KILL SWITCH (immediate)` not just heartbeat check

---

## After Market Close (4:00 PM ET)

### 15. EOD P&L reconciliation
```bash
grep -A 20 "P&L RECONCILIATION" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] `Ending Equity` populated
- [ ] `Prev Close Equity` matches yesterday's close ($1,005,527.43)
- [ ] `Trade-log PnL` — sum of closed trades
- [ ] `Alpaca Fees/Reg` — regulatory fees (may be $0 on paper)
- [ ] `Fee-Adjusted PnL` = trades - fees
- [ ] `Equity-Based PnL` = ending - prev close (Alpaca ground truth)
- [ ] `Discrepancy` — ideally within $1.00
- [ ] If discrepancy > $1: `P&L DISCREPANCY` warning with causes listed

### 16. Orphaned trade audit
```bash
grep -A 15 "ORPHANED.*TRADE AUDIT" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] `Alpaca filled orders today: N`
- [ ] `Monitor trade_log entries: M`
- [ ] `No orphaned or missed trades` (ideal)
- [ ] If MISSED/PHANTOM trades found → note for investigation

### 17. Pop screener summary — did it work?
```bash
echo "=== Pop activity ==="
grep -c "POP CANDIDATE" logs/monitor_2026-04-15.log
echo "=== Pop signals ==="
grep -c "POP_SIGNAL" logs/monitor_2026-04-15.log
echo "=== Pop errors ==="
grep -c "permanently failed.*Pop" logs/monitor_2026-04-15.log
echo "=== Pop skips (early exit) ==="
grep -c "POP skip" logs/monitor_2026-04-15.log
echo "=== Benzinga fetches ==="
grep -c "POP data" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] Pop errors = 0 (CRITICAL — if > 0, the fix didn't work)
- [ ] Pop skips > 0 (normal — most bars are quiet)
- [ ] Benzinga fetches > 0 (some tickers passed early exit and fetched data)
- [ ] Pop candidates >= 0 (may be 0 on a quiet day, but should fire on volatile days)

### 18. Sector concentration blocks
```bash
grep "sector.*already has\|BLOCKED.*sector" logs/monitor_2026-04-15.log | head -10
```
**Expect**:
- [ ] Sector blocks firing when 2+ positions in same sector

### 19. DB event counts
```bash
psql -d tradinghub -c "
SELECT event_type, COUNT(*) as cnt
FROM event_store
WHERE event_time >= CURRENT_DATE
GROUP BY event_type
ORDER BY cnt DESC;
"
```
**Expect**:
- [ ] `NewsDataSnapshot` — 50-200+ (depends on how many tickers passed early exit)
- [ ] `SocialDataSnapshot` — similar count
- [ ] `BarReceived` — thousands (every ticker every ~10s)
- [ ] Other event types as usual

### 20. Baseline data for tomorrow
```bash
psql -d tradinghub -c "
SELECT event_payload->>'ticker' as ticker,
       COUNT(*) as snapshots,
       AVG((event_payload->>'headlines_1h')::float) as avg_hl,
       AVG((event_payload->>'mention_velocity')::float) as avg_vel
FROM event_store
WHERE event_type IN ('NewsDataSnapshot', 'SocialDataSnapshot')
  AND event_time >= CURRENT_DATE
GROUP BY event_payload->>'ticker'
ORDER BY snapshots DESC
LIMIT 20;
"
```
**Expect**:
- [ ] Multiple tickers with 5+ snapshots each (enough for baselines tomorrow)
- [ ] Avg headline and velocity values that look reasonable
- [ ] Tomorrow's session should log: `SentimentBaseline ready | headline_tickers=N | from_db=True`

---

## Things to Watch For (Red Flags)

| Red Flag | What it Means | Action |
|---|---|---|
| `permanently failed.*rvol` | RVOL bug not fixed | Check `_bar_payload_to_slice` — is `payload.rvol_df` populated? |
| All `rvol=1.0` in POP logs | `rvol_df` empty or not passed | Check Tradier `fetch_batch_bars` returns rvol_cache |
| All `gap=0.0` in POP logs | Prior close not extracted | Check `rvol_df['close'].iloc[-1]` |
| `NewsDataSnapshot` count = 0 | DB persistence not working | Check EventSourcingSubscriber registered NEWS_DATA |
| `SocialDataSnapshot` count = 0 | Same | Check SOCIAL_DATA registration |
| `[StockTwits] Rate limit` | Hitting 200/hr limit | Normal if many tickers pass early exit — rate limiter handles it |
| RVOL block on QQQ/SPY | ETF threshold not applied | Check `is_etf()` returns True for the ticker |
| Pop candidates but no trades | Executor risk gate blocking | Check cooldown, max positions, sector limits |
| P&L discrepancy > $5 | Missed trades or fees | Review orphaned trade audit section |
| `KILL SWITCH (immediate)` | Daily loss limit breached | Review recent trades for pattern |

---

## Success Criteria

**Minimum bar for "pop screener is working":**
- [ ] Zero `permanently failed` errors for PopStrategyEngine
- [ ] RVOL values in logs are NOT all 1.0
- [ ] Gap values are NOT all 0.0
- [ ] At least 10 `POP data` messages (Benzinga/StockTwits fetched)
- [ ] At least 1 `NewsDataSnapshot` and `SocialDataSnapshot` in event_store

**Bonus (may not happen on a quiet day):**
- [ ] At least 1 `POP CANDIDATE` found
- [ ] At least 1 `POP_SIGNAL` emitted
- [ ] Options engine logs `POP_SIGNAL →` for a pop candidate
- [ ] ETF (QQQ/SPY) passes RVOL threshold somewhere in the pipeline

---

## Additional Checks (Added After Initial Plan)

### 15. Multi-Broker Smart Routing
```bash
grep "SmartRouter\|TradierBroker\|BROKER_MODE" logs/core_2026-04-15.log | head -10
```
**Expect**:
- [ ] `SmartRouter active | brokers=alpaca+tradier | mode=smart`
- [ ] `TradierBroker ready | account=VA92176885 | mode=sandbox`
- [ ] If Tradier fails: `FAILOVER` log + circuit breaker message

### 16. Portfolio Risk Gate
```bash
grep "PortfolioRisk" logs/core_2026-04-15.log | head -10
```
**Expect**:
- [ ] `PortfolioRiskGate ready | drawdown=$-5000 | notional=$100000`
- [ ] No `DRAWDOWN HALT` (hopefully)
- [ ] If orders blocked: `BLOCKED` with reason (notional/margin/Greeks)

### 17. Beta-Adjusted Sizing
```bash
grep "Size adjusted" logs/core_2026-04-15.log | head -10
```
**Expect**:
- [ ] TQQQ/SOXL entries show `Size adjusted: N → M (beta=3.00)`
- [ ] SPY/QQQ entries show smaller adjustments or no adjustment

### 18. Correlation Risk (News-Aware)
```bash
grep "correlation" logs/core_2026-04-15.log | head -10
```
**Expect**:
- [ ] `correlation_block` when 3+ tickers from same group held
- [ ] `correlation_override` if ticker has strong news catalyst
- [ ] No false blocks on unrelated tickers

### 19. Data Source Collector
```bash
grep "DataSourceCollector\|Collector" logs/core_2026-04-15.log | head -10
```
**Expect**:
- [ ] `DataSourceCollector: session start collection complete`
- [ ] Periodic collections at :10, :20, :30, :40, :50
- [ ] Fear & Greed + FRED values logged

### 20. News/Social Circuit Breaker
```bash
grep "news.*disabled\|social.*disabled\|news_failures\|social_failures" logs/pop_2026-04-15.log | head -5
```
**Expect**:
- [ ] No disabled messages (APIs should work)
- [ ] If Benzinga down: `news source disabled after 10 failures`

### 21. FILL Dedup
```bash
grep "Duplicate FILL" logs/core_2026-04-15.log
```
**Expect**:
- [ ] Zero duplicate FILLs (clean execution)
- [ ] If any: dedup correctly prevented double-processing

### 22. Smart Persistence (Data Sources)
```bash
psql -d tradinghub -c "
SELECT event_type, COUNT(*) as writes
FROM event_store
WHERE event_type LIKE 'DataSource_%%'
  AND event_time >= CURRENT_DATE
GROUP BY event_type ORDER BY writes DESC;"
```
**Expect**:
- [ ] `DataSource_fear_greed`: 5-15 writes (not 500+)
- [ ] `DataSource_fred_macro`: 1-3 writes (barely changes intraday)
- [ ] Total across all sources: <200 (smart persistence working)

### 23. Process Isolation Logs (Separate Per Engine)
```bash
ls -la logs/core_2026-04-15.log logs/pro_2026-04-15.log logs/pop_2026-04-15.log logs/options_2026-04-15.log logs/supervisor_2026-04-15.log
```
**Expect**:
- [ ] All 5 log files exist
- [ ] Each has content (not empty)
- [ ] Supervisor shows all 4 processes started

### 24. Tradier Reconciliation
```bash
grep "Tradier reconciliation" logs/core_2026-04-15.log
```
**Expect**:
- [ ] Runs at :30 of each hour alongside Alpaca reconciliation
- [ ] `Tradier reconciliation: N positions` or no positions

---

## Changes Deployed in This Session

| Change | Files | What |
|---|---|---|
| Pop RVOL fix | `pop_strategy_engine.py` | Real RVOL from `payload.rvol_df` (was always 1.0) |
| Pop gap_size fix | `pop_strategy_engine.py` | Real gap from prior close (was always 0.0) |
| Pop observability | `pop_strategy_engine.py` | Debug logging at 6 decision points |
| Benzinga/StockTwits persistence | `events.py`, `event_bus.py`, `event_sourcing_subscriber.py` | NEWS_DATA + SOCIAL_DATA events to DB |
| News/social timestamps | `events.py`, `pop_strategy_engine.py`, `stocktwits_social.py` | Full timing chain for latency analysis |
| Sentiment baselines | `pop_screener/sentiment_baseline.py` (NEW) | Per-ticker baselines from DB history |
| ETF RVOL thresholds | 7 files across all engines | Lower thresholds for 21 ETFs (0.7 vs 2.0) |
| Options ↔ Pop integration | `options/engine.py`, `events.py` | POP_SIGNAL → directional options trades |
| Entry condition logging | `monitor/strategy_engine.py` | Debug logs per ticker showing which condition failed |
| Process isolation | `scripts/supervisor.py`, `run_core/pro/pop/options.py` | 4 independent processes with IPC via Redpanda |
| Self-healing | `scripts/watchdog.py`, `scripts/crash_analyzer.py` | Auto-diagnose + fix + restart on crash |
| Multi-broker | `monitor/tradier_broker.py`, `monitor/smart_router.py` | Alpaca + Tradier with failover |
| Portfolio risk gate | `monitor/portfolio_risk.py` | Drawdown halt, notional cap, margin check, Greeks limits |
| Risk sizing | `monitor/risk_sizing.py` | Beta-adjusted, correlation (14 groups), volatility-scaled |
| News-aware correlation | `monitor/risk_sizing.py` | Ticker-specific catalysts override correlation blocks |
| 9 data sources | `data_sources/*` | Yahoo, FRED, Finviz, EDGAR, Polygon, Fear&Greed, AlphaVantage |
| Smart persistence | `data_sources/persistence.py`, `data_sources/collector.py` | Hash-based dedup, ~100 writes/day |
| Pro dashboard | `dashboards/pro_dashboard.py` | Dark terminal theme with risk + market tabs |
| Thread safety | `pop_strategy_engine.py` | threading.Lock on PopExecutor._positions |
| Memory bound | `backtests/sync_bus.py` | deque(maxlen=50000) |
| Chain rate limit | `options/chain.py` | _RateLimiter 3 req/s + 429 retry |
| Magic numbers to config | `config.py` + 3 files | 7 hardcoded values → env-configurable |
| FILL dedup | `monitor/position_manager.py` | event_id dedup (10K bounded set) |
| Broker-down halt | `monitor/smart_router.py` | CRITICAL alert when all brokers unhealthy |
| Backtest determinism | `backtests/sync_bus.py` | SimulatedTimeSource injection |
| Exception handlers | 5 files | 12 silent `except: pass` → logged |
| DB in satellites | `scripts/_db_helper.py` | PRO/POP/OPTIONS signals persisted in isolated mode |

---

## Post-Session Evaluation: Strategy Gaps to Investigate

### Momentum Grind / Trend Continuation Detector (evaluate after this session)

**Problem observed 2026-04-14**: QQQ moved $620→$628 (1.3%) but was never traded by any engine. The move was a slow, steady grind — no sharp gap, no opening range breakout, no VWAP dip-and-reclaim. All existing detectors are designed for sharp, catalyst-driven moves.

**What to look for in tomorrow's logs**:
```bash
# Find ETFs/mega-caps that moved 1%+ but were never signaled
grep "StrategyEngine.*SKIP.*QQQ\|StrategyEngine.*SKIP.*SPY\|StrategyEngine.*SKIP.*IWM" logs/monitor_2026-04-15.log | head -10
# Check which condition blocked them
grep "StrategyEngine.*SKIP" logs/monitor_2026-04-15.log | grep -o "SKIP:.*|" | sort | uniq -c | sort -rn
```

**Proposed detector (build only if pattern repeats)**:

A `MomentumGrindDetector` that fires when:
1. Price above VWAP for 30+ consecutive bars (steady trend, not a spike)
2. Higher lows over the window (each 10-bar chunk's low > previous chunk's low)
3. Cumulative move > 0.3% from session VWAP
4. No sharp pullback > 0.5× ATR in the window

This catches "quiet rally" patterns that existing detectors miss — stocks grinding up without a catalyst. Lower R:R than sharp breakouts (no clear stop from a breakout candle), but captures moves like QQQ's 1.3% day.

**Decision**: Evaluate after 2026-04-15 session. If multiple ETFs/mega-caps have 1%+ moves with zero signals, build it. If the pop screener + ETF RVOL fix already catches these via UNUSUAL_VOLUME or SENTIMENT_POP, skip it.

---

## Live Session Fixes Applied — 2026-04-15

### Summary
98 trades executed, 46% win rate. ~35 files modified across 30+ fixes. System went from broken (watchdog crash loop, options blowup, zero pop trades, no Tradier execution) to stable (zero errors, zero shorts, bracket stop-losses, multi-broker routing, full DB persistence).

### Critical Fixes

| # | Fix | Files | Impact |
|---|-----|-------|--------|
| 1 | **Watchdog lock file loop** — pre-flight check + stale lock removal | `scripts/watchdog.py` | Prevented 6 crash reports from a single stale lock |
| 2 | **Options poll race (.value enum fix)** — `str(OrderStatus.FILLED)` returned `"OrderStatus.FILLED"` not `"filled"` | `options/broker.py` | **Fixed 213 duplicate fills that blew $105K on paper account** |
| 3 | **Options race condition (TOCTOU)** — multiple threads passed risk check simultaneously | `options/risk.py`, `options/engine.py` | Added `reserve()`/`unreserve()` atomic locking, daily trade limit (50) |
| 4 | **Options close orders ($0.00 limit)** — close orders submitted with `limit_price=0` | `options/broker.py` | Changed to market orders for closes |
| 5 | **Options mleg URL double /v2** — `_request("POST", "/v2/orders")` → `/v2/v2/orders` | `options/broker.py` | Path fixed to `/orders` |
| 6 | **Options mleg qty format** — Alpaca needs top-level `qty`, per-leg `ratio_qty` only | `options/broker.py` | Fixed order body structure |
| 7 | **Double SELL (broker + SmartRouter)** — AlpacaBroker subscribed to ORDER_REQ AND SmartRouter called it directly | `monitor/smart_router.py` | Unsubscribe broker when SmartRouter active |
| 8 | **SELL routing to wrong broker** — round-robin split BUY/SELL across brokers creating shorts | `monitor/smart_router.py` | SELL routes to opening broker; round-robin only for BUY |
| 9 | **Partial sell cleared broker mapping** — `_position_broker.pop()` on any SELL | `monitor/smart_router.py` | Only clear on POSITION CLOSED event |
| 10 | **Position broker mapping lost on restart** — in-memory dict wiped | `monitor/smart_router.py` | Persisted to `data/position_broker_map.json`, loaded on startup |
| 11 | **Bracket order double-sell** — strategy engine sells + bracket stop fires = short | `monitor/brokers.py`, `monitor/tradier_broker.py` | Cancel all bracket/stop orders before SELL |
| 12 | **Stale positions after restart** — bot_state.json had positions closed by bracket stops | `monitor/monitor.py` | Reverse reconciliation removes local positions not at any broker |
| 13 | **Tradier OTOCO orphans** — bracket stops on Tradier can't be reliably cancelled | `monitor/tradier_broker.py` | Disabled Tradier brackets; simple limit orders only |

### Stop Loss Improvements

| # | Fix | Files | Impact |
|---|-----|-------|--------|
| 14 | **Micro-stops (0.04% from entry)** — 1-minute ATR too small for meaningful stops | `pro_setups/strategies/base.py` | Added 0.3% minimum floor |
| 15 | **Structure-aware stops** — full-day S/R levels with touch counting | `pro_setups/strategies/base.py` | Pivot detection, cluster scoring, strongest level wins |
| 16 | **Dynamic ATR stops** — daily ATR instead of 1-minute ATR | `pro_setups/strategies/base.py` | `DAILY_ATR_MULT × daily_atr` as fallback when no SR |
| 17 | **R:R validation** — reject trades where stop makes R:R < 1:1 | `pro_setups/engine.py` | Added in `_levels_valid()` |
| 18 | **Engine-level stop floor** — safety net for all strategy overrides | `pro_setups/engine.py` | 0.3% min enforced after `generate_stop()` returns |
| 19 | **VWAP stop floor** — core strategy engine had no minimum | `monitor/strategy_engine.py` | Added 0.3% floor on VWAP stop calculation |
| 20 | **Bracket orders at broker** — positions protected if system crashes | `monitor/brokers.py` | Alpaca `OrderClass.BRACKET` with `StopLossRequest` + `TakeProfitRequest` |
| 21 | **`outputs=` param on all 11 strategy overrides** — needed for S/R-based stops | 10 strategy files | `generate_stop(..., outputs=outputs)` |

### Data & DB Fixes

| # | Fix | Files | Impact |
|---|-----|-------|--------|
| 22 | **completed_trades not populating** — ProjectionBuilder never called | `db/event_sourcing_subscriber.py` | Inline `_write_completed_trade()` on PositionClosed (50 trades today) |
| 23 | **completed_trades schema (trading schema)** — table only in public schema | DB DDL | Created `trading.completed_trades` + added `broker` column |
| 24 | **PositionClosed qty=None** — snapshot is None by design for CLOSED | `monitor/events.py`, `monitor/position_manager.py` | Added `close_detail` dict with qty, entry_price, exit_price |
| 25 | **Broker tag on all events** — which broker executed each trade | `monitor/brokers.py`, `monitor/tradier_broker.py`, `monitor/position_manager.py`, `db/event_sourcing_subscriber.py` | `broker=alpaca/tradier` on FillExecuted, PositionOpened/Closed, completed_trades |
| 26 | **Bracket order tag in DB** — whether broker-side protection is active | `db/event_sourcing_subscriber.py` | `bracket_order=true/false` on FillExecuted |
| 27 | **Session fragmentation (24 sessions)** — random UUID per restart | `scripts/_db_helper.py` | Deterministic daily `uuid5` (4 stable session IDs) |
| 28 | **Session ID column type** — `uuid` rejected non-UUID IDs | DB DDL | Changed `session_id` from `uuid` to `text` |

### Engine Fixes

| # | Fix | Files | Impact |
|---|-----|-------|--------|
| 29 | **RVOL=0.00 in Pro** — RVOLEngine never initialized in Pro process | `scripts/run_pro.py` | Added `init_global_rvol_engine()` + `seed_profiles()` |
| 30 | **RVOL update() never called** — engine seeded but never updated with intraday bars | `pro_setups/engine.py` | Added `_global_rvol_engine.update()` before `compute_rvol()` |
| 31 | **Options IV rank (no history)** — 20-day minimum, 0 days available | `options/iv_tracker.py`, `options/selector.py` | Graduated confidence blend + relaxed raw IV thresholds |
| 32 | **Pop candidates not executing** — router returns empty silently | `pop_strategy_engine.py`, `pop_screener/strategy_router.py` | Added rejection logging + `MomentumEntryEngine` fallback |
| 33 | **SmartRouter + Tradier in isolated mode** — only existed in monolith | `scripts/run_core.py` | Added SmartRouter + TradierBroker init with round-robin |
| 34 | **EOD gate missing** — Pro/Pop/Options had no time cutoff | `pro_setups/engine.py`, `pop_strategy_engine.py`, `options/engine.py` | Added 3:45 PM ET cutoff (no new entries, exits still monitored) |
| 35 | **IndexError on bar data** — transient data gaps from Tradier | `monitor/strategy_engine.py` | Wrapped in try/except (IndexError, KeyError) |

### Infrastructure Fixes

| # | Fix | Files | Impact |
|---|-----|-------|--------|
| 36 | **Supervisor duplicate logs** — StreamHandler + file redirect | `scripts/supervisor.py` | Removed StreamHandler |
| 37 | **Supervisor restarts clean exits** — bogus crash reports | `scripts/supervisor.py` | Skip crash analysis for status='stopped' |
| 38 | **Supervisor restart counter** — never resets | `scripts/supervisor.py` | Reset after 60s stable uptime |
| 39 | **SUPERVISED_MODE** — lock file bypass in isolated mode | `scripts/supervisor.py`, `monitor/monitor.py` | `os.environ['SUPERVISED_MODE'] = '1'` |
| 40 | **StockTwits rate limiting** — 200 requests in 6 seconds | `pop_screener/stocktwits_social.py` | Non-blocking 20s throttle, 15min cache, 180/hr cap |
| 41 | **SMTP alerts** — missing ALERT_EMAIL_TO + stale connections | `.env`, `monitor/alerts.py` | Added env var, fresh connection per send |
| 42 | **SELL qty mismatch** — local state diverged from Alpaca | `monitor/monitor.py` | Startup reconciliation updates local qty from Alpaca |
| 43 | **Reconciliation script** — detect orphaned manual closes | `scripts/reconcile_manual_closes.py` (NEW) | Writes synthetic PositionClosed + completed_trades for manual API closes |
| 44 | **Pop heartbeat logging** — no visibility into data flow | `pop_strategy_engine.py` | 5-minute heartbeat: scans, news_hits, social_hits |

### Files Modified (35 unique)

```
monitor/brokers.py              monitor/tradier_broker.py
monitor/smart_router.py         monitor/position_manager.py
monitor/strategy_engine.py      monitor/events.py
monitor/monitor.py              monitor/alerts.py
db/event_sourcing_subscriber.py scripts/_db_helper.py
scripts/run_core.py             scripts/run_pro.py
scripts/supervisor.py           scripts/watchdog.py
options/engine.py               options/broker.py
options/risk.py                 options/selector.py
options/iv_tracker.py           pop_strategy_engine.py
pop_screener/strategy_router.py pop_screener/stocktwits_social.py
pop_screener/models.py          pro_setups/engine.py
pro_setups/strategies/base.py   pro_setups/strategies/tier1/sr_flip.py
pro_setups/strategies/tier1/trend_pullback.py
pro_setups/strategies/tier1/vwap_reclaim.py
pro_setups/strategies/tier2/*.py (4 files)
pro_setups/strategies/tier3/*.py (4 files)
pop_screener/strategies/momentum_entry_engine.py (NEW)
scripts/reconcile_manual_closes.py (NEW)
```

### New Files Created

| File | Purpose |
|------|---------|
| `pop_screener/strategies/momentum_entry_engine.py` | Simple momentum entry fallback for pop candidates |
| `scripts/reconcile_manual_closes.py` | CLI tool to detect and fix orphaned manual closes |
| `data/position_broker_map.json` | Persistent position→broker mapping (survives restarts) |

### End-of-Day Stats

| Account | Equity | P&L Today | Notes |
|---------|--------|-----------|-------|
| Alpaca Equity (paper) | $1,005,480 | -$47.38 | Started $1,005,527. 98 trades, 47% win |
| Alpaca Options (paper) | $882,014 | -$117,986 | Started $1,000,000. Blowup from duplicate fills (poll race bug) |
| Tradier (sandbox) | $100,009 | +$9 | Started $100,000. First day of Tradier execution |
| **Total** | **$1,987,503** | **-$117,324** | Options loss is 99% of total loss |

| Metric | Value |
|--------|-------|
| Total trades (equity, both brokers) | 98 |
| Win rate | 47% |
| SmartRouter distribution | Alpaca: ~55%, Tradier: ~45% |
| Events persisted to DB | 485,917 |
| completed_trades rows | 50 (was 0 at start of session) |
| Unintended shorts created (all fixed) | ~15 (root causes: duplicate fills, cross-broker sells, stale state) |
| Process restarts (debugging) | ~12 |
| Options duplicate fills (before fix) | 213 orders, ~$105K paper loss |
| Tradier orphaned positions (all closed) | ~10 (from OTOCO brackets — now disabled) |
