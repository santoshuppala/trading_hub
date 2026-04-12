-- ============================================================
-- 006_indexes.sql  — Additional composite indexes for analytics
-- ============================================================

-- Fast "last N bars for ticker" queries (used by feature store)
CREATE INDEX IF NOT EXISTS idx_bar_ticker_ts_ohlcv
    ON trading.bar_events (ticker, ts DESC)
    INCLUDE (open, high, low, close, volume, vwap, rvol, atr, rsi);

-- Fast "fills by reason" queries (used to separate pop vs vwap vs pro)
CREATE INDEX IF NOT EXISTS idx_fill_reason
    ON trading.fill_events (reason, ts DESC);

-- Fast "signals by action" queries (BUY signals for backtesting)
CREATE INDEX IF NOT EXISTS idx_signal_action_ts
    ON trading.signal_events (action, ts DESC);

-- Fast "pro signals by tier and confidence" queries (ML training)
CREATE INDEX IF NOT EXISTS idx_pro_tier_confidence
    ON trading.pro_strategy_signal_events (tier, confidence DESC, ts DESC);

-- Partial index: only executed (non-zero confidence) pro signals
CREATE INDEX IF NOT EXISTS idx_pro_executed
    ON trading.pro_strategy_signal_events (ticker, ts DESC)
    WHERE confidence >= 0.5;
