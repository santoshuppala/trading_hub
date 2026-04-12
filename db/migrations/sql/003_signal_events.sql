-- ============================================================
-- 003_signal_events.sql  — SIGNAL, ORDER_REQ, FILL, POSITION
-- ============================================================

-- ── SIGNAL events ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.signal_events (
    ts              TIMESTAMPTZ     NOT NULL,
    ticker          TEXT            NOT NULL,
    action          trading.signal_action NOT NULL,
    current_price   DOUBLE PRECISION NOT NULL,
    ask_price       DOUBLE PRECISION,
    atr_value       DOUBLE PRECISION,
    rsi_value       DOUBLE PRECISION,
    rvol            DOUBLE PRECISION,
    vwap            DOUBLE PRECISION,
    stop_price      DOUBLE PRECISION,
    target_price    DOUBLE PRECISION,
    half_target     DOUBLE PRECISION,
    event_id        UUID,
    correlation_id  UUID,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.signal_events', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_signal_ticker_ts
    ON trading.signal_events (ticker, ts DESC);

-- ── ORDER_REQ events ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.order_req_events (
    ts              TIMESTAMPTZ     NOT NULL,
    ticker          TEXT            NOT NULL,
    side            trading.order_side NOT NULL,
    qty             INT             NOT NULL,
    price           DOUBLE PRECISION NOT NULL,
    reason          TEXT,
    stop_price      DOUBLE PRECISION,
    target_price    DOUBLE PRECISION,
    atr_value       DOUBLE PRECISION,
    event_id        UUID,
    correlation_id  UUID,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.order_req_events', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_order_req_ticker_ts
    ON trading.order_req_events (ticker, ts DESC);

-- ── FILL events ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.fill_events (
    ts              TIMESTAMPTZ     NOT NULL,
    ticker          TEXT            NOT NULL,
    side            trading.order_side NOT NULL,
    qty             INT             NOT NULL,
    fill_price      DOUBLE PRECISION NOT NULL,
    order_id        TEXT,
    reason          TEXT,
    stop_price      DOUBLE PRECISION,
    target_price    DOUBLE PRECISION,
    atr_value       DOUBLE PRECISION,
    event_id        UUID,
    correlation_id  UUID,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.fill_events', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_fill_ticker_ts
    ON trading.fill_events (ticker, ts DESC);
CREATE INDEX IF NOT EXISTS idx_fill_order_id
    ON trading.fill_events (order_id);

-- ── POSITION events ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.position_events (
    ts              TIMESTAMPTZ     NOT NULL,
    ticker          TEXT            NOT NULL,
    action          trading.position_action NOT NULL,
    qty             INT,
    entry_price     DOUBLE PRECISION,
    current_price   DOUBLE PRECISION,
    stop_price      DOUBLE PRECISION,
    target_price    DOUBLE PRECISION,
    unrealised_pnl  DOUBLE PRECISION,
    realised_pnl    DOUBLE PRECISION,
    event_id        UUID,
    correlation_id  UUID,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.position_events', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_position_ticker_ts
    ON trading.position_events (ticker, ts DESC);
