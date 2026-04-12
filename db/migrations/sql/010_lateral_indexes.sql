-- ============================================================
-- 010_lateral_indexes.sql  — Indexes for feature_store LATERAL joins
--
-- The FeatureStore queries use LEFT JOIN LATERAL to correlate
-- signals with subsequent fills and bar highs within time windows.
-- Without ASC indexes on (ticker, ts), these are nested loop scans.
-- ============================================================

-- Feature store: join signal → next fill (BUY only, within 5 min)
CREATE INDEX IF NOT EXISTS idx_fill_ticker_ts_buy_asc
    ON trading.fill_events (ticker, ts ASC)
    WHERE side = 'BUY';

-- Feature store: join signal → max(high) in next 30 min
CREATE INDEX IF NOT EXISTS idx_bar_ticker_ts_asc
    ON trading.bar_events (ticker, ts ASC)
    INCLUDE (high);

-- Feature store: signal outcome labels (action + ts for BUY filtering)
CREATE INDEX IF NOT EXISTS idx_signal_action_ticker_ts_asc
    ON trading.signal_events (action, ticker, ts ASC)
    WHERE action = 'BUY';
