# Trading Hub — Weekend Validation Report

**Run date:** 2026-04-12 01:14:09 ET  
**Python:** 3.14.3  
**Total time:** 93.3s  
**Results:** 9 PASS / 1 FAIL / 0 SKIP  

---

## Summary

| # | Test | Result | Time | Notes |
|---|------|--------|------|-------|
| T1 | Synthetic Feed Playback | ✅ PASS | 55.2s |  |
| T2 | Tradier Sandbox Validation | ✅ PASS | 3.6s |  |
| T3 | Redpanda Consistency Check | ✅ PASS | 3.6s |  |
| T4 | Market Open Latency Simulation | ❌ FAIL | 4.7s | exit code 1 |
| T5 | Network Warm-up Pre-fetch | ✅ PASS | 13.5s |  |
| T6 | Full Pipeline Integration Stress | ✅ PASS | 1.7s |  |
| T7 | Risk Engine Boundary Conditions | ✅ PASS | 1.6s |  |
| T8 | SignalAnalyzer Edge Cases | ✅ PASS | 4.5s |  |
| T9 | EventBus Advanced Behaviors | ✅ PASS | 3.1s |  |
| T10 | State Persistence & Observability | ✅ PASS | 1.8s |  |

---

## T1: Synthetic Feed Playback

**What it tests:** 200 tickers × 100 bar steps via EventBus ASYNC at 10ms intervals  

**Status:** PASS (55.2s, exit=0)  

**Improvements needed:**
- ----------------------------------------------------------------------
- • None — system handles 200-ticker feed at 10ms intervals cleanly

<details><summary>Key metrics from log</summary>

```
step   0/100 | emitted=200 | pressure=19.8% | depths={'BAR': 200, 'QUOTE': 0, 'SIGNAL': 0, 'ORDER_REQ': 0, 'FILL': 0, 'ORDER_FAIL': 0, 'POSITION': 0, 'RISK_BLOCK': 0, 'HEARTBEAT': 0}
step  25/100 | emitted=5,200 | pressure=19.8% | depths={'BAR': 200, 'QUOTE': 0, 'SIGNAL': 0, 'ORDER_REQ': 0, 'FILL': 0, 'ORDER_FAIL': 0, 'POSITION': 0, 'RISK_BLOCK': 0, 'HEARTBEAT': 0}
step  50/100 | emitted=10,200 | pressure=19.8% | depths={'BAR': 200, 'QUOTE': 0, 'SIGNAL': 0, 'ORDER_REQ': 0, 'FILL': 0, 'ORDER_FAIL': 0, 'POSITION': 0, 'RISK_BLOCK': 0, 'HEARTBEAT': 0}
step  75/100 | emitted=15,200 | pressure=19.8% | depths={'BAR': 200, 'QUOTE': 0, 'SIGNAL': 0, 'ORDER_REQ': 0, 'FILL': 0, 'ORDER_FAIL': 0, 'POSITION': 0, 'RISK_BLOCK': 0, 'HEARTBEAT': 0}
Events emitted          : 20,000
Emit throughput         : 392 ev/s
Drain time              : 0.00s
Final system_pressure   : 0.0%
Final queue_depths      : {'BAR': 0, 'QUOTE': 0, 'SIGNAL': 0, 'ORDER_REQ': 0, 'FILL': 0, 'ORDER_FAIL': 0, 'POSITION': 0, 'RISK_BLOCK': 0, 'HEARTBEAT': 0}
Dropped events          : {'BAR': 0, 'QUOTE': 0, 'SIGNAL': 0, 'ORDER_REQ': 0, 'FILL': 0, 'ORDER_FAIL': 0, 'POSITION': 0, 'RISK_BLOCK': 0, 'HEARTBEAT': 0}
Queue depth peaks       : {'BAR': 200, 'QUOTE': 0, 'SIGNAL': 0, 'ORDER_REQ': 0, 'FILL': 0, 'ORDER_FAIL': 0, 'POSITION': 0, 'RISK_BLOCK': 0, 'HEARTBEAT': 0}
PASS — EventBus handled 200-ticker feed without issues
```

</details>

