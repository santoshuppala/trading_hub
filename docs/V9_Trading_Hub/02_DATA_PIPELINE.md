# V9 Data Pipeline — Ingestion to Strategy

## Two Parallel Data Paths

```
                      MARKET EVENT (NYSE/NASDAQ)
                              │
                              ▼ ~1-5ms (exchange → Tradier)
                              
          ┌───────────────────┴───────────────────┐
          ▼                                       ▼
   PATH A: STREAMING                      PATH B: REST POLLING
   (real-time, <1s)                       (batch, 12-60s)
```

### Path A: WebSocket Streaming

| Property | Value |
|----------|-------|
| Client | `TradierStreamClient` (`monitor/tradier_stream.py`) |
| Token | `TRADIER_TOKEN` (production, $10/month) |
| Endpoint | `wss://stream.tradier.com/v1/markets/events` |
| Tickers | HOT only (open positions, dynamically updated) |
| Latency | Market event → system: **<500ms** |
| Events emitted | `QUOTE` (bid, ask, spread_pct) |
| Used for | Exit monitoring (stop/target checks), RiskEngine ask price |

**Session lifecycle:**
1. `POST /v1/markets/events/session` → sessionid (valid 5 min)
2. Connect WebSocket with session epoch counter
3. Send subscription: `{symbols: [...], sessionid, filter: [quote, trade]}`
4. Receive continuous push data (interleaved, all tickers)
5. Auto-reconnect on disconnect (5s delay, session refresh)

**Correctness guards:**
- **Session epoch**: incremented on every reconnect. Messages from stale sessions rejected.
- **Quote age**: `get_quote(ticker, max_age=5s)` returns None if >5s old → forces REST fallback
- **Gap detection**: warns if >30s between messages (half-open socket)
- **Subscription versioning**: resend payload to add/remove tickers (no reconnect needed)

**Fallback**: if streaming fails, Path B fast-poll (12s) handles exit monitoring.

### Path B: REST Polling

| Property | Value |
|----------|-------|
| Client | `TradierDataClient` (`monitor/tradier_client.py`) |
| Token | `TRADIER_TOKEN` (production) |
| Tickers | ALL 183 (full universe) |
| Workers | ThreadPoolExecutor(40) |
| API calls | 183 tickers × 2 calls = 366 per cycle |
| Hard timeout | 20s (V9 R5, was unbounded) |
| Cycle | Full scan: 60s, HOT fast-poll: 12s |
| Latency | Data fetch: 12-13s |
| Events emitted | `BAR` (DataFrame with OHLCV + RVOL) |
| Used for | Entry signals, full strategy analysis, indicator computation |

**Dual-frequency polling (V9 L2):**
```
┌── Every 60s ──────────────────────────────────┐
│ Full universe fetch (183 tickers, 12-13s)     │
│ Emit BAR for ALL tickers                      │
│ Run periodic tasks (reconcile, ranking, etc.)  │
│                                                │
│  ┌── Every 12s (inner loop) ───────────────┐  │
│  │ IF streaming NOT active:                │  │
│  │   Fetch HOT tickers only (5-15)         │  │
│  │   Emit BAR for HOT tickers              │  │
│  │ IF streaming IS active:                 │  │
│  │   Skip (streaming handles exits)        │  │
│  │ Tick heartbeat                          │  │
│  └─────────────────────────────────────────┘  │
└────────────────────────────────────────────────┘
```

**Data failover (V9 R2):**
- Primary: Tradier ($10/month)
- Secondary: Alpaca IEX (free, degraded ~10% of trades)
- Auto-switch after 3 consecutive failures
- Auto-recover to primary after 5 min
- CRITICAL alert on failover

### Bar Validation (V9 R1)

Applied in `strategy_engine.py` before any strategy logic:

| Check | Rejects |
|-------|---------|
| `close <= 0` or `!isfinite(close)` | Zero/negative/NaN price |
| `volume < 0` or `!isfinite(volume)` | Invalid volume |
| `high < low` | OHLC inconsistency |
| `close > high × 1.001` | Close exceeds high (corrupt) |
| `close == prev_close && volume == 0` | Stale duplicate bar |
| `stream_seq <= last_seq` | Out-of-order bar (sequence check) |


## EventBus

| Property | Value |
|----------|-------|
| File | `monitor/event_bus.py` |
| Dispatch | SYNC (default) or ASYNC (per-EventType workers) |
| Ordering | Per-ticker partitioning: `hash(ticker) % n_workers` |
| Guarantee | Same ticker → same worker across ALL EventTypes |
| Latency | <0.1ms (emit → first handler) |

**Event routing:**

| EventType | Priority | Workers | Backpressure | Coalesce |
|-----------|----------|---------|--------------|----------|
| FILL | CRITICAL | 1 | BLOCK | No |
| ORDER_FAIL | CRITICAL | 1 | BLOCK | No |
| ORDER_REQ | CRITICAL | 1 | BLOCK | No |
| POSITION | NORMAL | 1 | BLOCK | No |
| SIGNAL | NORMAL | 2 | BLOCK | No |
| BAR | LOW | 8 | DROP_OLDEST (500) | Yes |
| QUOTE | LOW | 2 | DROP_OLDEST (100) | Yes |

**QUOTE coalescing**: only the LATEST quote per ticker is delivered. DROP_OLDEST keeps newest, evicts oldest — correct for price data.
