# Trading Hub — Remaining TODO

**Last updated**: 2026-04-13
**Overall system rating**: 8/10 (up from 5/10)

---

## P0 — Options Backtest Must Work (Next Session)

### 1. Fix mock chain `find_atm` compatibility
- `SyntheticOptionChainClient.find_atm()` filters by `c.right == option_type` but receives `'call'` while contracts use `'C'`
- Also affects `find_by_delta()` — same mismatch
- **Fix**: In `find_atm` and `find_by_delta`, normalize: `right = 'C' if option_type == 'call' else 'P'`
- **File**: `backtests/mocks/options_chain.py`
- **Effort**: 15 minutes

### 2. Fix selector BAR path for backtesting
- Real data ATR ratios are typically 0.005-0.015 (below current thresholds)
- With IV rank defaulting to 50 (no history), most BAR path conditions don't trigger
- **Fix**: Lower `ATR_MOD_THRESHOLD` to 0.01, or add moderate-IV condition for range-bound markets
- **File**: `options/selector.py`
- **Effort**: 30 minutes

### 3. Run end-to-end options backtest
```bash
python backtests/run_backtest.py --engine options --tickers AAPL NVDA TSLA --start 2026-04-07 --end 2026-04-11
```
- **Effort**: 1 hour (debugging + validation)

### 4. Run longer multi-engine backtest
```bash
python backtests/run_backtest.py --engine all --tickers AAPL NVDA TSLA AMZN META --start 2026-03-20 --end 2026-04-11
```
- Generate CSV reports via CsvReporter
- Analyze win rate by strategy type
- **Effort**: 2 hours

---

## P1 — Database Deployment (Next Trading Day)

### 5. Deploy SQL schemas to production
```bash
# Safe to run anytime (zero write overhead)
psql -d tradinghub -f db/schema_ml_tables.sql
psql -d tradinghub -f db/schema_analytics_views.sql

# Run after market hours
psql -d tradinghub -f db/schema_partition_event_store.sql
```
- **Effort**: 15 minutes

### 6. Run first post-session analytics
```bash
python scripts/post_session_analytics.py --date 2026-04-13
```
- Verify ml_trade_outcomes has MFE/MAE populated
- Verify ml_signal_context has labeled data
- Verify ml_rejection_log has counterfactuals
- **Effort**: 30 minutes

### 7. Backfill historical data
```bash
python scripts/post_session_analytics.py --backfill 30
```
- Populates last 30 days of ML tables from event_store
- **Effort**: 5 minutes (script runs automatically)

---

## P1 — Options Engine Fixes

### 8. Add ORDER_FAIL handler to event_sourcing_subscriber
- Currently missing — failed orders are invisible in the audit trail
- Need to check `OrderFailPayload` fields in `monitor/events.py`
- **File**: `db/event_sourcing_subscriber.py`
- **Effort**: 30 minutes

### 9. Live paper trading test
- Set `APCA_OPTIONS_KEY` and `APCA_OPTIONS_SECRET` env vars
- Run `python run_monitor.py` with `OPTIONS_PAPER_TRADING=true`
- Monitor for 1-2 trading sessions
- Verify: signals generated, orders submitted, fills received, exits triggered
- **Effort**: 2 trading sessions

### 10. Validate IV rank thresholds
- After 20+ trading days of IV history accumulated
- Compare returns with IV rank filtering ON vs OFF
- Tune `IV_RANK_HIGH` (currently 50) and `IV_RANK_LOW` (currently 30)
- **Effort**: 1 day (needs accumulated data)

---

## P2 — Data Architect Gaps

### 11. Dead-letter queue for dropped events
- When DBWriter drops rows (circuit breaker or queue full), they're gone forever
- Write dropped events to `logs/dropped_events.jsonl` for later recovery
- **File**: `db/writer.py`
- **Effort**: 2 hours

### 12. Document dual-write policy
- event_store (PostgreSQL) = source of truth for audit + replay
- TimescaleDB hypertables = optimized for time-series analytics
- Add to `docs/DATA_ARCHITECTURE.md`
- **Effort**: 1 hour

### 13. Fix v_trade_details JOIN uniqueness
- Current JOIN on `aggregate_id` can produce cartesian products if same ticker has multiple trades per session
- Add `session_id` to JOIN condition or use `ROW_NUMBER()` to pick latest PositionClosed per PositionOpened
- **File**: `db/schema_analytics_views.sql`
- **Effort**: 30 minutes