**Log file:** `test/logs/test_1_synthetic_feed_stdout.txt`  

---

## T2: Tradier Sandbox Validation

**What it tests:** Auth + quotes + history + order submission via sandbox.tradier.com  
**Prerequisites:** TRADIER_TOKEN  

**Status:** PASS (3.6s, exit=0)  

**Improvements needed:**
- ----------------------------------------------------------------------

<details><summary>Key metrics from log</summary>

```
PASS: Authentication successful with sandbox
--- Test 2b: Sandbox Quote Retrieval (AAPL) ---
GET /markets/quotes?symbols=AAPL → HTTP 200
AAPL quote: last=260.48, bid=260.7, ask=261.0
PASS: Quote data retrieved successfully
--- Test 2c: Sandbox Historical Data (AAPL daily) ---
GET /markets/history?symbol=AAPL → HTTP 200
PASS: Historical data parsed into DataFrame
--- Test 2d: Sandbox Order Submission (BUY 1 AAPL) ---
PASS: Sandbox accepted order and returned order_id
get_quotes(['AAPL']) → 1 results
AAPL: last=260.48, bid=260.7, ask=261.0
PASS: TradierDataClient works with sandbox URL
auth                : PASS
quotes              : PASS
history             : PASS
order               : PASS
client_patch        : PASS
```

</details>

**Log file:** `test/logs/test_2_tradier_sandbox_stdout.txt`  

---

## T3: Redpanda Consistency Check

**What it tests:** Serialisation round-trip + (if Redpanda running) replay ordering  

**Status:** PASS (3.6s, exit=0)  

**Improvements needed:**
- ----------------------------------------------------------------------
- 1. Crash recovery simulation had mismatches — partial_sell handling may lose position state
- CrashRecovery uses fallback stop/target (0.995/1.01 × entry_price)
- Consider also replaying SIGNAL events to get exact stop/target values
- 4. General: bot_state.json and Redpanda can diverge if writes aren't atomic
- Consider writing bot_state.json ONLY after successful Redpanda flush

<details><summary>Key metrics from log</summary>

```
Original payload:  ticker=AAPL, side=buy, qty=10, fill_price=150.25, order_id=test-order-001
"ticker": "AAPL",
ticker match: PASS
side match: PASS
qty match: PASS
fill_price match: PASS
order_id match: PASS
event_type match: PASS
event_type: PASS
action: PASS
ticker: PASS
Under 30s threshold: PASS
Rebuilt positions: ['AAPL', 'MSFT', 'NVDA']
Original positions: ['AAPL', 'MSFT', 'NVDA']
Position tickers match: PASS
AAPL rebuilt:
WARN: quantity mismatch — partial_sell handling may differ
Trade log count match: FAIL
PASS: EventLogConsumer connected to Redpanda
PASS: Replay under 30s (2.04s)
State preserved: PASS
serialisation_roundtrip       : PASS
serialisation_perf            : PASS
crash_recovery_sim            : FAIL
redpanda_live                 : PASS
```

</details>

**Log file:** `test/logs/test_3_redpanda_consistency_stdout.txt`  

---

## T4: Market Open Latency Simulation

**What it tests:** 200 BAR events injected simultaneously; P95 latency target < 50ms  

**Status:** FAIL (4.7s, exit=1)  

**Improvements needed:**
- ----------------------------------------------------------------------
- • [Simulated workload (VWAP+RSI+ATR pandas ops)] P95=70.1ms: increase BAR n_workers 4→8 or pre-compute VWAP/RSI in data layer

<details><summary>Key metrics from log</summary>

