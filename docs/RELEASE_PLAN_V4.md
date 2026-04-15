# Release Plan v4 — Path to 9.0

**Date**: 2026-04-15+
**Current Rating**: 7.6/10
**Target Rating**: 9.0/10
**Risk Level**: LOW-MEDIUM — mostly testing and tuning, minimal production code changes
**Prerequisite**: v1-v3 deployed, paper trading in isolated mode

---

## Remaining Gaps (7.6 → 9.0)

| # | Gap | Impact | Effort | Phase |
|---|-----|--------|--------|-------|
| 1 | Test coverage (0% on 15+ new modules) | -1.5 | 2-3 days | 2 |
| 2 | PopStrategy thread safety (race condition) | -0.1 | 30 min | 1 |
| 3 | Backtest SignalCapture unbounded memory | -0.1 | 15 min | 1 |
| 4 | Options chain rate limiting | -0.1 | 30 min | 1 |
| 5 | Remaining hardcoded magic numbers | -0.1 | 1 hr | 1 |
| 6 | ML classifier training (needs data) | -0.5 | 2-3 weeks wait | 4 |
| 7 | Options backtesting (IV seeding) | -0.3 | 1 day | 3 |
| 8 | Paper trading validation | -0.5 | 1-2 weeks | Ongoing |
| 9 | Threshold tuning | -0.2 | After week 1 | 5 |

---

## Phase 1: Quick Fixes (2 hours)

### Fix 1.1: PopStrategy Thread Safety -- DONE

**Problem**: `_positions` set and `_last_order_time` dict mutated without locks. Under concurrent BAR processing, position limits can be exceeded (two entries pass the limit check simultaneously).

**Constraint**: Must not introduce lock contention that slows BAR processing. Use fine-grained locking.

**Files**: `pop_strategy_engine.py`

**Prompt**:
```
Read pop_strategy_engine.py completely first.

Find the PopExecutor class (or equivalent — wherever _positions set and
_last_order_time dict are defined and mutated).

Current state:
  self._positions: Set[str] = set()
  self._last_order_time: Dict[str, float] = {}

These are accessed from EventBus worker threads without any lock.

Fix:

1. Add a lock in __init__:
   import threading
   self._lock = threading.Lock()

2. Wrap ALL reads and writes to _positions and _last_order_time with the lock.

   Find every location where these are accessed:
   - len(self._positions)
   - ticker in self._positions
   - self._positions.add(ticker)
   - self._positions.discard(ticker)
   - self._last_order_time[ticker] = ...
   - self._last_order_time.get(ticker, 0)

   Wrap each access site:
   with self._lock:
       if len(self._positions) >= self._max_positions:
           return
       self._positions.add(ticker)

3. For the entry path specifically, use a single atomic check-and-add:
   with self._lock:
       if len(self._positions) >= self._max_positions:
           log.info("POP BLOCKED: max positions reached")
           return False
       if ticker in self._positions:
           return False
       # ... cooldown check ...
       self._positions.add(ticker)
       self._last_order_time[ticker] = time.time()

4. For the exit path (on FILL/POSITION close):
   with self._lock:
       self._positions.discard(ticker)

CRITICAL:
- Read the file first to find ALL access sites
- Do NOT change any trading logic — only add thread safety
- Do NOT change method signatures
- The lock should be as narrow as possible — don't hold it during API calls
- Also check if there's a similar issue in ProSetupEngine / RiskAdapter
  (search for _positions without lock in pro_setups/)
```

**Verification**:
```bash
python -c "import py_compile; py_compile.compile('pop_strategy_engine.py', doraise=True); print('OK')"
grep -n "self._lock\|threading.Lock\|with self._lock" pop_strategy_engine.py
# Should show lock definition and usage sites
```

---

### Fix 1.2: Backtest Memory Bound -- DONE

**Problem**: `SignalCapture.all_signals` grows without limit during backtests. A 1-year backtest on 100 tickers can accumulate millions of entries, exhausting memory.

**Constraint**: Must preserve last N signals for metrics computation at backtest end.

