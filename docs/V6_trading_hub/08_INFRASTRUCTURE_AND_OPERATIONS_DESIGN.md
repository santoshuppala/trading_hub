# Infrastructure and Operations -- Design Document

**Source of truth:** Code as of 2026-04-15
**Files:** `scripts/supervisor.py`, `scripts/watchdog.py`, `scripts/crash_analyzer.py`, `monitor/alerts.py`, `monitor/observability.py`, `monitor/rvol.py`, `monitor/distributed_registry.py`, `monitor/signals.py`, `monitor/data_client.py`, `monitor/tradier_client.py`, `monitor/screener.py`, `config.py`

---

## 1. PURPOSE

This document covers the operational infrastructure that keeps Trading Hub V6.0 running reliably in production: process management, crash recovery, alerting, observability, market data plumbing, and configuration. These components sit beneath the strategy layer and above the OS, forming the platform that strategy engines depend on but never interact with directly.

---

## 2. SUPERVISOR (`scripts/supervisor.py`)

### 2.1 Process Isolation Model

The Supervisor replaces the monolith (`run_monitor.py`) with four independent child processes:

| Process | Script | Critical | Max Restarts | Restart Delay |
|---------|--------|----------|-------------|---------------|
| `core` | `run_core.py` | Yes | 3 | 10s |
| `pro` | `run_pro.py` | No | 5 | 15s |
| `pop` | `run_pop.py` | No | 5 | 15s |
| `options` | `run_options.py` | No | 5 | 20s |

**Design rationale:** A crash in the Options engine (Python-level exception, OOM, etc.) must never stop VWAP equity trading. Process isolation via `subprocess.Popen` with `preexec_fn=os.setsid` gives each engine its own process group, so `SIGTERM`/`SIGKILL` can cleanly target one engine without signal leakage.

### 2.2 Startup Sequence

1. Load `.env` into `os.environ` (only sets vars not already present -- existing env takes precedence).
2. Set `SUPERVISED_MODE=1` so child processes skip lock-file creation.
3. Start `core` first. Wait 5 seconds for shared cache and registry initialization.
4. Start `pro`, `pop`, `options` in sequence. Failures here are non-fatal -- the main loop will retry.

**Design choice:** Core starts first because it initializes `CacheWriter` and `DistributedPositionRegistry`. Satellites that start before cache is ready would emit signals against stale data.

### 2.3 Monitoring Loop

Runs every 30 seconds. For each `ProcessManager`:

1. `check()` polls `process.poll()`. If the process has been running >60 seconds, the restart counter resets to 0 (stability heuristic).
2. If status is `crashed` or `stopped`, `restart()` is called:
   - CrashAnalyzer diagnoses the traceback from `logs/YYYYMMDD/<engine>.log`
   - If a fix is found, it is applied (with syntax validation via `py_compile`)
   - The restart counter increments, a cooldown delay is applied, then `start()` is called
3. If a critical process (`core`) exhausts max restarts, all processes are stopped and a CRITICAL alert fires.
4. Non-critical processes that exhaust restarts are set to `disabled` -- other engines continue normally.

### 2.4 EOD Shutdown

At 4:00 PM ET (`now.hour >= 16`), the monitoring loop breaks. The shutdown sequence stops processes in reverse order (satellites first, core last) using `SIGTERM` to the process group, with a 10-second grace period before `SIGKILL`.

### 2.5 Stale Lock File Handling

Before starting `core`, the supervisor removes `.monitor.lock` if it exists. This prevents the "already running" error that occurs when a previous session crashed without cleanup.

### 2.6 Status Reporting

After every monitoring cycle, `write_status()` serializes all `ProcessManager` state to `data/supervisor_status.json`:

```json
{
  "supervisor": {"updated_at": "...", "mode": "process_isolation"},
  "processes": {
    "core": {"status": "running", "pid": 12345, "restart_count": 0, ...},
    ...
  }
}
```

---

## 3. WATCHDOG (`scripts/watchdog.py`)

### 3.1 Role