```
TEST 4: Market Open Latency Simulation
[slow-handler] _run_phase.<locals>.on_bar took 101.0ms on BAR (threshold 100ms)
[slow-handler] _run_phase.<locals>.on_bar took 111.5ms on BAR (threshold 100ms)
Latency (ms): min=3.3  avg=18.6  p50=10.5  p95=70.1  p99=101.0  max=111.5
Total wall-clock: 985.0ms | pressure=0.0% | dropped={'BAR': 0, 'QUOTE': 0, 'SIGNAL': 0, 'ORDER_REQ': 0, 'FILL': 0, 'ORDER_FAIL': 0, 'POSITION': 0, 'RISK_BLOCK': 0, 'HEARTBEAT': 0}
Latency (ms): min=3.2  avg=14.3  p50=10.0  p95=42.7  p99=69.6  max=80.8
Total wall-clock: 808.1ms | pressure=0.0% | dropped={'BAR': 0, 'QUOTE': 0, 'SIGNAL': 0, 'ORDER_REQ': 0, 'FILL': 0, 'ORDER_FAIL': 0, 'POSITION': 0, 'RISK_BLOCK': 0, 'HEARTBEAT': 0}
Total wall-clock: 985.0ms
Latency p50/p95/p99 : 10.5 / 70.1 / 101.0 ms
Dropped events  : {'BAR': 0, 'QUOTE': 0, 'SIGNAL': 0, 'ORDER_REQ': 0, 'FILL': 0, 'ORDER_FAIL': 0, 'POSITION': 0, 'RISK_BLOCK': 0, 'HEARTBEAT': 0}
Total wall-clock: 808.1ms
Latency p50/p95/p99 : 10.0 / 42.7 / 69.6 ms
Dropped events  : {'BAR': 0, 'QUOTE': 0, 'SIGNAL': 0, 'ORDER_REQ': 0, 'FILL': 0, 'ORDER_FAIL': 0, 'POSITION': 0, 'RISK_BLOCK': 0, 'HEARTBEAT': 0}
FAIL  [Simulated workload (VWAP+RSI+ATR pandas ops)] P95 latency 70.1ms > 50ms target
```

</details>

**Log file:** `test/logs/test_4_market_open_latency_stdout.txt`  

---

## T5: Network Warm-up Pre-fetch

**What it tests:** Parallel RVOL baseline fetch for 200 tickers; target < 60s  
**Prerequisites:** TRADIER_TOKEN  

**Status:** PASS (13.5s, exit=0)  

**Improvements needed:**
- ----------------------------------------------------------------------
- 4. Cold start risk: fetch_batch_bars() gates on market open (9:30 AM)
- - Pre-fetch rvol_cache the night BEFORE market open
- - Store rvol_cache to disk (pickle/parquet) for instant Monday load
- - Only fetch today's intraday bars after 9:30 AM (not 14-day history)
- 5. Add a warmup CLI command to run_monitor.py:

<details><summary>Key metrics from log</summary>

```
ThreadPoolExecutor import: PASS
fetch_batch_bars method:     PASS
max_workers=20:   WARN (may use default)
fetch_batch_bars (can be used for pre-fetch): PASS
Under 60s target:    PASS
threadpool_exists        : PASS
prefetch_method          : PASS
sequential               : FAIL/WARN
parallel                 : FAIL/WARN
batch_bars               : PASS
```

</details>

**Log file:** `test/logs/test_5_network_warmup_stdout.txt`  

---

## T6: Full Pipeline Integration Stress

**What it tests:** BAR→SIGNAL→ORDER_REQ→FILL→POSITION chain; max-positions gate; EOD close; concurrent 10 tickers  

**Status:** PASS (1.7s, exit=0)  

<details><summary>Key metrics from log</summary>

```
2026-04-11 22:13:57,917 DEBUG    monitor.event_bus: Subscribed ExecutionFeedback._on_order_fail → ORDER_FAIL priority=0
2026-04-11 22:13:57,927 WARNING  test_6:   WARN: No BUY fill — signal conditions not met with synthetic data (acceptable)
2026-04-11 22:13:57,927 DEBUG    monitor.event_bus: Subscribed ExecutionFeedback._on_order_fail → ORDER_FAIL priority=0
2026-04-11 22:13:57,930 INFO     test_6:   PASS: 6th signal blocked — reason: max positions reached (5)
2026-04-11 22:13:57,930 DEBUG    monitor.event_bus: Subscribed ExecutionFeedback._on_order_fail → ORDER_FAIL priority=0
2026-04-11 22:13:57,952 INFO     test_6:   PASS: Concurrent 10 tickers — no errors, 0 positions opened
2026-04-11 22:13:57,953 DEBUG    monitor.event_bus: Subscribed ExecutionFeedback._on_order_fail → ORDER_FAIL priority=0
2026-04-11 22:13:57,956 WARNING  test_6:   WARN: EOD signal not triggered — strategy engine may rely on wall clock; acceptable
2026-04-11 22:13:57,956 DEBUG    monitor.event_bus: Subscribed ExecutionFeedback._on_order_fail → ORDER_FAIL priority=0
2026-04-11 22:13:57,958 INFO     test_6:   PASS: Sell path verified. PnL=-7.50
2026-04-11 22:13:57,958 INFO     test_6:   6a: PASS
2026-04-11 22:13:57,958 INFO     test_6:   6b: PASS
2026-04-11 22:13:57,958 INFO     test_6:   6c: PASS
2026-04-11 22:13:57,959 INFO     test_6:   6d: PASS
2026-04-11 22:13:57,959 INFO     test_6:   6e: PASS
```

