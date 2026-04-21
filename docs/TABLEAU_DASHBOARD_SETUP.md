# Tableau Dashboard Setup — Trading Hub

## Step 1: Connect to Database

```
Server:   localhost
Port:     5432
Database: tradinghub
Username: trading
Password: trading_secret
```

In Tableau: **Connect → PostgreSQL** → enter credentials above.

---

## Step 2: Add Data Sources

Add these as **Custom SQL** connections (one per data source). Click **New Custom SQL** in the data source pane.

### Data Source 1: Trade Details (primary fact table)
```sql
SELECT
    t.trade_id,
    t.ticker,
    t.entry_ts,
    t.exit_ts,
    t.duration_sec,
    t.session_phase,
    t.entry_price,
    t.exit_price,
    t.qty,
    t.pnl,
    t.exit_reason,
    t.stop_price,
    t.target_price,
    t.aggregate_id,
    t.opened_event_id,
    t.closed_event_id,
    CASE WHEN t.pnl > 0 THEN 'Win' ELSE 'Loss' END as outcome,
    ROUND(t.pnl / NULLIF(t.entry_price * t.qty, 0) * 100, 2) as pnl_pct,
    EXTRACT(HOUR FROM t.entry_ts) as entry_hour,
    t.entry_ts::DATE as trading_date,
    s.layer,
    s.description as strategy_description,
    s.tier,
    s.is_credit
FROM v_trade_details t
LEFT JOIN dim_strategy s ON t.exit_reason = s.strategy_name
    OR t.aggregate_id LIKE '%' || s.strategy_name || '%'
ORDER BY t.entry_ts DESC
```

### Data Source 2: Equity Curve
```sql
SELECT
    ts,
    ts::DATE as trading_date,
    cumulative_pnl,
    trade_pnl,
    ticker,
    action,
    SUM(CASE WHEN trade_pnl > 0 THEN 1 ELSE 0 END) OVER (ORDER BY ts) as cumulative_wins,
    SUM(CASE WHEN trade_pnl <= 0 THEN 1 ELSE 0 END) OVER (ORDER BY ts) as cumulative_losses,
    ROW_NUMBER() OVER (ORDER BY ts) as trade_number
FROM v_equity_curve
ORDER BY ts
```

### Data Source 3: Daily Performance
```sql
SELECT
    trading_date,
    total_trades,
    wins,
    losses,
    win_rate,
    total_pnl,
    avg_win,
    avg_loss,
    largest_win,
    largest_loss,
    CASE
        WHEN avg_loss != 0 THEN ROUND(ABS(avg_win / avg_loss)::numeric, 2)
        ELSE NULL
    END as profit_factor
FROM v_daily_performance
ORDER BY trading_date DESC
```

### Data Source 4: ML Trade Outcomes (rich analysis)
```sql
SELECT
    trade_id,
    ticker,
    layer,
    strategy_name,
    entry_ts,
    entry_ts::DATE as trading_date,
    exit_ts,
    exit_reason,
    entry_price,
    exit_price,
    qty,
    realized_pnl,
    realized_pnl_pct,
    max_favorable_excursion as mfe,
    max_adverse_excursion as mae,
    mfe_pct,
    mae_pct,
    time_in_position_sec,
    ROUND(time_in_position_sec / 60.0, 1) as hold_minutes,
    concurrent_positions,
    entry_rsi,
    entry_atr,
    entry_rvol,
    entry_confidence,
    CASE WHEN realized_pnl > 0 THEN 'Win' ELSE 'Loss' END as outcome,
    options_strategy_type,
    options_max_risk,
    options_max_reward
FROM ml_trade_outcomes
WHERE exit_ts IS NOT NULL
ORDER BY entry_ts DESC
```

### Data Source 5: Strategy Dimension
```sql
SELECT * FROM dim_strategy
```

### Data Source 6: Time Dimension
```sql
SELECT * FROM dim_time_of_day WHERE is_market_hours = true
```

---

## Step 3: Build Dashboards

### Dashboard 1: Daily P&L Overview

**Layout**: 2×3 grid

| Position | Chart | Type |
|----------|-------|------|
| Top-left | **KPI Cards** | Text: Total P&L, Win Rate, Trades Today, Sharpe |
| Top-right | **Equity Curve** | Line chart: cumulative_pnl over ts |
| Mid-left | **P&L by Ticker** | Horizontal bar: SUM(pnl) by ticker, color=outcome |
| Mid-right | **Win/Loss Distribution** | Histogram: pnl bins, color=Win/Loss |
| Bot-left | **Exit Reason Breakdown** | Pie/donut: COUNT by exit_reason |
| Bot-right | **Session Phase Performance** | Bar: AVG(pnl) by session_phase |

**How to build Equity Curve:**
1. Drag `ts` → Columns
2. Drag `cumulative_pnl` → Rows
3. Mark type: Line
4. Add reference line at Y=0 (red dashed)
5. Color: green if above 0, red if below

**How to build KPI Cards:**
1. New worksheet → drag `total_pnl` → Text
2. Format: $#,##0.00
3. Repeat for win_rate (format: 0.0%), total_trades

---

### Dashboard 2: Strategy Performance

**Layout**: 3 rows