The Watchdog is the monolith-mode equivalent of the Supervisor. It wraps `run_monitor.py` in a crash-detect-fix-restart loop. In production the Supervisor is preferred; the Watchdog remains as a fallback for single-process deployments.

### 3.2 Flow

```
cron (6 AM) --> watchdog.py --> run_monitor.py --> [crash?]
                                                    |
                                               CrashAnalyzer
                                                    |
                                              [fix applicable?]
                                                 /        \
                                              yes          no
                                               |            |
                                          git stash     crash_report.json
                                          apply fix     send alert
                                          syntax check  retry or give up
                                          restart
```

### 3.3 Guardrails

- **Max 5 retries** per session (`MAX_RETRIES = 5`), with a 10-second cooldown between attempts.
- **8-hour timeout** on the subprocess (`timeout=8*3600`) -- covers the 6 AM to 4 PM trading window with buffer.
- **Git stash before fix**: `git stash push -m watchdog-pre-fix-<timestamp>` ensures every fix is rollback-safe.
- **Syntax validation**: after applying a fix, `py_compile.compile(path, doraise=True)` is run. If it fails, `git checkout -- <file>` reverts the change.
- **Forbidden patterns**: fixes are checked against `FORBIDDEN_PATTERNS` before application (see CrashAnalyzer below).
- **Lock-file awareness**: if the crash traceback mentions `.monitor.lock`, the watchdog checks whether the PID in the lock file is actually alive (and is a Python process). Stale locks are removed; live locks cause a clean exit.

---

## 4. CRASH ANALYZER (`scripts/crash_analyzer.py`)

### 4.1 Architecture

The CrashAnalyzer is a stateless pattern matcher. It receives a traceback string, parses it into structured fields, and attempts to match against a chain of fixers. Each fixer returns a `CrashDiagnosis` dataclass with the fix payload (old string, new string, target file).

### 4.2 Traceback Parsing

- **Error line**: scans from the bottom of the traceback for a line containing `:` that does not start with `File`. Splits on first `:` to get `(error_type, error_message)`.
- **Crash location**: regex `File "([^"]+)", line (\d+), in (\w+)` extracts all frames. The **last match** is used (innermost frame = actual crash site). Paths are made relative to `PROJECT_ROOT`.

### 4.3 Pattern-Based Fixers

Fixers are tried in order. The first one that returns `fix_applicable=True` wins.

| Fixer | Error Pattern | Fix Strategy | Confidence |
|-------|--------------|--------------|------------|
| `_fix_unexpected_kwarg` | `TypeError: X() got an unexpected keyword argument 'Y'` | Comment out the offending `kwarg=` line | 0.85 |
| `_fix_attribute_error` | `AttributeError: 'X' object has no attribute 'Y'` | Replace `obj.attr` with `getattr(obj, 'attr', None)` | 0.70 |
| `_fix_import_error` | `ImportError` / `ModuleNotFoundError` | Wrap import in `try/except ImportError: pass` | 0.60 |
| `_fix_unbound_local` | `UnboundLocalError` | Diagnosis only, no auto-fix | 0.00 |
| `_fix_validation_error` | `ValueError` in `__post_init__` | Diagnosis only, no auto-fix | 0.00 |
| `_fix_type_error_args` | `TypeError: X() takes N args but M given` | Diagnosis only, no auto-fix | 0.00 |

### 4.4 Safety Constraints

- **Allowed files whitelist** (`ALLOWED_FILES`): ~36 files. The analyzer will not suggest fixes outside this set.
- **Forbidden files**: `.env`, `db/schema.sql`, `db/migrations/`, `db/connection.py`.
- **Forbidden patterns** in fix output: `DATABASE_URL=`, `APCA_API.*=`, `password=`, `secret=`, `CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`.
- **Session fix limit**: 5 fixes per `CrashAnalyzer` instance. After that, all diagnoses return `fix_applicable=False`.
- **Crash reports**: when no fix is applicable, `write_crash_report()` writes the full `CrashDiagnosis` as JSON to `logs/crash_report_<timestamp>.json`.

