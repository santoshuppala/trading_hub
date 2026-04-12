-- ============================================================
-- 008_views.sql  — Continuous aggregates and ML feature views
-- ============================================================

-- ── 5-minute OHLCV continuous aggregate ──────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS trading.bar_5m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', ts) AS bucket,
    ticker,
    first(open,  ts)             AS open,
    max(high)                    AS high,
    min(low)                     AS low,
    last(close,  ts)             AS close,
    sum(volume)                  AS volume,
    avg(vwap)                    AS vwap,
    avg(rvol)                    AS rvol,
    avg(atr)                     AS atr,
    avg(rsi)                     AS rsi
FROM trading.bar_events
GROUP BY bucket, ticker
WITH NO DATA;

SELECT add_continuous_aggregate_policy('trading.bar_5m',
    start_offset  => INTERVAL '10 minutes',
    end_offset    => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists => TRUE);

-- ── 1-hour OHLCV continuous aggregate ────────────────────────────────────────
CREATE MATERIALIZED VIEW IF NOT EXISTS trading.bar_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts)    AS bucket,
    ticker,
    first(open,  ts)             AS open,
    max(high)                    AS high,
    min(low)                     AS low,
    last(close,  ts)             AS close,
    sum(volume)                  AS volume,
    avg(vwap)                    AS vwap,
    avg(rvol)                    AS rvol,
    avg(atr)                     AS atr,
    avg(rsi)                     AS rsi
FROM trading.bar_events
GROUP BY bucket, ticker
WITH NO DATA;

SELECT add_continuous_aggregate_policy('trading.bar_1h',
    start_offset  => INTERVAL '2 hours',
    end_offset    => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);

-- ── Daily trade summary view ──────────────────────────────────────────────────
CREATE OR REPLACE VIEW trading.daily_trade_summary AS
SELECT
    date_trunc('day', f.ts AT TIME ZONE 'America/New_York') AS trade_date,
    f.ticker,
    f.reason,
    count(*)                FILTER (WHERE f.side = 'BUY')    AS buys,
    count(*)                FILTER (WHERE f.side = 'SELL')   AS sells,
    avg(f.fill_price)       FILTER (WHERE f.side = 'BUY')    AS avg_entry,
    avg(f.fill_price)       FILTER (WHERE f.side = 'SELL')   AS avg_exit,
    sum(f.qty)              FILTER (WHERE f.side = 'BUY')    AS total_qty_bought,
    sum(f.qty)              FILTER (WHERE f.side = 'SELL')   AS total_qty_sold
FROM trading.fill_events f
GROUP BY 1, 2, 3;

-- ── Strategy signal rate view (for ML labelling) ──────────────────────────────
CREATE OR REPLACE VIEW trading.strategy_signal_rates AS
SELECT
    date_trunc('day', ts AT TIME ZONE 'America/New_York') AS trade_date,
    action,
    count(*) AS signal_count,
    count(DISTINCT ticker) AS unique_tickers,
    avg(rvol)     AS avg_rvol,
    avg(rsi_value) AS avg_rsi,
    avg(atr_value) AS avg_atr
FROM trading.signal_events
GROUP BY 1, 2;

-- ── Pro-strategy performance view ────────────────────────────────────────────
CREATE OR REPLACE VIEW trading.pro_strategy_performance AS
WITH signals AS (
    SELECT
        p.ts,
        p.ticker,
        p.strategy_name,
        p.tier,
        p.entry_price,
        p.stop_price,
        p.target_1,
        p.target_2,
        p.confidence,
        -- nearest subsequent fill for same ticker
        (
            SELECT f.fill_price
            FROM trading.fill_events f
            WHERE f.ticker = p.ticker
              AND f.side = 'BUY'
              AND f.ts BETWEEN p.ts AND p.ts + INTERVAL '5 minutes'
            ORDER BY f.ts
            LIMIT 1
        ) AS actual_fill
    FROM trading.pro_strategy_signal_events p
)
SELECT
    strategy_name,
    tier,
    count(*) AS total_signals,
    count(actual_fill) AS executed,
    round(count(actual_fill)::numeric / nullif(count(*),0) * 100, 1) AS exec_pct,
    avg(confidence)          AS avg_confidence,
    avg(target_1 - entry_price) AS avg_target_1_distance,
    avg(entry_price - stop_price) AS avg_stop_distance
FROM signals
GROUP BY strategy_name, tier
ORDER BY tier DESC, avg_confidence DESC;

-- ── ML feature store view (last 50 bars per ticker) ──────────────────────────
-- Used by downstream ML pipelines to extract feature vectors.
CREATE OR REPLACE VIEW trading.ml_bar_features AS
SELECT
    ts,
    ticker,
    open,  high,  low,  close,  volume,
    vwap,  rvol,  atr,  rsi,
    -- Derived features
    (close - open) / nullif(open, 0)             AS bar_return,
    (high - low)   / nullif(open, 0)             AS bar_range_pct,
    (close - low)  / nullif(high - low, 0)        AS close_position,   -- 0=bottom 1=top
    volume / nullif(
        avg(volume) OVER (PARTITION BY ticker ORDER BY ts ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
        0
    )                                              AS vol_ratio_20,
    close / nullif(
        avg(close) OVER (PARTITION BY ticker ORDER BY ts ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
        0
    ) - 1                                          AS price_vs_ma10
FROM trading.bar_events;
