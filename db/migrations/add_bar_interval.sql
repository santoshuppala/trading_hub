/**
 * Migration: Add bar_interval column to market_bars
 *
 * Supports storing 1-min, 5-min, and daily bars in the same table.
 * Required for multi-timeframe backtesting (Option C).
 *
 * SAFETY: Additive change only. Existing rows get default '1m'.
 * Run: psql -d tradinghub -f db/migrations/add_bar_interval.sql
 */

-- Add bar_interval column (default '1m' for existing rows)
ALTER TABLE trading.market_bars
    ADD COLUMN IF NOT EXISTS bar_interval VARCHAR(3) DEFAULT '1m';

-- Update PK to include bar_interval (allows 1m + 5m at same timestamp)
ALTER TABLE trading.market_bars DROP CONSTRAINT IF EXISTS market_bars_pkey;
ALTER TABLE trading.market_bars ADD PRIMARY KEY (ticker, bar_time, bar_interval);

-- Create index for interval-filtered queries
CREATE INDEX IF NOT EXISTS idx_market_bars_interval
    ON trading.market_bars (ticker, bar_interval, bar_time DESC);

-- Verify
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'trading'
          AND table_name = 'market_bars'
          AND column_name = 'bar_interval'
    ) THEN
        RAISE NOTICE 'market_bars.bar_interval column added successfully';
    ELSE
        RAISE EXCEPTION 'Failed to add bar_interval column';
    END IF;
END $$;