---

## 5. ALERTS (`monitor/alerts.py`)

### 5.1 Async Queue Architecture

`send_alert()` is the only public API. It enqueues a `(alert_email, tagged_message)` tuple and returns immediately -- SMTP delivery never blocks the trading loop.

```
Trading thread                Background daemon thread
     |                              |
  send_alert()                 _alert_worker()
     |                              |
  rate-limit check              queue.get() [blocking]
     |                              |
  queue.put_nowait()           _get_smtp_connection()
     |                              |
  [returns immediately]        _deliver_with_connection()
                                    |
                                server.quit()
```

### 5.2 Rate Limiting

Rate limiting is per unique message, keyed by the MD5 hash of the message body (truncated to 16 hex chars):

| Severity | Rate Limit | Subject Prefix |
|----------|-----------|---------------|
| `CRITICAL` | None -- always sent | `[CRITICAL]` |
| `WARNING` | 15 minutes per unique message | `[WARNING]` |
| `INFO` | 1 hour per unique message | (none) |

### 5.3 SMTP Delivery

- **Fresh connection per send**: Yahoo drops idle SMTP connections, so the worker creates a new `smtplib.SMTP` connection for each message, then calls `server.quit()` immediately after.
- **Auth**: Yahoo app password via `ALERT_EMAIL_USER` / `ALERT_EMAIL_PASS` env vars. Server defaults to `smtp.mail.yahoo.com:587` with STARTTLS.
- **Backoff on failure**: exponential backoff at `2^n` seconds (capped at 16s). A global `_consecutive_failures` counter tracks sequential failures; `_backoff_until` prevents rapid retry.
- **Queue capacity**: `maxsize=200`. If the queue is full, alerts are dropped and logged (`queue.Full` exception caught by `put_nowait()`).
- **Fallback**: if `alert_email` is `None` or empty, the message is written to the log instead.

---

## 6. OBSERVABILITY (`monitor/observability.py`)

### 6.1 EventLogger

A passive subscriber that wires itself to every `EventType` on construction:

```python
for et in EventType:
    bus.subscribe(et, self._on_event)
```

Log levels by event type:

| EventType | Level | Format |
|-----------|-------|--------|
| `BAR` | DEBUG | `[<eid>] BAR <ticker> rows=<n> rvol=yes/no` |
| `SIGNAL` | INFO | `[<eid>] SIGNAL <ticker> action=<a> price=$<p> rsi=<r> rvol=<v>x` |
| `ORDER_REQ` | INFO | `[<eid>] ORDER_REQ <side> <qty> <ticker> @ $<price> reason=<r>` |
| `FILL` | INFO | `[<eid>] FILL <side> <qty> <ticker> @ $<price> order=<id>` |
| `ORDER_FAIL` | WARNING | `[<eid>] ORDER_FAIL <side> <qty> <ticker> @ $<price> reason=<r>` |
| `POSITION` | INFO | `[<eid>] POSITION <ticker> action=<a> pnl=$<pnl>` |
| `RISK_BLOCK` | INFO | `[<eid>] RISK_BLOCK <ticker> signal=<a> reason=<r>` |
| `HEARTBEAT` | INFO | `[HEARTBEAT] tickers=<n> positions=<n> open=<list> trades=<n> wins=<n>(<rate>) pnl=$<pnl>` |

Event IDs are truncated to 8 characters for log readability while preserving correlation capability.

### 6.2 HeartbeatEmitter

- Runs on the **caller's thread** (the monitor run loop calls `tick()` each iteration).
- `tick()` uses `time.monotonic()` to check if the 60-second interval has elapsed. If not, it returns immediately (no-op).
- `_emit()` reads `StateEngine.snapshot()` and emits a `HEARTBEAT` event with `HeartbeatPayload(n_tickers, n_positions, open_tickers, n_trades, n_wins, total_pnl)`.
- `set_n_tickers(n)` is called by the monitor run loop when the watchlist size changes.

