-- ============================================================
-- 005_risk_heartbeat.sql  — RISK_BLOCK and HEARTBEAT events
-- ============================================================

-- ── RISK_BLOCK events ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.risk_block_events (
    ts              TIMESTAMPTZ     NOT NULL,
    ticker          TEXT            NOT NULL,
    reason          TEXT            NOT NULL,
    signal_action   trading.signal_action NOT NULL,
    event_id        UUID,
    correlation_id  UUID,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.risk_block_events', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_risk_block_ticker_ts
    ON trading.risk_block_events (ticker, ts DESC);

-- ── HEARTBEAT events ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.heartbeat_events (
    ts              TIMESTAMPTZ     NOT NULL,
    open_positions  SMALLINT,
    scan_count      INT,
    event_id        UUID,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.heartbeat_events', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);
