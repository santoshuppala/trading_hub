/**
 * ML-Optimized Tables for Trading Hub
 *
 * These tables supplement the event_store with pre-computed features
 * optimized for ML feature engineering and model training.
 *
 * Design principle: event_store is the source of truth (immutable).
 * These tables are DERIVED projections that can be rebuilt from events.
 */

-- ============================================================================
-- 1. ML TRADE OUTCOMES — Detailed trade analysis with excursion metrics
-- ============================================================================

DROP TABLE IF EXISTS ml_trade_outcomes CASCADE;

CREATE TABLE ml_trade_outcomes (
    trade_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker                TEXT NOT NULL,
    layer                 TEXT NOT NULL,       -- 'vwap', 'pro', 'pop', 'options'
    strategy_name         TEXT NOT NULL,       -- e.g. 'momentum_ignition', 'iron_condor'

    -- Entry details
    entry_ts              TIMESTAMP NOT NULL,
    entry_price           NUMERIC(10, 2) NOT NULL,
    entry_slippage_bps    NUMERIC(8, 2),       -- (fill_price - signal_price) / signal_price * 10000
    entry_latency_ms      INT,                 -- signal_time → fill_time
    entry_confidence      NUMERIC(5, 2),       -- signal confidence at entry time
    entry_rsi             NUMERIC(6, 2),
    entry_atr             NUMERIC(8, 4),
    entry_rvol            NUMERIC(6, 2),
    entry_iv_rank         NUMERIC(6, 2),       -- options: IV rank at entry (0-100)

    -- Exit details
    exit_ts               TIMESTAMP,
    exit_price            NUMERIC(10, 2),
    exit_slippage_bps     NUMERIC(8, 2),
    exit_latency_ms       INT,
    exit_reason           TEXT NOT NULL,        -- 'stop_hit', 'target_hit', 'profit_target', 'theta_bleed', 'dte_close', 'eod', etc.

    -- Performance metrics
    qty                   INT NOT NULL DEFAULT 1,
    realized_pnl          NUMERIC(12, 2) NOT NULL DEFAULT 0,
    realized_pnl_pct      NUMERIC(8, 4),
    commission            NUMERIC(8, 2) DEFAULT 0,

    -- Excursion analysis (CRITICAL for ML — measures trade quality)
    max_favorable_excursion  NUMERIC(10, 2),    -- highest unrealized profit during hold
    max_adverse_excursion    NUMERIC(10, 2),     -- lowest unrealized loss during hold
    mfe_pct               NUMERIC(8, 4),        -- MFE as % of entry
    mae_pct               NUMERIC(8, 4),        -- MAE as % of entry

    -- Context at trade time
    time_in_position_sec  INT,
    concurrent_positions  INT,                   -- how many other positions were open
    account_equity        NUMERIC(12, 2),        -- account value at entry

    -- Options-specific (NULL for equity trades)
    options_strategy_type TEXT,                   -- 'long_call', 'iron_condor', etc.
    options_net_debit     NUMERIC(10, 2),
    options_max_risk      NUMERIC(10, 2),
    options_max_reward    NUMERIC(10, 2),
    options_dte_at_entry  INT,
    options_iv_at_entry   NUMERIC(6, 4),

    -- Event linking
    opened_event_id       UUID,
    closed_event_id       UUID,
    session_id            UUID,

    created_at            TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ml_trade_outcomes_ticker ON ml_trade_outcomes (ticker, exit_ts DESC);
CREATE INDEX idx_ml_trade_outcomes_strategy ON ml_trade_outcomes (strategy_name, exit_ts DESC);
CREATE INDEX idx_ml_trade_outcomes_layer ON ml_trade_outcomes (layer, exit_ts DESC);
CREATE INDEX idx_ml_trade_outcomes_exit_reason ON ml_trade_outcomes (exit_reason);

-- ============================================================================
-- 2. ML SIGNAL CONTEXT — Feature snapshot at signal emission time
-- ============================================================================

DROP TABLE IF EXISTS ml_signal_context CASCADE;

CREATE TABLE ml_signal_context (
    signal_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_event_id       UUID,
    ts                    TIMESTAMP NOT NULL,
    ticker                TEXT NOT NULL,
    layer                 TEXT NOT NULL,          -- 'vwap', 'pro', 'pop', 'options'
    action                TEXT NOT NULL,          -- 'BUY', 'SELL_STOP', etc.
    strategy_name         TEXT,

    -- Core indicators at signal time
    current_price         NUMERIC(10, 2),
    rsi                   NUMERIC(6, 2),
    atr                   NUMERIC(8, 4),
    rvol                  NUMERIC(6, 2),
    vwap                  NUMERIC(10, 2),
    vwap_distance_pct     NUMERIC(6, 4),         -- (price - vwap) / vwap
    confidence            NUMERIC(5, 2),

    -- Options-specific
    iv_estimate           NUMERIC(6, 4),
    iv_rank               NUMERIC(6, 2),

    -- Bar context at signal time
    bar_return            NUMERIC(8, 4),          -- (close-open)/open of signal bar
    bar_range_pct         NUMERIC(8, 4),          -- (high-low)/open
    close_position_in_bar NUMERIC(3, 2),          -- 0=bottom wick, 1=top wick
    volume_rank_20        INT,                    -- percentile rank vs last 20 bars

    -- Signal outcome (filled after trade closes)
    was_executed          BOOLEAN DEFAULT FALSE,
    was_rejected          BOOLEAN DEFAULT FALSE,
    rejection_reason      TEXT,
    outcome_pnl           NUMERIC(12, 2),         -- P&L if executed (NULL if rejected)
    outcome_pnl_pct       NUMERIC(8, 4),

    -- Detector signals (pro_setups only)
    n_detectors_fired     INT,
    detector_agreement    NUMERIC(3, 2),          -- fraction of detectors that agreed

    created_at            TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ml_signal_context_ticker ON ml_signal_context (ticker, ts DESC);
CREATE INDEX idx_ml_signal_context_layer ON ml_signal_context (layer, ts DESC);
CREATE INDEX idx_ml_signal_context_executed ON ml_signal_context (was_executed, ts DESC);

-- ============================================================================
-- 3. ML EXECUTION QUALITY — Slippage and latency tracking
-- ============================================================================

DROP TABLE IF EXISTS ml_execution_quality CASCADE;

CREATE TABLE ml_execution_quality (
    exec_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_event_id        UUID,
    fill_event_id         UUID,
    ticker                TEXT NOT NULL,
    ts                    TIMESTAMP NOT NULL,

    -- Order details
    side                  TEXT NOT NULL,
    qty_ordered           INT NOT NULL,
    order_price           NUMERIC(10, 2),         -- price in ORDER_REQ

    -- Fill details
    fill_price            NUMERIC(10, 2),
    fill_qty              INT,
    fill_ts               TIMESTAMP,

    -- Quality metrics
    slippage_bps          NUMERIC(8, 2),           -- (fill - order) / order * 10000
    latency_ms            INT,                     -- order_time → fill_time
    partial_fill          BOOLEAN DEFAULT FALSE,

    -- Market context at order time
    bid_at_order          NUMERIC(10, 2),
    ask_at_order          NUMERIC(10, 2),
    spread_bps_at_order   NUMERIC(8, 2),

    created_at            TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ml_exec_quality_ticker ON ml_execution_quality (ticker, ts DESC);

-- ============================================================================
-- 4. ML REJECTION LOG — Structured rejection data for counterfactual analysis
-- ============================================================================

DROP TABLE IF EXISTS ml_rejection_log CASCADE;

CREATE TABLE ml_rejection_log (
    rejection_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_event_id       UUID,
    ts                    TIMESTAMP NOT NULL,
    ticker                TEXT NOT NULL,
    layer                 TEXT NOT NULL,
    action_blocked        TEXT NOT NULL,

    -- Rejection details
    reason_category       TEXT NOT NULL,            -- 'max_positions', 'cooldown', 'daily_loss', 'spread_wide', etc.
    reason_detail         TEXT,                     -- human-readable full reason

    -- Feature state at rejection (what the signal looked like)
    signal_rsi            NUMERIC(6, 2),
    signal_atr            NUMERIC(8, 4),
    signal_rvol           NUMERIC(6, 2),
    signal_confidence     NUMERIC(5, 2),
    signal_price          NUMERIC(10, 2),

    -- Portfolio state at rejection
    positions_held        INT,
    daily_pnl_at_reject   NUMERIC(12, 2),
    equity_at_reject      NUMERIC(12, 2),

    -- Counterfactual (filled AFTER the fact from price data)
    would_have_won        BOOLEAN,                 -- if entered, would it have been profitable?
    counterfactual_pnl    NUMERIC(12, 2),           -- estimated P&L if trade was taken

    created_at            TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_ml_rejection_log_ticker ON ml_rejection_log (ticker, ts DESC);
CREATE INDEX idx_ml_rejection_log_reason ON ml_rejection_log (reason_category, ts DESC);

-- ============================================================================
-- 5. ML DAILY REGIME — Market context per trading day
-- ============================================================================

DROP TABLE IF EXISTS ml_daily_regime CASCADE;

CREATE TABLE ml_daily_regime (
    regime_date           DATE PRIMARY KEY,

    -- Market-wide
    spy_return_pct        NUMERIC(6, 4),
    spy_vwap_distance     NUMERIC(6, 4),
    vix_close             NUMERIC(6, 2),
    vix_percentile_30d    NUMERIC(5, 2),

    -- Volume and breadth
    avg_rvol_universe     NUMERIC(6, 2),           -- average RVOL across watched tickers
    advance_decline_ratio NUMERIC(6, 2),

    -- Regime classification
    regime_label          TEXT,                     -- 'bull_trend', 'bear_trend', 'range_bound', 'volatile'
    volatility_regime     TEXT,                     -- 'low', 'normal', 'elevated', 'extreme'

    -- System performance
    total_signals         INT,
    total_trades          INT,
    total_pnl             NUMERIC(12, 2),
    win_rate              NUMERIC(5, 2),

    created_at            TIMESTAMP NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- 6. PERMISSIONS
-- ============================================================================

GRANT SELECT, INSERT, UPDATE ON ml_trade_outcomes TO trading;
GRANT SELECT, INSERT, UPDATE ON ml_signal_context TO trading;
GRANT SELECT, INSERT, UPDATE ON ml_execution_quality TO trading;
GRANT SELECT, INSERT, UPDATE ON ml_rejection_log TO trading;
GRANT SELECT, INSERT, UPDATE ON ml_daily_regime TO trading;

-- ============================================================================
-- 7. HELPER: Rebuild ml_trade_outcomes from event_store
-- ============================================================================

CREATE OR REPLACE FUNCTION rebuild_ml_trade_outcomes(p_days INT DEFAULT 30)
RETURNS INT AS $$
DECLARE
    rows_inserted INT;
BEGIN
    -- Clear recent data
    DELETE FROM ml_trade_outcomes
    WHERE entry_ts > NOW() - (p_days || ' days')::INTERVAL;

    -- Rebuild from PositionOpened + PositionClosed pairs
    INSERT INTO ml_trade_outcomes (
        ticker, layer, strategy_name,
        entry_ts, entry_price, exit_ts, exit_price,
        exit_reason, realized_pnl,
        opened_event_id, closed_event_id
    )
    SELECT
        o.event_payload->>'ticker',
        'vwap',
        COALESCE(c.event_payload->>'exit_reason', 'unknown'),
        o.event_time,
        (o.event_payload->>'entry_price')::NUMERIC,
        c.event_time,
        (c.event_payload->>'current_price')::NUMERIC,
        COALESCE(c.event_payload->>'exit_reason', 'unknown'),
        COALESCE((c.event_payload->>'pnl')::NUMERIC, 0),
        o.event_id,
        c.event_id
    FROM event_store o
    JOIN event_store c ON o.aggregate_id = c.aggregate_id
        AND c.event_type = 'PositionClosed'
    WHERE o.event_type = 'PositionOpened'
        AND o.event_time > NOW() - (p_days || ' days')::INTERVAL;

    GET DIAGNOSTICS rows_inserted = ROW_COUNT;
    RETURN rows_inserted;
END;
$$ LANGUAGE plpgsql;

GRANT EXECUTE ON FUNCTION rebuild_ml_trade_outcomes TO trading;