### 6.3 EODSummary

Static method `send()` generates a formatted trade summary:

- Per-trade detail: ticker, quantity, entry/exit price, PnL, reason (with checkmark/cross symbols).
- Aggregate: total trades, wins, win rate, total PnL.
- Output: logged at INFO and sent via `send_alert()`.

---

## 7. RVOL ENGINE (`monitor/rvol.py`)

### 7.1 Design

The RVOLEngine replaces two legacy implementations (`signals.py:get_rvol()` and `_compute.py:compute_rvol()`) with a single canonical source used by all strategy engines.

### 7.2 Volume Profile Construction

Each ticker gets a `_VolumeProfile` containing cumulative volume observations bucketed into 5-minute intervals (78 buckets per day) over a 20-day rolling window.

**Seeding** happens once at session start via `seed_profiles(tickers, data_client)`:

- If minute-level bars are available: precise bucketed cumulative volume per historical day.
- If only daily bars: an empirical U-shaped intraday volume curve distributes daily volume across buckets. The curve models:
  - Opening auction (9:30-9:45): 4.0x intensity
  - Mid-morning (10:00-10:30): 1.5x
  - Lunch doldrums (11:30-13:00): 0.6x
  - Power hour (15:00-15:30): 2.0x
  - Closing auction (15:30-16:00): 3.0x

**Weekday weighting**: when computing the expected cumulative volume at a given bucket, observations from the same weekday are counted twice in the median calculation. This captures day-of-week volume patterns (e.g., Mondays tend to be lighter).

Minimum 5 historical days required before a profile is usable (`MIN_PROFILE_DAYS`).

### 7.3 Real-Time Update

`update(ticker, bar_volume, bar_timestamp)` is called on every BAR event and returns:

```python
RVOLResult(
    rvol=2.35,              # raw: cumulative_actual / cumulative_expected
    rvol_smooth=2.12,       # EMA-smoothed (span=5, alpha=2/6)
    rvol_percentile=87.5,   # where today's RVOL sits vs last 20 days (0-100)
    acceleration=1.8,       # recent volume slope / expected slope
    quality='sustained',    # classification (see below)
)
```

**Clamping**: all RVOL values are clamped to `[0.01, 10.0]`. Expected volume below 1,000 shares returns `RVOLResult.neutral()`.

### 7.4 Quality Classification

| Quality | Condition | Interpretation |
|---------|-----------|---------------|
| `block_trade` | `raw > 5.0 AND smooth < 2.0` | Single large print, not sustained flow |
| `spike` | `raw > 3 * smooth` | One-bar volume burst |
| `sustained` | `10+ of last 15 bars > 2.0 AND smooth > 2.0` | Institutional flow |
| `normal` | Everything else | Typical volume |

### 7.5 Acceleration

Compares the sum of the last 3 bars (`ACCELERATION_WINDOW`) to the prior 3 bars, then divides by the expected slope (change in expected cumulative volume between those buckets). `acceleration > 1.0` means volume is growing faster than the profile predicts.

### 7.6 Thread Safety

All profile mutations occur under a `threading.Lock`. The `_state` dict uses `defaultdict(_TickerState)` for lock-free reads of per-ticker mutable state (safe under CPython GIL).

### 7.7 Global Singleton

`init_global_rvol_engine()` creates and stores a module-level `_global_rvol_engine` instance. `SignalAnalyzer` checks for this instance first and falls back to the legacy `get_rvol()` function if it is `None`.

---

## 8. DISTRIBUTED POSITION REGISTRY (`monitor/distributed_registry.py`)

### 8.1 Problem

In process isolation mode, four independent processes need to know which tickers are already held by other engines. An in-memory registry only works within a single process.

### 8.2 Solution

File-based JSON registry with `fcntl.flock` exclusive locking:

- **State file**: `data/position_registry.json` -- format `{"positions": {"AAPL": "core", "TSLA": "pop"}, "updated_at": "..."}`
- **Lock file**: `data/position_registry.json.lock` -- `fcntl.flock(fd, LOCK_EX)` for exclusive access during read-modify-write.

