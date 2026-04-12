-- ============================================================
-- 009_session_log.sql  — Session lifecycle tracking
--
-- Every monitor start/stop writes a row.  Used by:
--   1. Deterministic replay — know which events belong to which session
--   2. Crash detection      — session without a clean stop_ts
--   3. Audit trail          — who ran what, when
-- ============================================================

CREATE TABLE IF NOT EXISTS trading.session_log (
    session_id      UUID            NOT NULL DEFAULT gen_random_uuid(),
    start_ts        TIMESTAMPTZ     NOT NULL DEFAULT now(),
    stop_ts         TIMESTAMPTZ,                          -- NULL = still running or crashed
    mode            TEXT            NOT NULL DEFAULT 'live'
                                    CHECK (mode IN ('live','paper','replay','backtest')),
    broker          TEXT,                                  -- 'alpaca', 'paper', etc.
    tickers         TEXT[],                                -- watchlist at session start
    config_hash     TEXT,                                  -- SHA-256 of config snapshot
    events_emitted  BIGINT          DEFAULT 0,
    events_persisted BIGINT         DEFAULT 0,
    rows_dropped    BIGINT          DEFAULT 0,
    exit_reason     TEXT,                                  -- 'clean', 'sigint', 'exception', NULL
    error_message   TEXT,
    metadata        JSONB,                                 -- arbitrary session metadata
    PRIMARY KEY (session_id)
);

-- Not a hypertable — low volume, needs full SQL (UPDATE for stop_ts).
-- Index for "find latest session" and "find crashed sessions".
CREATE INDEX IF NOT EXISTS idx_session_start_ts
    ON trading.session_log (start_ts DESC);
CREATE INDEX IF NOT EXISTS idx_session_running
    ON trading.session_log (stop_ts)
    WHERE stop_ts IS NULL;