---

## P2 — Data Analyst Gaps

### 14. Win/loss streak tracking view
```sql
CREATE VIEW v_trade_streaks AS
WITH trades_ordered AS (
    SELECT *,
        CASE WHEN pnl > 0 THEN 'W' ELSE 'L' END as result,
        ROW_NUMBER() OVER (ORDER BY exit_ts) as rn
    FROM v_trade_details
),
streak_groups AS (
    SELECT *,
        rn - ROW_NUMBER() OVER (PARTITION BY result ORDER BY exit_ts) as grp
    FROM trades_ordered
)
SELECT
    result as streak_type,
    MIN(exit_ts) as streak_start,
    MAX(exit_ts) as streak_end,
    COUNT(*) as streak_length,
    SUM(pnl) as streak_pnl
FROM streak_groups
GROUP BY result, grp
ORDER BY streak_start;
```
- **File**: `db/schema_analytics_views.sql`
- **Effort**: 30 minutes

### 15. Drawdown curve materialized view
- Needs peak equity tracking via window function
- Compute: peak_equity, current_equity, drawdown_pct, days_in_drawdown
- Run as part of post-session analytics
- **Effort**: 1 hour

### 16. Per-hour performance pre-joined view
```sql
CREATE VIEW v_hourly_performance AS
SELECT
    d.session_phase,
    COUNT(*) as trades,
    AVG(t.pnl) as avg_pnl,
    SUM(CASE WHEN t.pnl > 0 THEN 1 ELSE 0 END)::FLOAT / COUNT(*) as win_rate
FROM v_trade_details t
JOIN dim_time_of_day d
    ON EXTRACT(HOUR FROM t.entry_ts) * 60 + EXTRACT(MINUTE FROM t.entry_ts) = d.minute_of_day
GROUP BY d.session_phase;
```
- **File**: `db/schema_analytics_views.sql`
- **Effort**: 15 minutes

---

## P2 — Tableau Developer Gaps

### 17. Tableau data source file (.tds)
- Pre-configure PostgreSQL connection
- Set up relationships: v_trade_details → dim_strategy → dim_time_of_day
- Define default aggregations and calculated fields
- **Effort**: 2 hours

### 18. Incremental extract strategy
- Add `WHERE event_time > :last_extract_ts` filter to VIEWs
- Or create extract-specific views filtered to last 90 days
- **Effort**: 1 hour

### 19. Column metadata documentation
- Create a data dictionary table or markdown doc mapping each column to business meaning
- Example: `mfe_pct` = "Maximum Favorable Excursion as percentage of entry price — how far the trade went in your favor before closing"
- **Effort**: 2 hours

---

## P3 — ML Engineer Gaps

### 20. Negative examples for signal classifier
- Sample 5-10% of bars where NO signal was generated
- Store in `ml_signal_context` with `was_executed=FALSE, was_rejected=FALSE, action='NO_SIGNAL'`
- Adds ~3,300-6,600 rows/day (5-10% of 66,300 bars)
- **File**: `scripts/post_session_analytics.py` (new job)
- **Effort**: 3 hours

### 21. Model registry table
```sql
CREATE TABLE ml_model_registry (
    model_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_name TEXT NOT NULL,
    model_type TEXT NOT NULL,           -- 'classifier', 'regressor', 'ranker'
    target_variable TEXT NOT NULL,      -- 'outcome_pnl_positive', 'slippage_bps', etc.
    trained_at TIMESTAMP NOT NULL,
    training_rows INT,
    training_date_range TEXT,           -- '2026-01-01 to 2026-04-01'
    validation_auc NUMERIC(5,4),
    validation_accuracy NUMERIC(5,4),
    feature_importance JSONB,           -- {'rsi': 0.15, 'rvol': 0.22, ...}
    hyperparameters JSONB,
    is_active BOOLEAN DEFAULT FALSE,
    deployed_at TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
```
- **Effort**: 1 hour

