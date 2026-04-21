# V9 Evolution Timeline and Roadmap

## Version History

### V1 — Proof of Concept
A single-file trading script with one strategy (VWAP reclaim) and basic Alpaca integration. No risk management, no event system, no database. Proved that the core VWAP reclaim signal had edge in paper trading.

### V2 — Strategy Expansion
Added multiple strategy detectors and a basic risk engine. Introduced position tracking via in-memory dictionaries. Still single-process, no persistence across restarts.

### V3 — Event-Driven Architecture
Introduced the EventBus for decoupled component communication. Added BAR, SIGNAL, ORDER_REQ, and FILL event types. Components subscribed to events instead of calling each other directly. First steps toward modularity.

### V4 — Multi-Process and Data Sources
Split into separate processes (Core, Options, DataCollector) with Redpanda for IPC. Added 8 alternative data sources (Benzinga, StockTwits, SEC EDGAR, etc.). Options engine with 13 multi-leg strategies on a separate Alpaca account.

### V5 — Event Sourcing and Database
Added PostgreSQL + TimescaleDB for persistent event storage. Every event written to `event_store` table. Enabled replay and audit. Fixed 7 correctness issues in EventBus (duplicate delivery, ordering, error isolation).

### V6 — Risk Stack Hardening
Built the full 20+ check risk stack: correlation groups, beta-weighted exposure, ATR sizing, sector concentration, earnings blocks, drawdown halts. Added PortfolioRiskGate and per-strategy kill switches.

### V7 — Supervisor and Production Readiness
Added Supervisor process manager with auto-restart, health monitoring, and startup cleanup. Introduced SafeStateFile for atomic JSON persistence. Deprecated old run scripts. First real paper trading sessions.

### V8 — Pro + Pop Merger
Merged Pro and Pop engines into a unified Core process (5 processes down to 3). Fixed 104 issues across 9 layers. Added SmartRouter for dual-broker execution (Alpaca + Tradier). Reconciliation imports positions from both brokers.

### V9 — Streaming, FillLedger, and Backtesting
The current version. Added Tradier WebSocket streaming for sub-second exit detection. Replaced mutable position dict with lot-based FillLedger (FIFO P&L matching). Built full backtesting framework with Tradier + DB data pipeline. Added Session Watchdog with self-healing and HotfixManager. First production run on April 20, 2026.


## System Metrics — V1 to V9

| Metric | V1 | V4 | V7 | V9 |
|--------|----|----|----|----|
| Files | ~5 | ~40 | ~120 | ~246 |
| Strategies | 1 | 14 | 24 | 24 (12 equity + 12 options) |
| Risk checks | 0 | 5 | 15 | 23 |
| Processes | 1 | 3 | 3 + supervisor | 3 + supervisor + watchdog |
| Data sources | 1 (Alpaca) | 8 | 10 | 10 |
| Brokers | 1 (Alpaca) | 1 | 2 (Alpaca + Tradier) | 2 (lot-aware routing) |
| Event types | 0 | 6 | 10 | 12 |
| Database tables | 0 | 2 | 5 | 7 |
| Position tracking | Dict | Dict | Dict + registry | FillLedger (lot-based FIFO) |
| Exit latency | Minutes | 30-60s | 0-60s | <1s (WebSocket) |
| Protection layers | 0 | 2 | 5 | 7 (risk + kill switch + dedup + reconciliation + watchdog + hotfix + fill ledger) |
| Backtesting | None | None | None | Full framework (3 data sources, dashboard) |


## Future Roadmap

### Phase 2 — Wire Rankings and Signal Quality (Target: 2 weeks)

- **Ticker ranking integration**: Connect `data_sources/ticker_ranking.py` to strategy engines. Rank tickers by composite score (news sentiment, social momentum, institutional flow, technicals) and prioritize high-ranked tickers for entry.
- **Signal confidence scoring**: Weight signals by data source agreement. A VWAP reclaim with bullish news + institutional flow > VWAP reclaim alone.
- **Expected impact**: Fewer but higher-quality trades. Target win rate improvement from 38.7% to 45%+.

### Phase 3 — Intelligence Layer (Target: 2 months)

- **Regime detection**: Classify market conditions (trending, ranging, volatile, quiet) and adjust strategy parameters dynamically. Disable gap_and_go in ranging markets, increase position size in strong trends.
- **Sector rotation**: Track sector-level momentum and rotate watchlist toward leading sectors. Avoid fading sectors.
- **Adaptive stops**: Adjust stop-loss widths based on current volatility regime. Tighter in quiet markets, wider in volatile.
- **Portfolio-level hedging**: Automatic SPY put hedges when portfolio beta exceeds threshold.
- **Expected impact**: Profit factor improvement from 1.06 to 1.3+.

### Phase 4 — ML and Scale (Target: 6 months)

- **ML signal filtering**: Train a classifier on historical trades to predict signal quality. Filter out low-probability signals before they reach risk engine.
- **Multi-account execution**: Extend BrokerRegistry to manage multiple live accounts with independent risk budgets.
- **Options integration**: Use backtest signals to trigger options strategies (e.g., bull call spread on high-confidence VWAP reclaim).
- **Cloud deployment**: Move from local Mac to cloud (AWS/GCP) for reliability and lower latency to exchanges.
- **Expected impact**: Profit factor 1.5+ with scaled capital.


## Growth Projection Disclaimer

The current profit factor of **1.06** is thin. While the system is net-positive after the gap_and_go fix, this edge is not yet robust enough to scale capital aggressively. The priority is:

1. **Improve signal quality** (Phase 2) to reach PF 1.3+ before increasing position sizes.
2. **Validate with more data** — the April 19 backtest covers 33 sessions. Need 100+ sessions across different market conditions (bull, bear, chop) to confirm edge persistence.
3. **Paper trade extensively** — run the fixed system for at least 2-4 weeks of paper trading to verify live performance matches backtest expectations.
4. **Scale gradually** — when PF consistently exceeds 1.3 in paper trading, begin with small live capital and increase only as real results confirm the edge.

Do not confuse a working system with a profitable system. The infrastructure is production-grade; the alpha generation needs more work.