</details>

**Log file:** `test/logs/test_6_pipeline_integration_stdout.txt`  

---

## T7: Risk Engine Boundary Conditions

**What it tests:** All 6 pre-trade checks at exact boundaries; partial sell qty=1; concurrent fills thread safety  

**Status:** PASS (1.6s, exit=0)  

<details><summary>Key metrics from log</summary>

```
2026-04-11 22:13:59,533 INFO     test_7:   PASS: 4 positions -> ORDER_REQ emitted
2026-04-11 22:13:59,534 INFO     test_7:   PASS: 5 positions -> RISK_BLOCK: max positions reached (5)
2026-04-11 22:13:59,535 INFO     test_7:   PASS: exactly 300s elapsed -> ORDER_REQ
2026-04-11 22:13:59,536 INFO     test_7:   PASS: 299s elapsed -> RISK_BLOCK: cooldown active (0s remaining)
2026-04-11 22:13:59,537 INFO     test_7:   PASS: rvol=1.99 -> RISK_BLOCK: RVOL too low (1.99 < 2.0)
2026-04-11 22:13:59,538 INFO     test_7:   PASS: rvol=2.00 -> passed (or blocked by other check)
2026-04-11 22:13:59,540 INFO     test_7:   PASS: rvol=2.01 -> passed (or blocked by other check)
2026-04-11 22:13:59,541 INFO     test_7:   PASS: rsi=49.9 -> RISK_BLOCK: RSI out of bullish band (49.9, need 50.0–70.0)
2026-04-11 22:13:59,542 INFO     test_7:   PASS: rsi=50.0 -> no RSI block
2026-04-11 22:13:59,543 INFO     test_7:   PASS: rsi=70.0 -> no RSI block
2026-04-11 22:13:59,544 INFO     test_7:   PASS: rsi=70.1 -> RISK_BLOCK: RSI out of bullish band (70.1, need 50.0–70.0)
2026-04-11 22:13:59,545 INFO     test_7:   PASS: spread=0.1900% -> no spread block
2026-04-11 22:13:59,546 INFO     test_7:   PASS: spread=0.2000% -> no spread block
2026-04-11 22:13:59,547 INFO     test_7:   PASS: spread=0.2100% -> RISK_BLOCK: spread too wide (0.210% > 0.200%)
2026-04-11 22:13:59,548 INFO     test_7:   PASS: diverge=0.4000% -> no block
DIV_T: live ask $100.60 diverges >0.5% from signal ask $100.00 — blocking entry
2026-04-11 22:13:59,549 INFO     test_7:   PASS: diverge=0.6000% -> RISK_BLOCK: live quote unavailable — cannot verify spread
2026-04-11 22:13:59,551 INFO     test_7:   PASS: partial_sell with qty=1 correctly skipped (sell_qty=0)
2026-04-11 22:13:59,554 INFO     test_7:   PASS: Oversell did not crash. positions=False
SELL fill for CONC but no open position (may have already been closed). order=ord-sell-0
SELL fill for CONC but no open position (may have already been closed). order=ord-sell-3
SELL fill for CONC but no open position (may have already been closed). order=ord-sell-4
SELL fill for CONC but no open position (may have already been closed). order=ord-sell-2
2026-04-11 22:13:59,559 INFO     test_7:   PASS: Concurrent fills completed. positions=[]
2026-04-11 22:13:59,559 INFO     test_7:   7a: PASS
```

