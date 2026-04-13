-- ============================================================
-- 011_activity_log.sql — Comprehensive activity + signal log
--
-- Replaces CSV-based signal instrumentation with a proper
-- TimescaleDB hypertable. Every trading-relevant event is
-- logged here for post-session analysis and strategy improvement.
--
-- Tables:
--   1. activity_log        — every event (signal, order, fill, block, etc.)
--   2. signal_analysis     — enriched signal data for ML/strategy review
--   3. system_health_log   — heartbeat, latency, queue depth snapshots
--   4. preflight_log       — pre-market connectivity check results
--   5. kill_switch_log     — daily loss kill switch activations
-- ============================================================

-- ══════════════════════════════════════════════════════════════
-- 1. ACTIVITY LOG — every trading event in one table
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trading.activity_log (
    ts              TIMESTAMPTZ     NOT NULL DEFAULT now(),
    event_type      TEXT            NOT NULL,       -- SIGNAL, ORDER_REQ, FILL, RISK_BLOCK, etc.
    ticker          TEXT,
    action          TEXT,                           -- buy, sell, sell_stop, hold, etc.
    direction       TEXT,                           -- long, short
    price           DOUBLE PRECISION,               -- entry/fill/signal price
    stop_price      DOUBLE PRECISION,
    target_price    DOUBLE PRECISION,
    qty             INT,
    reason          TEXT,                           -- VWAP reclaim, cooldown, max_positions, etc.
    strategy        TEXT,                           -- strategy name
    tier            SMALLINT,
    atr_value       DOUBLE PRECISION,
    rsi_value       DOUBLE PRECISION,
    rvol            DOUBLE PRECISION,
    vwap            DOUBLE PRECISION,
    confidence      DOUBLE PRECISION,
    event_id        UUID,
    correlation_id  UUID,
    stream_seq      BIGINT,
    layer           TEXT,                           -- T4, T3.6, T3.5
    outcome         TEXT,                           -- approved, blocked, filled, failed
    metadata        JSONB,                          -- any extra fields
    ingested_at     TIMESTAMPTZ     NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.activity_log', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_activity_event_type_ts
    ON trading.activity_log (event_type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_activity_ticker_ts
    ON trading.activity_log (ticker, ts DESC);
CREATE INDEX IF NOT EXISTS idx_activity_strategy_ts
    ON trading.activity_log (strategy, ts DESC)
    WHERE strategy IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_activity_outcome
    ON trading.activity_log (outcome, ts DESC);

-- Compression: segment by ticker for fast per-ticker queries
ALTER TABLE trading.activity_log
    SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'ticker',
        timescaledb.compress_orderby   = 'ts DESC'
    );

SELECT add_compression_policy('trading.activity_log',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.activity_log',
    drop_after => INTERVAL '365 days',
    if_not_exists => TRUE);


-- ══════════════════════════════════════════════════════════════
-- 2. SIGNAL ANALYSIS — enriched signal data for review
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trading.signal_analysis (
    ts                  TIMESTAMPTZ     NOT NULL DEFAULT now(),
    ticker              TEXT            NOT NULL,
    signal_type         TEXT            NOT NULL,   -- SIGNAL, POP_SIGNAL, PRO_STRATEGY_SIGNAL
    action              TEXT,                       -- buy, sell_stop, etc.
    strategy            TEXT,
    tier                SMALLINT,
    direction           TEXT,
    entry_price         DOUBLE PRECISION,
    stop_price          DOUBLE PRECISION,
    target_1            DOUBLE PRECISION,
    target_2            DOUBLE PRECISION,
    risk_per_share      DOUBLE PRECISION,           -- entry - stop
    reward_per_share    DOUBLE PRECISION,            -- target_2 - entry
    rr_ratio            DOUBLE PRECISION,            -- reward / risk
    atr_value           DOUBLE PRECISION,
    rsi_value           DOUBLE PRECISION,
    rvol                DOUBLE PRECISION,
    vwap                DOUBLE PRECISION,
    vwap_distance_pct   DOUBLE PRECISION,            -- (price - vwap) / vwap
    confidence          DOUBLE PRECISION,
    pop_reason          TEXT,                        -- for POP_SIGNAL
    detectors_fired     JSONB,                       -- for PRO_STRATEGY_SIGNAL
    features_json       JSONB,                       -- full feature snapshot
    was_executed        BOOLEAN         DEFAULT FALSE,  -- did it become an ORDER_REQ?
    was_blocked         BOOLEAN         DEFAULT FALSE,  -- did RiskEngine block it?
    block_reason        TEXT,
    fill_price          DOUBLE PRECISION,            -- actual fill (populated after FILL)
    fill_slippage_pct   DOUBLE PRECISION,            -- (fill - entry) / entry
    event_id            UUID,
    session_id          UUID,
    ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.signal_analysis', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_signal_analysis_ticker_ts
    ON trading.signal_analysis (ticker, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signal_analysis_strategy
    ON trading.signal_analysis (strategy, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signal_analysis_executed
    ON trading.signal_analysis (was_executed, ts DESC);
CREATE INDEX IF NOT EXISTS idx_signal_analysis_blocked
    ON trading.signal_analysis (was_blocked, ts DESC)
    WHERE was_blocked = TRUE;

ALTER TABLE trading.signal_analysis
    SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'ticker',
        timescaledb.compress_orderby   = 'ts DESC'
    );

SELECT add_compression_policy('trading.signal_analysis',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.signal_analysis',
    drop_after => INTERVAL '365 days',
    if_not_exists => TRUE);


-- ══════════════════════════════════════════════════════════════
-- 3. SYSTEM HEALTH LOG — heartbeats, queue depths, latency
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trading.system_health_log (
    ts                  TIMESTAMPTZ     NOT NULL DEFAULT now(),
    tickers_scanned     INT,
    open_positions      INT,
    trades_today        INT,
    wins_today          INT,
    losses_today        INT,
    daily_pnl           DOUBLE PRECISION,
    system_pressure     DOUBLE PRECISION,            -- EventBus backpressure [0,1]
    db_rows_written     BIGINT,
    db_rows_dropped     BIGINT,
    db_batches_flushed  BIGINT,
    registry_count      INT,                         -- GlobalPositionRegistry count
    kill_switch_active  BOOLEAN         DEFAULT FALSE,
    queue_depths        JSONB,                       -- per-EventType queue sizes
    handler_avg_ms      JSONB,                       -- per-handler avg latency
    memory_mb           DOUBLE PRECISION,            -- process RSS
    ingested_at         TIMESTAMPTZ     NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.system_health_log', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

ALTER TABLE trading.system_health_log
    SET (timescaledb.compress, timescaledb.compress_orderby = 'ts DESC');

SELECT add_compression_policy('trading.system_health_log',
    compress_after => INTERVAL '3 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.system_health_log',
    drop_after => INTERVAL '90 days',
    if_not_exists => TRUE);


-- ══════════════════════════════════════════════════════════════
-- 4. PREFLIGHT LOG — pre-market connectivity checks
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trading.preflight_log (
    ts              TIMESTAMPTZ     NOT NULL DEFAULT now(),
    check_name      TEXT            NOT NULL,       -- tradier, alpaca, benzinga, stocktwits, redpanda, timescaledb
    status          TEXT            NOT NULL,       -- ok, warn, fail, skip
    response_ms     INT,                            -- round-trip time
    detail          TEXT,                           -- status code, error message, account info
    session_id      UUID
);

-- Not a hypertable (low volume)
CREATE INDEX IF NOT EXISTS idx_preflight_ts
    ON trading.preflight_log (ts DESC);


-- ══════════════════════════════════════════════════════════════
-- 5. KILL SWITCH LOG — activations
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS trading.kill_switch_log (
    ts              TIMESTAMPTZ     NOT NULL DEFAULT now(),
    daily_pnl       DOUBLE PRECISION NOT NULL,
    threshold       DOUBLE PRECISION NOT NULL,
    trades_count    INT,
    wins            INT,
    losses          INT,
    positions_closed TEXT[],                         -- tickers force-closed
    session_id      UUID
);

CREATE INDEX IF NOT EXISTS idx_kill_switch_ts
    ON trading.kill_switch_log (ts DESC);


-- ══════════════════════════════════════════════════════════════
-- 6. ANALYSIS VIEWS — ready-to-query for strategy improvement
-- ══════════════════════════════════════════════════════════════

-- Daily strategy scorecard
CREATE OR REPLACE VIEW trading.daily_strategy_scorecard AS
SELECT
    date_trunc('day', ts AT TIME ZONE 'America/New_York') AS trade_date,
    strategy,
    tier,
    count(*)                                              AS total_signals,
    count(*) FILTER (WHERE was_executed)                  AS executed,
    count(*) FILTER (WHERE was_blocked)                   AS blocked,
    round(avg(rr_ratio)::numeric, 2)                      AS avg_rr,
    round(avg(confidence)::numeric, 3)                    AS avg_confidence,
    round(avg(rvol)::numeric, 2)                          AS avg_rvol,
    round(avg(rsi_value)::numeric, 1)                     AS avg_rsi,
    round(avg(fill_slippage_pct)::numeric, 5)             AS avg_slippage,
    string_agg(DISTINCT block_reason, ', ')
        FILTER (WHERE was_blocked)                        AS block_reasons
FROM trading.signal_analysis
GROUP BY 1, 2, 3
ORDER BY 1 DESC, 2;

-- Signal-to-fill pipeline efficiency
CREATE OR REPLACE VIEW trading.signal_pipeline_efficiency AS
SELECT
    date_trunc('day', ts AT TIME ZONE 'America/New_York') AS trade_date,
    signal_type,
    count(*)                                              AS signals,
    count(*) FILTER (WHERE was_executed)                  AS orders,
    count(*) FILTER (WHERE fill_price IS NOT NULL)        AS fills,
    count(*) FILTER (WHERE was_blocked)                   AS blocks,
    round(
        count(*) FILTER (WHERE was_executed)::numeric /
        nullif(count(*), 0) * 100, 1
    )                                                     AS exec_rate_pct,
    round(
        count(*) FILTER (WHERE fill_price IS NOT NULL)::numeric /
        nullif(count(*) FILTER (WHERE was_executed), 0) * 100, 1
    )                                                     AS fill_rate_pct
FROM trading.signal_analysis
GROUP BY 1, 2
ORDER BY 1 DESC, 2;

-- Rejection analysis (why are signals being blocked?)
CREATE OR REPLACE VIEW trading.rejection_analysis AS
SELECT
    date_trunc('day', ts AT TIME ZONE 'America/New_York') AS trade_date,
    block_reason,
    count(*)                                              AS occurrences,
    count(DISTINCT ticker)                                AS unique_tickers,
    round(avg(confidence)::numeric, 3)                    AS avg_confidence_blocked,
    round(avg(rr_ratio)::numeric, 2)                      AS avg_rr_blocked
FROM trading.signal_analysis
WHERE was_blocked = TRUE
GROUP BY 1, 2
ORDER BY 1 DESC, 3 DESC;

-- Slippage analysis (actual fill vs signal price)
CREATE OR REPLACE VIEW trading.slippage_analysis AS
SELECT
    date_trunc('day', ts AT TIME ZONE 'America/New_York') AS trade_date,
    strategy,
    count(*) FILTER (WHERE fill_price IS NOT NULL)        AS fills,
    round(avg(fill_slippage_pct)::numeric, 5)             AS avg_slippage_pct,
    round(min(fill_slippage_pct)::numeric, 5)             AS best_slippage,
    round(max(fill_slippage_pct)::numeric, 5)             AS worst_slippage,
    round(percentile_cont(0.95) WITHIN GROUP
        (ORDER BY fill_slippage_pct)::numeric, 5)         AS p95_slippage
FROM trading.signal_analysis
WHERE fill_price IS NOT NULL
GROUP BY 1, 2
ORDER BY 1 DESC, 2;

-- System health timeline (for dashboards)
CREATE OR REPLACE VIEW trading.health_timeline AS
SELECT
    ts,
    tickers_scanned,
    open_positions,
    trades_today,
    daily_pnl,
    system_pressure,
    kill_switch_active,
    db_rows_written,
    db_rows_dropped,
    registry_count,
    memory_mb
FROM trading.system_health_log
ORDER BY ts DESC;

-- Activity feed (last N events, human-readable)
CREATE OR REPLACE VIEW trading.activity_feed AS
SELECT
    ts,
    event_type,
    ticker,
    action,
    outcome,
    price,
    qty,
    reason,
    strategy,
    tier,
    confidence
FROM trading.activity_log
ORDER BY ts DESC;