| Position | Chart | Type |
|----------|-------|------|
| Row 1 | **Strategy Comparison Table** | Text table: strategy_name, trades, win_rate, avg_pnl, profit_factor |
| Row 2-left | **Strategy P&L Waterfall** | Waterfall: SUM(pnl) by strategy_name |
| Row 2-right | **Win Rate by Strategy** | Bar: win_rate by strategy_name, color by tier |
| Row 3-left | **MFE vs MAE Scatter** | Scatter: mae_pct (X) vs mfe_pct (Y), color=outcome |
| Row 3-right | **Hold Time vs P&L** | Scatter: hold_minutes (X) vs realized_pnl (Y) |

**MFE/MAE Scatter (the most valuable chart):**
1. Data source: ML Trade Outcomes
2. Drag `mae_pct` → Columns
3. Drag `mfe_pct` → Rows
4. Color: outcome (Win=green, Loss=red)
5. Size: ABS(realized_pnl)
6. Add reference lines: diagonal (MFE=MAE means breakeven exit efficiency)
7. Tooltip: ticker, strategy, pnl, hold_minutes

**Interpretation:**
- Points above diagonal: exited before capturing full MFE (left money on table)
- Points below diagonal: MAE exceeded MFE (bad trade selection)
- Cluster in top-left: good trades — high MFE, low MAE

---

### Dashboard 3: Execution Quality

**Layout**: 2×2

| Position | Chart | Type |
|----------|-------|------|
| Top-left | **Trades by Hour** | Bar: COUNT trades by entry_hour, color=outcome |
| Top-right | **Duration Distribution** | Histogram: hold_minutes bins |
| Bot-left | **P&L by Duration Bucket** | Bar: AVG(pnl) by duration bucket (< 5min, 5-15, 15-60, 60+) |
| Bot-right | **Entry Price vs Exit Price** | Scatter: entry_price (X) vs exit_price (Y), 45° line = breakeven |

**Duration Bucket calculated field:**
```
IF [hold_minutes] < 5 THEN "< 5 min"
ELSEIF [hold_minutes] < 15 THEN "5-15 min"
ELSEIF [hold_minutes] < 60 THEN "15-60 min"
ELSE "60+ min"
END
```

---

### Dashboard 4: Risk Analysis

**Layout**: 2×2

| Position | Chart | Type |
|----------|-------|------|
| Top-left | **Drawdown Chart** | Area chart: running min(cumulative_pnl) - cumulative_pnl |
| Top-right | **Concurrent Positions** | Line: MAX(concurrent_positions) over time |
| Bot-left | **Loss Distribution** | Histogram: pnl WHERE outcome='Loss' |
| Bot-right | **Stop Hit Rate by Ticker** | Bar: % of trades where exit_reason='SELL_STOP' by ticker |

**Drawdown calculated field:**
```
[cumulative_pnl] - WINDOW_MAX([cumulative_pnl])
```

---

### Dashboard 5: Options Performance (when data arrives)

**Layout**: 2×2

| Position | Chart | Type |
|----------|-------|------|
| Top-left | **Options P&L by Strategy** | Bar: SUM(realized_pnl) by options_strategy_type |
| Top-right | **Credit vs Debit Returns** | Grouped bar: AVG(pnl) by is_credit |
| Bot-left | **Max Risk vs Actual P&L** | Scatter: options_max_risk (X) vs realized_pnl (Y) |
| Bot-right | **IV Rank at Entry vs Outcome** | Scatter: entry_iv_rank (X) vs pnl (Y), trend line |

---

## Step 4: Filters & Interactivity

Add these as **dashboard filters** (apply to all sheets):

1. **Trading Date** — date range selector
2. **Ticker** — multi-select dropdown
3. **Strategy** — multi-select dropdown
4. **Outcome** — Win/Loss toggle
5. **Session Phase** — multi-select

**Actions:**
- Click on ticker bar → filter all other charts to that ticker
- Click on strategy → filter all charts to that strategy
- Hover on equity curve → tooltip shows trade details

---

## Step 5: Refresh Schedule

| Frequency | What | How |
|-----------|------|-----|
| **Live** (during trading) | event_store, v_trade_details | Tableau auto-refresh (set to 1 min) |
| **EOD** (after 4 PM) | ml_trade_outcomes, v_daily_performance | After `post_session_analytics.py` runs |
| **Weekly** | dim_strategy | Manual (only changes when new strategy added) |

Set extract refresh: **Data → Extract → Refresh** → schedule for 4:30 PM daily.

---

## Quick Start: First Dashboard in 5 Minutes

1. Open Tableau → Connect → PostgreSQL → enter credentials
2. Click **New Custom SQL** → paste the "Trade Details" query above
3. Click **Sheet 1**
4. Drag `ticker` → Rows
5. Drag `pnl` (SUM) → Columns
6. Drag `outcome` → Color
7. Sort descending by SUM(pnl)
8. You now have a P&L by ticker chart

Then:
1. Click **New Sheet**
2. Drag `ts` from equity curve → Columns (set to Continuous, Exact Date)
3. Drag `cumulative_pnl` → Rows
4. Mark type: Line
5. You now have an equity curve

Combine both sheets into a Dashboard → drag sheets from the left panel.
