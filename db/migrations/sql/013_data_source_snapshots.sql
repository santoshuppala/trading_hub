-- V7.1: Data source snapshot table for alternative data persistence
-- Stores Fear & Greed, FRED macro regime, Finviz screener, SEC filings,
-- Polygon prev-day, Yahoo earnings — all from DataCollector satellite.

CREATE TABLE IF NOT EXISTS trading.data_source_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_name     TEXT NOT NULL,         -- 'fear_greed', 'fred_macro', 'finviz', 'sec_edgar', 'polygon', 'yahoo'
    ticker          TEXT,                  -- NULL for market-wide (fear_greed, fred), ticker for per-stock
    data_payload    JSONB NOT NULL,        -- flexible schema per source
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for time-series queries
CREATE INDEX IF NOT EXISTS idx_ds_snapshots_ts
    ON trading.data_source_snapshots (ts DESC);

-- Index for source + ticker lookups
CREATE INDEX IF NOT EXISTS idx_ds_snapshots_source_ticker
    ON trading.data_source_snapshots (source_name, ticker, ts DESC);

-- V7.1: Discovered tickers table
CREATE TABLE IF NOT EXISTS trading.discovered_tickers (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker          TEXT NOT NULL,
    source          TEXT NOT NULL,         -- 'finviz_movers', 'yahoo_earnings', 'sec_filing', 'polygon_gap'
    discovery_data  JSONB,                 -- source-specific context
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_discovered_tickers_ts
    ON trading.discovered_tickers (ts DESC);

CREATE INDEX IF NOT EXISTS idx_discovered_tickers_ticker
    ON trading.discovered_tickers (ticker, ts DESC);

-- Retention: keep 30 days of snapshots, 90 days of discoveries
-- (applied by TimescaleDB if hypertable, otherwise manual cleanup)
