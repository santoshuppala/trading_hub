-- ============================================================
-- 004_pop_pro_events.sql  — POP_SIGNAL and PRO_STRATEGY_SIGNAL
-- ============================================================

-- ── POP_SIGNAL events ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.pop_signal_events (
    ts                      TIMESTAMPTZ     NOT NULL,
    symbol                  TEXT            NOT NULL,
    strategy_type           TEXT            NOT NULL,
    entry_price             DOUBLE PRECISION NOT NULL,
    stop_price              DOUBLE PRECISION NOT NULL,
    target_1                DOUBLE PRECISION NOT NULL,
    target_2                DOUBLE PRECISION NOT NULL,
    pop_reason              TEXT,
    atr_value               DOUBLE PRECISION,
    rvol                    DOUBLE PRECISION,
    vwap_distance           DOUBLE PRECISION,
    strategy_confidence     DOUBLE PRECISION,
    features_json           JSONB,
    event_id                UUID,
    correlation_id          UUID,
    ingested_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.pop_signal_events', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_pop_signal_symbol_ts
    ON trading.pop_signal_events (symbol, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pop_signal_strategy
    ON trading.pop_signal_events (strategy_type, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pop_features_gin
    ON trading.pop_signal_events USING GIN (features_json);

-- ── PRO_STRATEGY_SIGNAL events ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.pro_strategy_signal_events (
    ts                  TIMESTAMPTZ     NOT NULL,
    ticker              TEXT            NOT NULL,
    strategy_name       TEXT            NOT NULL,
    tier                SMALLINT        NOT NULL CHECK (tier IN (1,2,3)),
    direction           TEXT            NOT NULL CHECK (direction IN ('long','short')),
    entry_price         DOUBLE PRECISION NOT NULL,
    stop_price          DOUBLE PRECISION NOT NULL,
    target_1            DOUBLE PRECISION NOT NULL,
    target_2            DOUBLE PRECISION NOT NULL,
    atr_value           DOUBLE PRECISION,
    rvol                DOUBLE PRECISION,
    rsi_value           DOUBLE PRECISION,
    vwap                DOUBLE PRECISION,
    confidence          DOUBLE PRECISION,
    detector_signals    JSONB,
    event_id            UUID,
    correlation_id      UUID,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.pro_strategy_signal_events', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_pro_strategy_ticker_ts
    ON trading.pro_strategy_signal_events (ticker, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pro_strategy_name_ts
    ON trading.pro_strategy_signal_events (strategy_name, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pro_strategy_tier_ts
    ON trading.pro_strategy_signal_events (tier, ts DESC);
CREATE INDEX IF NOT EXISTS idx_pro_detector_signals_gin
    ON trading.pro_strategy_signal_events USING GIN (detector_signals);