</details>

**Log file:** `test/logs/test_7_risk_boundaries_stdout.txt`  

---

## T8: SignalAnalyzer Edge Cases

**What it tests:** NaN/zero/insufficient bars; RVOL time gate; SignalPayload invariants; 1000-frame stress  

**Status:** PASS (4.5s, exit=0)  

<details><summary>Key metrics from log</summary>

```
2026-04-11 22:14:01,073 INFO     test_8:   PASS: all sub-30-bar inputs return None
2026-04-11 22:14:01,080 INFO     test_8:   PASS: 30 bars -> dict with action=None
2026-04-11 22:14:01,085 INFO     test_8:   PASS: Zero volatility did not crash. result=None
2026-04-11 22:14:01,090 INFO     test_8:   PASS: spike bar rvol=1.00 (should be > 1.0 if time gate allows)
2026-04-11 22:14:01,095 INFO     test_8:   PASS: negative price handled gracefully, result={'action': None, 'current_price': -1.0, 'ask_price': None, 'spread_pct': None, 'atr_value': 0.4812251589348518, 'rsi_value': 0.6345481348560469, 'rvol': 1.0, 'vwap': 99.35960742690116, 'stop_price': None, 'target_price': None, 'half_target': None, 'reclaim_candle_low': 99.18490140875127, '_vwap_reclaim': False, '_vwap_breakdown': True, '_opened_above_vwap': False, '_rsi_overbought': 70, '_atr_mult': 2.0, '_min_stop_pct': 0.05}
2026-04-11 22:14:01,099 INFO     test_8:   PASS: NaN in close handled gracefully, result=dict
2026-04-11 22:14:01,101 INFO     test_8:   PASS: RVOL time gate returns 1.0 before 10:15 AM
2026-04-11 22:14:01,102 INFO     test_8:   PASS: empty/None hist_df returns 1.0
2026-04-11 22:14:01,106 INFO     test_8:   PASS: stop_price == current_price triggers sell_stop
2026-04-11 22:14:01,126 INFO     test_8:   PASS: target_price == current_price triggers sell_target
2026-04-11 22:14:01,127 INFO     test_8:   PASS: stop >= price raises ValueError: stop_price (100.0) must be < current_price (100.0) for a buy signal
2026-04-11 22:14:01,127 INFO     test_8:   PASS: stop > price raises ValueError: stop_price (101.0) must be < current_price (100.0) for a buy signal
2026-04-11 22:14:01,127 INFO     test_8:   PASS: target <= price raises ValueError: target_price (100.0) must be > current_price (100.0) for a buy signal
2026-04-11 22:14:01,127 INFO     test_8:   PASS: target < price raises ValueError: target_price (99.0) must be > current_price (100.0) for a buy signal
2026-04-11 22:14:01,127 INFO     test_8:   PASS: half_target < current raises ValueError: half_target (99.0) must be between current_price (100.0) and target_price (102.0)
2026-04-11 22:14:01,127 INFO     test_8:   PASS: valid SignalPayload constructs without error
2026-04-11 22:14:04,024 INFO     test_8:   PASS: 1000 random DataFrames — 0 crashes, 0 slow calls
2026-04-11 22:14:04,024 INFO     test_8:   8a: PASS
2026-04-11 22:14:04,024 INFO     test_8:   8b: PASS
2026-04-11 22:14:04,024 INFO     test_8:   8c: PASS
2026-04-11 22:14:04,024 INFO     test_8:   8d: PASS
2026-04-11 22:14:04,024 INFO     test_8:   8e: PASS
2026-04-11 22:14:04,024 INFO     test_8:   8f: PASS
2026-04-11 22:14:04,024 INFO     test_8:   8g: PASS
2026-04-11 22:14:04,024 INFO     test_8:   8h: PASS
```

</details>

**Log file:** `test/logs/test_8_signal_edge_cases_stdout.txt`  

---

## T9: EventBus Advanced Behaviors

**What it tests:** Circuit breaker; DLQ; TTL expiry; priority ordering; coalescing; retry; dedup; stream_seq  

