# TradingHub — Analysis Query Reference

> All queries run against TimescaleDB (`tradinghub` database, `trading` schema).
> Connect via pgAdmin, DBeaver, or psql:
> ```
> psql -U postgres -d tradinghub -h localhost
> ```

---

## Table of Contents

1. [Real-Time Monitoring](#1-real-time-monitoring)
2. [Daily Performance](#2-daily-performance)
3. [Strategy Analysis](#3-strategy-analysis)
4. [Signal Quality](#4-signal-quality)
5. [Risk & Rejection Analysis](#5-risk--rejection-analysis)
6. [Execution Quality](#6-execution-quality)
7. [System Health](#7-system-health)
8. [Kill Switch & Safety](#8-kill-switch--safety)
9. [Paper Trading Validation](#9-paper-trading-validation)
10. [ML Feature Extraction](#10-ml-feature-extraction)
11. [Pre-built Views](#11-pre-built-views)

---

## 1. Real-Time Monitoring

### What's happening RIGHT NOW?

```sql
-- Last 20 events (live feed)
SELECT ts, event_type, ticker, action, outcome, price, qty, reason, strategy
FROM trading.activity_log
ORDER BY ts DESC
LIMIT 20;
```

### Current open positions

```sql
-- Positions opened today that haven't closed
SELECT
    o.ticker,
    o.price AS entry_price,
    o.qty,
    o.strategy,
    o.ts AS opened_at,
    extract(epoch FROM (now() - o.ts)) / 60 AS minutes_held
FROM trading.activity_log o
WHERE o.event_type = 'FILL'
  AND o.action IN ('buy', 'BUY')
  AND o.ts > now() - INTERVAL '1 day'
  AND NOT EXISTS (
      SELECT 1
      FROM trading.activity_log c
      WHERE c.event_type = 'FILL'
        AND c.action IN ('sell', 'SELL')
        AND c.ticker = o.ticker
        AND c.ts > o.ts
  )
ORDER BY o.ts DESC;
```

### Today's P&L running total

```sql
SELECT
    count(*) AS total_trades,
    count(*) FILTER (WHERE action IN ('sell', 'SELL')) AS closes,
    sum(CASE
        WHEN action IN ('sell', 'SELL') THEN price * qty
        WHEN action IN ('buy', 'BUY') THEN -price * qty
        ELSE 0
    END) AS estimated_pnl
FROM trading.activity_log
WHERE event_type = 'FILL'
  AND ts > date_trunc('day', now() AT TIME ZONE 'America/New_York');
```

---

## 2. Daily Performance

### Daily P&L from trade log

```sql
SELECT
    date_trunc('day', f.ts AT TIME ZONE 'America/New_York') AS trade_date,
    count(*) FILTER (WHERE f.side = 'BUY')  AS buys,
    count(*) FILTER (WHERE f.side = 'SELL') AS sells,
    sum(CASE WHEN f.side = 'SELL' THEN f.fill_price * f.qty ELSE 0 END) -
    sum(CASE WHEN f.side = 'BUY'  THEN f.fill_price * f.qty ELSE 0 END) AS net_pnl
FROM trading.fill_events f
GROUP BY 1
ORDER BY 1 DESC;
```

### Daily win/loss breakdown

```sql
-- Uses the daily_trade_summary view
SELECT * FROM trading.daily_trade_summary
ORDER BY trade_date DESC, ticker;
```

### Cumulative P&L curve (for charting)

```sql
SELECT
    ts,
    ticker,
    action,
    price,
    qty,
    sum(CASE
        WHEN action IN ('sell', 'SELL') THEN price * qty
        WHEN action IN ('buy', 'BUY') THEN -price * qty
        ELSE 0
    END) OVER (ORDER BY ts) AS cumulative_pnl
FROM trading.activity_log
WHERE event_type = 'FILL'
  AND ts > now() - INTERVAL '7 days'
ORDER BY ts;
```

---

## 3. Strategy Analysis

### Which strategies generate the most signals?

```sql
SELECT
    strategy,
    tier,
    count(*) AS total_signals,
    count(*) FILTER (WHERE was_executed) AS executed,
    count(*) FILTER (WHERE was_blocked) AS blocked,
    count(*) FILTER (WHERE fill_price IS NOT NULL) AS filled,
    round(count(*) FILTER (WHERE was_executed)::numeric / nullif(count(*), 0) * 100, 1) AS exec_rate_pct
FROM trading.signal_analysis
WHERE ts > now() - INTERVAL '7 days'
GROUP BY strategy, tier
ORDER BY total_signals DESC;
```

### Strategy performance by tier

```sql
SELECT
    tier,
    count(DISTINCT strategy) AS strategies,
    count(*) AS signals,
    round(avg(rr_ratio)::numeric, 2) AS avg_rr,
    round(avg(confidence)::numeric, 3) AS avg_confidence,
    round(avg(rvol)::numeric, 2) AS avg_rvol,
    round(avg(rsi_value)::numeric, 1) AS avg_rsi,
    count(*) FILTER (WHERE was_executed) AS orders,
    count(*) FILTER (WHERE fill_price IS NOT NULL) AS fills
FROM trading.signal_analysis
WHERE ts > now() - INTERVAL '7 days'
  AND signal_type != 'RISK_BLOCK'
GROUP BY tier
ORDER BY tier;
```

### Per-strategy daily scorecard (pre-built view)

```sql
SELECT * FROM trading.daily_strategy_scorecard
ORDER BY trade_date DESC, strategy;
```

### Strategy signal frequency by hour

```sql
SELECT
    extract(hour FROM ts AT TIME ZONE 'America/New_York') AS hour_et,
    strategy,
    count(*) AS signals
FROM trading.signal_analysis
WHERE ts > now() - INTERVAL '7 days'
  AND signal_type != 'RISK_BLOCK'
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
```

### Which strategy/ticker combos fire most often?

```sql
SELECT
    strategy,
    ticker,
    count(*) AS signals,
    count(*) FILTER (WHERE was_executed) AS executed,
    round(avg(confidence)::numeric, 3) AS avg_conf
FROM trading.signal_analysis
WHERE ts > now() - INTERVAL '7 days'
  AND signal_type != 'RISK_BLOCK'
GROUP BY strategy, ticker
HAVING count(*) >= 3
ORDER BY signals DESC
LIMIT 30;
```

---

## 4. Signal Quality

### Signals that were executed vs blocked

```sql
SELECT * FROM trading.signal_pipeline_efficiency
ORDER BY trade_date DESC;
```

### Signal confidence distribution

```sql
SELECT
    width_bucket(confidence, 0, 1, 10) AS bucket,
    round(width_bucket(confidence, 0, 1, 10) * 0.1, 1) AS conf_range,
    count(*) AS signals,
    count(*) FILTER (WHERE was_executed) AS executed,
    count(*) FILTER (WHERE fill_price IS NOT NULL) AS filled
FROM trading.signal_analysis
WHERE ts > now() - INTERVAL '7 days'
  AND confidence IS NOT NULL
GROUP BY 1
ORDER BY 1;
```

### Signals with highest confidence that were NOT executed (missed opportunities?)

```sql
SELECT
    ts AT TIME ZONE 'America/New_York' AS time_et,
    ticker,
    strategy,
    tier,
    direction,
    entry_price,
    rr_ratio,
    confidence,
    was_blocked,
    block_reason
FROM trading.signal_analysis
WHERE ts > now() - INTERVAL '7 days'
  AND confidence >= 0.80
  AND was_executed = FALSE
ORDER BY confidence DESC
LIMIT 20;
```

### R:R ratio distribution by strategy

```sql
SELECT
    strategy,
    count(*) AS signals,
    round(min(rr_ratio)::numeric, 2) AS min_rr,
    round(avg(rr_ratio)::numeric, 2) AS avg_rr,
    round(percentile_cont(0.5) WITHIN GROUP (ORDER BY rr_ratio)::numeric, 2) AS median_rr,
    round(max(rr_ratio)::numeric, 2) AS max_rr
FROM trading.signal_analysis
WHERE ts > now() - INTERVAL '7 days'
  AND rr_ratio IS NOT NULL
  AND signal_type != 'RISK_BLOCK'
GROUP BY strategy
ORDER BY avg_rr DESC;
```

---

## 5. Risk & Rejection Analysis

### Why are signals being blocked? (pre-built view)

```sql
SELECT * FROM trading.rejection_analysis
ORDER BY trade_date DESC, occurrences DESC;
```

### Block reasons over time

```sql
SELECT
    date_trunc('hour', ts AT TIME ZONE 'America/New_York') AS hour_et,
    block_reason,
    count(*) AS blocks
FROM trading.signal_analysis
WHERE was_blocked = TRUE
  AND ts > now() - INTERVAL '3 days'
GROUP BY 1, 2
ORDER BY 1 DESC, 3 DESC;
```

### Are risk gates too strict? (signals blocked with high confidence)

```sql
SELECT
    block_reason,
    count(*) AS blocked,
    round(avg(confidence)::numeric, 3) AS avg_confidence,
    round(avg(rr_ratio)::numeric, 2) AS avg_rr,
    round(avg(rvol)::numeric, 2) AS avg_rvol
FROM trading.signal_analysis
WHERE was_blocked = TRUE
  AND confidence >= 0.70
  AND ts > now() - INTERVAL '7 days'
GROUP BY block_reason
ORDER BY blocked DESC;
```

### Cross-layer position dedup effectiveness

```sql
SELECT
    block_reason,
    count(*) AS blocked
FROM trading.activity_log
WHERE event_type = 'RISK_BLOCK'
  AND reason LIKE '%another strategy layer%'
  AND ts > now() - INTERVAL '7 days'
GROUP BY block_reason
ORDER BY blocked DESC;
```

### Cooldown blocks — are we missing re-entries?

```sql
SELECT
    ticker,
    count(*) AS cooldown_blocks,
    min(ts) AS first_block,
    max(ts) AS last_block
FROM trading.activity_log
WHERE event_type = 'RISK_BLOCK'
  AND reason LIKE '%cooldown%'
  AND ts > now() - INTERVAL '7 days'
GROUP BY ticker
ORDER BY cooldown_blocks DESC
LIMIT 20;
```

---

## 6. Execution Quality

### Slippage analysis (pre-built view)

```sql
SELECT * FROM trading.slippage_analysis
ORDER BY trade_date DESC;
```

### Fill rate (what % of orders actually fill?)

```sql
SELECT
    date_trunc('day', ts AT TIME ZONE 'America/New_York') AS trade_date,
    count(*) FILTER (WHERE event_type = 'ORDER_REQ') AS orders,
    count(*) FILTER (WHERE event_type = 'FILL') AS fills,
    count(*) FILTER (WHERE event_type = 'ORDER_FAIL') AS failures,
    round(
        count(*) FILTER (WHERE event_type = 'FILL')::numeric /
        nullif(count(*) FILTER (WHERE event_type = 'ORDER_REQ'), 0) * 100, 1
    ) AS fill_rate_pct
FROM trading.activity_log
WHERE event_type IN ('ORDER_REQ', 'FILL', 'ORDER_FAIL')
  AND ts > now() - INTERVAL '7 days'
GROUP BY 1
ORDER BY 1 DESC;
```

### Average time from signal to fill

```sql
SELECT
    s.strategy,
    count(*) AS signals_with_fills,
    round(avg(extract(epoch FROM (f.ts - s.ts)))::numeric, 2) AS avg_seconds_to_fill,
    round(max(extract(epoch FROM (f.ts - s.ts)))::numeric, 2) AS max_seconds_to_fill
FROM trading.signal_analysis s
JOIN trading.fill_events f
    ON f.ticker = s.ticker
    AND f.side = 'BUY'
    AND f.ts BETWEEN s.ts AND s.ts + INTERVAL '5 minutes'
WHERE s.ts > now() - INTERVAL '7 days'
  AND s.was_executed = TRUE
GROUP BY s.strategy
ORDER BY avg_seconds_to_fill;
```

### Order failures — what's going wrong?

```sql
SELECT
    ts AT TIME ZONE 'America/New_York' AS time_et,
    ticker,
    reason,
    price,
    qty
FROM trading.activity_log
WHERE event_type = 'ORDER_FAIL'
  AND ts > now() - INTERVAL '7 days'
ORDER BY ts DESC;
```

---

## 7. System Health

### Health timeline (pre-built view)

```sql
SELECT * FROM trading.health_timeline
WHERE ts > now() - INTERVAL '4 hours'
ORDER BY ts DESC;
```

### System pressure spikes

```sql
SELECT
    ts AT TIME ZONE 'America/New_York' AS time_et,
    system_pressure,
    open_positions,
    tickers_scanned,
    memory_mb
FROM trading.system_health_log
WHERE system_pressure > 0.5
  AND ts > now() - INTERVAL '7 days'
ORDER BY system_pressure DESC
LIMIT 20;
```

### DB writer performance

```sql
SELECT
    ts AT TIME ZONE 'America/New_York' AS time_et,
    db_rows_written,
    db_rows_dropped,
    db_batches_flushed,
    round(db_rows_dropped::numeric / nullif(db_rows_written + db_rows_dropped, 0) * 100, 2) AS drop_rate_pct
FROM trading.system_health_log
WHERE ts > now() - INTERVAL '1 day'
ORDER BY ts DESC
LIMIT 60;
```

### Memory usage over the day

```sql
SELECT
    time_bucket('5 minutes', ts) AS bucket,
    round(avg(memory_mb)::numeric, 1) AS avg_mb,
    round(max(memory_mb)::numeric, 1) AS peak_mb
FROM trading.system_health_log
WHERE ts > now() - INTERVAL '1 day'
GROUP BY 1
ORDER BY 1;
```

---

## 8. Kill Switch & Safety

### Kill switch activations

```sql
SELECT
    ts AT TIME ZONE 'America/New_York' AS activation_time,
    daily_pnl,
    threshold,
    trades_count,
    wins,
    losses,
    positions_closed
FROM trading.kill_switch_log
ORDER BY ts DESC;
```

### How close did we get to the kill switch?

```sql
SELECT
    date_trunc('day', ts AT TIME ZONE 'America/New_York') AS trade_date,
    min(daily_pnl) AS worst_pnl,
    max(daily_pnl) AS best_pnl,
    count(*) FILTER (WHERE kill_switch_active) AS minutes_halted
FROM trading.system_health_log
WHERE ts > now() - INTERVAL '30 days'
GROUP BY 1
ORDER BY 1 DESC;
```

### Pre-market check history

```sql
SELECT
    ts AT TIME ZONE 'America/New_York' AS check_time,
    check_name,
    status,
    response_ms,
    detail
FROM trading.preflight_log
ORDER BY ts DESC
LIMIT 30;
```

### Pre-market reliability by service

```sql
SELECT
    check_name,
    count(*) AS checks,
    count(*) FILTER (WHERE status = 'ok') AS ok,
    count(*) FILTER (WHERE status = 'fail') AS failed,
    round(avg(response_ms)::numeric, 0) AS avg_ms,
    round(count(*) FILTER (WHERE status = 'ok')::numeric / count(*) * 100, 1) AS uptime_pct
FROM trading.preflight_log
GROUP BY check_name
ORDER BY uptime_pct;
```

---

## 9. Paper Trading Validation

> Use these queries during the 2-week paper trading period to determine
> if the system meets the go-live thresholds.

### Go-live readiness dashboard

```sql
WITH fills AS (
    SELECT
        ticker,
        side,
        fill_price,
        qty,
        ts
    FROM trading.fill_events
    WHERE ts > now() - INTERVAL '14 days'
),
trades AS (
    SELECT
        b.ticker,
        b.fill_price AS entry,
        s.fill_price AS exit,
        b.qty,
        (s.fill_price - b.fill_price) * b.qty AS pnl,
        (s.fill_price - b.fill_price) * b.qty > 0 AS is_win
    FROM fills b
    JOIN LATERAL (
        SELECT fill_price, ts
        FROM fills s2
        WHERE s2.ticker = b.ticker
          AND s2.side = 'SELL'
          AND s2.ts > b.ts
        ORDER BY s2.ts
        LIMIT 1
    ) s ON TRUE
    WHERE b.side = 'BUY'
)
SELECT
    count(*)                                    AS total_trades,
    count(*) FILTER (WHERE is_win)              AS wins,
    count(*) FILTER (WHERE NOT is_win)          AS losses,
    round(count(*) FILTER (WHERE is_win)::numeric / nullif(count(*), 0) * 100, 1)
                                                AS win_rate_pct,
    round(avg(pnl) FILTER (WHERE is_win)::numeric, 2)
                                                AS avg_win,
    round(avg(pnl) FILTER (WHERE NOT is_win)::numeric, 2)
                                                AS avg_loss,
    round(sum(pnl)::numeric, 2)                 AS total_pnl,
    round(max(-pnl)::numeric, 2)                AS worst_single_loss,
    -- Go-live thresholds
    CASE WHEN count(*) >= 50 THEN 'PASS' ELSE 'FAIL: need 50+ trades' END
                                                AS threshold_trade_count,
    CASE WHEN count(*) FILTER (WHERE is_win)::numeric / nullif(count(*), 0) >= 0.40
         THEN 'PASS' ELSE 'FAIL: win rate < 40%' END
                                                AS threshold_win_rate,
    CASE WHEN avg(pnl) FILTER (WHERE is_win) / nullif(abs(avg(pnl) FILTER (WHERE NOT is_win)), 0.01) >= 1.5
         THEN 'PASS' ELSE 'FAIL: avg_win/avg_loss < 1.5' END
                                                AS threshold_rr
FROM trades;
```

### Daily drawdown check

```sql
SELECT
    date_trunc('day', ts AT TIME ZONE 'America/New_York') AS trade_date,
    min(daily_pnl) AS worst_pnl_of_day,
    CASE WHEN min(daily_pnl) > -500 THEN 'PASS' ELSE 'FAIL: exceeded -$500' END AS drawdown_check
FROM trading.system_health_log
WHERE ts > now() - INTERVAL '14 days'
GROUP BY 1
ORDER BY 1 DESC;
```

### System uptime check

```sql
SELECT
    date_trunc('day', ts AT TIME ZONE 'America/New_York') AS trade_date,
    count(*) AS health_checks,
    -- Expect ~345 checks per day (9:30-4:00 = 390 minutes)
    round(count(*)::numeric / 390 * 100, 1) AS uptime_pct,
    CASE WHEN count(*)::numeric / 390 >= 0.98 THEN 'PASS' ELSE 'FAIL: < 98% uptime' END AS uptime_check
FROM trading.system_health_log
WHERE ts > now() - INTERVAL '14 days'
GROUP BY 1
ORDER BY 1 DESC;
```

### Signal-to-fill conversion check

```sql
SELECT
    count(*) FILTER (WHERE event_type = 'ORDER_REQ') AS total_orders,
    count(*) FILTER (WHERE event_type = 'FILL') AS total_fills,
    round(
        count(*) FILTER (WHERE event_type = 'FILL')::numeric /
        nullif(count(*) FILTER (WHERE event_type = 'ORDER_REQ'), 0) * 100, 1
    ) AS fill_rate_pct,
    CASE WHEN count(*) FILTER (WHERE event_type = 'FILL')::numeric /
              nullif(count(*) FILTER (WHERE event_type = 'ORDER_REQ'), 0) >= 0.80
         THEN 'PASS' ELSE 'FAIL: fill rate < 80%' END AS fill_check
FROM trading.activity_log
WHERE ts > now() - INTERVAL '14 days'
  AND event_type IN ('ORDER_REQ', 'FILL');
```

### Position doubling check (must be ZERO)

```sql
SELECT
    ticker,
    count(*) AS duplicate_blocks
FROM trading.activity_log
WHERE event_type = 'RISK_BLOCK'
  AND reason LIKE '%another strategy layer%'
  AND ts > now() - INTERVAL '14 days'
GROUP BY ticker
ORDER BY duplicate_blocks DESC;
-- If this returns rows, GlobalPositionRegistry is working (blocking dupes).
-- If fill_events shows the same ticker opened twice simultaneously, it's a bug.
```

### Orphaned position check (must be ZERO)

```sql
SELECT
    ticker,
    count(*) AS orphan_imports
FROM trading.activity_log
WHERE reason LIKE '%reconcil%' OR reason LIKE '%orphan%'
  AND ts > now() - INTERVAL '14 days'
GROUP BY ticker;
```

---

## 10. ML Feature Extraction

### Bar features with derived indicators (pre-built view)

```sql
SELECT * FROM trading.ml_bar_features
WHERE ticker = 'AAPL'
ORDER BY ts DESC
LIMIT 50;
```

### Signal outcomes: did BUY signals actually profit?

```sql
SELECT
    s.ts,
    s.ticker,
    s.action,
    s.current_price AS signal_price,
    s.atr_value,
    s.rsi_value,
    s.rvol,
    f.fill_price,
    CASE WHEN f.fill_price IS NOT NULL THEN 1 ELSE 0 END AS executed,
    (f.fill_price - s.current_price) / nullif(s.current_price, 0) AS outcome_return
FROM trading.signal_events s
LEFT JOIN LATERAL (
    SELECT fill_price
    FROM trading.fill_events f2
    WHERE f2.ticker = s.ticker
      AND f2.side = 'BUY'
      AND f2.ts BETWEEN s.ts AND s.ts + INTERVAL '5 minutes'
    ORDER BY f2.ts
    LIMIT 1
) f ON TRUE
WHERE s.ts > now() - INTERVAL '30 days'
  AND s.action = 'BUY'
ORDER BY s.ts DESC;
```

### Detector effectiveness (which detectors lead to profitable fills?)

```sql
SELECT * FROM trading.pro_strategy_performance;
```

### Feature importance: what indicators correlate with wins?

```sql
SELECT
    CASE WHEN sa.fill_price > sa.entry_price THEN 'WIN' ELSE 'LOSS' END AS outcome,
    count(*) AS trades,
    round(avg(sa.rsi_value)::numeric, 1) AS avg_rsi,
    round(avg(sa.rvol)::numeric, 2) AS avg_rvol,
    round(avg(sa.atr_value)::numeric, 4) AS avg_atr,
    round(avg(sa.confidence)::numeric, 3) AS avg_confidence,
    round(avg(sa.rr_ratio)::numeric, 2) AS avg_rr,
    round(avg(sa.vwap_distance_pct)::numeric, 4) AS avg_vwap_dist
FROM trading.signal_analysis sa
WHERE sa.fill_price IS NOT NULL
  AND sa.entry_price IS NOT NULL
  AND sa.ts > now() - INTERVAL '30 days'
GROUP BY 1;
```

---

## 11. Pre-built Views

These views are created by migration `011_activity_log.sql` and are ready to query:

| View | Query | Description |
|------|-------|-------------|
| `trading.daily_strategy_scorecard` | `SELECT * FROM trading.daily_strategy_scorecard` | Per-strategy daily stats: signals, executed, blocked, avg R:R, block reasons |
| `trading.signal_pipeline_efficiency` | `SELECT * FROM trading.signal_pipeline_efficiency` | Signal → order → fill conversion rates |
| `trading.rejection_analysis` | `SELECT * FROM trading.rejection_analysis` | Block reasons grouped by day |
| `trading.slippage_analysis` | `SELECT * FROM trading.slippage_analysis` | Fill slippage: avg, P95, worst per strategy |
| `trading.health_timeline` | `SELECT * FROM trading.health_timeline` | System health over time |
| `trading.activity_feed` | `SELECT * FROM trading.activity_feed LIMIT 50` | Real-time event feed |
| `trading.daily_trade_summary` | `SELECT * FROM trading.daily_trade_summary` | Daily buys/sells/avg prices per ticker |
| `trading.strategy_signal_rates` | `SELECT * FROM trading.strategy_signal_rates` | Signal counts by action per day |
| `trading.pro_strategy_performance` | `SELECT * FROM trading.pro_strategy_performance` | Pro-setup execution rates and confidence |
| `trading.ml_bar_features` | `SELECT * FROM trading.ml_bar_features WHERE ticker='AAPL'` | Bar data + derived ML features |
| `trading.bar_5m` | `SELECT * FROM trading.bar_5m WHERE ticker='AAPL'` | 5-minute OHLCV continuous aggregate |
| `trading.bar_1h` | `SELECT * FROM trading.bar_1h WHERE ticker='AAPL'` | 1-hour OHLCV continuous aggregate |

---

## Quick Start: Monday Morning Paper Trading Review

Run these 5 queries after the first trading session:

```sql
-- 1. How many signals fired?
SELECT event_type, count(*) FROM trading.activity_log
WHERE ts > current_date GROUP BY 1 ORDER BY 2 DESC;

-- 2. Any fills?
SELECT * FROM trading.activity_feed
WHERE event_type = 'FILL' LIMIT 20;

-- 3. Why were signals blocked?
SELECT * FROM trading.rejection_analysis;

-- 4. System healthy?
SELECT * FROM trading.health_timeline LIMIT 10;

-- 5. Pipeline working end-to-end?
SELECT * FROM trading.signal_pipeline_efficiency;
```
