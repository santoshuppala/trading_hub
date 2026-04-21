# V9 Trading Hub Architecture Overview

## System Identity

| Property | Value |
|----------|-------|
| Architecture | Hybrid Streaming + Polling (Architecture #3) |
| Evolved from | Architecture #5 (Bar-Driven, 60s poll) |
| Processes | 3: Core, Options, DataCollector (via Supervisor) |
| Market data | Tradier WebSocket streaming ($10/month) + REST polling |
| Order execution | Tradier Sandbox (paper) + Alpaca Paper (free) |
| Position tracking | FillLedger (lot-based, append-only) + legacy dict (shadow mode) |
| Event backbone | In-process EventBus + Redpanda (cross-process IPC) |
| Database | PostgreSQL 16 + TimescaleDB extension |
| Cost | $10/month Tradier (already paying) + $0 Alpaca free tier |

## Process Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        SUPERVISOR                                │
│              scripts/supervisor.py (process manager)            │
│                                                                  │
│  Startup cleanup:                                               │
│    __pycache__ cleared, state files validated,                  │
│    deprecated V7 files removed, .tmp cleaned                    │
│                                                                  │
│  ┌──────────────────┐ ┌──────────────┐ ┌─────────────────────┐  │
│  │ CORE             │ │ OPTIONS      │ │ DATA COLLECTOR      │  │
│  │ run_core.py      │ │ run_options. │ │ run_data_collector  │  │
│  │                  │ │ py           │ │ .py                 │  │
│  │ VWAP strategy    │ │              │ │                     │  │
│  │ Pro 11 strategies│ │ 13 options   │ │ Benzinga news       │  │
│  │ Pop scanner      │ │ strategies   │ │ StockTwits social   │  │
│  │ SmartRouter      │ │ Iron condors │ │ SEC EDGAR filings   │  │
│  │ Alpaca broker    │ │ Butterflies  │ │ UOF institutional   │  │
│  │ Tradier broker   │ │ Verticals    │ │ Polygon market      │  │
│  │ FillLedger       │ │ etc.         │ │ Yahoo earnings      │  │
│  │ BrokerRegistry   │ │              │ │ FRED macro          │  │
│  │ EquityTracker    │ │ Separate     │ │ Fear & Greed        │  │
│  │ TradierStream    │ │ Alpaca acct  │ │                     │  │
│  │                  │ │              │ │ Writes to           │  │
│  │ critical=true    │ │ critical=    │ │ alt_data_cache.json │  │
│  │ max_restarts=3   │ │ false        │ │                     │  │
│  │ exit_code_3=halt │ │ max_restart  │ │ critical=false      │  │
│  └──────────────────┘ │ =5           │ └─────────────────────┘  │
│                       └──────────────┘                          │
│                                                                  │
│  IPC: Redpanda (127.0.0.1:9092)                                │
│    th-signals: Core → Options                                   │
│    th-pop-signals: Core → Options                               │
│    th-fills: Core → Satellites                                  │
│    th-registry: position dedup                                  │
│    th-discovery: DataCollector → Core                           │
└─────────────────────────────────────────────────────────────────┘
```

## Token / Account Separation

```
TRADIER PRODUCTION ($10/month) ─── TRADIER_TOKEN
  └─ WebSocket streaming (wss://stream.tradier.com)
  └─ REST market data (api.tradier.com)
  └─ DATA ONLY — never places orders

TRADIER SANDBOX (free) ─── TRADIER_SANDBOX_TOKEN
  └─ Paper trading orders (sandbox.tradier.com)
  └─ No streaming (scope-stream not available)

ALPACA PAPER (free) ─── APCA_API_KEY_ID
  └─ Paper trading orders (paper-api.alpaca.markets)
  └─ FREE WebSocket trade_updates (fill confirmation)
  └─ Market data NOT used (IEX only on free tier)
```

## Key V9 Components (New)

| Component | File | Purpose |
|-----------|------|---------|
| FillLedger | `monitor/fill_ledger.py` | Lot-based position tracking (append-only) |
| FillLot | `monitor/fill_lot.py` | Immutable per-fill record |
| LotMatcher | `monitor/lot_matcher.py` | FIFO lot matching for P&L |
| PositionProjection | `monitor/position_projection.py` | Backward-compat dict view over lots |
| BrokerRegistry | `monitor/broker_registry.py` | Central broker collection (extensible) |
| EquityTracker | `monitor/equity_tracker.py` | P&L drift detection vs broker equity |
| TradierStreamClient | `monitor/tradier_stream.py` | WebSocket streaming for real-time quotes |
| AlpacaFillStream | `monitor/brokers.py` | FREE WebSocket for Alpaca fill confirmation |
| FailoverDataClient | `monitor/data_client.py` | Auto-switch Tradier → Alpaca on failure |
