# FINANCIAL ANALYST AUDIT REPORT — STRATEGY BUG INVESTIGATION
## Date: 2026-04-13
## Critical Issue: confirmed_crossover strategy used instead of vwap_reclaim on 2026-04-10

---

## ISSUE SUMMARY

**Problem:** On 2026-04-10 at 08:32:26, logs show "Strategy: confirmed_crossover" but config.py sets STRATEGY = 'vwap_reclaim'

**Evidence:**
- Log line 1 of monitor_2026-04-10.log: "Starting monitor | Strategy: confirmed_crossover"
- config.py line 53: "STRATEGY = 'vwap_reclaim'"
- **DISCREPANCY:** System is running the wrong strategy

**Severity:** CRITICAL — This is a major trading bug affecting real capital allocation.

---

## ROOT CAUSE ANALYSIS

### Finding 1: StrategyEngine ignores STRATEGY configuration
- **File:** monitor/strategy_engine.py
- **Issue:** StrategyEngine.__init__() takes strategy_params dict but NEVER takes strategy_name parameter
- **Impact:** The STRATEGY configuration variable is NEVER used by the live trading system
- **Code:** Line 95 only uses "SignalAnalyzer(strategy_params, per_ticker_params)"

### Finding 2: SignalAnalyzer is hardcoded to VWAP Reclaim logic
- **File:** monitor/signals.py
- **Issue:** SignalAnalyzer.analyze() implements VWAP Reclaim specifically (checks _vwap_reclaim, _opened_above_vwap)
- **Code:** Lines 140-160 of strategy_engine.py show hardcoded VWAP Reclaim entry logic
- **Evidence:** "_vwap_reclaim", "_opened_above_vwap", "reclaim_candle_low" checks are all specific to VWAP Reclaim

### Finding 3: main.py has "confirmed_crossover" strategy but run_monitor.py doesn't use it
- **File:** main.py
- **Issue:** Contains ConfirmedCrossoverStrategy class for backtesting only
- **Impact:** This strategy never executes in live trading (live uses run_monitor.py not main.py)

### Finding 4: strategies/__init__.py registry is unused
- **File:** strategies/__init__.py
- **Issue:** Defines get_strategy_class() but it's NEVER called by run_monitor.py or monitor code
- **Code:** RealTimeMonitor never imports or calls get_strategy_class()

### Finding 5: Logging message is misleading
- **File:** run_monitor.py line 67
- **Code:** `log.info(f"Starting monitor | Strategy: {STRATEGY} | ...`
- **Issue:** Logs STRATEGY variable but the system doesn't actually use it
- **This explains the confusion:** Analyst sees "confirmed_crossover" in logs but can't find it in live trading code

---

## WHERE IS "confirmed_crossover" COMING FROM?

**CRITICAL DISCOVERY:** The "confirmed_crossover" in the logs is coming from a HARDCODED value somewhere in the initialization chain, not from config.py. This means:

1. The logging line showing "confirmed_crossover" is not logging config.STRATEGY
2. There must be a hardcoded strategy name somewhere in RealTimeMonitor or initialization
3. The STRATEGY variable is being overridden or shadowed

---

## BREAKTHROUGH: THE REAL ISSUE

### What Actually Happened on 2026-04-10

**Timeline:**
- 2026-04-10 08:31:40 — Commit "redesign" with config.py STRATEGY = 'confirmed_crossover'
- 2026-04-10 08:32:26 — Trading started, logs show "Strategy: confirmed_crossover"
- System actually ran VWAP Reclaim (hardcoded in StrategyEngine)
- Analyst expected vwap_reclaim but config said confirmed_crossover
- **Analyst caught the discrepancy and flagged it as a bug**

### The Real Problem: Architecture Flaw

**The "confirmed_crossover" strategy was CONFIGURED but NEVER IMPLEMENTED in live trading.**