**Files**: `backtests/sync_bus.py`

**Prompt**:
```
Read backtests/sync_bus.py completely first.

Find the SignalCapture class and its all_signals list.

Fix: Replace the unbounded list with collections.deque(maxlen=50000).

from collections import deque

class SignalCapture:
    MAX_SIGNALS = 50000  # Keep last 50K signals (enough for metrics)

    def __init__(self):
        self.all_signals = deque(maxlen=self.MAX_SIGNALS)

Then .append() automatically evicts the oldest when full.

Check if any code slices all_signals (deque doesn't support slicing).
If so, convert to list at the point of use:
    signals_list = list(self.all_signals)

Find ALL append sites in the class:
- on_pro()
- on_pop()
- on_options()
- any other method that appends

Each one should just use .append() — the deque handles eviction.

CRITICAL:
- Read the file first to find ALL append sites
- Do NOT change the signal capture interface (other code reads all_signals)
- If using deque, convert to list at the end for compatibility
- 50K is enough for daily metrics — tune if needed
```

**Verification**:
```bash
python -c "import py_compile; py_compile.compile('backtests/sync_bus.py', doraise=True); print('OK')"
grep -n "deque\|maxlen\|MAX_SIGNALS" backtests/sync_bus.py
```

---

### Fix 1.3: Options Chain Rate Limiting -- DONE

**Problem**: No per-second rate limiting on chain API calls. If cache misses coincide (startup, new tickers), the client can burst 10+ requests/second and hit Alpaca's rate limit. All calls then return empty for the rest of the session.

**Constraint**: Alpaca allows ~3-5 requests/second. Must not block the monitor thread.

**Files**: `options/chain.py`

**Prompt**:
```
Read options/chain.py completely first.

Find the method that makes the actual Alpaca API call (likely get_chain()
or _fetch_chain() — the one that calls requests.get or the Alpaca SDK).

Add a simple token-bucket rate limiter:

import threading
import time

class _RateLimiter:
    """Simple token-bucket rate limiter."""
    def __init__(self, max_per_second: float = 3.0):
        self._interval = 1.0 / max_per_second
        self._last_request = 0.0
        self._lock = threading.Lock()

    def wait(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request
            if elapsed < self._interval:
                time.sleep(self._interval - elapsed)
            self._last_request = time.monotonic()

In AlpacaOptionChainClient.__init__():
    self._rate_limiter = _RateLimiter(max_per_second=3.0)

Before every actual API call (NOT cache hits):
    self._rate_limiter.wait()

Also add retry with backoff on 429 responses:
    for attempt in range(3):
        self._rate_limiter.wait()
        r = requests.get(...)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        break

CRITICAL:
- Read the file first to find the exact API call location
- Rate limiting only applies to actual API calls, NOT cache hits
- Do NOT change the cache TTL or cache key logic
- The rate limiter must be thread-safe (used from EventBus workers)
- Sleep is OK here — options chain fetches are already slow (network I/O)
```

**Verification**:
```bash
python -c "import py_compile; py_compile.compile('options/chain.py', doraise=True); print('OK')"
grep -n "_RateLimiter\|rate_limiter\|\.wait()" options/chain.py
```

---

### Fix 1.4: Remaining Magic Numbers -- DONE

**Problem**: Stop percentages, trade times still hardcoded in several files.

**Files**: `config.py`, `monitor/brokers.py`, `monitor/position_manager.py`, `monitor/strategy_engine.py`

