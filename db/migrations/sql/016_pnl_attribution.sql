-- V10: P&L Attribution — Institutional-grade alpha/beta decomposition.
-- Per-trade attribution with realized intraday beta, cost attribution,
-- session phase, regime context, and statistical significance.

-- ── Per-trade attribution results ────────────────────────────────────
CREATE TABLE IF NOT EXISTS trading.ml_pnl_attribution (
    id                      BIGSERIAL PRIMARY KEY,
    session_date            DATE NOT NULL,
    trade_id                TEXT,
    ticker                  TEXT NOT NULL,
    strategy                TEXT,
    qty                     INT,
    entry_price             DECIMAL,
    exit_price              DECIMAL,
    realized_pnl            DECIMAL,

    -- Core attribution
    trade_return            DECIMAL,    -- raw % return of the trade
    spy_return              DECIMAL,    -- SPY % return over same holding period
    intraday_beta           DECIMAL,    -- realized intraday beta (regression)
    beta_pnl                DECIMAL,    -- $ attributed to market
    alpha_pnl               DECIMAL,    -- $ attributed to skill
    alpha_return            DECIMAL,    -- % alpha

    -- Cost attribution
    slippage_cost           DECIMAL,    -- $ lost to slippage
    gross_alpha_pnl         DECIMAL,    -- alpha before costs
    net_alpha_pnl           DECIMAL,    -- alpha after slippage

    -- Context for slicing
    session_phase           TEXT,       -- open/morning/midday/afternoon/close
    regime_trend            DECIMAL,    -- regime trend score at entry
    regime_vrp              DECIMAL,    -- regime VRP score at entry
    regime_participation    DECIMAL,    -- regime participation score at entry

    -- Significance (NULL until strategy has ≥ 20 trades)
    alpha_t_stat            DECIMAL,
    alpha_p_value           DECIMAL,

    -- Time
    entry_time              TIMESTAMPTZ,
    exit_time               TIMESTAMPTZ,
    duration_sec            INT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ml_pnl_attr_unique
    ON trading.ml_pnl_attribution (session_date, trade_id);

CREATE INDEX IF NOT EXISTS idx_ml_pnl_attr_strategy
    ON trading.ml_pnl_attribution (strategy, session_date);

CREATE INDEX IF NOT EXISTS idx_ml_pnl_attr_date
    ON trading.ml_pnl_attribution (session_date);

-- ── Intraday beta cache (one per ticker per day) ─────────────────────
CREATE TABLE IF NOT EXISTS trading.ml_intraday_beta (
    ticker          TEXT NOT NULL,
    session_date    DATE NOT NULL,
    intraday_beta   DECIMAL NOT NULL,
    r_squared       DECIMAL,
    n_bars          INT,
    fallback_used   BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ticker, session_date)
);
