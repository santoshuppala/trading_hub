/**
 * 014 — Edge Context Columns
 *
 * Adds market context fields captured at signal time across 4 tables:
 *   fill_lots                  — V9 lot-based tracking (JSON-primary, DB ready)
 *   fill_events                — active fill projection (event_sourcing_subscriber)
 *   signal_events              — VWAP signal projection
 *   pro_strategy_signal_events — Pro setup signal projection
 *
 * These fields enable the edge model to bucket trade outcomes by
 * (strategy, timeframe, regime, time_bucket) and compute historical
 * expected R-multiples for signal arbitration.
 *
 * All columns are nullable with defaults — existing rows are unaffected.
 * No data migration needed; new fields populate on new events only.
 *
 * SAFETY: Run AFTER market hours. All changes are additive.
 */

-- ============================================================================
-- 1. FILL_LOTS (lot-based tracking)
-- ============================================================================

ALTER TABLE fill_lots ADD COLUMN IF NOT EXISTS timeframe TEXT DEFAULT '1min';
ALTER TABLE fill_lots ADD COLUMN IF NOT EXISTS regime_at_entry TEXT DEFAULT '';
ALTER TABLE fill_lots ADD COLUMN IF NOT EXISTS time_bucket TEXT DEFAULT '';
ALTER TABLE fill_lots ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE fill_lots ADD COLUMN IF NOT EXISTS confluence_score DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE fill_lots ADD COLUMN IF NOT EXISTS tier SMALLINT DEFAULT 0;

-- ============================================================================
-- 2. FILL_EVENTS (active fill projection)
-- ============================================================================

ALTER TABLE fill_events ADD COLUMN IF NOT EXISTS timeframe TEXT DEFAULT '1min';
ALTER TABLE fill_events ADD COLUMN IF NOT EXISTS regime_at_entry TEXT DEFAULT '';
ALTER TABLE fill_events ADD COLUMN IF NOT EXISTS time_bucket TEXT DEFAULT '';
ALTER TABLE fill_events ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE fill_events ADD COLUMN IF NOT EXISTS confluence_score DOUBLE PRECISION DEFAULT 0.0;
ALTER TABLE fill_events ADD COLUMN IF NOT EXISTS tier SMALLINT DEFAULT 0;

-- ============================================================================
-- 3. SIGNAL_EVENTS (VWAP signals — new fields from SignalPayload)
-- ============================================================================

ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS timeframe TEXT DEFAULT '1min';
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS regime_at_entry TEXT DEFAULT '';
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS time_bucket TEXT DEFAULT '';
ALTER TABLE signal_events ADD COLUMN IF NOT EXISTS confluence_score DOUBLE PRECISION DEFAULT 0.0;

-- ============================================================================
-- 4. PRO_STRATEGY_SIGNAL_EVENTS (pro signals — new fields from ProStrategySignalPayload)
-- ============================================================================

ALTER TABLE pro_strategy_signal_events ADD COLUMN IF NOT EXISTS timeframe TEXT DEFAULT '1min';
ALTER TABLE pro_strategy_signal_events ADD COLUMN IF NOT EXISTS regime_at_entry TEXT DEFAULT '';
ALTER TABLE pro_strategy_signal_events ADD COLUMN IF NOT EXISTS time_bucket TEXT DEFAULT '';
ALTER TABLE pro_strategy_signal_events ADD COLUMN IF NOT EXISTS confluence_score DOUBLE PRECISION DEFAULT 0.0;

-- ============================================================================
-- 5. POP_SIGNAL_EVENTS (pop signals)
-- ============================================================================

ALTER TABLE pop_signal_events ADD COLUMN IF NOT EXISTS timeframe TEXT DEFAULT '1min';
ALTER TABLE pop_signal_events ADD COLUMN IF NOT EXISTS regime_at_entry TEXT DEFAULT '';
ALTER TABLE pop_signal_events ADD COLUMN IF NOT EXISTS time_bucket TEXT DEFAULT '';

-- ============================================================================
-- 6. INDEXES FOR EDGE MODEL LOOKUPS
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_fill_lots_edge_context
    ON fill_lots (strategy, timeframe, regime_at_entry, time_bucket)
    WHERE side = 'BUY';

CREATE INDEX IF NOT EXISTS idx_fill_lots_strategy_tier
    ON fill_lots (strategy, tier)
    WHERE side = 'BUY';

-- Edge analysis query: join signals to fills via correlation_id
CREATE INDEX IF NOT EXISTS idx_signal_events_edge
    ON signal_events (regime_at_entry, time_bucket)
    WHERE action = 'BUY';

CREATE INDEX IF NOT EXISTS idx_pro_signal_events_edge
    ON pro_strategy_signal_events (strategy_name, timeframe, regime_at_entry, time_bucket);
