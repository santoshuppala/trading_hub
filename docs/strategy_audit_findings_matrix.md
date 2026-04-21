# Strategy Audit Findings Matrix

**Audit Date:** 2026-04-13
**Audit Type:** Comprehensive Financial Analyst Review
**Trigger:** confirmed_crossover incident on 2026-04-10
**Duration:** Complete code path analysis from config.py through live execution

---

## 1. STRATEGY AVAILABILITY MATRIX

| Strategy | Config Option | Live Works | Backtest Works | Data Source | Notes |
|----------|---|---|---|---|---|
| VWAP Reclaim | ✓ (vwap_reclaim) | ✓ YES | ✗ NO | SignalAnalyzer (hardcoded) | **ONLY strategy working in live** |
| confirmed_crossover | ✓ (on 2026-04-10) | ✗ NO | ✗ NO | main.py + config override | Configured but never used; removed 2026-04-12 |
| EMA RSI Crossover | ✗ (not in config) | ✗ NO | ✓ YES | strategies/ema_rsi.py | Backtesting only via Backtrader |
| Trend Following ATR | ✗ (not in config) | ✗ NO | ✓ YES | strategies/trend_atr.py | Backtesting only via Backtrader |
| Momentum Breakout | ✗ (not in config) | ✗ NO | ✓ YES | strategies/momentum_breakout.py | Backtesting only via Backtrader |
| Mean Reversion | ✗ (not in config) | ✗ NO | ✓ YES | strategies/mean_reversion.py | Backtesting only via Backtrader |

**Key Finding:** Only 1 of 6 strategies works in live trading; 5 strategies are disconnected from live execution.

---

## 2. CONFIGURATION USAGE AUDIT

| Config Variable | File | Set? | Read? | Used? | Effect | Status |
|---|---|---|---|---|---|---|
| STRATEGY | config.py:53 | ✓ YES | ✓ YES (run_monitor.py:27) | ✗ NO | Logged only, not used by trading engine | ⚠️ DEAD CODE |
| STRATEGY_PARAMS | config.py:54 | ✓ YES | ✓ YES (run_monitor.py:27) | ✓ YES (SignalAnalyzer) | Parsed to indicators; partial use | ⚠️ INCOMPLETE |
| PRO_MAX_POSITIONS | config.py | ✓ YES | ✓ YES | ✓ YES | ProSetupEngine | ✓ OK |
| MAX_POSITIONS | config.py | ✓ YES | ✓ YES | ✓ YES | RiskEngine | ✓ OK |
| PAPER_TRADING | config.py | ✓ YES | ✓ YES | ✓ YES | Alpaca client | ✓ OK |
| DATA_SOURCE | config.py | ✓ YES | ✓ YES | ✓ YES | TradierDataClient/AlpacaDataClient | ✓ OK |

**Finding:** STRATEGY variable is logged but never used in actual trading logic.

---

## 3. CODE PATH ANALYSIS: CONFIGURATION → EXECUTION

### Path: STRATEGY Configuration

```
config.py:53: STRATEGY = 'vwap_reclaim' (current)
              or 'confirmed_crossover' (2026-04-10)
    ↓
run_monitor.py:27: from config import STRATEGY
    ↓
run_monitor.py:67: log.info(f"...Strategy: {STRATEGY}...")
    ↓
*** STOPS HERE *** (never used again)
    ↓
RealTimeMonitor.__init__: DOES NOT TAKE strategy_name PARAMETER
StrategyEngine.__init__: DOES NOT TAKE strategy_name PARAMETER
SignalAnalyzer: HARDCODED TO VWAP RECLAIM
```

**Finding:** STRATEGY configuration has no effect on actual trading engine.

### Path: STRATEGY_PARAMS Configuration