**Prompt**:
```
Read config.py first to understand the pattern.

Add these new constants to config.py (in the Risk / order settings section):

# -- Execution tuning ---------------------------------------------------------
MAX_SLIPPAGE_PCT    = float(os.getenv('MAX_SLIPPAGE_PCT', 0.005))      # 0.5% — abandon retry
DEFAULT_STOP_PCT    = float(os.getenv('DEFAULT_STOP_PCT', 0.005))      # 0.5% — fallback stop
DEFAULT_TARGET_PCT  = float(os.getenv('DEFAULT_TARGET_PCT', 0.005))    # 0.5% — fallback half-target
ORPHAN_STOP_PCT     = float(os.getenv('ORPHAN_STOP_PCT', 0.03))        # 3% — orphaned position stop
ORPHAN_TARGET_PCT   = float(os.getenv('ORPHAN_TARGET_PCT', 0.05))      # 5% — orphaned position target
ASK_DIVERGENCE_PCT  = float(os.getenv('ASK_DIVERGENCE_PCT', 0.005))    # 0.5% — ask price divergence
TRADE_START_TIME    = os.getenv('TRADE_START_TIME', '09:45')            # ET — earliest entry
FORCE_CLOSE_TIME    = os.getenv('FORCE_CLOSE_TIME', '15:00')           # ET — force close VWAP
MIN_BARS_REQUIRED   = int(os.getenv('MIN_BARS_REQUIRED', 30))          # min bars before analysis

Then update each file to import and use these instead of hardcoded values:

1. monitor/brokers.py:
   - Replace MAX_SLIPPAGE_PCT = 0.005 with import from config
   (line ~87: self.MAX_SLIPPAGE_PCT)

2. monitor/position_manager.py:
   - Replace 0.995 with (1 - DEFAULT_STOP_PCT)
   - Replace 1.005 with (1 + DEFAULT_TARGET_PCT)

3. monitor/strategy_engine.py:
   - Replace (9, 45) with parsed TRADE_START_TIME
   - Replace (15, 0) with parsed FORCE_CLOSE_TIME
   - Replace 30 with MIN_BARS_REQUIRED

4. monitor/monitor.py:
   - Replace 0.97 with (1 - ORPHAN_STOP_PCT)
   - Replace 1.05 with (1 + ORPHAN_TARGET_PCT)

CRITICAL:
- Read each file first to find exact line numbers
- Existing behavior must be preserved (same default values)
- Do NOT change any logic — only change where values come from
- Add the imports at the top of each modified file
```

**Verification**:
```bash
python -c "
from config import MAX_SLIPPAGE_PCT, DEFAULT_STOP_PCT, ORPHAN_STOP_PCT
assert MAX_SLIPPAGE_PCT == 0.005
assert DEFAULT_STOP_PCT == 0.005
assert ORPHAN_STOP_PCT == 0.03
print('Config OK')
"
```

---

## Phase 2: Test Coverage (2-3 days)

### Test 2.1: Options E2E (11 test cases)

**Problem**: Options engine has 13 strategies, 793+ lines, Greeks-based exits, and ZERO tests. This is the single largest untested surface area.

**Files**: `test/test_19_options_e2e.py`

**Prompt**:
```
Read these files completely first:
- options/engine.py (full file — understand all entry/exit paths)
- options/selector.py (IV rank thresholds, strategy selection logic)
- options/chain.py (OptionContract dataclass, get_chain, get_greeks)
- options/risk.py (OptionsRiskGate — max positions, budget checks)
- options/strategies/base.py (BaseOptionsStrategy interface)
- options/strategies/directional.py (long_call, long_put builders)
- options/strategies/vertical.py (bull_call_spread, bear_put_spread)
- options/strategies/neutral.py (iron_condor, iron_butterfly)
- options/iv_tracker.py (IV rank calculation, update, seed methods)
- backtests/adapters/options_adapter.py (SyntheticOptionChainClient)
- backtests/sync_bus.py (SyncBus for deterministic testing)
- test/test_6_pipeline_integration.py (test pattern reference)

Create test/test_19_options_e2e.py with these test cases:

1. test_iv_rank_calculation — Seed 30 days IV, verify rank != default 50
2. test_iv_insufficient_history — < 20 points returns 50.0
3. test_iv_high_low_thresholds — Verify is_iv_high/is_iv_low functions
4. test_risk_gate_max_positions — Block when max positions reached
5. test_risk_gate_budget — Block when total budget exceeded
6. test_risk_gate_release — After release, allow new entry
7. test_portfolio_greeks_empty — Empty positions return zero Greeks
8. test_portfolio_greeks_snapshot — Snapshot before update returns zeros
9. test_risk_metrics_basic — 5 trades compute Sharpe, profit factor
10. test_risk_metrics_empty — Empty trades return zeroed metrics
11. test_attribution_from_trade_log — Groups by strategy, sums P&L

Adapt test cases to match actual method signatures found in source files.
No real API calls — mock or synthetic everything.
Each test must be independent (setUp/tearDown).

CRITICAL:
- Read ALL source files listed above before writing tests
- Adapt test cases to match actual APIs (method names, parameters)
- If a method doesn't exist or has different signature, skip and add TODO comment
- Tests must pass: python -m pytest test/test_19_options_e2e.py -v
```

