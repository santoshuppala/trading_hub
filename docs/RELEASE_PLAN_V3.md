# Release Plan v3 — Production Hardening & Institutional Compliance

**Date**: 2026-04-15+
**Risk Level**: HIGH — fixes race conditions, memory leaks, and silent failures
**Prerequisite**: Release Plan v1 (commit `148fb28`) and v2 deployed

---

## System Audit Findings (v2 Post-Implementation)

After implementing v2, a comprehensive audit against hedge fund standards revealed **11 critical gaps**:

| # | Finding | Severity | Impact |
|---|---------|----------|--------|
| 1 | PopStrategyEngine race conditions — unprotected mutable state | CRITICAL | Position limits can be exceeded under concurrent load |
| 2 | 51 bare/silent exception handlers across codebase | CRITICAL | Failures go undetected, data loss |
| 3 | Options system has ZERO test coverage (17 files) | CRITICAL | Any refactor could silently break options |
| 4 | Unbounded `all_signals` list in backtest SignalCapture | HIGH | Memory exhaustion on long backtests |
| 5 | 9 hardcoded magic numbers in risk/execution paths | HIGH | Can't tune risk without code changes |
| 6 | New analytics (risk_metrics, attribution, Greeks) not wired into dashboard | HIGH | Computed but invisible |
| 7 | Options chain client has no API rate limiting | HIGH | Alpaca 429 errors under load |
| 8 | 3 order submissions missing log statements | MEDIUM | Audit trail gaps |
| 9 | Backtests don't verify Greeks exits or ML classifier | MEDIUM | New features untested in replay |
| 10 | IV history not seeded for backtests (deferred from v2) | MEDIUM | Options backtests always see IV rank 50 |
| 11 | Options E2E test suite not created (deferred from v2) | CRITICAL | No regression safety net |

---

## Phase 1: Critical Safety Fixes (Day 1)

*These are deploy blockers — fix before next trading session*

### Fix 1.1: PopStrategyEngine Thread Safety

**Problem**: `_positions` set and `_last_order_time` dict are mutated from EventBus worker threads without locks. Under concurrent BAR processing, position limits can be exceeded (two entries pass the limit check simultaneously).

**Constraint**: Must not introduce lock contention that slows BAR processing. Use fine-grained locking.

**Files to modify**:
- `pop_strategy_engine.py`

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

### Fix 1.2: Bound Backtest SignalCapture Memory

**Problem**: `SignalCapture.all_signals` list grows without limit during backtests. A 1-year backtest on 100 tickers can accumulate millions of entries, exhausting memory.

**Constraint**: Must preserve last N signals for metrics computation at backtest end.

**Files to modify**:
- `backtests/sync_bus.py`

**Prompt**:
```
Read backtests/sync_bus.py completely first.

Find the SignalCapture class and its all_signals list.

Fix: Add a max size constant and FIFO eviction.

class SignalCapture:
    MAX_SIGNALS = 50000  # Keep last 50K signals (enough for metrics)

    def __init__(self):
        self.all_signals: List[tuple] = []

In every method that appends to all_signals, add the eviction check:

    def on_pro(self, event):
        ...
        self.all_signals.append(('pro', payload))
        if len(self.all_signals) > self.MAX_SIGNALS:
            self.all_signals = self.all_signals[-self.MAX_SIGNALS:]

    # Same for on_pop, on_options, and any other append site

Alternative (more efficient): use collections.deque(maxlen=50000)
    from collections import deque
    self.all_signals = deque(maxlen=50000)

Then .append() automatically evicts the oldest when full. But deque
doesn't support slicing — check if any code slices all_signals.

CRITICAL:
- Read the file first to find ALL append sites
- Do NOT change the signal capture interface (other code reads all_signals)
- If using deque, convert to list at the end for compatibility
- 50K is enough for daily metrics — tune if needed
```

**Verification**:
```bash
python -c "import py_compile; py_compile.compile('backtests/sync_bus.py', doraise=True); print('OK')"
```

---

### Fix 1.3: Options Chain API Rate Limiting

**Problem**: AlpacaOptionChainClient has a 60s cache but no per-second rate limiting. If cache misses coincide (startup, new tickers), the client can burst 10+ requests/second and hit Alpaca's rate limit. All calls then return empty, and the options engine sees "no chain available" for the rest of the session.

**Constraint**: Alpaca allows ~3-5 requests/second. Must not block the monitor thread.

**Files to modify**:
- `options/chain.py`

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

## Phase 2: Observability & Configuration (Day 2)