### 8.3 Operations

| Method | Behavior |
|--------|----------|
| `try_acquire(ticker, layer)` | Atomic: read JSON, check not held by another layer, check `count < global_max`, write ticker->layer, return `True`/`False` |
| `release(ticker)` | Remove ticker from positions dict |
| `held_by(ticker)` | Return the layer name holding this ticker, or `None` |
| `count()` | Total positions across all layers |
| `tickers_for_layer(layer)` | Set of tickers held by a specific layer |
| `all_positions()` | Full `{ticker: layer}` dict |
| `reset()` | Clear all positions (called at session start) |

### 8.4 Global Limit

`global_max=75` (from `GLOBAL_MAX_POSITIONS` in config). `try_acquire()` returns `False` if the total position count across all layers would exceed this limit.

### 8.5 Lock Semantics

The `_file_lock` context manager opens the lock file, acquires `LOCK_EX` (blocking), and releases `LOCK_UN` on exit. This is a POSIX advisory lock -- correct only if all processes use this class. Direct JSON file edits bypass the lock.

---

## 9. SIGNAL ANALYZER (`monitor/signals.py`)

### 9.1 Role

The SignalAnalyzer implements the VWAP reclaim entry/exit strategy for the Core engine. It is the bridge between raw bar data and actionable trading signals.

### 9.2 Indicator Computation

On each `analyze(ticker, data, rvol_cache)` call:

1. **VWAP**: computed via `vwap_utils.compute_vwap(high, low, close, volume)`.
2. **RSI(14)**: standard Wilder RSI. Returns 50.0 (neutral) if insufficient bars.
3. **ATR(14)**: true range with 14-bar rolling mean.
4. **RVOL**: preferentially from the global `RVOLEngine` (with lazy seeding via `seed_from_bar_payload`), falling back to the legacy `get_rvol()` function.

### 9.3 LRU Indicator Cache

Cache key: `(ticker, id(df), rsi_period, atr_period)`. Using `id(df)` means cache entries are invalidated when a new DataFrame slice is created (new bar arrives). Maximum 500 entries; oldest entry is evicted via `next(iter(...))` on dict insertion order.

### 9.4 Entry Conditions (all must be true)

1. Two-bar VWAP reclaim confirmation (configurable lookback, default 6 bars, max dip age 4 bars).
2. Stock opened above VWAP (bullish day bias).
3. RSI 50-70.
4. RVOL >= 2x (institutional participation).
5. SPY above its VWAP (market tailwind).

### 9.5 Exit Conditions

| Exit Type | Condition |
|-----------|-----------|
| `PARTIAL_SELL` | Price >= half_target (1x ATR) and qty >= 2 |
| `SELL_STOP` | Price <= trailing stop (updated each bar: `price - 1*ATR`, ratchets up only) |
| `SELL_TARGET` | Price >= target (2x ATR) |
| `SELL_RSI` | RSI > rsi_overbought (default 70) |
| `SELL_VWAP` | VWAP breakdown detected |

### 9.6 Legacy RVOL Fallback (`get_rvol`)

Still present in the same file for backward compatibility. Uses 5-day lookback, supports both minute and daily bar formats. For daily bars, returns neutral (1.0) during the first hour to avoid artificially inflated readings from time-fraction approximation.

---

## 10. DATA CLIENT (`monitor/data_client.py` + `monitor/tradier_client.py`)

### 10.1 Strategy Pattern

`BaseDataClient` (ABC) defines the interface:

| Method | Purpose |
|--------|---------|
| `fetch_batch_bars(tickers)` | Parallel fetch of today's 1-min bars + RVOL history |
| `get_bars(ticker, bars_cache, rvol_cache)` | Cached bars with live-fetch fallback |
| `check_spread(ticker)` | Real-time Level 1 quote -> `(spread_pct, ask_price)` |
| `get_spy_vwap_bias(bars_cache)` | SPY above/below intraday VWAP |
| `get_quotes(symbols)` | Batch quote data for screener |
| `get_daily_history(symbol, start, end)` | Daily OHLCV bars |