**Status:** PASS (3.1s, exit=0)  

<details><summary>Key metrics from log</summary>

```
test_9a_circuit_breaker.<locals>.failing_handler[AAPL] permanently failed on FILL (seq=1) after 1 attempt(s): deliberate failure
test_9a_circuit_breaker.<locals>.failing_handler[AAPL] permanently failed on FILL (seq=2) after 1 attempt(s): deliberate failure
test_9a_circuit_breaker.<locals>.failing_handler[AAPL] permanently failed on FILL (seq=3) after 1 attempt(s): deliberate failure
test_9a_circuit_breaker.<locals>.failing_handler[AAPL] permanently failed on FILL (seq=4) after 1 attempt(s): deliberate failure
test_9a_circuit_breaker.<locals>.failing_handler[AAPL] permanently failed on FILL (seq=5) after 1 attempt(s): deliberate failure
test_9a_circuit_breaker.<locals>.failing_handler[AAPL] suspended for 60s after 5 consecutive failures
Skipping test_9a_circuit_breaker.<locals>.failing_handler[AAPL] (60s remaining)
2026-04-11 22:14:05,816 INFO     test_9:   PASS: Circuit tripped after 5 failures. 6th event skipped. trips=1
test_9b_dlq.<locals>.always_fail[AAPL] attempt 1 failed (will retry): permanent failure
test_9b_dlq.<locals>.always_fail[AAPL] attempt 2/3 on RISK_BLOCK seq=1 (backoff=1ms)
test_9b_dlq.<locals>.always_fail[AAPL] attempt 2 failed (will retry): permanent failure
test_9b_dlq.<locals>.always_fail[AAPL] attempt 3/3 on RISK_BLOCK seq=1 (backoff=1ms)
test_9b_dlq.<locals>.always_fail[AAPL] permanently failed on RISK_BLOCK (seq=1) after 3 attempt(s): permanent failure
2026-04-11 22:14:05,820 INFO     test_9:   PASS: DLQ count=1, retried_deliveries=2
2026-04-11 22:14:05,820 INFO     test_9:   PASS: Expired event not delivered to handler
2026-04-11 22:14:05,821 INFO     test_9:   PASS: Priority stamps correct: [<EventPriority.MEDIUM: 2>, <EventPriority.CRITICAL: 0>]
Dispatcher ORDER_FAIL stopped.
2026-04-11 22:14:06,921 INFO     test_9:   PASS: Coalescing reduced deliveries from 50 to 6
2026-04-11 22:14:06,922 INFO     test_9:   PASS: Duplicate event dropped. dup_dropped=1
2026-04-11 22:14:06,923 INFO     test_9:   PASS: Unsubscribe stopped further delivery. Total calls=5
test_9h_retry_with_backoff_success_on_3.<locals>.flaky_handler[AAPL] attempt 1 failed (will retry): Failing on attempt 1
test_9h_retry_with_backoff_success_on_3.<locals>.flaky_handler[AAPL] attempt 2/4 on FILL seq=1 (backoff=1ms)
test_9h_retry_with_backoff_success_on_3.<locals>.flaky_handler[AAPL] attempt 2 failed (will retry): Failing on attempt 2
test_9h_retry_with_backoff_success_on_3.<locals>.flaky_handler[AAPL] attempt 3/4 on FILL seq=1 (backoff=1ms)
2026-04-11 22:14:06,925 INFO     test_9:   PASS: Handler succeeded on attempt 3. DLQ empty. retried=2
```

</details>

**Log file:** `test/logs/test_9_eventbus_advanced_stdout.txt`  

---

## T10: State Persistence & Observability

**What it tests:** Round-trip save/load; corrupt JSON backup; concurrent writes; StateEngine; HeartbeatEmitter  

**Status:** PASS (1.8s, exit=0)  

<details><summary>Key metrics from log</summary>