### Enhancement 2.1: Extract Magic Numbers to Config

**Problem**: 9 hardcoded values in risk/execution code. Tuning requires code changes instead of env var overrides.

**Constraint**: Must be backward compatible — existing behavior with no env vars set.

**Files to modify**:
- `config.py` — add new constants
- `monitor/brokers.py` — use config imports
- `monitor/position_manager.py` — use config imports
- `monitor/strategy_engine.py` — use config imports
- `monitor/monitor.py` — use config imports

**Prompt**:
```
Read config.py first to understand the pattern.

Add these new constants to config.py (in the Risk / order settings section):

# ── Execution tuning ─────────────────────────────────────────────────
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

### Enhancement 2.2: Wire Analytics into Dashboard

**Problem**: risk_metrics.py, attribution.py, and portfolio_greeks.py were created in v2 but never integrated into the dashboard. The dashboard computes basic metrics inline instead of using the proper engines.

**Constraint**: Dashboard must still work if DB is unavailable (graceful degradation).

**Files to modify**:
- `dashboards/trading_dashboard.py`

**Prompt**:
```
Read dashboards/trading_dashboard.py completely first.

Find the Performance Metrics section (likely a tab or section header).

Add two new sections AFTER the existing Performance Metrics:

Section 1: "Risk Analytics" (using RiskMetricsEngine)

    from analytics.risk_metrics import RiskMetricsEngine

    st.subheader("Risk Analytics")
    if len(trades_df) >= 5:
        risk_engine = RiskMetricsEngine()
        risk_metrics = risk_engine.compute(trades_df.to_dict('records'))

        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Sharpe Ratio", f"{risk_metrics['sharpe_ratio']:.2f}")
        r2.metric("Sortino Ratio", f"{risk_metrics['sortino_ratio']:.2f}")
        r3.metric("Daily VaR (95%)", f"${risk_metrics['daily_var_95']:.2f}")
        r4.metric("Kelly Fraction", f"{risk_metrics['kelly_fraction']:.2f}")

        r5, r6, r7, r8 = st.columns(4)
        r5.metric("Profit Factor", f"{risk_metrics['profit_factor']:.2f}")
        r6.metric("Expectancy", f"${risk_metrics['expectancy']:.2f}")
        r7.metric("Max Consecutive Losses", risk_metrics['consecutive_losses_max'])
        r8.metric("Recovery Factor", f"{risk_metrics['recovery_factor']:.2f}")

        # Rolling Sharpe chart
        rolling = risk_engine.rolling_sharpe(trades_df.to_dict('records'), window=20)
        if rolling:
            import pandas as pd
            st.line_chart(pd.DataFrame({'Rolling Sharpe (20-trade)': rolling}))
    else:
        st.info("Need at least 5 trades for risk analytics")

Section 2: "Strategy Attribution" (using StrategyAttributionEngine)

    from analytics.attribution import StrategyAttributionEngine

    st.subheader("Strategy Attribution")
    attr_engine = StrategyAttributionEngine()
    attr = attr_engine.from_trade_log(trades_df.to_dict('records'))

    if attr['strategies']:
        import pandas as pd
        attr_df = pd.DataFrame([
            {'Strategy': name, **stats}
            for name, stats in attr['strategies'].items()
        ]).sort_values('pnl', ascending=False)

        st.dataframe(attr_df, use_container_width=True, hide_index=True)

        if attr.get('best_strategy'):
            st.success(f"Best: {attr['best_strategy']}")
        if attr.get('worst_strategy'):
            st.error(f"Worst: {attr['worst_strategy']}")

Wrap each section in try/except so dashboard still works if imports fail:
    try:
        from analytics.risk_metrics import RiskMetricsEngine
        ...
    except ImportError:
        st.info("Risk analytics module not available")