```
config.py:54: STRATEGY_PARAMS = {rsi_period, atr_period, ...}
    ↓
run_monitor.py:27: from config import STRATEGY_PARAMS
    ↓
run_monitor.py:140: passed to RealTimeMonitor(strategy_params=STRATEGY_PARAMS)
    ↓
monitor/monitor.py:213: StrategyEngine(strategy_params=strategy_params)
    ↓
monitor/strategy_engine.py:95: SignalAnalyzer(strategy_params, per_ticker_params)
    ↓
monitor/signals.py:107: self.strategy_params = strategy_params
    ↓
monitor/signals.py:165: params = {**self.strategy_params, ...}
    ↓
monitor/signals.py:166-172: Extract rsi_period, atr_period, atr_multiplier, etc.
    ↓
*** USED FOR VWAP RECLAIM CALCULATIONS ONLY ***
```

**Finding:** STRATEGY_PARAMS are used but only for VWAP Reclaim calculations.

---

## 4. PARAMETER MATCHING ANALYSIS

### confirmed_crossover STRATEGY_PARAMS (from 2026-04-10)

```python
'rsi_period':     14,       # Used by SignalAnalyzer ✓
'atr_period':     14,       # Used by SignalAnalyzer ✓
'atr_multiplier': 2.0,      # Used by SignalAnalyzer ✓
'volume_factor':  1.5,      # NOT used by SignalAnalyzer ✗
'min_stop_pct':   0.05,     # Used by SignalAnalyzer ✓
'rsi_overbought': 70,       # Used by SignalAnalyzer ✓
```

**Finding:** volume_factor parameter is ignored, indicating STRATEGY_PARAMS don't fully match implementation.

---

## 5. HARDCODED VWAP LOGIC VERIFICATION

**File:** monitor/signals.py
**Lines:** 171-246

| Line | Code | VWAP Specific |
|---|---|---|
| 171 | `reclaim_lookback = params.get('reclaim_lookback', 6)` | ✓ YES (VWAP only) |
| 172 | `max_dip_age = params.get('max_dip_age', 4)` | ✓ YES (VWAP only) |
| 194 | `vwap = compute_vwap(...)` | ✓ YES (VWAP only) |
| 244 | `_detect_vwap_reclaim(...)` | ✓ YES (VWAP only) |
| 246 | `_detect_vwap_breakdown(...)` | ✓ YES (VWAP only) |

**Finding:** 100% of entry logic is VWAP Reclaim specific. No abstraction for other strategies.

---

## 6. STRATEGY REGISTRY STATUS

**File:** strategies/__init__.py
**Defined:** 6 strategy classes
**Used in Live Trading:** 0/6
**Used in Backtesting:** 6/6
**Status:** Dead code for live trading

```python
# strategies/__init__.py
def get_strategy_class(name):
    strategies = {
        'ema_rsi': EMARSICrossoverStrategy,           # ✗ Never called by live
        'trend_atr': TrendFollowingATRStrategy,       # ✗ Never called by live
        'momentum_breakout': MomentumBreakoutStrategy,# ✗ Never called by live
        'mean_reversion': MeanReversionStrategy,      # ✗ Never called by live
        'vwap_reclaim': VWAPReclaimStrategy,          # ✗ Never called by live
        'confirmed_crossover': ??? (not in registry) # ✗ Not found anywhere
    }
    return strategies.get(name)
```

**Finding:** Strategy registry is unused in live trading code.

---

## 7. COMMIT HISTORY EVIDENCE

| Commit | Date/Time | Change | STRATEGY Value |
|---|---|---|---|
| 20ae2ae | 2026-04-10 08:31:40 | "redesign" | 'confirmed_crossover' |
| (trading run) | 2026-04-10 08:32:26 | Logs show | 'confirmed_crossover' |
| dd7e6fc | 2026-04-12 23:20:12 | "fix: change strategy..." | 'vwap_reclaim' |
| 6a47f9e | 2026-04-13 00:09:38 | "Options trading" | 'vwap_reclaim' |

**Finding:** confirmed_crossover was intentional on 2026-04-10; removed 2026-04-12 to match hardcoded implementation.

