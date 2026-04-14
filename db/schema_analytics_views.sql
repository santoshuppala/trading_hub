-- schema_analytics_views.sql
-- Analytics-ready VIEWs and dimension tables for the Trading Hub event store.
-- ZERO write overhead — all views are computed on read only.
-- Safe to deploy during trading hours.
--
-- Requires: PostgreSQL 16+, event_store table from schema_event_sourcing_pg.sql

-- ============================================================================
-- 1. v_signal_features — flattens all signal event_payloads into typed columns
-- ============================================================================
CREATE OR REPLACE VIEW v_signal_features AS
SELECT
    event_id,
    event_time AS ts,
    event_type,
    session_id,
    event_payload->>'ticker' AS ticker,
    event_payload->>'action' AS action,
    (event_payload->>'current_price')::NUMERIC(10,2) AS current_price,
    (event_payload->>'atr_value')::NUMERIC(8,4) AS atr,
    (event_payload->>'rsi_value')::NUMERIC(6,2) AS rsi,
    (event_payload->>'rvol')::NUMERIC(6,2) AS rvol,
    (event_payload->>'vwap')::NUMERIC(10,2) AS vwap,
    (event_payload->>'stop_price')::NUMERIC(10,2) AS stop_price,
    (event_payload->>'target_price')::NUMERIC(10,2) AS target_price,
    (event_payload->>'confidence')::NUMERIC(5,2) AS confidence,
    -- Strategy layer identification
    CASE event_type
        WHEN 'StrategySignal' THEN 'vwap'
        WHEN 'PopStrategySignal' THEN 'pop'
        WHEN 'ProStrategySignal' THEN 'pro'
        WHEN 'OptionsSignal' THEN 'options'
        ELSE 'unknown'
    END AS layer,
    -- Pro-specific
    event_payload->>'strategy_name' AS strategy_name,
    (event_payload->>'tier')::INT AS tier,
    event_payload->>'direction' AS direction,
    -- Pop-specific
    event_payload->>'pop_reason' AS pop_reason,
    event_payload->>'strategy_type' AS strategy_type,
    (event_payload->>'vwap_distance')::NUMERIC(6,4) AS vwap_distance,
    -- Options-specific
    (event_payload->>'net_debit')::NUMERIC(10,2) AS net_debit,
    (event_payload->>'max_risk')::NUMERIC(10,2) AS max_risk,
    (event_payload->>'max_reward')::NUMERIC(10,2) AS max_reward,
    event_payload->>'source' AS options_source,
    correlation_id
FROM event_store
WHERE event_type IN ('StrategySignal', 'PopStrategySignal', 'ProStrategySignal', 'OptionsSignal');


-- ============================================================================
-- 2. v_trade_details — complete trade view with entry + exit joined
-- ============================================================================
CREATE OR REPLACE VIEW v_trade_details AS
SELECT
    c.event_id AS trade_id,
    o.event_payload->>'ticker' AS ticker,
    o.event_time AS entry_ts,
    c.event_time AS exit_ts,
    EXTRACT(EPOCH FROM (c.event_time - o.event_time))::INT AS duration_sec,
    -- Session phase
    CASE
        WHEN EXTRACT(HOUR FROM o.event_time) * 60 + EXTRACT(MINUTE FROM o.event_time) < 585 THEN 'first_15min'
        WHEN EXTRACT(HOUR FROM o.event_time) * 60 + EXTRACT(MINUTE FROM o.event_time) < 630 THEN 'morning'
        WHEN EXTRACT(HOUR FROM o.event_time) * 60 + EXTRACT(MINUTE FROM o.event_time) < 780 THEN 'midday'
        WHEN EXTRACT(HOUR FROM o.event_time) * 60 + EXTRACT(MINUTE FROM o.event_time) < 945 THEN 'afternoon'
        ELSE 'power_hour'
    END AS session_phase,
    (o.event_payload->>'entry_price')::NUMERIC(10,2) AS entry_price,
    (c.event_payload->>'current_price')::NUMERIC(10,2) AS exit_price,
    (o.event_payload->>'qty')::INT AS qty,
    (c.event_payload->>'pnl')::NUMERIC(10,2) AS pnl,
    -- Exit reason from the CLOSED event action or from payload
    COALESCE(c.event_payload->>'exit_reason', c.event_payload->>'action', 'unknown') AS exit_reason,
    -- Entry indicators
    (o.event_payload->>'stop_price')::NUMERIC(10,2) AS stop_price,
    (o.event_payload->>'target_price')::NUMERIC(10,2) AS target_price,
    o.aggregate_id,
    o.session_id,
    o.event_id AS opened_event_id,
    c.event_id AS closed_event_id
