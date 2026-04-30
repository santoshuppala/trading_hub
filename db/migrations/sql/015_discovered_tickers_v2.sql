-- V10: Upgrade discovered_tickers table for DB-as-source-of-truth discovery.
-- Adds session_date for daily partitioning and unique constraint for upsert.
-- Replaces Kafka + JSON file discovery pipeline with direct DB reads/writes.

-- Add session_date column (defaults to today)
ALTER TABLE trading.discovered_tickers
    ADD COLUMN IF NOT EXISTS session_date DATE NOT NULL DEFAULT CURRENT_DATE;

-- Add metadata column (richer than discovery_data, captures WHY the ticker was flagged)
-- Keep discovery_data for backward compat, metadata is the new standard
ALTER TABLE trading.discovered_tickers
    ADD COLUMN IF NOT EXISTS metadata JSONB;

-- Add last_seen for tracking re-discoveries within a session
ALTER TABLE trading.discovered_tickers
    ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Unique constraint: one entry per ticker per day per source
-- Enables ON CONFLICT (ticker, session_date, source) DO UPDATE
CREATE UNIQUE INDEX IF NOT EXISTS idx_discovered_tickers_unique
    ON trading.discovered_tickers (ticker, session_date, source);

-- Fast lookup for Core polling: "give me all tickers discovered today after X"
CREATE INDEX IF NOT EXISTS idx_discovered_tickers_session_ts
    ON trading.discovered_tickers (session_date, ts);

-- Backfill session_date from ts for existing rows
UPDATE trading.discovered_tickers
SET session_date = ts::date
WHERE session_date = CURRENT_DATE AND ts::date != CURRENT_DATE;