Evidence:
1. **config.py on 2026-04-10:** `STRATEGY = 'confirmed_crossover'` ✓ (configured)
2. **RealTimeMonitor.strategy_name:** Never used ✗ (not passed)
3. **StrategyEngine.__init__():** Never takes strategy_name parameter ✗ (not accepted)
4. **SignalAnalyzer:** Hardcoded to VWAP Reclaim only ✗ (can't switch)
5. **ConfirmedCrossoverStrategy:** Exists only in main.py for backtesting ✗ (never used live)

### What the System Actually Did

Even though config.py specified 'confirmed_crossover', the system DID NOT run that strategy. Instead:

1. run_monitor.py imported STRATEGY from config.py (confirmed_crossover)
2. run_monitor.py logged it: "Starting monitor | Strategy: confirmed_crossover"
3. run_monitor.py created RealTimeMonitor() but NEVER passed strategy_name to it
4. RealTimeMonitor created StrategyEngine with only strategy_params
5. StrategyEngine created SignalAnalyzer which hardcoded VWAP Reclaim logic
6. **System ran VWAP Reclaim logic regardless of config.STRATEGY value**

### Why This Is A Critical Bug

1. **Configuration is ignored:** STRATEGY variable is meaningless in live trading
2. **Cannot switch strategies:** StrategyEngine can only run VWAP Reclaim
3. **False security:** Analyst can't actually change strategies by editing config.py
4. **Misleading logs:** Logs show one strategy but system runs another
5. **Testing blind spot:** Tests didn't catch this because they only test VWAP Reclaim
6. **Hidden coupling:** StrategyEngine is hardcoded to SignalAnalyzer's VWAP logic

---

## ARCHITECTURAL ISSUES IDENTIFIED

### Issue 1: Strategy Registry Not Wired to Live Trading
- **Files:** strategies/__init__.py defines get_strategy_class()
- **Problem:** Never called by RealTimeMonitor or any live trading code
- **Status:** DEAD CODE for live trading

### Issue 2: StrategyEngine Ignores Configuration
- **File:** monitor/strategy_engine.py
- **Problem:** Takes only strategy_params, not strategy_name
- **Cannot support:** Multiple strategies at runtime
- **Status:** ARCHITECTURAL FLAW

### Issue 3: SignalAnalyzer Hardcoded to One Strategy
- **File:** monitor/signals.py
- **Problem:** analyze() method implements VWAP Reclaim only
- **No abstraction:** No base class for different entry logic
- **Status:** MONOLITHIC DESIGN

### Issue 4: Backtesting Strategies Not Used in Live
- **File:** strategies/ package
- **Problem:** Contains EMA_RSI, Trend_ATR, Momentum_Breakout but they're for backtesting only (main.py)
- **Live trading:** Completely separate code path (run_monitor.py → RealTimeMonitor → StrategyEngine → SignalAnalyzer)
- **Status:** TWO SEPARATE SYSTEMS

---

## DETAILED STRATEGY ANALYSIS

I will now audit each strategy to understand what was supposed to work and what actually works.

### Strategy 1: VWAP Reclaim
**Status:** ✓ WORKS (hardcoded in live)
- Implemented in: monitor/signals.py (SignalAnalyzer)
- Used by: StrategyEngine in live trading (run_monitor.py)
- Entry: _vwap_reclaim + _opened_above_vwap (lines 140-160 of strategy_engine.py)
- Exit: check_position_exit() with RSI/VWAP breakdown checks
- **Verified:** Working as expected in logs

### Strategy 2: confirmed_crossover
**Status:** ✗ BROKEN (configured but not implemented)
- Config: STRATEGY = 'confirmed_crossover' on 2026-04-10
- Implemented in: main.py (ConfirmedCrossoverStrategy for backtesting)
- Used by: **NEVER CALLED in live trading**
- Live execution: Falls through to VWAP Reclaim (hardcoded)
- **Issue:** Cannot select or run this strategy in live trading

### Strategy 3: ema_rsi
**Status:** ✗ NOT AVAILABLE (backtesting only)
- Implemented in: strategies/ema_rsi.py (EMARSICrossoverStrategy)
- Used by: main.py (backtesting via Backtrader)
- Live execution: **NOT AVAILABLE** — no live trading code
- **Issue:** Defined in strategy registry but StrategyEngine doesn't call it

### Strategy 4: trend_atr
**Status:** ✗ NOT AVAILABLE (backtesting only)
- Implemented in: strategies/trend_atr.py (TrendFollowingATRStrategy)
- Used by: main.py (backtesting via Backtrader)
- Live execution: **NOT AVAILABLE** — no live trading code
- **Issue:** Defined in strategy registry but StrategyEngine doesn't call it

### Strategy 5: momentum_breakout
**Status:** ✗ NOT AVAILABLE (backtesting only)
- Implemented in: strategies/momentum_breakout.py (MomentumBreakoutStrategy)
- Used by: main.py (backtesting via Backtrader)
- Live execution: **NOT AVAILABLE** — no live trading code
- **Issue:** Defined in strategy registry but StrategyEngine doesn't call it

### Strategy 6: mean_reversion
**Status:** ✗ NOT AVAILABLE (backtesting only)
- Implemented in: strategies/mean_reversion.py (MeanReversionStrategy)
- Used by: main.py (backtesting via Backtrader)
- Live execution: **NOT AVAILABLE** — no live trading code
- **Issue:** Defined in strategy registry but StrategyEngine doesn't call it

---

## SUMMARY TABLE

| Strategy | Config | Live Impl | Backtesting | Status | Notes |
|----------|--------|-----------|-------------|--------|-------|
| vwap_reclaim | ✓ | ✓ (SignalAnalyzer) | ✗ | **WORKS** | Only strategy that works in live |
| confirmed_crossover | ✓ (2026-04-10) | ✗ | ✗ | **BROKEN** | Config says use it; system ignores and runs VWAP |
| ema_rsi | ✗ | ✗ | ✓ | **BACKTESTING ONLY** | Never works in live |
| trend_atr | ✗ | ✗ | ✓ | **BACKTESTING ONLY** | Never works in live |
| momentum_breakout | ✗ | ✗ | ✓ | **BACKTESTING ONLY** | Never works in live |
| mean_reversion | ✗ | ✗ | ✓ | **BACKTESTING ONLY** | Never works in live |

---

## ROOT CAUSE CONCLUSION

The "confirmed_crossover" strategy incident reveals that:

1. **Live and backtest codepaths are completely separate**
   - Live: run_monitor.py → RealTimeMonitor → StrategyEngine → SignalAnalyzer (VWAP only)
   - Backtest: main.py → Backtrader → strategies/ (6 different strategies)

2. **Configuration is disconnected from implementation**
   - STRATEGY variable in config.py has no effect on live trading
   - StrategyEngine never reads or uses the STRATEGY variable
   - Only STRATEGY_PARAMS are used, and they're for VWAP Reclaim only

3. **No abstraction for multiple strategies**
   - StrategyEngine doesn't have a pluggable strategy interface
   - SignalAnalyzer is monolithic (VWAP Reclaim only)
   - No way to switch strategies without rewriting StrategyEngine

4. **Testing blind spot**
   - Tests pass because they only test VWAP Reclaim
   - No tests verify that configured strategy actually runs
   - No tests verify multi-strategy switching in live mode

---

## RECOMMENDATIONS

### Immediate Actions (Critical)

1. **Remove misleading STRATEGY configuration**
   - STRATEGY variable in config.py has no effect
   - Either implement strategy switching or document that only VWAP Reclaim is available
   - Update logs to not show STRATEGY (it's false information)

2. **Fix configuration discrepancy**
   - Commit dd7e6fc changed to vwap_reclaim
   - Verify that current code in live only runs VWAP Reclaim
   - Update documentation to clarify this

3. **Create architecture decision document**
   - Document that live trading only supports VWAP Reclaim
   - Explain that strategies/ package is backtesting-only
   - Clarify that confirmed_crossover was removed from live (commit dd7e6fc)

### Medium-term Actions (Design)

1. **Create abstraction for live strategy selection**
   - Design BaseStrategyEngine interface
   - Implement concrete engines for each strategy
   - Make StrategyEngine a factory that delegates to correct implementation

2. **Consolidate live and backtest strategies**
   - Port strategies from main.py to shared codebase
   - Create live versions of all backtesting strategies
   - Eliminate code duplication

3. **Add configuration validation**
   - Validate at startup that configured strategy is actually available
   - Fail fast if strategy not found (don't silently use default)
   - Log warning if STRATEGY_PARAMS don't match selected strategy

### Testing Actions (Quality)

1. **Add strategy configuration tests**
   - Test that STRATEGY config actually selects correct engine
   - Test multi-strategy switching
   - Test fallback behavior if strategy unavailable

2. **Add strategy selection logging**
   - Log which strategy engine was selected
   - Log when config.STRATEGY doesn't match running strategy
   - Log strategy parameters used for each trade

3. **Audit all configuration variables**
   - Verify every config variable is actually used
   - Remove dead configuration
   - Add comments for why each config exists

---

## VALIDATION OF FINDINGS

### Finding 1: STRATEGY_PARAMS Mismatch
**Test:** Check if confirmed_crossover's STRATEGY_PARAMS are used by live system

**Result:** ✗ MISMATCH DETECTED
- confirmed_crossover STRATEGY_PARAMS included: `'volume_factor': 1.5`
- SignalAnalyzer.analyze() uses: rsi_period, atr_period, rsi_overbought, atr_multiplier, min_stop_pct, reclaim_lookback, max_dip_age
- **Finding:** 'volume_factor' is NOT used by SignalAnalyzer → confirmed_crossover params ignored

### Finding 2: VWAP-Specific Code in SignalAnalyzer
**Test:** Search for VWAP-specific parameter usage

**Result:** ✓ CONFIRMED HARDCODED
- Line 171: `reclaim_lookback = params.get('reclaim_lookback', 6)` — VWAP specific
- Line 172: `max_dip_age = params.get('max_dip_age', 4)` — VWAP specific
- Line 244: `_detect_vwap_reclaim()` call — VWAP specific
- Line 246: `_detect_vwap_breakdown()` call — VWAP specific
- **Finding:** SignalAnalyzer is 100% VWAP Reclaim specific

### Finding 3: Confirmed Crossover Never Called in Live
**Test:** Search codebase for ConfirmedCrossoverStrategy usage in live trading

**Result:** ✗ NOT USED IN LIVE
- ConfirmedCrossoverStrategy defined in: main.py (backtesting only)
- Called by: Backtrader strategy framework in main.py
- Never called by: RealTimeMonitor, StrategyEngine, or any run_monitor.py code
- **Finding:** confirmed_crossover strategy exists but is completely disconnected from live trading

### Finding 4: Configuration Variable Not Wired
**Test:** Trace STRATEGY variable from config.py through live trading

**Trace:**
1. config.py line 53: `STRATEGY = 'confirmed_crossover'` (on 2026-04-10)
2. run_monitor.py line 27: `from config import STRATEGY`
3. run_monitor.py line 67: `log.info(f"...Strategy: {STRATEGY}...")` ← only use is logging
4. run_monitor.py main() function: STRATEGY never used again
5. RealTimeMonitor.__init__: STRATEGY parameter NOT passed
6. StrategyEngine.__init__: Takes strategy_params only, not strategy_name
7. StrategyEngine never reads STRATEGY

**Result:** ✗ STRATEGY VARIABLE IS DEAD CODE
- Configured in: config.py
- Logged in: run_monitor.py line 67
- Used in: **NOWHERE ELSE**

---

## ANALYST SIGN-OFF

### Issues Confirmed

| Issue | Evidence | Severity | Status |
|-------|----------|----------|--------|
| confirmed_crossover configured but not used | Config says 'confirmed_crossover' but system runs VWAP Reclaim | CRITICAL | Confirmed by commit history + code tracing |
| STRATEGY configuration ignored | STRATEGY variable logged but never used by trading engine | CRITICAL | Confirmed by grep + code review |
| SignalAnalyzer hardcoded to VWAP | All VWAP-specific functions called; no strategy abstraction | CRITICAL | Confirmed by parameter analysis |
| Strategy registry unused in live | strategies/__init__.py has 6 strategies but live only uses hardcoded VWAP | HIGH | Confirmed by code search |
| Two separate strategy systems | main.py and run_monitor.py have completely different strategy implementations | HIGH | Confirmed by file structure analysis |
| Misleading logs | Logs show "confirmed_crossover" but system runs "vwap_reclaim" | MEDIUM | Confirmed by commit timeline + code review |

### Root Cause

The trading system has a **fundamental architectural flaw**: the STRATEGY configuration is disconnected from the actual strategy implementation. The live trading system is hardcoded to VWAP Reclaim in SignalAnalyzer, while configuration allows selecting different strategies that are never used.

**This was discovered on 2026-04-10** when confirmed_crossover was configured but the system still ran VWAP Reclaim. The analyst correctly identified the discrepancy between expected and actual behavior.

**Fix implemented 2026-04-12** when commit dd7e6fc changed STRATEGY back to 'vwap_reclaim' to match the hardcoded implementation.

### Recommendation

This audit confirms that the confirmed_crossover incident was NOT a bug in the strategy logic, but a **fundamental design flaw** where configuration and implementation are disconnected. The system needs architectural refactoring to support true multi-strategy trading.

**Interim fix (applied):** Set STRATEGY = 'vwap_reclaim' to match hardcoded implementation

**Permanent fix (required):** Refactor StrategyEngine to support pluggable strategy implementations
