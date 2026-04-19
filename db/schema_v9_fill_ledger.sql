/**
 * V9 Schema Migration — FillLedger tables + fractional qty + cleanup
 *
 * Changes:
 *   1. New fill_lots table for lot-based position tracking
 *   2. New lot_matches table for FIFO match audit trail
 *   3. qty INT → NUMERIC(12,6) on projection tables (fractional shares)
 *   4. New FillLotAppended event type
 *   5. Stop writing new BarReceived (keep historical for compliance)
 *
 * SAFETY: Run AFTER market hours (post 4 PM ET).
 * All changes are additive — existing tables are not dropped.
 */

-- ============================================================================
-- 1. FILL LOTS TABLE (lot-based position tracking)
-- ============================================================================

CREATE TABLE IF NOT EXISTS fill_lots (
    lot_id            UUID PRIMARY KEY,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    direction         TEXT NOT NULL DEFAULT 'long' CHECK (direction IN ('long', 'short')),
    qty               NUMERIC(12, 6) NOT NULL CHECK (qty > 0),
    fill_price        NUMERIC(10, 2) NOT NULL CHECK (fill_price > 0),
    signal_price      NUMERIC(10, 2),
    order_id          TEXT,
    broker            TEXT NOT NULL,
    strategy          TEXT,
    correlation_id    UUID,
    fill_time         TIMESTAMP NOT NULL,
    reason            TEXT,
    order_mode        TEXT DEFAULT 'qty' CHECK (order_mode IN ('qty', 'notional')),
    synthetic         BOOLEAN DEFAULT FALSE,
    init_stop         NUMERIC(10, 2),
    init_target       NUMERIC(10, 2),
    init_atr          NUMERIC(10, 4),
    created_at        TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Fast lookup for open lots (BUY lots that haven't been fully matched)
CREATE INDEX IF NOT EXISTS idx_fill_lots_ticker
    ON fill_lots (ticker, fill_time DESC);

CREATE INDEX IF NOT EXISTS idx_fill_lots_open
    ON fill_lots (ticker)
    WHERE side = 'BUY';

CREATE INDEX IF NOT EXISTS idx_fill_lots_broker
    ON fill_lots (broker, ticker);

CREATE INDEX IF NOT EXISTS idx_fill_lots_daily
    ON fill_lots (fill_time::date, ticker);

-- ============================================================================
-- 2. LOT MATCHES TABLE (FIFO matching audit trail)
-- ============================================================================

CREATE TABLE IF NOT EXISTS lot_matches (
    match_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    buy_lot_id        UUID NOT NULL REFERENCES fill_lots(lot_id),
    sell_lot_id       UUID NOT NULL REFERENCES fill_lots(lot_id),
    matched_qty       NUMERIC(12, 6) NOT NULL CHECK (matched_qty > 0),
    buy_price         NUMERIC(10, 2) NOT NULL,
    sell_price        NUMERIC(10, 2) NOT NULL,
    realized_pnl      NUMERIC(10, 2) NOT NULL,
    ticker            TEXT NOT NULL,
    matched_at        TIMESTAMP NOT NULL,
    estimated         BOOLEAN DEFAULT FALSE,
    created_at        TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lot_matches_ticker
    ON lot_matches (ticker, matched_at DESC);

CREATE INDEX IF NOT EXISTS idx_lot_matches_daily
    ON lot_matches ((matched_at::date));

CREATE INDEX IF NOT EXISTS idx_lot_matches_buy
    ON lot_matches (buy_lot_id);

-- ============================================================================
-- 3. FRACTIONAL SHARE SUPPORT — qty INT → NUMERIC(12,6)
-- ============================================================================

-- position_state (if exists in trading schema)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'position_state' AND column_name = 'qty'
                 AND data_type = 'integer') THEN
        ALTER TABLE position_state ALTER COLUMN qty TYPE NUMERIC(12, 6);
        RAISE NOTICE 'position_state.qty → NUMERIC(12,6)';
    END IF;
END $$;

-- completed_trades
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'completed_trades' AND column_name = 'qty'
                 AND data_type = 'integer') THEN
        ALTER TABLE completed_trades ALTER COLUMN qty TYPE NUMERIC(12, 6);
        RAISE NOTICE 'completed_trades.qty → NUMERIC(12,6)';
    END IF;
END $$;

-- fill_history
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'fill_history' AND column_name = 'qty'
                 AND data_type = 'integer') THEN
        ALTER TABLE fill_history ALTER COLUMN qty TYPE NUMERIC(12, 6);
        RAISE NOTICE 'fill_history.qty → NUMERIC(12,6)';
    END IF;
