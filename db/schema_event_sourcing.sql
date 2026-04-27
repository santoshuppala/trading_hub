/**
 * Event Sourcing Schema for Trading Hub
 *
 * Core principle: Immutable append-only event store with complete timestamp trails
 * All trading events are recorded with full audit context
 * Projections are built FROM events, never the other way around
 */

-- ============================================================================
-- 1. CORE EVENT STORE (Immutable, Append-Only)
-- ============================================================================

CREATE TABLE IF NOT EXISTS event_store (
    -- Event Identity (immutable)
    event_id              UUID NOT NULL PRIMARY KEY,
    event_sequence        BIGINT NOT NULL,        -- Ordering for same microsecond
    event_type            TEXT NOT NULL,          -- 'PositionOpened', 'PositionClosed', 'FillExecuted', etc.
    event_version         INT DEFAULT 1,          -- Schema version for evolution

    -- Complete Timestamp Trail (CRITICAL for audit)
    event_time            TIMESTAMP NOT NULL,     -- When did it actually happen? (market time)
    received_time         TIMESTAMP NOT NULL,     -- When did system first see it? (ingestion time)
    queued_time           TIMESTAMP,              -- When did it enter queue? (optional)
    processed_time        TIMESTAMP NOT NULL,     -- When did DBSubscriber process it?
    persisted_time        TIMESTAMP DEFAULT now() NOT NULL,  -- When written to DB (DB trigger)

    -- Aggregate (entity being changed)
    aggregate_id          TEXT NOT NULL,          -- 'position_ARM', 'account_123', 'trade_xyz'
    aggregate_type        TEXT NOT NULL,          -- 'Position', 'Account', 'Trade', 'Signal'
    aggregate_version     INT NOT NULL,           -- Which version of the aggregate?

    -- Event Payload (immutable, stores complete event data)
    event_payload         JSONB NOT NULL,         -- Full event data as JSON (schema-flexible)

    -- Event Context (linking events together)
    correlation_id        UUID,                   -- Trace related events across the system
    causation_id          UUID,                   -- What event caused this event?
    parent_event_id       UUID,                   -- Parent-child relationships

    -- System Context
    source_system         TEXT NOT NULL,          -- 'PositionManager', 'RiskEngine', 'SignalEngine'
    source_version        TEXT,                   -- 'v5.1', 'v5.2', etc.
    session_id            UUID,                   -- Which monitor session created this?

    -- Computed Latencies (for monitoring)
    ingest_latency_ms     INT GENERATED AS (
        EXTRACT(EPOCH FROM (received_time - event_time)) * 1000
    ) STORED,

    queue_latency_ms      INT GENERATED AS (
        EXTRACT(EPOCH FROM (COALESCE(queued_time, processed_time) - received_time)) * 1000
    ) STORED,

    total_latency_ms      INT GENERATED AS (
        EXTRACT(EPOCH FROM (persisted_time - event_time)) * 1000
    ) STORED,

    -- Indexing
    CONSTRAINT event_store_pkey PRIMARY KEY (event_id),
    UNIQUE(aggregate_type, aggregate_id, event_sequence)  -- Ensure ordering
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_event_store_event_time
    ON event_store (event_time DESC);

CREATE INDEX IF NOT EXISTS idx_event_store_type
    ON event_store (event_type, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_event_store_aggregate
    ON event_store (aggregate_type, aggregate_id, event_sequence DESC);

CREATE INDEX IF NOT EXISTS idx_event_store_correlation
    ON event_store (correlation_id)
    WHERE correlation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_event_store_latency
    ON event_store (total_latency_ms DESC)
    WHERE total_latency_ms > 1000;  -- Only slow events

CREATE INDEX IF NOT EXISTS idx_event_store_session
    ON event_store (session_id, event_time DESC);

-- ============================================================================
-- 2. PROJECTION TABLES (Built FROM events, read-only for app logic)
-- ============================================================================

-- Position State Projection (current state of all positions)
CREATE TABLE IF NOT EXISTS position_state (
    ticker              TEXT PRIMARY KEY,
    position_id         UUID NOT NULL,

    -- Position Details
    action              TEXT NOT NULL,           -- 'OPENED', 'CLOSED', 'PARTIAL_EXIT'
    quantity            INT NOT NULL,
    entry_price         DECIMAL(10, 2),
    entry_time          TIMESTAMP,
    current_price       DECIMAL(10, 2),
    stop_price          DECIMAL(10, 2),
    target_1_price      DECIMAL(10, 2),
    target_2_price      DECIMAL(10, 2),

    -- P&L Tracking
    unrealised_pnl      DECIMAL(10, 2),
    realised_pnl        DECIMAL(10, 2),

    -- Audit
    opened_event_id     UUID NOT NULL,
    last_modified_event_id UUID,
    last_modified_time  TIMESTAMP DEFAULT now(),

    INDEX idx_position_state_time(last_modified_time DESC)
) ENGINE=InnoDB;

-- Completed Trades Projection (historical record of closed trades)
CREATE TABLE IF NOT EXISTS completed_trades (
    trade_id            UUID PRIMARY KEY,

    -- Trade Details
    ticker              TEXT NOT NULL,
    quantity            INT NOT NULL,
    entry_price         DECIMAL(10, 2),
    entry_time          TIMESTAMP,
    exit_price          DECIMAL(10, 2),
    exit_time           TIMESTAMP,

    -- P&L
    gross_pnl           DECIMAL(10, 2),
    realised_pnl        DECIMAL(10, 2),
    is_win              BOOLEAN DEFAULT false,
    duration_seconds    INT,

    -- Context
    reason              TEXT,
    strategy_name       TEXT,
    tier                INT,
    detectors_fired     TEXT[],

    -- Events
    opened_event_id     UUID NOT NULL,
    closed_event_id     UUID NOT NULL,

    -- Audit
    created_at          TIMESTAMP DEFAULT now(),

    INDEX idx_completed_trades_time(exit_time DESC),
    INDEX idx_completed_trades_ticker(ticker),
    INDEX idx_completed_trades_pnl(realised_pnl)
) ENGINE=InnoDB;

-- Signal History Projection (all signals emitted)
CREATE TABLE IF NOT EXISTS signal_history (
    signal_id           UUID PRIMARY KEY,

    -- Signal Details
    signal_type         TEXT NOT NULL,           -- 'pro_strategy', 'pop', 'options'
    ticker              TEXT NOT NULL,
    action              TEXT,                    -- 'BUY', 'SELL_RSI', 'SELL_VWAP', etc.
    direction           TEXT,                    -- 'long', 'short'

    -- Metrics
    entry_price         DECIMAL(10, 2),
    stop_price          DECIMAL(10, 2),
    target_1            DECIMAL(10, 2),
    target_2            DECIMAL(10, 2),
    confidence          DECIMAL(5, 2),

    -- Context
    strategy_name       TEXT,
    tier                INT,
    atr_value           DECIMAL(10, 4),
    rsi_value           DECIMAL(5, 2),
    rvol                DECIMAL(5, 2),

    -- Timing
    signal_time         TIMESTAMP NOT NULL,
    filled_at           TIMESTAMP,

    -- Events
    signal_event_id     UUID NOT NULL,
    fill_event_id       UUID,

    INDEX idx_signal_history_time(signal_time DESC),
    INDEX idx_signal_history_ticker(ticker),
    INDEX idx_signal_history_filled(filled_at)
) ENGINE=InnoDB;

-- Fill History Projection (all fills executed)
CREATE TABLE IF NOT EXISTS fill_history (
    fill_id             UUID PRIMARY KEY,

    -- Fill Details
    ticker              TEXT NOT NULL,
    side                TEXT NOT NULL,          -- 'BUY', 'SELL'
    quantity            INT NOT NULL,
    fill_price          DECIMAL(10, 2),
    fill_time           TIMESTAMP NOT NULL,

    -- Order Context
    order_id            TEXT,
    reason              TEXT,

    -- Events
    fill_event_id       UUID NOT NULL,

    INDEX idx_fill_history_time(fill_time DESC),
    INDEX idx_fill_history_ticker(ticker),
    INDEX idx_fill_history_side(side)
) ENGINE=InnoDB;

-- Daily Metrics Projection (aggregated daily stats)
CREATE TABLE IF NOT EXISTS daily_metrics (
    metric_date         DATE PRIMARY KEY,

    -- Trading Stats
    total_trades        INT DEFAULT 0,
    winning_trades      INT DEFAULT 0,
    losing_trades       INT DEFAULT 0,
    breakeven_trades    INT DEFAULT 0,

    -- P&L
    total_pnl           DECIMAL(12, 2) DEFAULT 0,
    gross_pnl           DECIMAL(12, 2) DEFAULT 0,
    max_win             DECIMAL(10, 2),
    max_loss            DECIMAL(10, 2),

    -- Risk
    max_drawdown        DECIMAL(10, 2),
    consecutive_losses  INT DEFAULT 0,

    -- Performance
    win_rate            DECIMAL(5, 2),
    profit_factor       DECIMAL(5, 2),

    -- Timing
    first_trade         TIMESTAMP,
    last_trade          TIMESTAMP,

    -- Audit
    last_updated        TIMESTAMP DEFAULT now()
) ENGINE=InnoDB;

-- ============================================================================
-- 3. EVENT TYPE CONSTANTS (for application logic)
-- ============================================================================

CREATE TABLE IF NOT EXISTS event_types (
    event_type          TEXT PRIMARY KEY,
    description         TEXT,
    aggregate_type      TEXT,
    schema_version      INT DEFAULT 1,
    created_at          TIMESTAMP DEFAULT now()
) ENGINE=InnoDB;

INSERT INTO event_types (event_type, aggregate_type, description) VALUES
    -- Position Events
    ('PositionOpened',      'Position', 'Position opened after buy fill'),
    ('PositionClosed',      'Position', 'Position closed after sell fill'),
    ('PartialExited',       'Position', 'Position partially exited'),
    ('StopHit',             'Position', 'Stop price hit'),
    ('TargetHit',           'Position', 'Target price hit'),

    -- Fill Events
    ('FillExecuted',        'Trade',    'Order fill executed'),
    ('PartialFill',         'Trade',    'Partial fill executed'),

    -- Signal Events
    ('ProStrategySignal',   'Signal',   'Pro setup engine signal'),
    ('PopStrategySignal',   'Signal',   'Pop strategy engine signal'),
    ('OptionsSignal',       'Signal',   'Options strategy signal'),
    ('OptionsPositionClosed','OptionsPosition', 'Options position closed with full lifecycle'),

    -- Risk Events
    ('RiskBlocked',         'Risk',     'Position blocked by risk adapter'),
    ('RiskAllowed',         'Risk',     'Position passed risk check'),

    -- Order Events
    ('OrderRequested',      'Order',    'Order request created'),
    ('OrderRejected',       'Order',    'Order rejected'),

    -- System Events
    ('SessionStarted',      'Session',  'Monitor session started'),
    ('SessionEnded',        'Session',  'Monitor session ended'),
    ('CrashRecovered',      'Session',  'Recovered from crash')
ON DUPLICATE KEY UPDATE description = VALUES(description);

-- ============================================================================
-- 4. HELPER FUNCTIONS
-- ============================================================================

-- Function: Get all events for an aggregate
CREATE OR REPLACE FUNCTION get_aggregate_events(
    p_aggregate_type TEXT,
    p_aggregate_id TEXT
) RETURNS TABLE (
    event_id UUID,
    event_type TEXT,
    event_time TIMESTAMP,
    event_payload JSONB
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        e.event_id,
        e.event_type,
        e.event_time,
        e.event_payload
    FROM event_store e
    WHERE e.aggregate_type = p_aggregate_type
      AND e.aggregate_id = p_aggregate_id
    ORDER BY e.event_sequence ASC;
END;
$$ LANGUAGE plpgsql;

-- Function: Rebuild position state from events
CREATE OR REPLACE FUNCTION rebuild_position_state(
    p_ticker TEXT
) RETURNS void AS $$
DECLARE
    v_events RECORD;
    v_position_id UUID;
    v_action TEXT;
    v_quantity INT;
    v_entry_price DECIMAL;
    v_current_price DECIMAL;
BEGIN
    -- Get all position events for this ticker
    FOR v_events IN
        SELECT
            event_id,
            event_type,
            (event_payload->>'action') as action,
            (event_payload->>'quantity')::INT as qty,
            (event_payload->'entry_price')::DECIMAL as entry_price,
            event_payload
        FROM event_store
        WHERE aggregate_id = CONCAT('position_', p_ticker)
        ORDER BY event_sequence ASC
    LOOP
        -- Update or insert position state based on event
        IF v_events.action = 'OPENED' THEN
            INSERT INTO position_state (
                ticker, position_id, action, quantity, entry_price,
                entry_time, opened_event_id, last_modified_event_id
            ) VALUES (
                p_ticker, v_position_id, v_events.action, v_events.qty,
                v_events.entry_price, NOW(), v_events.event_id, v_events.event_id
            )
            ON DUPLICATE KEY UPDATE
                action = v_events.action,
                quantity = v_events.qty,
                entry_price = v_events.entry_price,
                last_modified_event_id = v_events.event_id;
        ELSIF v_events.action = 'CLOSED' THEN
            DELETE FROM position_state WHERE ticker = p_ticker;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Function: Calculate daily metrics
CREATE OR REPLACE FUNCTION calculate_daily_metrics(
    p_date DATE
) RETURNS void AS $$
BEGIN
    INSERT INTO daily_metrics (
        metric_date, total_trades, winning_trades, losing_trades,
        total_pnl, win_rate, last_updated
    )
    SELECT
        p_date,
        COUNT(*),
        SUM(CASE WHEN realised_pnl >= 0 THEN 1 ELSE 0 END),
        SUM(CASE WHEN realised_pnl < 0 THEN 1 ELSE 0 END),
        SUM(realised_pnl),
        ROUND(
            SUM(CASE WHEN realised_pnl >= 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*),
            2
        ),
        NOW()
    FROM completed_trades
    WHERE DATE(exit_time) = p_date
    ON DUPLICATE KEY UPDATE
        total_trades = VALUES(total_trades),
        winning_trades = VALUES(winning_trades),
        losing_trades = VALUES(losing_trades),
        total_pnl = VALUES(total_pnl),
        win_rate = VALUES(win_rate),
        last_updated = NOW();
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 5. CLEANUP: Mark old tables as deprecated (keep for migration)
-- ============================================================================

-- Comment on old tables
ALTER TABLE trading.bar_events COMMENT = 'DEPRECATED: Use event_store instead';
ALTER TABLE trading.signal_events COMMENT = 'DEPRECATED: Use event_store instead';
ALTER TABLE trading.fill_events COMMENT = 'DEPRECATED: Use event_store instead';
ALTER TABLE trading.position_events COMMENT = 'DEPRECATED: Use event_store instead';

COMMIT;
