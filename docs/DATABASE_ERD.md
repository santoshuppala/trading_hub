# Database ERD — Trading Hub V10

```
DATABASE: tradinghub (PostgreSQL 16 + TimescaleDB)
SIZE: 16 GB | 23.2M events | 1,627 completed trades
SCHEMA: trading (default search_path)
```

---

## Entity Relationship Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          EVENT SOURCING LAYER                            │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────────┐                                                   │
│  │   event_store    │  (23.2M rows — immutable event log)               │
│  ├──────────────────┤                                                   │
│  │ PK event_id      │                                                   │
│  │    event_sequence │                                                   │
│  │    event_type     │──────────────────────────────────────────┐       │
│  │    event_time     │                                          │       │
│  │    aggregate_id   │                                          │       │
│  │    aggregate_type │                                          │       │
│  │    event_payload  │ (JSONB — full event data)                │       │
│  │    correlation_id │──────────────────────┐                   │       │
│  │    session_id     │                      │                   │       │
│  └──────────────────┘                      │                   │       │
│                                             │                   │       │
└─────────────────────────────────────────────┼───────────────────┼───────┘
                                              │                   │
┌─────────────────────────────────────────────┼───────────────────┼───────┐
│                      EVENT TYPE PROJECTIONS  │                   │       │
├─────────────────────────────────────────────┼───────────────────┼───────┤
│                                             │                   │       │
│  ┌──────────────────┐   ┌──────────────────┐│  ┌──────────────────┐    │
│  │  signal_events   │   │  fill_events     ││  │order_req_events  │    │
│  ├──────────────────┤   ├──────────────────┤│  ├──────────────────┤    │
│  │ ts               │   │ ts               ││  │ ts               │    │
│  │ ticker           │   │ ticker           ││  │ ticker           │    │
│  │ action (BUY/SELL)│   │ side (BUY/SELL)  ││  │ side             │    │
│  │ current_price    │   │ fill_price       ││  │ qty, price       │    │
│  │ rsi, atr, rvol   │   │ qty, order_id    ││  │ reason           │    │
│  │ vwap, stop, tgt  │   │ reason           ││  │ stop, target     │    │
│  │ timeframe        │   │ timeframe        ││  │ event_id         │    │
│  │ regime_at_entry  │   │ regime_at_entry  ││  │ correlation_id   │    │
│  │ confluence_score │   │ confluence_score ││  └──────────────────┘    │
│  └──────────────────┘   └──────────────────┘│                          │
│                                             │                          │
│  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐   │
│  │ position_events  │   │risk_block_events │   │pro_strategy_     │   │
│  ├──────────────────┤   ├──────────────────┤   │signal_events     │   │
│  │ ts               │   │ ts               │   ├──────────────────┤   │
│  │ ticker           │   │ ticker           │   │ ts               │   │
│  │ action (OPEN/    │   │ reason           │   │ ticker           │   │
│  │   CLOSE/PARTIAL) │   │ signal_action    │   │ strategy_name    │   │
│  │ qty, entry_price │   │ event_id         │   │ tier, direction  │   │
│  │ stop, target     │   │ correlation_id   │   │ entry/stop/tgt   │   │
│  │ realised_pnl     │   └──────────────────┘   │ atr, rvol, rsi   │   │
│  │ event_id         │                           │ confidence       │   │
│  └──────────────────┘                           │ detector_signals │   │
│                                                 │ regime, timeframe│   │
│  ┌──────────────────┐   ┌──────────────────┐   └──────────────────┘   │
│  │heartbeat_events  │   │pop_signal_events │                           │
│  ├──────────────────┤   ├──────────────────┤                           │
│  │ ts               │   │ ts, symbol       │                           │
│  │ open_positions   │   │ strategy_type    │                           │
│  │ scan_count       │   │ entry/stop/tgt   │                           │
│  └──────────────────┘   │ pop_reason       │                           │
│                          │ timeframe/regime │                           │
│                          └──────────────────┘                           │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                         TRADE LIFECYCLE LAYER                            │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────────────┐          ┌──────────────────────┐            │
│  │   completed_trades   │          │    position_state    │            │
│  ├──────────────────────┤          ├──────────────────────┤            │
│  │ PK trade_id          │          │ PK ticker+position_id│            │
│  │    ticker             │          │    action             │            │
│  │    entry_time         │          │    qty, entry_price   │            │
│  │    exit_time          │          │    stop, target       │            │
│  │    entry_price        │          │    pnl                │            │
│  │    exit_price         │          │    last_modified_time │            │
│  │    qty, pnl           │          └──────────────────────┘            │
│  │    pnl_pct            │                                              │
│  │    duration_seconds   │                                              │
│  │    strategy           │                                              │
│  │    broker             │                                              │
│  │    lifecycle_data     │ (JSONB — scorer events, phase transitions)   │
│  └──────────────────────┘                                              │
│           │                                                             │
│           │                                                             │
│  ┌────────┴─────────────┐          ┌──────────────────────┐            │
│  │     fill_lots        │───FK────▶│    lot_matches       │            │
│  ├──────────────────────┤          ├──────────────────────┤            │
│  │ PK lot_id            │          │ PK match_id          │            │
│  │    ticker, side      │          │ FK buy_lot_id ───────│──┐         │
│  │    qty, fill_price   │          │ FK sell_lot_id ──────│──┤         │
│  │    order_id          │          │    matched_qty       │  │         │
│  │    broker, strategy  │◀─────────│    buy_price         │  │         │
│  │    fill_time         │          │    sell_price        │  │         │
│  │    init_stop/target  │          │    realized_pnl      │  │         │
│  │    init_atr          │          │    ticker            │  │         │
│  │    timeframe         │          │    matched_at        │  │         │
│  │    regime_at_entry   │          │    estimated         │  │         │
│  │    confidence, tier  │          └──────────────────────┘  │         │
│  │    confluence_score  │                                    │         │
│  └──────────────────────┘◀───────────────────────────────────┘         │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                            ML / ANALYTICS LAYER                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────────────┐   ┌──────────────────────┐                   │
│  │  ml_signal_context   │   │  ml_trade_outcomes   │                   │
│  ├──────────────────────┤   ├──────────────────────┤                   │
│  │ PK signal_id         │   │ PK trade_id          │                   │
│  │    ts, ticker        │   │    ticker, layer     │                   │
│  │    layer, action     │   │    strategy_name     │                   │
│  │    strategy_name     │   │    entry_ts/price    │                   │
│  │    current_price     │   │    exit_ts/price     │                   │
│  │    rsi, atr, rvol    │   │    realized_pnl      │                   │
│  │    vwap, vwap_dist   │   │    entry_confidence  │                   │
│  │    confidence        │   │    entry_rsi/atr/rvol│                   │
│  │    iv_estimate       │   │    exit_reason       │                   │
│  │    bar_return        │   │    time_in_position  │                   │
│  │    volume_rank_20    │   │    max_favorable_exc │                   │
│  │    was_executed      │   │    max_adverse_exc   │                   │
│  │    was_rejected      │   │    concurrent_pos    │                   │
│  │    rejection_reason  │   │    options_* fields  │                   │
│  │    outcome_pnl       │   └──────────────────────┘                   │
│  │    n_detectors_fired │                                              │
│  │    detector_agreement│                                              │
│  └──────────────────────┘                                              │
│                                                                         │
│  ┌──────────────────────┐   ┌──────────────────────┐                   │
│  │  ml_rejection_log   │   │  ml_daily_regime     │                   │
│  ├──────────────────────┤   ├──────────────────────┤                   │
│  │ PK rejection_id      │   │ PK regime_date       │                   │
│  │    ts, ticker        │   │    spy_return_pct    │                   │
│  │    layer             │   │    vix_close         │                   │
│  │    action_blocked    │   │    avg_rvol_universe │                   │
│  │    reason_category   │   │    regime_label      │                   │
│  │    reason_detail     │   │    volatility_regime │                   │
│  │    signal_rsi/atr    │   │    total_signals     │                   │
│  │    signal_confidence │   │    total_pnl         │                   │
│  │    positions_held    │   │    win_rate          │                   │
│  │    would_have_won    │   └──────────────────────┘                   │
│  │    counterfactual_pnl│                                              │
│  └──────────────────────┘                                              │
│                                                                         │
│  ┌──────────────────────┐   ┌──────────────────────┐                   │
│  │ ml_execution_quality │   │   ml_bar_features   │                   │
│  ├──────────────────────┤   ├──────────────────────┤                   │
│  │ PK exec_id           │   │    ts, ticker       │                   │
│  │    ticker, ts        │   │    OHLCV            │                   │
│  │    qty_ordered       │   │    vwap, rvol, atr  │                   │
│  │    order_price       │   │    rsi              │                   │
│  │    fill_price        │   │    bar_return       │                   │
│  │    slippage_bps      │   │    bar_range_pct    │                   │
│  │    latency_ms        │   │    close_position   │                   │
│  │    bid/ask_at_order  │   │    vol_ratio_20     │                   │
│  └──────────────────────┘   └──────────────────────┘                   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                           MARKET DATA LAYER                             │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────────────┐   ┌──────────────────────┐                   │
│  │    market_bars       │   │     bar_events       │                   │
│  ├──────────────────────┤   ├──────────────────────┤                   │
│  │ PK ticker+bar_time+  │   │    ts, ticker       │                   │
│  │    bar_interval      │   │    OHLCV            │                   │
│  │    open, high, low   │   │    vwap, rvol, atr  │                   │
│  │    close, volume     │   │    rsi              │                   │
│  │    vwap, rvol        │   │    event_id         │                   │
│  │    session_id        │   └──────────────────────┘                   │
│  └──────────────────────┘                                              │
│                                                                         │
│  ┌──────────────────────┐   ┌──────────────────────┐                   │
│  │      bar_5m          │   │       bar_1h         │                   │
│  ├──────────────────────┤   ├──────────────────────┤                   │
│  │  (TimescaleDB cont.  │   │  (TimescaleDB cont.  │                   │
│  │   aggregate of       │   │   aggregate of       │                   │
│  │   bar_events)        │   │   bar_events)        │                   │
│  │    bucket, ticker    │   │    bucket, ticker    │                   │
│  │    OHLCV + indicators│   │    OHLCV + indicators│                   │
│  └──────────────────────┘   └──────────────────────┘                   │
│                                                                         │
│  ┌──────────────────────┐   ┌──────────────────────┐                   │
│  │    iv_history        │   │data_source_snapshots │                   │
│  ├──────────────────────┤   ├──────────────────────┤                   │
│  │ PK ts+ticker         │   │ PK id               │                   │
│  │    iv, iv_rank       │   │    source_name      │                   │
│  │    iv_percentile     │   │    ticker           │                   │
│  └──────────────────────┘   │    data_payload     │ (JSONB)           │
│                              └──────────────────────┘                   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                         OPERATIONS / MONITORING                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐      │
│  │  session_log     │  │system_health_log │  │  preflight_log   │      │
│  ├──────────────────┤  ├──────────────────┤  ├──────────────────┤      │
│  │ PK session_id    │  │ ts               │  │ ts               │      │
│  │    start/stop_ts │  │ tickers_scanned  │  │ check_name       │      │
│  │    mode          │  │ open_positions   │  │ status (OK/FAIL) │      │
│  │    broker        │  │ daily_pnl        │  │ response_ms      │      │
│  │    tickers[]     │  │ system_pressure  │  │ detail           │      │
│  │    events_emitted│  │ db_rows_written  │  └──────────────────┘      │
│  │    exit_reason   │  │ memory_mb        │                            │
│  │    metadata      │  │ queue_depths     │  ┌──────────────────┐      │
│  └──────────────────┘  │ handler_avg_ms   │  │ kill_switch_log  │      │
│                         └──────────────────┘  ├──────────────────┤      │
│                                               │ ts               │      │
│  ┌──────────────────┐  ┌──────────────────┐  │ daily_pnl        │      │
│  │discovered_tickers│  │schema_migrations │  │ threshold        │      │
│  ├──────────────────┤  ├──────────────────┤  │ positions_closed │      │
│  │ PK id            │  │ PK filename      │  └──────────────────┘      │
│  │    ticker        │  │    applied_at    │                            │
│  │    source        │  │    checksum      │                            │
│  │    discovery_data│  └──────────────────┘                            │
│  └──────────────────┘                                                   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                        DIMENSION / REFERENCE                             │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐    │
│  │  dim_strategy    │   │ dim_time_of_day  │   │  daily_metrics   │    │
│  ├──────────────────┤   ├──────────────────┤   ├──────────────────┤    │
│  │ PK strategy_name │   │ PK minute_of_day │   │ PK metric_id     │    │
│  │    layer         │   │    hour, minute  │   │    metric_date   │    │
│  │    description   │   │    session_phase │   │    total_trades  │    │
│  │    typical_win   │   │    is_market_hrs │   │    win_rate      │    │
│  │    typical_rr    │   └──────────────────┘   │    total_pnl     │    │
│  │    tier          │                           │    sharpe_ratio  │    │
│  │    is_credit     │                           │    max_drawdown  │    │
│  │    is_active     │                           └──────────────────┘    │
│  └──────────────────┘                                                   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                      MATERIALIZED VIEWS / ANALYTICS                      │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  v_daily_performance      — daily P&L, win rate, trade counts           │
│  v_daily_lot_pnl          — FIFO lot-level P&L by day                   │
│  activity_feed            — human-readable event stream                  │
│  health_timeline          — system health over time                      │
│  signal_pipeline_efficiency — signal→order→fill conversion rates         │
│  strategy_signal_rates    — signal volume per strategy per day           │
│  daily_trade_summary      — ticker-level daily aggregates               │
│  daily_strategy_scorecard — per-strategy performance metrics             │
│  rejection_analysis       — why signals get blocked                      │
│  slippage_analysis        — execution quality per strategy               │
│  pro_strategy_performance — Pro setup hit rates                          │
│  signal_analysis          — combined signal+fill+outcome                 │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Relationships