### 22. Feature drift detection
```sql
CREATE TABLE ml_feature_distributions (
    snapshot_date DATE NOT NULL,
    feature_name TEXT NOT NULL,
    layer TEXT,                          -- 'vwap', 'pro', 'pop', 'options', 'all'
    p10 NUMERIC(10,4),
    p25 NUMERIC(10,4),
    p50 NUMERIC(10,4),
    p75 NUMERIC(10,4),
    p90 NUMERIC(10,4),
    mean NUMERIC(10,4),
    std NUMERIC(10,4),
    n_samples INT,
    PRIMARY KEY (snapshot_date, feature_name, layer)
);
```
- Compute daily from `ml_signal_context` in post-session analytics
- Alert if p50 drifts > 2 std from 30-day rolling mean
- **Effort**: 3 hours

### 23. VIX regime detection
- Fetch VIX daily close via yfinance at EOD
- Populate `ml_daily_regime.vix_close` and `vix_percentile_30d`
- Classify: low (<15), normal (15-25), elevated (25-35), extreme (>35)
- **File**: `scripts/post_session_analytics.py` (extend job_daily_regime)
- **Effort**: 2 hours

---

## P3 — Options Engine Improvements

### 24. Position sizing by Kelly criterion
- Current: flat $500/trade
- Better: size = (edge × win_rate - (1 - win_rate)) / edge × total_budget
- Use per-strategy win rates from `ml_trade_outcomes`
- **File**: `options/engine.py`
- **Effort**: 1 day

### 25. Correlation tracking
- Don't sell iron condors on 5 correlated tech names simultaneously
- Track 30-day rolling correlation between held tickers
- Block entry if portfolio correlation > 0.7
- **File**: new `options/correlation.py`
- **Effort**: 2 days

### 26. Transaction cost model
- Add per-leg commission ($0.65 per contract or broker-specific)
- Subtract from max_reward in risk/reward calculations
- Reflect in ml_trade_outcomes.commission column
- **Files**: `options/strategies/base.py`, `options/engine.py`
- **Effort**: 2 hours

---

## P3 — StrategyEngine Fixes (from 2026-04-13 Signal Flow Report)

### 27. Fix vwap_reclaim SignalAction enum mismatch
- 196 errors/day from undefined values: `'partial_done'`, `'partial_sell'`, `'sell_rsi'`
- Add missing enums to `SignalAction` in `monitor/events.py`
- **Effort**: 1-2 hours

### 28. Optimize PopStrategyEngine latency
- 100% of bars exceed 100ms threshold (avg 227.7ms, max 3,291ms)
- Profile news/social sentiment API calls
- Implement sentiment caching (60s TTL)
- **Effort**: 1-2 days

### 29. Audit ProSetupEngine signal deduplication
- 2,438 signals for 146 trades = 16.7 signals per trade (should be 2-4)
- Check for duplicate (ticker, strategy, entry_time) within 60s windows
- **Effort**: 1 day

---

## Completed (Reference)

### 2026-04-13 — Options Engine Rebuild
- [x] Case mismatch fix in selector.py
- [x] Registry cross-layer blocking removed
- [x] 13/13 strategies implemented (was 2/13)
- [x] Real Alpaca chain client + broker with execution
- [x] Exit management (5 conditions: profit target, stop loss, DTE, theta bleed, roll)
- [x] IV rank tracker + earnings calendar
- [x] Skew-aware iron condor/butterfly strikes
- [x] Risk gate tracks max_risk (not net_debit)
- [x] P&L math: signed mark-to-market using bid/ask

### 2026-04-13 — Database & Analytics
- [x] 3 missing event handlers added (OPTIONS_SIGNAL, QUOTE, aggregate_version)
- [x] received_time now distinct from event_time
- [x] 7 flattened VIEWs for Tableau
- [x] 2 dimension tables (dim_strategy, dim_time_of_day)
- [x] 5 ML tables (trade_outcomes, signal_context, execution_quality, rejection_log, daily_regime)
- [x] Post-session batch pipeline (MFE/MAE, counterfactuals, IV history, regime)
- [x] Event store partitioning schema (monthly range)
- [x] Accounting bug fixes (div/0, race condition, reward cap)

### 2026-04-13 — Monitor Changes
- [x] Market close time: 3:30 PM ET (was 4:00 PM)
- [x] Post-session analytics auto-trigger at shutdown
- [x] Options EOD close at shutdown

---

## Effort Summary

| Priority | Items | Est. Total Effort |
|----------|-------|-------------------|
| **P0** | 4 items | 4 hours |
| **P1** | 5 items | 2 days |
| **P2** | 9 items | 3 days |
| **P3** | 7 items | 2 weeks |
| **Total** | **25 items** | **~3 weeks** |
