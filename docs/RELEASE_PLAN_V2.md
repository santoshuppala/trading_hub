# Release Plan v2 — Institutional Grade Hardening

**Date**: 2026-04-14
**Status**: COMPLETED
**Branch**: `tradinghub_options`

---

## Phase 1: Options Validation & Greeks — DONE

- [ ] **Fix 1.1**: Options E2E test suite — NOT IMPLEMENTED (test writing deferred)
- [ ] **Fix 1.2**: IV history seeding for backtests — NOT IMPLEMENTED (deferred)
- [x] **Fix 1.3**: Greeks-aware position monitoring — `options/engine.py`, `options/chain.py`
  - Added `get_greeks()` to AlpacaOptionChainClient (batched snapshot, 60s cache)
  - Added `_evaluate_greeks_exit()` with 3 triggers: gamma_spike, theta_bleed_fast, delta_drift
  - Neutral strategies (iron_condor, straddle, etc.) exit on delta > 0.30

## Phase 2: Portfolio Risk Analytics — DONE

- [x] **Enhancement 2.1**: Risk metrics engine — NEW: `analytics/risk_metrics.py`
  - RiskMetricsEngine: Sharpe, Sortino, Calmar, VaR (95/99), max drawdown, Kelly, expectancy
  - rolling_sharpe(), rolling_var(), sector_exposure()
- [x] **Enhancement 2.2**: Intraday position reconciliation — `run_monitor.py`
  - Hourly at :30, compares monitor vs Alpaca positions, logs mismatches as WARNING
  - Read-only, non-fatal
- [x] **Enhancement 2.3**: Portfolio Greeks tracker — NEW: `options/portfolio_greeks.py`
  - PortfolioGreeksTracker: aggregate delta/gamma/theta/vega across options positions
  - Per-position breakdown, 60s cached, snapshot() for dashboard

## Phase 3: ML Signal Enhancement — DONE

- [x] **Enhancement 3.1**: XGBoost pop classifier — NEW: `pop_screener/ml_classifier.py`, `scripts/train_pop_classifier.py`
  - MLStrategyClassifier with transparent rules fallback when model unavailable
  - Training script queries event_store, uses TimeSeriesSplit, saves to data/pop_classifier.joblib
  - Requires: `pip install xgboost scikit-learn joblib` + sufficient training data
- [x] **Enhancement 3.2**: Unusual options flow scanner — NEW: `pop_screener/unusual_options_flow.py`
  - UnusualOptionsFlowScanner: detects high volume/OI ratios, classifies bullish/bearish/neutral
  - 5-min cache, universe rotation for rate limiting

## Phase 4: Operational Excellence — DONE

- [x] **Enhancement 4.1**: Live session dashboard — `dashboards/trading_dashboard.py`
  - Live Session Monitor section: real-time P&L, equity curve, last 10 trades, auto-refresh
- [x] **Enhancement 4.2**: Strategy attribution — `analytics/attribution.py`
  - StrategyAttributionEngine: per-strategy P&L, traces signals via correlation_id
  - from_trade_log() for real-time, daily_attribution() for DB-backed
  - Auto-generates recommendations (pause underperformers, scale winners)
- [x] **Enhancement 4.3**: Alert escalation — `monitor/alerts.py`
  - Severity levels: CRITICAL (no rate limit), WARNING (15min), INFO (1hr)
  - Backward compatible, subject tagging, rate-limit logging

## Not Implemented (Deferred to v3)

- Fix 1.1: Options E2E test suite (11 test cases)
- Fix 1.2: IV history seeding for backtests
- Dashboard integration of risk_metrics and portfolio_greeks KPI cards
- Strategy attribution in post_session_analytics.py
