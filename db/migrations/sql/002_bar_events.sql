-- ============================================================
-- 002_bar_events.sql  — BAR hypertable (1-minute OHLCV bars)
-- ============================================================

CREATE TABLE IF NOT EXISTS trading.bar_events (
    ts          TIMESTAMPTZ     NOT NULL,           -- bar close time (UTC)
    ticker      TEXT            NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      BIGINT          NOT NULL,
    vwap        DOUBLE PRECISION,
    rvol        DOUBLE PRECISION,                   -- relative volume vs 20-bar avg
    atr         DOUBLE PRECISION,                   -- 14-period ATR
    rsi         DOUBLE PRECISION,                   -- 14-period RSI
    event_id    UUID,
    ingested_at TIMESTAMPTZ     NOT NULL DEFAULT now()
);

SELECT create_hypertable(
    'trading.bar_events', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

CREATE INDEX IF NOT EXISTS idx_bar_ticker_ts
    ON trading.bar_events (ticker, ts DESC);