FROM event_store o
JOIN event_store c
    ON o.aggregate_id = c.aggregate_id
    AND c.event_type = 'PositionClosed'
WHERE o.event_type = 'PositionOpened';


-- ============================================================================
-- 3. v_risk_block_analysis — structured rejection data
-- ============================================================================
CREATE OR REPLACE VIEW v_risk_block_analysis AS
SELECT
    event_id,
    event_time AS ts,
    event_payload->>'ticker' AS ticker,
    event_payload->>'reason' AS reason_detail,
    event_payload->>'signal_action' AS action_blocked,
    -- Categorize the reason
    CASE
        WHEN event_payload->>'reason' ILIKE '%max_position%' THEN 'max_positions'
        WHEN event_payload->>'reason' ILIKE '%cooldown%' THEN 'cooldown'
        WHEN event_payload->>'reason' ILIKE '%daily%loss%' THEN 'daily_loss_limit'
        WHEN event_payload->>'reason' ILIKE '%spread%' THEN 'spread_too_wide'
        WHEN event_payload->>'reason' ILIKE '%already%' THEN 'duplicate_position'
        WHEN event_payload->>'reason' ILIKE '%budget%' OR event_payload->>'reason' ILIKE '%capital%' THEN 'insufficient_capital'
        ELSE 'other'
    END AS reason_category,
    session_id
FROM event_store
WHERE event_type = 'RiskBlocked';


-- ============================================================================
-- 4. v_fill_quality — slippage analysis
-- ============================================================================
CREATE OR REPLACE VIEW v_fill_quality AS
SELECT
    f.event_id AS fill_id,
    f.event_time AS fill_ts,
    f.event_payload->>'ticker' AS ticker,
    f.event_payload->>'side' AS side,
    (f.event_payload->>'qty')::INT AS qty,
    (f.event_payload->>'fill_price')::NUMERIC(10,2) AS fill_price,
    f.event_payload->>'order_id' AS order_id,
    -- Find matching ORDER_REQ via correlation_id
    (o.event_payload->>'price')::NUMERIC(10,2) AS order_price,
    o.event_time AS order_ts,
    -- Compute slippage in basis points
    CASE WHEN (o.event_payload->>'price')::NUMERIC > 0 THEN
        ROUND(((f.event_payload->>'fill_price')::NUMERIC - (o.event_payload->>'price')::NUMERIC) / (o.event_payload->>'price')::NUMERIC * 10000, 2)
    ELSE NULL END AS slippage_bps,
    -- Latency in milliseconds
    EXTRACT(EPOCH FROM (f.event_time - o.event_time)) * 1000 AS latency_ms,
    f.session_id
FROM event_store f
LEFT JOIN event_store o
    ON f.correlation_id = o.correlation_id
    AND o.event_type = 'OrderRequested'
    AND f.event_payload->>'ticker' = o.event_payload->>'ticker'
WHERE f.event_type = 'FillExecuted';