END $$;

-- ============================================================================
-- 4. NEW EVENT TYPE
-- ============================================================================

INSERT INTO event_types (event_type, aggregate_type, description)
VALUES ('FillLotAppended', 'FillLedger', 'Fill lot appended to ledger (BUY or SELL)')
ON CONFLICT (event_type) DO NOTHING;

-- ============================================================================
-- 5. BarReceived — STOP writing new, KEEP historical
-- ============================================================================
--
-- Historical BarReceived events are KEPT because:
--   - analytics/compliance.py queries them for VWAP slippage calculation
--   - scripts/post_session_analytics.py uses them for end-of-day reports
--
-- New BarReceived writes are stopped in code (event_sourcing_subscriber.py
-- subscription commented out). No new events accumulate.
--
-- To reclaim space from OLD BarReceived events AFTER migrating analytics
-- to use a dedicated market_bars table, run:
--
--   DELETE FROM event_store WHERE event_type = 'BarReceived'
--     AND event_time < NOW() - INTERVAL '30 days';
--   VACUUM event_store;
--
-- This keeps the last 30 days for compliance lookback and deletes the rest.

-- ============================================================================
-- 6. MARKET BARS TABLE (lean replacement for BarReceived in event_store)
-- ============================================================================
--
-- ~10x smaller per row vs event_store (no UUID, no audit timestamps, no JSONB).
-- Used by: analytics/compliance.py (VWAP slippage), post_session_analytics.py
-- (MFE/MAE, bar context, counterfactual, regime classification).
--
-- The EventSourcingSubscriber._on_bar_lean() writes here instead of event_store.
-- If this table doesn't exist yet, it falls back to event_store (pre-migration).

CREATE TABLE IF NOT EXISTS market_bars (
    ticker            TEXT NOT NULL,
    bar_time          TIMESTAMP NOT NULL,
    open              NUMERIC(10, 2),
    high              NUMERIC(10, 2),
    low               NUMERIC(10, 2),
    close             NUMERIC(10, 2),
    volume            BIGINT,
    vwap              NUMERIC(10, 2),
    rvol              NUMERIC(6, 2),
    session_id        UUID,
    PRIMARY KEY (ticker, bar_time)
);

CREATE INDEX IF NOT EXISTS idx_market_bars_time
    ON market_bars (bar_time DESC);

CREATE INDEX IF NOT EXISTS idx_market_bars_ticker_time
    ON market_bars (ticker, bar_time DESC);

-- If using TimescaleDB (much better for time-series):
-- SELECT create_hypertable('market_bars', 'bar_time');
-- ALTER TABLE market_bars SET (timescaledb.compress);
-- SELECT add_compression_policy('market_bars', INTERVAL '7 days');

-- ============================================================================
-- 7. PERMISSIONS
-- ============================================================================

GRANT SELECT, INSERT ON fill_lots TO trading;
GRANT SELECT, INSERT ON lot_matches TO trading;
GRANT SELECT, INSERT ON market_bars TO trading;

-- ============================================================================
-- 8. ANALYTICS VIEW — Daily P&L from lot matches
-- ============================================================================

CREATE OR REPLACE VIEW v_daily_lot_pnl AS
SELECT
    matched_at::date AS trade_date,
    ticker,
    COUNT(*) AS match_count,
    SUM(matched_qty) AS total_qty,
    SUM(realized_pnl) AS total_pnl,
    SUM(CASE WHEN realized_pnl >= 0 THEN realized_pnl ELSE 0 END) AS gross_profit,
    SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END) AS gross_loss,
    SUM(CASE WHEN estimated THEN 1 ELSE 0 END) AS estimated_count
FROM lot_matches
GROUP BY matched_at::date, ticker
ORDER BY matched_at::date DESC, total_pnl DESC;

GRANT SELECT ON v_daily_lot_pnl TO trading;