CRITICAL:
- Read the file first to find the exact insertion point
- Do NOT break existing sections
- Wrap new imports in try/except for graceful degradation
- Use the existing trades_df variable (check what it's called)
- Match the existing Streamlit patterns (columns, metrics, charts)
```

**Verification**:
```bash
python -c "import py_compile; py_compile.compile('dashboards/trading_dashboard.py', doraise=True); print('OK')"
```

---

### Enhancement 2.3: Add Logging to Order Submissions

**Problem**: 3 order submission calls in brokers.py have no log statement. If an order is submitted but the fill poll fails, there's no audit trail of what was sent to Alpaca.

**Files to modify**:
- `monitor/brokers.py`

**Prompt**:
```
Read monitor/brokers.py completely first.

Find every call to self._client.submit_order(req) — there should be
at least 2 (one for buy, one for sell).

Before each submit_order call, add a log.info:

For buy orders:
    log.info("SUBMIT BUY: %s qty=%d limit=$%.2f attempt=%d",
             p.ticker, p.qty, limit_price, attempt)

For sell orders:
    log.info("SUBMIT SELL: %s qty=%d type=market order_id=%s reason=%s",
             p.ticker, actual_qty, client_order_id, p.reason)

CRITICAL:
- Read the file first to find exact locations
- Do NOT change any order logic
- Only add log.info statements BEFORE the submit_order call
- Use the variables that are in scope at each call site
- Match the existing log format used elsewhere in the file
```

**Verification**:
```bash
grep -n "SUBMIT BUY\|SUBMIT SELL" monitor/brokers.py
# Should show 2-3 new log lines
```

---

## Phase 3: Test Coverage (Day 3-4)

### Fix 3.1: Options E2E Test Suite

**Problem**: Options engine has 13 strategies, 793+ lines, Greeks-based exits, and ZERO tests. This is the single largest untested surface area in the system.

**Constraint**: Tests must use SyntheticOptionChainClient (no real API calls). Must use SyncBus for determinism.

**Files to create**:
- `test/test_19_options_e2e.py`

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

import unittest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

class TestIVTracker(unittest.TestCase):
    def test_rank_calculation(self):
        """Seed 30 days, verify IV rank is not default 50."""
        from options.iv_tracker import IVTracker
        import datetime
        t = IVTracker(save_path=None)  # no file persistence
        for i in range(30):
            d = datetime.date(2026, 3, 1) + datetime.timedelta(days=i)
            t.update('AAPL', 0.20 + i * 0.01, date=d)
        rank = t.iv_rank('AAPL')
        self.assertNotEqual(rank, 50.0)
        self.assertGreater(rank, 0)
        self.assertLessEqual(rank, 100)

    def test_insufficient_history_returns_neutral(self):
        """< 20 data points should return 50.0 (neutral)."""
        from options.iv_tracker import IVTracker
        t = IVTracker(save_path=None)
        for i in range(10):
            t.update('AAPL', 0.25 + i * 0.01)
        self.assertEqual(t.iv_rank('AAPL'), 50.0)

    def test_is_iv_high_low(self):
        """Verify threshold functions."""
        from options.iv_tracker import IVTracker
        import datetime
        t = IVTracker(save_path=None)
        # Seed with range 0.20-0.60 (IV rank of 0.60 = 100%)
        for i in range(40):
            d = datetime.date(2026, 1, 1) + datetime.timedelta(days=i)
            t.update('TEST', 0.20 + i * 0.01, date=d)
        # Current should be near high end
        self.assertTrue(t.is_iv_high('TEST'))

class TestOptionsRiskGate(unittest.TestCase):
    def test_max_positions_blocks(self):
        """Should block when max positions reached."""
        from options.risk import OptionsRiskGate
        gate = OptionsRiskGate(max_positions=2, total_budget=10000, trade_budget=500)
        self.assertTrue(gate.check_entry('AAPL', 100))
        gate.record_entry('AAPL', 100)
        self.assertTrue(gate.check_entry('NVDA', 100))
        gate.record_entry('NVDA', 100)
        self.assertFalse(gate.check_entry('MSFT', 100))

    def test_budget_blocks(self):
        """Should block when total budget exceeded."""
        from options.risk import OptionsRiskGate
        gate = OptionsRiskGate(max_positions=10, total_budget=500, trade_budget=500)
        self.assertTrue(gate.check_entry('AAPL', 400))
        gate.record_entry('AAPL', 400)
        self.assertFalse(gate.check_entry('NVDA', 200))

    def test_release_allows_reentry(self):
        """After releasing a position, should allow new entry."""
        from options.risk import OptionsRiskGate
        gate = OptionsRiskGate(max_positions=1, total_budget=10000, trade_budget=500)
        gate.record_entry('AAPL', 100)
        self.assertFalse(gate.check_entry('NVDA', 100))
        gate.release('AAPL', 100)
        self.assertTrue(gate.check_entry('NVDA', 100))

class TestPortfolioGreeks(unittest.TestCase):
    def test_empty_positions(self):
        """Empty positions should return zero Greeks."""
        from options.portfolio_greeks import PortfolioGreeksTracker
        tracker = PortfolioGreeksTracker()
        snap = tracker.update([])
        self.assertEqual(snap['total_delta'], 0.0)
        self.assertEqual(snap['total_gamma'], 0.0)

    def test_snapshot_without_update(self):
        """Snapshot before any update should return zeros."""
        from options.portfolio_greeks import PortfolioGreeksTracker
        tracker = PortfolioGreeksTracker()
        snap = tracker.snapshot()
        self.assertEqual(snap['total_delta'], 0.0)

class TestRiskMetrics(unittest.TestCase):
    def test_basic_metrics(self):
        """Verify core metrics compute correctly."""
        from analytics.risk_metrics import RiskMetricsEngine
        engine = RiskMetricsEngine()
        trades = [{'pnl': 10}, {'pnl': -5}, {'pnl': 15}, {'pnl': -3}, {'pnl': 8}]
        m = engine.compute(trades)
        self.assertEqual(m['trade_count'], 5)
        self.assertEqual(m['total_pnl'], 25.0)
        self.assertEqual(m['win_rate'], 60.0)
        self.assertGreater(m['sharpe_ratio'], 0)
        self.assertGreater(m['profit_factor'], 1.0)

    def test_empty_trades(self):
        """Empty trade list should return zeroed metrics."""
        from analytics.risk_metrics import RiskMetricsEngine
        m = RiskMetricsEngine().compute([])
        self.assertEqual(m['trade_count'], 0)

    def test_rolling_sharpe(self):
        """Rolling Sharpe with window > trades should return empty."""
        from analytics.risk_metrics import RiskMetricsEngine
        engine = RiskMetricsEngine()
        trades = [{'pnl': 1}] * 5
        self.assertEqual(engine.rolling_sharpe(trades, window=10), [])
        result = engine.rolling_sharpe(trades, window=3)
        self.assertEqual(len(result), 3)

class TestAttribution(unittest.TestCase):
    def test_from_trade_log(self):
        """Verify trade log attribution groups by strategy."""
        from analytics.attribution import StrategyAttributionEngine
        engine = StrategyAttributionEngine()
        trades = [
            {'pnl': 5, 'reason': 'SELL_TARGET', 'ticker': 'AAPL'},
            {'pnl': -2, 'reason': 'SELL_STOP', 'ticker': 'NVDA'},
        ]
        result = engine.from_trade_log(trades)
        self.assertIn('vwap_reclaim', result['strategies'])
        self.assertEqual(result['total_pnl'], 3.0)

if __name__ == '__main__':
    unittest.main()

Adapt the test cases above to match the actual method signatures and
class interfaces you find in the source files. Some methods may have
different names or parameters than assumed above.

CRITICAL:
- Read ALL source files listed above before writing tests
- Adapt test cases to match actual APIs (method names, parameters)
- If a method doesn't exist or has different signature, skip that test
  and add a comment: # TODO: test X when API stabilizes
- No real API calls — mock or synthetic everything
- Each test must be independent (setUp/tearDown)
- Tests must pass: python -m pytest test/test_19_options_e2e.py -v
```

**Verification**:
```bash
python -m pytest test/test_19_options_e2e.py -v
```

---

### Fix 3.2: IV History Seeding for Backtests

**Problem**: Options backtests start with IV rank = 50.0 (neutral default) because IVTracker has no historical data. The selector never sees "high IV" or "low IV" conditions, making options backtests meaningless.

**Constraint**: No historical options IV data available from Alpaca. Must derive IV proxy from historical volatility (HV) of the underlying.

**Files to modify**:
- `options/iv_tracker.py` — add optional `date` param to `update()`
- `backtests/adapters/options_adapter.py` — seed IV before backtest

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
                # Use index date if available, otherwise offset from first date
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
- IV premium of 1.15 means IV ≈ 115% of HV (standard for equity options)
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

## Phase 4: Exception Handling Cleanup (Day 5)

### Enhancement 4.1: Structured Exception Handling

**Problem**: 51 bare/silent `except Exception: pass` handlers across the codebase. Failures go undetected, making debugging impossible. Institutional standard requires all exceptions to be logged with context.

**Constraint**: Many of these are intentionally non-fatal (e.g., "best-effort" operations). We can't make them all fatal. But they must be logged.

**Files to modify**: (prioritized list — fix these first)
- `monitor/brokers.py` — order execution errors (3 sites)
- `options/engine.py` — options lifecycle errors
- `monitor/monitor.py` — core monitor errors (3 sites)
- `monitor/strategy_engine.py` — signal analysis errors (2 sites)
- `pop_strategy_engine.py` — pop execution errors

**Prompt**:
```
Search for all `except Exception` and bare `except:` in these files:
- monitor/brokers.py
- options/engine.py
- monitor/monitor.py
- monitor/strategy_engine.py
- pop_strategy_engine.py

For each one, determine if it's:
A) Truly best-effort (OK to fail silently) — change to log.debug
B) Important but non-fatal — change to log.warning with context
C) Critical path — change to log.error with full exc_info

Rules for each category:

Category A (best-effort): Add log.debug
BEFORE: except Exception: pass
AFTER:  except Exception as exc:
            log.debug("Context description: %s", exc)

Category B (important non-fatal): Add log.warning
BEFORE: except Exception: pass
AFTER:  except Exception as exc:
            log.warning("Context description for %s: %s", ticker, exc)

Category C (critical path): Add log.error with traceback
BEFORE: except Exception: pass
AFTER:  except Exception as exc:
            log.error("Critical operation failed for %s: %s",
                      ticker, exc, exc_info=True)

Classification guide:
- Order submission/fill checking → Category C (critical)
- Position management → Category C (critical)
- Data fetching → Category B (important)
- Cache operations → Category A (best-effort)
- UI/logging operations → Category A (best-effort)
- Greeks calculation → Category B (important)

CRITICAL:
- Read each file first
- Do NOT change any logic — only add/improve logging
- Do NOT change `except Exception` to specific types (that's a separate task)
- Preserve the existing behavior (catch and continue)
- The bare `except:` in alerts.py must be changed to `except Exception:`
  (bare except catches SystemExit and KeyboardInterrupt)
- Add meaningful context to every log message (what was being done, for which ticker)
```

**Verification**:
```bash
# Count remaining bare handlers (should be reduced)
grep -rn "except:$\|except Exception:$" monitor/ options/ pop_strategy_engine.py --include="*.py" | grep -v "test/" | wc -l
# Before: ~30+. After: should still be ~30 but all have log statements
grep -rn "except.*pass$" monitor/ options/ pop_strategy_engine.py --include="*.py" | grep -v "test/" | wc -l
# Should be 0 — no more silent passes in critical files
```

---

## Deployment Order

```
DAY 1: Safety (before market open)
  - Fix 1.1 (PopStrategy thread safety) — prevents position limit bypass
  - Fix 1.2 (backtest memory bound) — prevents OOM on long backtests
  - Fix 1.3 (chain rate limiting) — prevents API lockout

DAY 2: Observability
  - Enhancement 2.1 (magic numbers to config) — enables runtime tuning
  - Enhancement 2.2 (dashboard analytics wiring) — makes v2 features visible
  - Enhancement 2.3 (order submission logging) — completes audit trail

DAY 3-4: Test coverage
  - Fix 3.1 (options E2E tests) — regression safety net
  - Fix 3.2 (IV history seeding) — makes options backtests meaningful

DAY 5: Exception handling
  - Enhancement 4.1 (structured exceptions) — eliminates silent failures
```

---

## Risk Checklist Before Each Deploy

- [ ] Read the file being modified BEFORE writing
- [ ] `python -c "import py_compile; py_compile.compile('file.py', doraise=True)"` after every change
- [ ] No changes to EventBus priorities or dispatch mode
- [ ] No changes to Alpaca order execution logic
- [ ] `grep -n "def "` on modified files to verify no methods were removed
- [ ] Test on paper account for 1 full session before live
- [ ] Options changes require test_19 passing before deploy

---

## Remaining Gaps (Beyond v3 Scope)

These require external dependencies, significant effort, or architectural changes:

| Gap | Why Deferred | Workaround |
|---|---|---|
| Multi-broker failover | Only have Alpaca; need second broker account | Monitor Alpaca status page; manual failover |
| Barra/Axioma factor model | $50K+/yr license | Simplified beta/sector model (sector_map.py) |
| Sub-100ms execution | Retail broker, no co-location | Accept 2-5s fills; optimize entry timing |
| Streaming Greeks | Alpaca provides snapshots only | Poll every 60s with caching (implemented) |
| Reddit/Twitter sentiment | API costs + rate limits + NLP | Benzinga + StockTwits sufficient for now |
| Intraday VaR with tick data | No tick-level data feed | Trade-level parametric VaR (implemented) |
| Cross-asset trading | Alpaca scope is US equities + options | Focus on equities + options |
| Full exception type specialization | 200+ catch sites, needs careful per-site analysis | Log all exceptions first (this release), then specialize |
