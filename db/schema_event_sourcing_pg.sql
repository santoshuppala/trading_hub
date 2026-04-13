/**
 * Event Sourcing Schema for Trading Hub
 * Pure PostgreSQL 16+ compatible
 *
 * Core principle: Immutable append-only event store with complete timestamp trails
 * All trading events are recorded with full audit context
 * Projections are built FROM events, never the other way around
 */

-- ============================================================================
-- 1. CORE EVENT STORE (Immutable, Append-Only)
-- ============================================================================

DROP TABLE IF EXISTS event_store CASCADE;

CREATE TABLE event_store (
    -- Event Identity (immutable)
    event_id              UUID NOT NULL PRIMARY KEY,
    event_sequence        BIGINT NOT NULL,
    event_type            TEXT NOT NULL,
    event_version         INT NOT NULL DEFAULT 1,

    -- Complete Timestamp Trail (CRITICAL for audit)
    event_time            TIMESTAMP NOT NULL,     -- When did it actually happen
    received_time         TIMESTAMP NOT NULL,     -- When system received it
    queued_time           TIMESTAMP,              -- When it entered queue (optional)
    processed_time        TIMESTAMP NOT NULL,     -- When we processed it
    persisted_time        TIMESTAMP NOT NULL DEFAULT NOW(),  -- When written to DB

    -- Aggregate (entity being changed)
    aggregate_id          TEXT NOT NULL,
    aggregate_type        TEXT NOT NULL,
    aggregate_version     INT NOT NULL,

    -- Event Payload (complete, immutable)
    event_payload         JSONB NOT NULL,

    -- Event Context (linking)
    correlation_id        UUID,
    causation_id          UUID,
    parent_event_id       UUID,

    -- System Context
    source_system         TEXT NOT NULL,
    source_version        TEXT,
    session_id            UUID,

    -- Constraints
    CONSTRAINT event_store_unique_aggregate_seq
        UNIQUE(aggregate_type, aggregate_id, event_sequence)
);

-- Indexes for common queries
CREATE INDEX idx_event_store_event_time ON event_store (event_time DESC);
CREATE INDEX idx_event_store_type ON event_store (event_type, event_time DESC);
CREATE INDEX idx_event_store_aggregate ON event_store (aggregate_type, aggregate_id, event_sequence DESC);
CREATE INDEX idx_event_store_correlation ON event_store (correlation_id) WHERE correlation_id IS NOT NULL;
CREATE INDEX idx_event_store_session ON event_store (session_id, event_time DESC);
CREATE INDEX idx_event_store_causation ON event_store (causation_id) WHERE causation_id IS NOT NULL;

-- Latency view (computed on read for performance)
CREATE OR REPLACE VIEW event_store_latencies AS
SELECT
    event_id,
    event_type,
    event_time,
    received_time,
    processed_time,
    persisted_time,
    EXTRACT(EPOCH FROM (received_time - event_time)) * 1000 as ingest_latency_ms,
    EXTRACT(EPOCH FROM (COALESCE(queued_time, processed_time) - received_time)) * 1000 as queue_latency_ms,
    EXTRACT(EPOCH FROM (persisted_time - event_time)) * 1000 as total_latency_ms
FROM event_store;

-- ============================================================================
-- 2. PROJECTION TABLES (Built FROM events, rebuilt when needed)
-- ============================================================================

DROP TABLE IF EXISTS position_state CASCADE;

CREATE TABLE position_state (
    ticker                TEXT PRIMARY KEY,
    position_id           UUID NOT NULL,
    action                TEXT NOT NULL,
    qty                   INT NOT NULL,
    entry_price           NUMERIC(10, 2) NOT NULL,
    entry_time            TIMESTAMP NOT NULL,
    current_price         NUMERIC(10, 2),
    stop_price            NUMERIC(10, 2),
    target_price          NUMERIC(10, 2),
    half_target           NUMERIC(10, 2),
    pnl                   NUMERIC(10, 2),
    last_modified_time    TIMESTAMP NOT NULL DEFAULT NOW(),
    source_event_id       UUID
);

CREATE INDEX idx_position_state_time ON position_state (last_modified_time DESC);

DROP TABLE IF EXISTS completed_trades CASCADE;

