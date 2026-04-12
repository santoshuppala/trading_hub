-- ============================================================
-- 007_compression_retention.sql
-- Compress chunks older than 7 days; drop chunks older than 90 days.
-- Applied to all hypertables.
-- ============================================================

-- ── bar_events ────────────────────────────────────────────────────────────────
ALTER TABLE trading.bar_events
    SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'ticker',
        timescaledb.compress_orderby   = 'ts DESC'
    );

SELECT add_compression_policy('trading.bar_events',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.bar_events',
    drop_after => INTERVAL '90 days',
    if_not_exists => TRUE);

-- ── signal_events ─────────────────────────────────────────────────────────────
ALTER TABLE trading.signal_events
    SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'ticker',
        timescaledb.compress_orderby   = 'ts DESC'
    );

SELECT add_compression_policy('trading.signal_events',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.signal_events',
    drop_after => INTERVAL '90 days',
    if_not_exists => TRUE);

-- ── order_req_events ──────────────────────────────────────────────────────────
ALTER TABLE trading.order_req_events
    SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'ticker',
        timescaledb.compress_orderby   = 'ts DESC'
    );

SELECT add_compression_policy('trading.order_req_events',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.order_req_events',
    drop_after => INTERVAL '365 days',
    if_not_exists => TRUE);

-- ── fill_events (keep 365 days — trade audit) ─────────────────────────────────
ALTER TABLE trading.fill_events
    SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'ticker',
        timescaledb.compress_orderby   = 'ts DESC'
    );

SELECT add_compression_policy('trading.fill_events',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.fill_events',
    drop_after => INTERVAL '365 days',
    if_not_exists => TRUE);

-- ── position_events ───────────────────────────────────────────────────────────
ALTER TABLE trading.position_events
    SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'ticker',
        timescaledb.compress_orderby   = 'ts DESC'
    );

SELECT add_compression_policy('trading.position_events',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.position_events',
    drop_after => INTERVAL '365 days',
    if_not_exists => TRUE);

-- ── pop_signal_events ─────────────────────────────────────────────────────────
ALTER TABLE trading.pop_signal_events
    SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'symbol',
        timescaledb.compress_orderby   = 'ts DESC'
    );

SELECT add_compression_policy('trading.pop_signal_events',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.pop_signal_events',
    drop_after => INTERVAL '90 days',
    if_not_exists => TRUE);

-- ── pro_strategy_signal_events ────────────────────────────────────────────────
ALTER TABLE trading.pro_strategy_signal_events
    SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'ticker',
        timescaledb.compress_orderby   = 'ts DESC'
    );

SELECT add_compression_policy('trading.pro_strategy_signal_events',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.pro_strategy_signal_events',
    drop_after => INTERVAL '90 days',
    if_not_exists => TRUE);

-- ── risk_block_events ─────────────────────────────────────────────────────────
ALTER TABLE trading.risk_block_events
    SET (
        timescaledb.compress,
        timescaledb.compress_segmentby = 'ticker',
        timescaledb.compress_orderby   = 'ts DESC'
    );

SELECT add_compression_policy('trading.risk_block_events',
    compress_after => INTERVAL '7 days',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.risk_block_events',
    drop_after => INTERVAL '30 days',
    if_not_exists => TRUE);

-- ── heartbeat_events ─────────────────────────────────────────────────────────
ALTER TABLE trading.heartbeat_events
    SET (timescaledb.compress, timescaledb.compress_orderby = 'ts DESC');

SELECT add_compression_policy('trading.heartbeat_events',
    compress_after => INTERVAL '1 day',
    if_not_exists => TRUE);

SELECT add_retention_policy('trading.heartbeat_events',
    drop_after => INTERVAL '7 days',
    if_not_exists => TRUE);