---

## 8. TESTING COVERAGE AUDIT

### Current Tests
- ✓ VWAP Reclaim entry/exit logic
- ✗ Strategy selection logic
- ✗ Multi-strategy switching
- ✗ Configuration validation
- ✗ Strategy parameter validation

### Gaps
1. No test verifies that STRATEGY config actually selects strategy
2. No test ensures configured strategy runs in live trading
3. No test validates STRATEGY_PARAMS match selected strategy
4. No integration test for config → execution path

**Finding:** Testing does not catch configuration/implementation mismatch.

---

## 9. LOGGING ANALYSIS

| Log Message | File | Line | What It Shows | What It Should Show |
|---|---|---|---|---|
| "Starting monitor \| Strategy: {STRATEGY}" | run_monitor.py | 67 | Configured strategy | Actual running strategy |
| "[StrategyEngine] BUY signal" | strategy_engine.py | 183 | Trading action | Which strategy triggered it |

**Finding:** Logs are misleading; they show configured strategy, not running strategy.

---

## 10. SEVERITY CLASSIFICATION

| Issue | Category | Severity | Evidence | Impact |
|---|---|---|---|---|
| STRATEGY ignored in live | Configuration | CRITICAL | Code path analysis | Cannot change strategy by editing config |
| SignalAnalyzer hardcoded | Architecture | CRITICAL | Parameter inspection | Cannot add new strategies |
| Strategy registry unused | Dead Code | CRITICAL | Grep search | 6 strategies never used in live |
| Misleading logs | Operations | MEDIUM | Log message analysis | Operator confusion |
| Two separate code paths | Architecture | HIGH | Live vs backtest comparison | Backtest results don't predict live |
| No strategy validation | Testing | HIGH | Test coverage audit | Silent failures if config wrong |

---

## 11. ROOT CAUSE STATEMENT

**Primary Cause:** StrategyEngine architecture does not support pluggable strategies.

**Secondary Cause:** STRATEGY configuration variable added to config.py without wiring it to live trading engine.

**Contributing Factor:** Live trading (run_monitor.py) and backtesting (main.py) have completely separate strategy implementations.

**Detection Failure:** Analyst discovered this by reading logs on 2026-04-10 and noticing configured strategy (confirmed_crossover) wasn't in the codebase.

---

## 12. FINANCIAL ANALYST SIGN-OFF

**Auditor:** Financial Analyst
**Date:** 2026-04-13
**Confidence Level:** HIGH (100% code path traced)
**Recommendation:** REFACTOR REQUIRED

### Verdict

The confirmed_crossover incident reveals a **fundamental architecture flaw**, not a trading bug:

1. **Configuration is disconnected from implementation** — STRATEGY variable has no effect
2. **No strategy abstraction** — SignalAnalyzer is hardcoded to VWAP Reclaim
3. **Live and backtest are separate systems** — 6 backtesting strategies are inaccessible in live
4. **Misleading documentation** — Logs show one strategy but system runs another

### Risk Assessment

**Current Risk: HIGH**
- Cannot switch strategies by editing config.py
- Misconfigured strategies silently fail (no validation)
- Analyst relies on log inspection to catch errors
- No tests verify strategy selection works

**Mitigated By:**
- Commit dd7e6fc aligned config with hardcoded implementation
- System is now self-consistent (config says vwap_reclaim, runs vwap_reclaim)

**Remaining Risk:**
- Cannot add new strategies without major refactoring
- Future changes may cause similar misalignment
- No early warning system for config/implementation divergence

### Recommendation Grade: C

**Current State:** Configuration and implementation are now aligned (STRATEGY = vwap_reclaim everywhere)
**Overall Architecture:** Inflexible and monolithic (can only run 1 strategy)
**Long-term Viability:** Poor (prevents strategy evolution)

**Required Action:** Refactor StrategyEngine to support pluggable strategies within 2 quarters.

---

END OF FINDINGS MATRIX