`make_data_client(source, ...)` is the factory. Currently supports `'tradier'` and `'alpaca'`.

### 10.2 TradierDataClient

- **Connection pooling**: `requests.Session` with `HTTPAdapter(pool_connections=20, pool_maxsize=20)` to match the `ThreadPoolExecutor` worker count.
- **Auth**: Bearer token via `Authorization` header.
- **Rate limit handling**: 3 retries on HTTP 429 with exponential backoff (`2^attempt` seconds: 1s, 2s, 4s).
- **Batch bars**: `fetch_batch_bars()` uses `ThreadPoolExecutor(max_workers=20)` to fetch today's 1-min timesales + 14-day daily history in parallel for all tickers.
- **Market hours gating**: `fetch_batch_bars()` returns empty dicts if called before 9:30 AM ET.
- **Batch quotes**: single API call (`/markets/quotes`) handles up to ~400 symbols.
- **SPY VWAP bias**: computes intraday VWAP from cached SPY bars (or fetches live if not cached) and compares last close to VWAP.

---

## 11. SCREENER (`monitor/screener.py`)

### 11.1 Universe

82-symbol static universe (`_SCREEN_UNIVERSE`) covering mega-cap tech, semiconductors, fintech, crypto-adjacent, and sector ETFs. Augmented dynamically by `TickerDiscovery` (Benzinga news + StockTwits trending) when available.

### 11.2 Refresh Logic

`refresh(base_tickers, current_tickers, last_refresh)` runs only during market hours (9:30 AM - 3:00 PM ET) and throttles to 30-minute intervals:

1. Fetch batch quotes for the screen universe.
2. Rank by volume (top 50 most active) and by % change (top 20 gainers).
3. Union the two sets with TickerDiscovery results.
4. Filter: alpha-only symbols, length <= 5, not in base_tickers.
5. Relative strength filter: keep symbols whose 5-day return > SPY's 5-day return.

### 11.3 Relative Strength Filter

Uses `ThreadPoolExecutor(max_workers=10)` to fetch 8-day daily history for all candidates + SPY in parallel. Computes 5-day return (`close[-1]/close[0] - 1`) and compares to SPY. Candidates without data pass through (fail-open).

---

## 12. CONFIGURATION (`config.py`)

### 12.1 Design

All configuration is env-var driven with hardcoded defaults. No config files, no YAML, no TOML. The pattern is `X = type(os.getenv('X', default))` throughout.

### 12.2 Watchlist

180+ tickers across mega-cap tech, semiconductors, software, fintech, crypto-adjacent, healthcare, energy, financials, industrials, consumer, and ETFs. Deduplicated while preserving order via `list(dict.fromkeys(TICKERS))`.

### 12.3 Multi-Account Architecture