**Verification**:
```bash
python -m pytest test/test_19_options_e2e.py -v
```

---

### Test 2.2: Risk Module Tests -- DONE

**Problem**: `portfolio_risk.py`, `risk_sizing.py`, `smart_router.py` have zero test coverage.

**Files**: NEW `test/test_20_risk_modules.py`

**Prompt**:
```
Read these files completely first:
- monitor/portfolio_risk.py
- monitor/risk_sizing.py
- monitor/smart_router.py

Create test/test_20_risk_modules.py with:

1. test_portfolio_risk_drawdown_halt
   - Create PortfolioRiskManager with MAX_INTRADAY_DRAWDOWN=-5000
   - Simulate open positions losing $6000 total
   - Verify halt_trading() returns True
   - Verify new entries are blocked

2. test_portfolio_risk_notional_cap
   - Create PortfolioRiskManager with MAX_NOTIONAL_EXPOSURE=100000
   - Add positions totaling $110K notional
   - Verify check_notional() blocks new entry

3. test_risk_sizer_beta_adjustment
   - TQQQ (beta ~3.0): 10 raw shares should become ~3 after beta adjustment
   - SPY (beta ~1.0): shares unchanged
   - Verify via RiskSizer.adjust_for_beta()

4. test_risk_sizer_correlation_block
   - 3 semiconductor positions held (AMD, NVDA, INTC)
   - Attempt MU entry — should be blocked by correlation group limit
   - Verify via RiskSizer.check_correlation()

5. test_risk_sizer_news_override
   - Ticker-specific catalyst (earnings) overrides correlation block
   - Verify news_override=True allows entry despite correlation limit

6. test_risk_sizer_volatility_scaling
   - High ATR ticker gets fewer shares than low ATR ticker
   - Verify via RiskSizer.scale_for_volatility()

7. test_smart_router_failover
   - Primary broker (Alpaca) returns failure
   - Verify order routes to secondary (Tradier)
   - Verify SmartRouter.route() returns secondary broker

8. test_smart_router_circuit_breaker
   - 3 consecutive failures on Alpaca
   - Verify Alpaca is disabled (circuit breaker open)
   - Verify all orders route to Tradier

Adapt all test cases to match actual class names, method signatures,
and constructor parameters found in the source files. Use mocks for
broker API calls.

CRITICAL:
- Read each source file first to find exact APIs
- Use unittest.mock for broker calls
- Each test must be independent
- Tests must pass: python -m pytest test/test_20_risk_modules.py -v
```

**Verification**:
```bash
python -m pytest test/test_20_risk_modules.py -v
```

---

### Test 2.3: Data Source Tests -- DONE

**Problem**: `data_sources/` has 12 files and zero tests. Circuit breaker, smart persistence, and dedup logic are untested.

**Files**: NEW `test/test_21_data_sources.py`