CREATE TABLE completed_trades (
    trade_id              UUID PRIMARY KEY,
    ticker                TEXT NOT NULL,
    entry_time            TIMESTAMP NOT NULL,
    exit_time             TIMESTAMP NOT NULL,
    entry_price           NUMERIC(10, 2) NOT NULL,
    exit_price            NUMERIC(10, 2) NOT NULL,
    qty                   INT NOT NULL,
    pnl                   NUMERIC(10, 2) NOT NULL,
    pnl_pct               NUMERIC(5, 2),
    duration_seconds      INT,
    strategy              TEXT,
    opened_event_id       UUID,
    closed_event_id       UUID,
    created_at            TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_completed_trades_time ON completed_trades (exit_time DESC);
CREATE INDEX idx_completed_trades_ticker ON completed_trades (ticker, exit_time DESC);

DROP TABLE IF EXISTS signal_history CASCADE;

CREATE TABLE signal_history (
    signal_id             UUID PRIMARY KEY,
    ticker                TEXT NOT NULL,
    signal_time           TIMESTAMP NOT NULL,
    signal_type           TEXT NOT NULL,
    action                TEXT NOT NULL,
    current_price         NUMERIC(10, 2),
    reason                TEXT,
    strategy              TEXT,
    confidence            NUMERIC(3, 2),
    source_event_id       UUID,
    created_at            TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_signal_history_time ON signal_history (signal_time DESC);
CREATE INDEX idx_signal_history_ticker ON signal_history (ticker, signal_time DESC);

DROP TABLE IF EXISTS fill_history CASCADE;

CREATE TABLE fill_history (
    fill_id               UUID PRIMARY KEY,
    ticker                TEXT NOT NULL,
    fill_time             TIMESTAMP NOT NULL,
    side                  TEXT NOT NULL,
    qty                   INT NOT NULL,
    fill_price            NUMERIC(10, 2) NOT NULL,
    order_id              TEXT,
    reason                TEXT,
    source_event_id       UUID,
    created_at            TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_fill_history_time ON fill_history (fill_time DESC);
CREATE INDEX idx_fill_history_ticker ON fill_history (ticker, fill_time DESC);

DROP TABLE IF EXISTS daily_metrics CASCADE;

CREATE TABLE daily_metrics (
    metric_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    metric_date           DATE NOT NULL UNIQUE,
    total_trades          INT NOT NULL DEFAULT 0,
    winning_trades        INT NOT NULL DEFAULT 0,
    losing_trades         INT NOT NULL DEFAULT 0,
    win_rate              NUMERIC(5, 2),
    total_pnl             NUMERIC(12, 2) NOT NULL DEFAULT 0,
    avg_win               NUMERIC(10, 2),
    avg_loss              NUMERIC(10, 2),
    largest_win           NUMERIC(10, 2),
    largest_loss          NUMERIC(10, 2),
    max_drawdown          NUMERIC(5, 2),
    sharpe_ratio          NUMERIC(5, 2),
    created_at            TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_daily_metrics_date ON daily_metrics (metric_date DESC);

-- ============================================================================
-- 3. HELPER FUNCTIONS
-- ============================================================================

-- Function to get all events for an aggregate
CREATE OR REPLACE FUNCTION get_aggregate_events(
    p_aggregate_type TEXT,
    p_aggregate_id TEXT
)
RETURNS TABLE (
    event_id UUID,
    event_time TIMESTAMP,
    event_type TEXT,
    event_payload JSONB
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        event_store.event_id,
        event_store.event_time,
        event_store.event_type,
        event_store.event_payload
    FROM event_store
    WHERE aggregate_type = p_aggregate_type
      AND aggregate_id = p_aggregate_id
    ORDER BY event_sequence;
END;
$$ LANGUAGE plpgsql STABLE;

-- Function to get latency metrics for a time window
CREATE OR REPLACE FUNCTION get_latency_metrics(
    p_start_time TIMESTAMP DEFAULT NOW() - INTERVAL '1 hour',
    p_end_time TIMESTAMP DEFAULT NOW()
)
RETURNS TABLE (
    metric_name TEXT,
    avg_ms NUMERIC,
    max_ms NUMERIC,
    p95_ms NUMERIC,
    event_count INT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        'ingest'::TEXT,
        ROUND(AVG(EXTRACT(EPOCH FROM (received_time - event_time)) * 1000)::numeric, 2),
        ROUND(MAX(EXTRACT(EPOCH FROM (received_time - event_time)) * 1000)::numeric, 2),
        ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (received_time - event_time)) * 1000)::numeric, 2),
        COUNT(*)::INT
    FROM event_store
    WHERE event_time BETWEEN p_start_time AND p_end_time

    UNION ALL

    SELECT
        'total'::TEXT,
        ROUND(AVG(EXTRACT(EPOCH FROM (persisted_time - event_time)) * 1000)::numeric, 2),
        ROUND(MAX(EXTRACT(EPOCH FROM (persisted_time - event_time)) * 1000)::numeric, 2),
        ROUND(PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (persisted_time - event_time)) * 1000)::numeric, 2),
        COUNT(*)::INT
    FROM event_store
    WHERE event_time BETWEEN p_start_time AND p_end_time;
END;
$$ LANGUAGE plpgsql STABLE;

-- Audit function to check event store quality
CREATE OR REPLACE FUNCTION audit_event_store(
    p_days INT DEFAULT 1
)
RETURNS TABLE (
    check_name TEXT,
    result TEXT,
    detail TEXT
) AS $$
BEGIN
    RETURN QUERY
    WITH recent_events AS (
        SELECT * FROM event_store
        WHERE event_time > NOW() - (p_days || ' days')::INTERVAL
    )
    SELECT 'Event Count'::TEXT, COUNT(*)::TEXT, 'Total events in past ' || p_days || ' days'::TEXT
    FROM recent_events

    UNION ALL

    SELECT 'PositionOpened Count'::TEXT, COUNT(*)::TEXT, 'Trades opened'::TEXT
    FROM recent_events WHERE event_type = 'PositionOpened'

    UNION ALL

    SELECT 'PositionClosed Count'::TEXT, COUNT(*)::TEXT, 'Trades closed'::TEXT
    FROM recent_events WHERE event_type = 'PositionClosed'

    UNION ALL

    SELECT 'Missing Timestamps'::TEXT, COUNT(*)::TEXT, 'Events with NULL timestamps (should be 0)'::TEXT
    FROM recent_events WHERE event_time IS NULL OR received_time IS NULL OR processed_time IS NULL;
END;
$$ LANGUAGE plpgsql STABLE;

-- ============================================================================
-- 4. PERMISSIONS
-- ============================================================================

-- Grant permissions to trading user
GRANT SELECT, INSERT ON event_store TO trading;
GRANT SELECT ON position_state, completed_trades, signal_history, fill_history, daily_metrics TO trading;
GRANT UPDATE ON position_state, completed_trades, signal_history, fill_history, daily_metrics TO trading;
GRANT EXECUTE ON FUNCTION get_aggregate_events TO trading;
GRANT EXECUTE ON FUNCTION get_latency_metrics TO trading;
GRANT EXECUTE ON FUNCTION audit_event_store TO trading;