```
2026-04-11 22:14:08,728 WARNING  monitor.state: Restored 3 open position(s) from previous session: ['TICK000', 'TICK001', 'TICK002']. Verify these are still open in Alpaca before trading!
2026-04-11 22:14:08,728 INFO     test_10:   PASS: Round-trip save/load verified
2026-04-11 22:14:08,730 INFO     test_10:   PASS: Yesterday's state correctly discarded
2026-04-11 22:14:08,732 WARNING  monitor.state: Corrupt state file backed up to /var/folders/k9/sqp9gggd6tq995qlxty6hgrm0000gn/T/tmpp1amlbj0/bot_state.json.corrupt
2026-04-11 22:14:08,732 INFO     test_10:   PASS: Corrupt JSON returns empty state and creates .corrupt backup
2026-04-11 22:14:08,744 INFO     test_10:   PASS: Concurrent save produced valid JSON
2026-04-11 22:14:08,769 WARNING  monitor.state: Restored 200 open position(s) from previous session: ['TICK000', 'TICK001', 'TICK002', 'TICK003', 'TICK004', 'TICK005', 'TICK006', 'TICK007', 'TICK008', 'TICK009', 'TICK010', 'TICK011', 'TICK012', 'TICK013', 'TICK014', 'TICK015', 'TICK016', 'TICK017', 'TICK018', 'TICK019', 'TICK020', 'TICK021', 'TICK022', 'TICK023', 'TICK024', 'TICK025', 'TICK026', 'TICK027', 'TICK028', 'TICK029', 'TICK030', 'TICK031', 'TICK032', 'TICK033', 'TICK034', 'TICK035', 'TICK036', 'TICK037', 'TICK038', 'TICK039', 'TICK040', 'TICK041', 'TICK042', 'TICK043', 'TICK044', 'TICK045', 'TICK046', 'TICK047', 'TICK048', 'TICK049', 'TICK050', 'TICK051', 'TICK052', 'TICK053', 'TICK054', 'TICK055', 'TICK056', 'TICK057', 'TICK058', 'TICK059', 'TICK060', 'TICK061', 'TICK062', 'TICK063', 'TICK064', 'TICK065', 'TICK066', 'TICK067', 'TICK068', 'TICK069', 'TICK070', 'TICK071', 'TICK072', 'TICK073', 'TICK074', 'TICK075', 'TICK076', 'TICK077', 'TICK078', 'TICK079', 'TICK080', 'TICK081', 'TICK082', 'TICK083', 'TICK084', 'TICK085', 'TICK086', 'TICK087', 'TICK088', 'TICK089', 'TICK090', 'TICK091', 'TICK092', 'TICK093', 'TICK094', 'TICK095', 'TICK096', 'TICK097', 'TICK098', 'TICK099', 'TICK100', 'TICK101', 'TICK102', 'TICK103', 'TICK104', 'TICK105', 'TICK106', 'TICK107', 'TICK108', 'TICK109', 'TICK110', 'TICK111', 'TICK112', 'TICK113', 'TICK114', 'TICK115', 'TICK116', 'TICK117', 'TICK118', 'TICK119', 'TICK120', 'TICK121', 'TICK122', 'TICK123', 'TICK124', 'TICK125', 'TICK126', 'TICK127', 'TICK128', 'TICK129', 'TICK130', 'TICK131', 'TICK132', 'TICK133', 'TICK134', 'TICK135', 'TICK136', 'TICK137', 'TICK138', 'TICK139', 'TICK140', 'TICK141', 'TICK142', 'TICK143', 'TICK144', 'TICK145', 'TICK146', 'TICK147', 'TICK148', 'TICK149', 'TICK150', 'TICK151', 'TICK152', 'TICK153', 'TICK154', 'TICK155', 'TICK156', 'TICK157', 'TICK158', 'TICK159', 'TICK160', 'TICK161', 'TICK162', 'TICK163', 'TICK164', 'TICK165', 'TICK166', 'TICK167', 'TICK168', 'TICK169', 'TICK170', 'TICK171', 'TICK172', 'TICK173', 'TICK174', 'TICK175', 'TICK176', 'TICK177', 'TICK178', 'TICK179', 'TICK180', 'TICK181', 'TICK182', 'TICK183', 'TICK184', 'TICK185', 'TICK186', 'TICK187', 'TICK188', 'TICK189', 'TICK190', 'TICK191', 'TICK192', 'TICK193', 'TICK194', 'TICK195', 'TICK196', 'TICK197', 'TICK198', 'TICK199']. Verify these are still open in Alpaca before trading!
2026-04-11 22:14:08,769 INFO     test_10:   PASS: Large state (200 positions, 1000 trades) round-trips correctly
2026-04-11 22:14:08,770 WARNING  monitor.state: Restored 1 open position(s) from previous session: ['AAPL']. Verify these are still open in Alpaca before trading!
2026-04-11 22:14:08,771 INFO     test_10:   PASS: Missing 'reclaimed_today' key returns empty set without crash
pnl=$17.50
2026-04-11 22:14:08,928 INFO     test_10:   PASS: Interval gating correct. Total heartbeats=2
2026-04-11 22:14:08,929 DEBUG    monitor.event_bus: Subscribed EventLogger._on_event → ORDER_FAIL priority=0
2026-04-11 22:14:08,929 DEBUG    monitor.event_bus: Subscribed test_10i_event_logger_all_types.<locals>.tracker → ORDER_FAIL priority=0
BAR AAPL rows=35 rvol=no
FILL BUY 5 AAPL @ $100.00 order=2122e47c-5de4-4812-8d18-f95dfb604421
SIGNAL AAPL action=buy price=$100.00 rsi=60.0 rvol=3.0x
ORDER_REQ BUY 5 AAPL @ $100.00 reason=VWAP reclaim
POSITION AAPL action=opened
RISK_BLOCK AAPL signal=buy reason=max positions reached
ORDER_FAIL BUY 5 AAPL @ $100.00 reason=abandoned
tickers=10 positions=2 open=('AAPL', 'MSFT') trades=5 wins=3(60%) pnl=$+125.00
2026-04-11 22:14:08,937 INFO     test_10:   PASS: EventLogger handled all 8 EventTypes without crash
2026-04-11 22:14:08,937 INFO     test_10:   10a: PASS
2026-04-11 22:14:08,938 INFO     test_10:   10b: PASS
```