-- ============================================================================
-- 5. v_options_trades — options-specific trade view
-- ============================================================================
CREATE OR REPLACE VIEW v_options_trades AS
SELECT
    event_id,
    event_time AS ts,
    event_payload->>'ticker' AS ticker,
    event_payload->>'strategy_type' AS strategy_type,
    (event_payload->>'underlying_price')::NUMERIC(10,2) AS underlying_price,
    event_payload->>'expiry_date' AS expiry_date,
    (event_payload->>'net_debit')::NUMERIC(10,2) AS net_debit,
    (event_payload->>'max_risk')::NUMERIC(10,2) AS max_risk,
    (event_payload->>'max_reward')::NUMERIC(10,2) AS max_reward,
    (event_payload->>'atr_value')::NUMERIC(8,4) AS atr,
    (event_payload->>'rvol')::NUMERIC(6,2) AS rvol,
    (event_payload->>'rsi_value')::NUMERIC(6,2) AS rsi,
    event_payload->>'source' AS source,
    event_payload->'legs' AS legs,
    -- Risk/reward ratio
    CASE WHEN (event_payload->>'max_risk')::NUMERIC > 0 THEN
        ROUND((event_payload->>'max_reward')::NUMERIC / (event_payload->>'max_risk')::NUMERIC, 2)
    ELSE NULL END AS rr_ratio,
    -- Credit or debit trade
    CASE WHEN (event_payload->>'net_debit')::NUMERIC < 0 THEN 'credit' ELSE 'debit' END AS trade_type,
    session_id
FROM event_store
WHERE event_type = 'OptionsSignal';


-- ============================================================================
-- 6. v_equity_curve — reconstruct equity from position events
-- ============================================================================
CREATE OR REPLACE VIEW v_equity_curve AS
SELECT
    event_time AS ts,
    SUM(COALESCE((event_payload->>'pnl')::NUMERIC, 0)) OVER (ORDER BY event_time) AS cumulative_pnl,
    (event_payload->>'pnl')::NUMERIC(10,2) AS trade_pnl,
    event_payload->>'ticker' AS ticker,
    event_payload->>'action' AS action,
    session_id
FROM event_store
WHERE event_type IN ('PositionClosed', 'PartialExited')
ORDER BY event_time;


-- ============================================================================
-- 7. v_daily_performance — daily P&L summary
-- ============================================================================
CREATE OR REPLACE VIEW v_daily_performance AS
SELECT
    event_time::DATE AS trading_date,
    COUNT(*) AS total_trades,
    COUNT(*) FILTER (WHERE (event_payload->>'pnl')::NUMERIC > 0) AS wins,
    COUNT(*) FILTER (WHERE (event_payload->>'pnl')::NUMERIC <= 0) AS losses,
    ROUND(100.0 * COUNT(*) FILTER (WHERE (event_payload->>'pnl')::NUMERIC > 0) / NULLIF(COUNT(*), 0), 1) AS win_rate,
    ROUND(SUM((event_payload->>'pnl')::NUMERIC)::NUMERIC, 2) AS total_pnl,
    ROUND(AVG((event_payload->>'pnl')::NUMERIC) FILTER (WHERE (event_payload->>'pnl')::NUMERIC > 0)::NUMERIC, 2) AS avg_win,
    ROUND(AVG((event_payload->>'pnl')::NUMERIC) FILTER (WHERE (event_payload->>'pnl')::NUMERIC <= 0)::NUMERIC, 2) AS avg_loss,
    MAX((event_payload->>'pnl')::NUMERIC)::NUMERIC(10,2) AS largest_win,
    MIN((event_payload->>'pnl')::NUMERIC)::NUMERIC(10,2) AS largest_loss
FROM event_store
WHERE event_type = 'PositionClosed'
GROUP BY event_time::DATE
ORDER BY trading_date DESC;


-- ============================================================================
-- 8. Dimension tables (static, loaded once)
-- ============================================================================

-- Strategy dimension
CREATE TABLE IF NOT EXISTS dim_strategy (
    strategy_name TEXT PRIMARY KEY,
    layer TEXT NOT NULL,         -- 'vwap', 'pro', 'pop', 'options'
    description TEXT,
    typical_win_rate NUMERIC(5,2),
    typical_rr_ratio NUMERIC(5,2),
    tier INT,                   -- 1/2/3 for pro, NULL for others
    is_credit BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE
);

