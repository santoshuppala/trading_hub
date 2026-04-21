# V7: P4 Fixes — Extensibility

**Status:** Implemented and tested (49/49 checks pass)
**Test file:** `test/test_p4_fixes.py`
**Files modified:** `monitor/smart_router.py`, `scripts/supervisor.py`, `pop_screener/strategy_router.py`, `config.py`

---

## P4-1: Generic Broker Registration in SmartRouter

**Problem:** SmartRouter accepted only `alpaca_broker=` and `tradier_broker=` parameters. Adding a third broker (e.g., IBKR) required modifying `__init__`, plus position detection used Alpaca-specific `broker._client.get_open_position()`.

**Fix:**
1. `__init__` now accepts `brokers: Dict[str, BaseBroker]` parameter for generic registration
2. New `_register_broker(name, broker, bus)` method handles unsubscription generically
3. `_select_broker()` tries `broker.has_position(ticker)` first (generic interface), falls back to `broker._client.get_open_position()` (legacy Alpaca path)
4. Legacy `alpaca_broker=` / `tradier_broker=` parameters still work (backward compat)

**To add a new broker:**
```python
router = SmartRouter(bus=bus, brokers={
    'alpaca': alpaca_broker,
    'tradier': tradier_broker,
    'ibkr': ibkr_broker,       # just add to dict — no code changes
})
```

---

## P4-2: Config-Driven Engine Registration

**Problem:** Adding a 5th engine required changes in 3+ hardcoded locations: `supervisor.py` PROCESSES dict, new `run_*.py` script, `config.py`.

**Fix:**
1. `_DEFAULT_PROCESSES` dict holds built-in engines (core, pro, pop, options)
2. `_load_engine_config()` merges defaults with `ENGINE_CONFIG` env var (JSON)
3. Auto-discovers `scripts/run_*.py` files not already in config

**To add a new engine (zero code changes):**
```bash
# Create the run script
vi scripts/run_arbitrage.py

# Option A: Auto-discovered (just having the file is enough)
# Supervisor finds run_arbitrage.py → registers 'arbitrage' engine

# Option B: Explicit config via env var
export ENGINE_CONFIG='{"arbitrage": {"critical": false, "max_restarts": 5}}'

# Option C: Disable a default engine
export ENGINE_CONFIG='{"pop": null}'
```

---

## P4-3: STRATEGY_REGISTRY for Pop Strategies

**Problem:** Pop's `StrategyRouter.__init__()` had a hardcoded `_engines` dict. Adding a strategy required modifying the class.

**Fix:** Module-level `STRATEGY_REGISTRY` dict (same pattern as Options engine):

```python
# pop_screener/strategy_router.py
STRATEGY_REGISTRY = {
    StrategyType.VWAP_RECLAIM:           VWAPReclaimEngine(),
    StrategyType.ORB:                    ORBEngine(),
    ...
}
```

**To add a new Pop strategy:**
1. Create engine class in `pop_screener/strategies/`
2. Add `StrategyType` enum entry to `models.py`
3. Add one line: `STRATEGY_REGISTRY[StrategyType.NEW_TYPE] = NewEngine()`

No router, classifier, or screener changes needed.

---

## P4-4: Config Validation

**Problem:** `config.py` read env vars with `os.getenv()` and cast to int/float with no validation. Invalid values (negative budget, slippage > 100%, wrong broker name) silently propagated into trading logic.

**Fix:** Validation block at end of `config.py` with fail-fast `ValueError`:

```python
_validate_positive('TRADE_BUDGET', TRADE_BUDGET)
_validate_range('MAX_SLIPPAGE_PCT', MAX_SLIPPAGE_PCT, 0.0, 0.10)
_validate_choice('BROKER', BROKER, ('alpaca', 'paper'))
if MAX_DAILY_LOSS >= 0:
    _CONFIG_ERRORS.append("MAX_DAILY_LOSS must be negative")

if _CONFIG_ERRORS:
    raise ValueError(f"Config validation failed: {_CONFIG_ERRORS}")
```

**What's validated:**
- Positive: budgets, positions, cooldowns, bar requirements
- Negative: kill switch thresholds (MAX_DAILY_LOSS, PRO/POP/OPTIONS variants)
- Range: slippage (0-10%), stop percent (0-50%)
- Choice: BROKER (alpaca/paper), DATA_SOURCE (tradier/alpaca), BROKER_MODE (alpaca/tradier/smart)
- Ordering: OPTIONS_MIN_DTE < OPTIONS_MAX_DTE
- Portfolio: notional/delta/gamma must be positive, drawdown must be negative

**Behavior:** If any env var is invalid, `import config` raises `ValueError` with all errors listed. Process fails immediately on startup — bad config never reaches trading logic.

---

*Implemented 2026-04-15. 49/49 tests passing.*
