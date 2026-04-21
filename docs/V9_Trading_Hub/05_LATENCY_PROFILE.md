# V9 Latency Profile — Measured Performance

## Entry Trade: Signal → Position Open

| Sub-stage | Before V9 | After V9 (streaming) | After V9 (REST fallback) |
|-----------|-----------|---------------------|--------------------------|
| Data staleness (market → system) | 0-73s | <0.5s | 0-12s (HOT) / 0-73s (COLD) |
| EventBus dispatch | <0.1ms | <0.1ms | <0.1ms |
| Strategy analysis | 35ms / 10s spike | 35ms / <2s spike | same |
| Ask quote (Check 5) | 300-3000ms | **0.02ms** | 300-3000ms |
| Risk engine total | 500-3000ms | **<5ms** | 500-3000ms |
| Buying power check | 3000-10000ms | **<1ms** | **<1ms** |
| Portfolio risk total | 3000-10000ms | **<5ms** | **<5ms** |
| SmartRouter routing | <1ms | <1ms | <1ms |
| Order submission | 500-2000ms | 500-2000ms | 500-2000ms |
| Fill confirm (Alpaca) | 500-5000ms | **<100ms** | 500-5000ms |
| Fill confirm (Tradier) | 500-5000ms | 500-5000ms | 500-5000ms |
| Settlement verify | 1000ms | **200-600ms** | **200-600ms** |
| Position tracking | <5ms | <10ms | <10ms |
| Post-fill processing | <10ms | <10ms | <10ms |
| | | | |
| **TOTAL (Alpaca, streaming)** | **8-25s** | **~1-3s** | — |
| **TOTAL (Alpaca, REST)** | **8-25s** | — | **~2-8s** |
| **TOTAL (Tradier, streaming)** | **8-25s** | **~1.5-8s** | — |
| **TOTAL (Tradier, REST)** | **8-25s** | — | **~2-13s** |


## Exit Trade: Price Breach → Position Closed

| Sub-stage | Before V9 | After V9 (streaming) | After V9 (polling) |
|-----------|-----------|---------------------|-------------------|
| Detection | 0-60s | **<1s** | 0-12s (HOT) |
| Exit analysis | 5-15ms | **<1ms** (QUOTE) | 5-15ms (BAR) |
| Risk (SELL passthrough) | <1ms | <1ms | <1ms |
| Order submit + fill | 1-8s | 0.6-3s (Alpaca WS) | 1-8s |
| Position close | <12ms | <12ms | <12ms |
| | | | |
| **TOTAL (streaming + Alpaca)** | **1-70s** | **~1-4s** | — |
| **TOTAL (polling + Tradier)** | **1-70s** | — | **~1-20s** |


## Full Round-Trip: Data → Buy → Monitor → Sell

| Scenario | Before V9 | After V9 |
|----------|-----------|----------|
| Best (streaming + Alpaca fills) | ~10s | **~3s** |
| Typical (mixed brokers) | ~2-3 min | **~15-30s** |
| Worst (market open + Tradier) | 5+ min | **~1-2 min** |


## Optimization Breakdown

| Fix | Component | Before | After | Speedup |
|-----|-----------|--------|-------|---------|
| L1 | Tradier WebSocket streaming | 0-60s exit detection | <1s | 60x |
| L2 | Dual-frequency polling | 60s HOT cycle | 12s | 5x |
| L3 | Background buying power | 3-10s blocking | <1ms | 10,000x |
| L4 | Detector cascade | 10s market open spike | <2s | 5x |
| L5 | Remove 1s settlement sleep | 1000ms | 200-600ms | 2x |
| L6 | Alpaca WebSocket fills | 500-5000ms | <100ms | 50x |
| Check 5 | Streaming ask price | 300-3000ms | 0.02ms | **16,000x** |


## Measured Benchmarks

```
Streaming ask price lookup:
  STREAM: AAPL bid=$270.50 ask=$270.75 — 0.020ms
  REST:   AAPL bid=$270.50 ask=$270.75 — 321.4ms
  Speedup: 16,174x

WebSocket streaming test (5 tickers, 8 seconds):
  10 events received (quotes + trades)
  Symbols: AAPL, MSFT, NVDA, SPY, TSLA
  0 reconnects
```