| Engine | Credential Env Vars | Account |
|--------|-------------------|---------|
| Core (VWAP) | `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | Main Alpaca |
| Pro | Same as Core (shared main account) | Main Alpaca |
| Pop | `APCA_POPUP_KEY`, `APCA_PUPUP_SECRET_KEY` | Dedicated Alpaca sub-account |
| Options | `APCA_OPTIONS_KEY`, `APCA_OPTIONS_SECRET` | Dedicated Alpaca sub-account |
| Tradier (data + secondary broker) | `TRADIER_TOKEN`, `TRADIER_TRADING_TOKEN`, `TRADIER_ACCOUNT_ID` | Tradier account |

### 12.4 Risk Limits

| Parameter | Default | Scope |
|-----------|---------|-------|
| `MAX_DAILY_LOSS` | -$10,000 | Core engine kill switch |
| `PRO_MAX_DAILY_LOSS` | -$2,000 | Pro engine kill switch |
| `POP_MAX_DAILY_LOSS` | -$2,000 | Pop engine kill switch |
| `OPTIONS_MAX_DAILY_LOSS` | -$3,000 | Options engine kill switch |
| `GLOBAL_MAX_POSITIONS` | 75 | Aggregate across all layers |
| `MAX_POSITIONS` | 5 | Core engine concurrent positions |
| `POP_MAX_POSITIONS` | 15 | Pop engine concurrent positions |
| `PRO_MAX_POSITIONS` | 15 | Pro engine concurrent positions |
| `OPTIONS_MAX_POSITIONS` | 5 | Options engine concurrent positions |

### 12.5 Execution Tuning

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `MAX_SLIPPAGE_PCT` | 0.5% | Max acceptable slippage |
| `DEFAULT_STOP_PCT` | 0.5% | Default stop loss percentage |
| `ORPHAN_STOP_PCT` | 3% | Stop for orphaned positions |
| `ORPHAN_TARGET_PCT` | 5% | Target for orphaned positions |
| `TRADE_START_TIME` | 09:45 | Trading window open |
| `FORCE_CLOSE_TIME` | 15:00 | Force-close all positions |
| `MIN_BARS_REQUIRED` | 30 | Minimum bars before signal generation |
| `ORDER_COOLDOWN` | 300s | Seconds between orders on same ticker |

### 12.6 Options Parameters

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `OPTIONS_TRADE_BUDGET` | $2,000 | Per-trade allocation |
| `OPTIONS_TOTAL_BUDGET` | $20,000 | Total options capital ceiling |
| `OPTIONS_MIN_DTE` / `MAX_DTE` | 20 / 45 days | Standard expiry window |
| `OPTIONS_LEAPS_DTE` | 365 days | LEAPS leg DTE |
| `OPTIONS_PROFIT_TARGET_CREDIT` | 50% | Close credit spread at 50% max reward |
| `OPTIONS_PROFIT_TARGET_DEBIT` | 100% | Close debit spread at 100% max reward |
| `OPTIONS_STOP_LOSS_FRACTION` | 80% | Cut at 80% of max risk |
| `OPTIONS_DTE_CLOSE` | 7 days | Time-based close threshold |

### 12.7 Portfolio-Level Risk

| Parameter | Default |
|-----------|---------|
| `MAX_INTRADAY_DRAWDOWN` | -$5,000 |
| `MAX_NOTIONAL_EXPOSURE` | $100,000 |
| `MAX_PORTFOLIO_DELTA` | 5.0 |
| `MAX_PORTFOLIO_GAMMA` | 1.0 |

### 12.8 Data Source Selection

`DATA_SOURCE` env var controls market data provider (`'tradier'` default, `'alpaca'` alternative). `BROKER` controls order execution (`'alpaca'` default, `'paper'` for simulation). `BROKER_MODE` controls smart routing (`'alpaca'`, `'tradier'`, or `'smart'`).

---

## 13. CROSS-CUTTING CONCERNS

### 13.1 Logging Convention

All components use date-stamped log directories: `logs/YYYYMMDD/<component>.log`. The Supervisor, Watchdog, and child processes each write to their own log file within the same date directory.

### 13.2 Timezone

All time operations use `America/New_York` via `zoneinfo.ZoneInfo`. The constant `ET` is defined at module level in every file that needs it.

### 13.3 Graceful Shutdown

Both Supervisor and Watchdog handle `SIGTERM` and `SIGINT`. The Supervisor uses `signal.signal()` to set a `shutdown` flag checked by the monitoring loop. Child processes are stopped via `SIGTERM` to the process group (`os.killpg`), with `SIGKILL` fallback after 10 seconds.

### 13.4 Crash Recovery Chain

```
Process crashes
    |
    v
Supervisor detects (poll() returns non-zero)
    |
    v
CrashAnalyzer parses traceback from log file
    |
    v
Pattern match? ----yes----> Apply fix (syntax-validated)
    |                              |
    no                        Restart process
    |
    v
Write crash_report.json
    |
    v
Restart anyway (may crash again)
    |
    v
Max restarts reached? ----yes----> Disable + Alert
    |
    no
    |
    v
Continue monitoring
```