**Prompt**:
```
Read these files completely first:
- data_sources/yahoo_finance.py
- data_sources/fear_greed.py
- data_sources/fred_macro.py
- data_sources/finviz_data.py
- data_sources/persistence.py
- data_sources/collector.py

Create test/test_21_data_sources.py with:

1. test_yahoo_finance_fundamentals
   - Mock HTTP response for AAPL
   - Verify returns market_cap, pe_ratio, beta fields
   - Verify types are correct (float/int)

2. test_fear_greed_range
   - Mock CNN Fear & Greed response
   - Verify value in 0-100 range
   - Verify label in ['Extreme Fear', 'Fear', 'Neutral', 'Greed', 'Extreme Greed']

3. test_fred_macro_regime
   - Mock FRED series data
   - Verify regime in ['risk_on', 'neutral', 'risk_off', 'crisis']

4. test_finviz_screener
   - Mock Finviz HTML response for AAPL
   - Verify returns price, short_float fields

5. test_smart_persistence_dedup
   - Save data for AAPL
   - Save identical data again
   - Verify second save was skipped (dedup)
   - Save changed data — verify it was persisted

6. test_smart_persistence_threshold
   - Fear & Greed value = 50
   - New value = 51 (change < 2 points)
   - Verify skipped (below threshold)
   - New value = 55 (change >= 2 points)
   - Verify persisted

7. test_collector_circuit_breaker
   - Mock a data source that fails 3 times
   - Verify source is disabled for 30 minutes
   - Verify collector skips disabled sources
   - Verify collector re-enables after cooldown

Adapt all test cases to match actual class names, method signatures,
and constructor parameters found in the source files.

CRITICAL:
- Read each source file first
- Use unittest.mock.patch for all HTTP calls
- No real API calls
- Each test must be independent
- Tests must pass: python -m pytest test/test_21_data_sources.py -v
```

**Verification**:
```bash
python -m pytest test/test_21_data_sources.py -v
```

---

## Phase 3: Backtesting (1 day)

### Fix 3.1: IV History Seeding

**Problem**: Options backtests start with IV rank = 50.0 (neutral default) because IVTracker has no historical data. The selector never sees "high IV" or "low IV" conditions, making options backtests meaningless.

**Constraint**: No historical options IV data available from Alpaca. Must derive IV proxy from historical volatility (HV) of the underlying.

**Files**: `options/iv_tracker.py`, `backtests/adapters/options_adapter.py`

**Prompt**:
```
Read these files completely first:
- options/iv_tracker.py (especially the update() method and _history dict)
- backtests/adapters/options_adapter.py
- backtests/engine.py (how backtest date range and daily_bars work)

Step 1: Modify options/iv_tracker.py

The update() method currently uses datetime.now() for the date key.
Add an optional `date` parameter:

    def update(self, ticker: str, iv: float, date=None):
        if date is None:
            date = datetime.now().date()
        elif isinstance(date, datetime):
            date = date.date()
        # ... rest of existing logic using date as the key ...

Check the actual code — the date handling may use a string key or
datetime.date key. Match the existing pattern.

Step 2: Add _seed_iv_history to OptionsBacktestAdapter

    def _seed_iv_history(self, tickers, daily_bars):
        """Seed IV tracker with HV-derived estimates for backtest realism.

        IV proxy: annualized 20-day historical volatility * 1.15 (vol premium)
        """
        import numpy as np
        iv_premium = 1.15

        for ticker in tickers:
            if ticker not in daily_bars or daily_bars[ticker] is None:
                continue
            df = daily_bars[ticker]
            if len(df) < 25:
                continue

            close = df['close'].values
            log_ret = np.diff(np.log(close))
            # Rolling 20-day HV
            for i in range(20, len(log_ret)):
                window = log_ret[i-20:i]
                hv = float(np.std(window, ddof=1) * np.sqrt(252))
                iv_est = hv * iv_premium
                if hasattr(df, 'index') and hasattr(df.index[i], 'date'):
                    date = df.index[i].date()
                else:
                    from datetime import timedelta
                    date = datetime.date(2026, 1, 1) + timedelta(days=i)
                self.engine._iv_tracker.update(ticker, iv_est, date=date)

        log.info("IV history seeded for %d tickers", len(tickers))

Call _seed_iv_history from __init__ or a setup method, after the engine
is created but before any bars are processed.

CRITICAL:
- Read iv_tracker.py first to understand the exact update() signature
- The date parameter must not break existing callers (default None = now)
- IV premium of 1.15 means IV ~ 115% of HV (standard for equity options)
- Handle DataFrame index types gracefully (DatetimeIndex vs RangeIndex)
```