-- Pre-populate with known strategies
INSERT INTO dim_strategy (strategy_name, layer, description, tier, is_credit) VALUES
    ('vwap_reclaim', 'vwap', 'VWAP reclaim with 2-bar confirmation', NULL, FALSE),
    ('momentum_ignition', 'pro', 'Volume-driven breakout', 2, FALSE),
    ('orb', 'pro', 'Opening range breakout', 2, FALSE),
    ('trend_pullback', 'pro', 'Pullback in established trend', 1, FALSE),
    ('fib_confluence', 'pro', 'Fibonacci retracement confluence', 2, FALSE),
    ('sr_flip', 'pro', 'Support/resistance role reversal', 2, FALSE),
    ('bollinger_squeeze', 'pro', 'Bollinger band squeeze breakout', 3, FALSE),
    ('liquidity_sweep', 'pro', 'Liquidity sweep and reclaim', 3, FALSE),
    ('long_call', 'options', 'Directional bullish single leg', NULL, FALSE),
    ('long_put', 'options', 'Directional bearish single leg', NULL, FALSE),
    ('bull_call_spread', 'options', 'Bullish debit vertical', NULL, FALSE),
    ('bear_put_spread', 'options', 'Bearish debit vertical', NULL, FALSE),
    ('bull_put_spread', 'options', 'Bullish credit vertical', NULL, TRUE),
    ('bear_call_spread', 'options', 'Bearish credit vertical', NULL, TRUE),
    ('iron_condor', 'options', 'Neutral credit 4-leg', NULL, TRUE),
    ('iron_butterfly', 'options', 'Neutral credit ATM 4-leg', NULL, TRUE),
    ('long_straddle', 'options', 'Volatility buy ATM', NULL, FALSE),
    ('long_strangle', 'options', 'Volatility buy OTM', NULL, FALSE),
    ('calendar_spread', 'options', 'Time decay play near/far', NULL, FALSE),
    ('diagonal_spread', 'options', 'LEAPS + short near-term', NULL, FALSE),
    ('butterfly_spread', 'options', 'Low-cost pinning play', NULL, FALSE)
ON CONFLICT (strategy_name) DO NOTHING;

-- Time dimension for session phase analysis
CREATE TABLE IF NOT EXISTS dim_time_of_day (
    minute_of_day INT PRIMARY KEY,    -- 0-1439
    hour INT NOT NULL,
    minute INT NOT NULL,
    session_phase TEXT NOT NULL,       -- 'pre_market', 'first_15min', 'morning', 'midday', 'afternoon', 'power_hour', 'post_market'
    is_market_hours BOOLEAN NOT NULL
);

-- Populate time dimension (9:30 AM - 4:00 PM ET = minutes 570-960)
INSERT INTO dim_time_of_day (minute_of_day, hour, minute, session_phase, is_market_hours)
SELECT
    m,
    m / 60,
    m % 60,
    CASE
        WHEN m < 570 THEN 'pre_market'
        WHEN m < 585 THEN 'first_15min'
        WHEN m < 690 THEN 'morning'
        WHEN m < 810 THEN 'midday'
        WHEN m < 930 THEN 'afternoon'
        WHEN m < 960 THEN 'power_hour'
        ELSE 'post_market'
    END,
    m >= 570 AND m < 960
FROM generate_series(0, 1439) AS m
ON CONFLICT (minute_of_day) DO NOTHING;


-- ============================================================================
-- Permissions
-- ============================================================================
GRANT SELECT ON v_signal_features TO trading;
GRANT SELECT ON v_trade_details TO trading;
GRANT SELECT ON v_risk_block_analysis TO trading;
GRANT SELECT ON v_fill_quality TO trading;
GRANT SELECT ON v_options_trades TO trading;
GRANT SELECT ON v_equity_curve TO trading;
GRANT SELECT ON v_daily_performance TO trading;
GRANT SELECT ON dim_strategy TO trading;
GRANT SELECT ON dim_time_of_day TO trading;