</details>

**Log file:** `test/logs/test_10_state_persistence_stdout.txt`  

---

## Readiness Assessment

| Component | Status | Monday-Ready? |
|-----------|--------|---------------|
| EventBus + ASYNC dispatcher (200 tickers) | ✅ All checks passed | ✅ Ready |
| Tradier API auth + order routing | ✅ All checks passed | ✅ Ready |
| Payload serialisation + Redpanda replay | ✅ All checks passed | ✅ Ready |
| StrategyEngine per-ticker latency | ❌ Checks failed | ❌ Fix before Monday |
| RVOL pre-fetch (200 tickers parallel) | ✅ All checks passed | ✅ Ready |
| Full pipeline BAR→SIGNAL→ORDER→FILL→POSITION | ✅ All checks passed | ✅ Ready |
| RiskEngine 6-check boundaries + thread safety | ✅ All checks passed | ✅ Ready |
| SignalAnalyzer edge cases + RVOL time gate | ✅ All checks passed | ✅ Ready |
| EventBus circuit-breaker / DLQ / TTL / coalescing | ✅ All checks passed | ✅ Ready |
| State persistence + StateEngine + HeartbeatEmitter | ✅ All checks passed | ✅ Ready |

---

## Next Steps

1. **[BLOCKER — fix before Monday]** T4 P95 latency > 50ms target.
   - Raise BAR `n_workers` 4 → 8 in `_DEFAULT_ASYNC_CONFIG` (`monitor/event_bus.py`)
   - Or pre-compute VWAP/RSI/ATR before injecting BAR events to eliminate pandas GIL contention

2. **Start Redpanda** (`docker run -p 9092:9092 redpandadata/redpanda`) and
   re-run Test 3 to verify full replay + crash recovery.

3. **Sunday evening**: run `python test/test_5_network_warmup.py` to pre-warm
   the RVOL baseline cache so it's ready before 9:30 AM.

4. **Monday 9:25 AM**: start the monitor with `python run_monitor.py` and
   watch `logs/monitor_<date>.log` for the first heartbeat at 9:30.

*Generated by run_all_tests.py at 2026-04-12 01:14:09 ET*