**Verification**:
```bash
python -c "
from options.iv_tracker import IVTracker
import datetime
t = IVTracker(save_path=None)
t.update('AAPL', 0.30, date=datetime.date(2026, 3, 15))
t.update('AAPL', 0.35, date=datetime.date(2026, 3, 16))
print(f'History entries: {len(t._history.get(\"AAPL\", {}))}')
print('OK')
"
```

---

### Fix 3.2: New Risk Checks in Backtests -- DONE

**Problem**: Backtests don't use beta sizing, correlation limits, or portfolio risk. Backtest results overstate performance because real-world risk constraints are not applied.

**Files**: `backtests/engine.py`

**Prompt**:
```
Read backtests/engine.py completely first.
Read monitor/risk_sizing.py to understand the RiskSizer interface.
Read monitor/portfolio_risk.py to understand the PortfolioRiskManager interface.

Wire RiskSizer into the backtest pipeline:

1. Import RiskSizer and PortfolioRiskManager
2. In the backtest engine's __init__ or setup, create instances:
   self._risk_sizer = RiskSizer()
   self._portfolio_risk = PortfolioRiskManager()

3. Before executing a trade in the backtest:
   # Beta-adjusted sizing
   adjusted_qty = self._risk_sizer.adjust_for_beta(ticker, raw_qty)

   # Correlation check
   if not self._risk_sizer.check_correlation(ticker, current_positions):
       log.info("BACKTEST BLOCKED: correlation limit for %s", ticker)
       continue

   # Portfolio risk check
   if self._portfolio_risk.halt_trading():
       log.info("BACKTEST HALTED: drawdown limit reached")
       break

4. After each trade, update portfolio risk state:
   self._portfolio_risk.update_pnl(trade_pnl)

This ensures backtest results reflect real-world risk constraints.

CRITICAL:
- Read backtests/engine.py first to find exact insertion points
- Do NOT change the backtest output format
- Make risk checks optional (default on, env var to disable for speed)
- Risk sizer needs sector_map — import from config or use default
```

**Verification**:
```bash
python -c "import py_compile; py_compile.compile('backtests/engine.py', doraise=True); print('OK')"
```

---

## Phase 4: ML Training (Week 3+)

### Enhancement 4.1: Train Pop Classifier

**Prerequisite**: 2-3 weeks of PopStrategySignal + PositionClosed events in DB.

**Problem**: The pop classifier currently uses a static rule-based approach. With sufficient historical data, a trained ML model can improve strategy selection accuracy.

**Command**: `python scripts/train_pop_classifier.py`

**Validation**: Classification report with per-strategy accuracy > 60%

**Prompt**:
```
Read these files first:
- pop_screener/classifier.py (current rule-based classifier)
- db/reader.py (how to query event_store)
- db/feature_store.py (ML feature persistence)

The training script should:
1. Query event_store for PopStrategySignal events with their outcomes
   (join with PositionClosed events by ticker + time window)
2. Extract features: rvol, vwap_distance, rsi, atr, time_of_day, sector
3. Train a RandomForest classifier (sklearn) on strategy_type prediction
4. Save model to models/pop_classifier.pkl
5. Print classification report with per-strategy accuracy
6. Only train if >= 500 signal-outcome pairs available

CRITICAL:
- Do NOT overwrite the rule-based classifier
- The ML classifier should be an optional overlay (config flag)
- If ML model exists and flag is set, use ML; otherwise fall back to rules
```

---

### Enhancement 4.2: Signal Quality Scoring

**Problem**: All signals treated equally. A signal that historically wins 80% should be sized larger.

**Files**: NEW `analytics/signal_scorer.py`