```
FOREIGN KEYS:
  lot_matches.buy_lot_id  → fill_lots.lot_id
  lot_matches.sell_lot_id → fill_lots.lot_id

LOGICAL (via correlation_id / event_id):
  signal_events.event_id → event_store.event_id
  fill_events.correlation_id → signal_events.event_id
  order_req_events.correlation_id → signal_events.event_id
  risk_block_events.correlation_id → signal_events.event_id
  position_events.correlation_id → fill_events.event_id
  completed_trades.opened_event_id → position_events.event_id (OPENED)
  completed_trades.closed_event_id → position_events.event_id (CLOSED)
```

---

## Table Categories

| Category | Tables | Purpose |
|----------|--------|---------|
| **Event Source** | event_store | Immutable event log (23M rows, full history) |
| **Event Projections** | signal_events, fill_events, order_req_events, position_events, risk_block_events, pro_strategy_signal_events, pop_signal_events, heartbeat_events | Fast-query tables per event type |
| **Trade Lifecycle** | completed_trades, position_state, fill_lots, lot_matches | Trade history + FIFO P&L matching |
| **ML Features** | ml_signal_context, ml_trade_outcomes, ml_rejection_log, ml_daily_regime, ml_execution_quality, ml_bar_features | Training data for alpha models |
| **Market Data** | market_bars, bar_events, bar_5m, bar_1h, iv_history | OHLCV + indicators at multiple timeframes |
| **Alt Data** | data_source_snapshots, discovered_tickers | External data (Benzinga, Finviz, etc.) |
| **Operations** | session_log, system_health_log, preflight_log, kill_switch_log, schema_migrations | System monitoring + operations |
| **Dimensions** | dim_strategy, dim_time_of_day, daily_metrics | Reference data + daily aggregates |
| **Views** | v_daily_performance, v_daily_lot_pnl, activity_feed, health_timeline, + 8 more | Pre-computed analytics |

---

## Data Volumes (April 28, 2026)

| Table | Today | All Time |
|-------|-------|----------|
| event_store | 2,691,936 | 23,229,067 |
| completed_trades | 58 | 1,627 |
| ml_signal_context | 27 → (to be backfilled) | 35,776 |
| ml_trade_outcomes | 165 | 1,959 |
| ml_rejection_log | 177 | 1,561 |
| market_bars | 0 → (persisting on shutdown now) | ~50K/day expected |

---

## Key Indexes

| Table | Index | Purpose |
|-------|-------|---------|
| event_store | (event_type, event_time) | Filter by type + time range |
| event_store | (correlation_id) | Trace signal → fill → position chain |
| fill_lots | (ticker, side, fill_time) | FIFO matching queries |
| lot_matches | (ticker, matched_at) | Daily P&L rollup |
| market_bars | (ticker, bar_time, bar_interval) | PK + time-series queries |
| ml_signal_context | (ticker, ts) | Per-ticker feature lookup |
| ml_trade_outcomes | (strategy_name, exit_reason) | Strategy performance analysis |
| pro_strategy_signal_events | (strategy_name, ts) | Pro signal analytics |
| completed_trades | (exit_time) | Daily trade queries |
