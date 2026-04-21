# Trading Hub V6.0 — Design Documentation

**Source of truth:** Codebase as of 2026-04-15
**Principle:** Every design choice, threshold, and data flow traced strictly from code. No assumptions.

---

## Documents

| # | Document | Scope | Files Covered |
|---|----------|-------|---------------|
| 0 | [V6 Architecture & Design](V6_ARCHITECTURE_AND_DESIGN.md) | System-wide architecture: 5 processes, event flows, IPC, persistence, file structure | All |
| 1 | [Core Engine Design](01_CORE_ENGINE_DESIGN.md) | VWAP strategy, risk gating, position lifecycle, state persistence, crash recovery | run_core.py, monitor.py, strategy_engine.py, risk_engine.py, position_manager.py, execution_feedback.py, state_engine.py, state.py |
| 2 | [Event System Design](02_EVENT_SYSTEM_DESIGN.md) | EventBus (priority, partitioning, circuit breakers), 14 payload types, IPC (Redpanda), shared cache, durable event log | event_bus.py, events.py, ipc.py, shared_cache.py, event_log.py |
| 3 | [Broker Architecture Design](03_BROKER_ARCHITECTURE_DESIGN.md) | SmartRouter (round-robin, failover), AlpacaBroker (bracket orders), TradierBroker (standalone stops), PortfolioRiskGate, AlpacaOptionsBroker (multi-leg) | smart_router.py, brokers.py, tradier_broker.py, portfolio_risk.py, options/broker.py |
| 4 | [Pro Engine Design](04_PRO_ENGINE_DESIGN.md) | 11 detectors, strategy classifier, 11 strategies (3 tiers), S/R-based stop generation, RiskAdapter (9 checks) | pro_setups/engine.py, detectors/*, strategies/*, classifiers/*, risk/risk_adapter.py |
| 5 | [Pop Engine Design](05_POP_ENGINE_DESIGN.md) | Screening pipeline (6 rules), feature engineering, classifier, 7+1 strategies, Benzinga/StockTwits integration, ticker discovery | pop_strategy_engine.py, pop_screener/*.py, pop_screener/strategies/* |
| 6 | [Options Engine Design](06_OPTIONS_ENGINE_DESIGN.md) | 13 option strategies, IV rank computation, 3 entry paths, exit management (6 conditions), TOCTOU risk prevention, portfolio Greeks | options/engine.py, options/broker.py, options/chain.py, options/selector.py, options/iv_tracker.py, options/risk.py, options/strategies/* |
| 7 | [Persistence & Lifecycle Design](07_PERSISTENCE_AND_LIFECYCLE_DESIGN.md) | DB layer (async batching, event sourcing, 9 projections), lifecycle management (state persistence, kill switch, heartbeat, EOD reports, reconciliation) | db/*.py, lifecycle/*.py, lifecycle/adapters/*.py |
| 8 | [Infrastructure & Operations Design](08_INFRASTRUCTURE_AND_OPERATIONS_DESIGN.md) | Supervisor, watchdog, crash analyzer, alerts, observability, RVOL engine, position registry, data clients, configuration | scripts/supervisor.py, scripts/watchdog.py, monitor/alerts.py, monitor/observability.py, monitor/rvol.py, config.py |

---

## Quick Reference

### Process Architecture
```
Supervisor (PID 1)
  |-- Core (VWAP + broker execution + position management)
  |-- Pro (11 technical setups, 3 tiers)
  |-- Pop (news + social momentum screening)
  `-- Options (13 multi-leg strategies, own Alpaca account)
```

### Event Chain (Critical Path)
```
BAR --> SIGNAL --> ORDER_REQ --> FILL --> POSITION
         |            |           |          |
    StrategyEngine  RiskEngine  Broker  PositionManager
                       |
                  SmartRouter (broker selection)
                       |
                  PortfolioRiskGate (aggregate limits)
```

### Key Design Principles
1. **Broker is always source of truth** — local state reconciled to match
2. **Sell never blocked** — exits bypass all risk checks
3. **Atomic state persistence** — tmpfile + os.replace on every mutation
4. **Fail-fast on untracked execution** — ORDER_REQ dropped if Redpanda hook fails
5. **Single-writer shared state** — PositionManager owns all mutable position data
6. **IV rank over raw IV** — strategy selection uses percentile rank with confidence blending
7. **Structure-aware stops** — S/R pivots with touch counting, not arbitrary ATR distance
8. **Process isolation** — engines communicate only via Redpanda IPC and shared cache

---

*9 documents, ~195KB, covering 100+ source files. Generated 2026-04-15.*