**Prompt**:
```
Read these files first:
- db/reader.py (how to query event_store)
- analytics/attribution.py (existing attribution pattern)
- monitor/risk_sizing.py (how sizing is applied)

Create analytics/signal_scorer.py that:

1. Queries event_store for historical signal -> outcome pairs
2. Computes per-(ticker, strategy, time_of_day) win rate
3. Returns a confidence multiplier (0.5-2.0) for position sizing:
   - win_rate >= 70%: multiplier = 2.0 (double budget)
   - win_rate >= 55%: multiplier = 1.5
   - win_rate >= 45%: multiplier = 1.0 (default)
   - win_rate >= 35%: multiplier = 0.75
   - win_rate < 35%: multiplier = 0.5 (half budget)
4. Requires minimum 20 historical trades per bucket for confidence
5. Falls back to 1.0 (default) if insufficient data
6. Caches results for 1 hour (don't query DB on every signal)

class SignalScorer:
    def __init__(self, db_reader=None):
        ...

    def confidence_multiplier(self, ticker: str, strategy: str,
                               hour: int) -> float:
        ...

    def refresh_cache(self) -> None:
        ...

CRITICAL:
- Do NOT modify existing code — this is a new standalone module
- Cache must be thread-safe (used from EventBus workers)
- Graceful degradation if DB is unavailable (return 1.0)
```

**Verification**:
```bash
python -c "import py_compile; py_compile.compile('analytics/signal_scorer.py', doraise=True); print('OK')"
```

---

## Phase 5: Tuning (After Week 1 of Data)

### Enhancement 5.1: Threshold Calibration

After 1 week of paper trading with real RVOL/gap data, review:

| Threshold | Current Value | Question |
|-----------|---------------|----------|
| ETF RVOL threshold | 0.7 | Are ETFs generating signals? |
| Correlation group max | 3 | Too restrictive? |
| Sector limit | 2 | Should some sectors allow 3? |
| Pop screener RVOL minimum | 1.5 | Are any pop signals firing? |
| Kill switch (monolith) | -$10K | Appropriate for account size? |
| Kill switch (portfolio risk) | -$5K | Appropriate for isolated mode? |
| Beta sizing (TQQQ) | 1/3 | Too aggressive reduction? |
| Order cooldown | 300s | Missing re-entries? |

**Process**:
```bash
# Query signal generation rate per threshold
python -c "
from db.reader import DBReader
reader = DBReader()
# Count signals by type and time
signals = reader.query_signals(start='2026-04-15', end='2026-04-22')
print(f'Total signals: {len(signals)}')
# Group by strategy, hour, outcome
"
```

Adjust thresholds in `.env` (no code changes needed if Fix 1.4 is deployed).

---

### Enhancement 5.2: Strategy Performance Review

After 2 weeks, run strategy attribution:

```bash
python -c "
from analytics.attribution import StrategyAttributionEngine
engine = StrategyAttributionEngine()
# Run for each trading day
import datetime
start = datetime.date(2026, 4, 15)
for i in range(10):
    day = start + datetime.timedelta(days=i)
    if day.weekday() < 5:  # weekdays only
        result = engine.daily_attribution(str(day))
        if result:
            print(f'{day}: best={result[\"best_strategy\"]} worst={result[\"worst_strategy\"]}')
"
```

**Decision rules**:
- Disable strategies with negative expectancy over 10+ sessions
- Increase budget for strategies with win rate > 60% and positive expectancy
- Review strategies with < 5 trades (insufficient data to judge)

---

## Deployment Order

```
WEEK 1 (Day 1-2):
  Phase 1: Quick fixes (thread safety, memory, rate limiting, magic numbers)
  Start Phase 2: Test coverage

WEEK 1 (Day 3-5):
  Complete Phase 2: All test suites passing
  Phase 3: Backtesting improvements

WEEK 2-3:
  Paper trading validation (daily EOD review)
  Phase 5: Threshold tuning based on live data

WEEK 3+:
  Phase 4: ML training (when sufficient data accumulated)
  Phase 5: Strategy performance review
```

---

## Risk Checklist

- [ ] All files compile after every change
- [ ] Integration test (scripts/test_isolated_mode.py) passes
- [ ] No changes to EventBus priorities
- [ ] No changes to Alpaca order execution logic
- [ ] Paper trade for 1 full session before any live consideration
