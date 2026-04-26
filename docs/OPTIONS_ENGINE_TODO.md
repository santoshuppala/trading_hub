# Options Engine — TODO

**Last updated**: 2026-04-26
**Status**: V10 exit lifecycle deployed, PF analysis wired, awaiting live data

---

## Completed Today

### Bug Fixes
- [x] Case mismatch in selector.py (`'buy'` vs `'BUY'`) — all signals were silently dropped
- [x] Registry cross-layer blocking — equity positions no longer block options
- [x] Risk gate capital tracking — now tracks max_risk, not net_debit (prevents credit over-leverage)
- [x] P&L math — `_get_position_mark()` returns signed mark-to-market using bid/ask
- [x] Liquidity filter — tightened from 50% to 20% spread, $0.05 min bid
- [x] Exit thresholds — stop loss 50% (was 80%), DTE close 10 (was 7), profit target 80% debit

### New Modules Built
- [x] `options/iv_tracker.py` — rolling 252-day IV history, IV rank/percentile, auto-save
- [x] `options/earnings_calendar.py` — yfinance earnings dates, blocks credit strategies within 7 days
- [x] `options/selector.py` — IV rank-based selection matrix with raw IV fallback
- [x] `options/engine.py` — full rewrite with exit management (5 exit conditions), Greeks refresh, daily stats

### Strategies Implemented (13/13)
- [x] `directional.py` — long_call, long_put
- [x] `vertical.py` — bull_call_spread, bear_put_spread, bull_put_spread, bear_call_spread
- [x] `volatility.py` — long_straddle, long_strangle
- [x] `neutral.py` — iron_condor, iron_butterfly (skew-aware OTM% strikes)
- [x] `time_based.py` — calendar_spread, diagonal_spread
- [x] `complex.py` — butterfly_spread

### Broker & Chain (Real Alpaca API)
- [x] `options/chain.py` — real Alpaca API with caching, get_quote(), find_leaps()
- [x] `options/broker.py` — real execution with fill polling, retry, close_position()

### Backtesting Framework (Partially Done)
- [x] `backtests/adapters/options_adapter.py` — wraps OptionsEngine with synthetic chain
- [x] `backtests/fill_simulator.py` — options P&L model (profit target, stop loss, DTE, theta bleed)
- [x] `backtests/reporters/csv_reporter.py` — trades, equity curve, summary CSVs
- [x] `backtests/engine.py` — wired options adapter into main engine
- [x] `backtests/data_loader.py` — fixed yfinance MultiIndex columns + 7-day batching

---

## TODO Tomorrow

### P0 — Backtest Must Work

1. **Fix mock chain `find_atm` compatibility**
   - `SyntheticOptionChainClient.find_atm(contracts, 'call', spot)` returns `None`
   - Root cause: `find_atm` filters by `c.right == option_type` but `option_type='call'` and `c.right='C'`
   - Fix: update `find_atm` and `find_by_delta` in mock to accept both `'call'`/`'C'` formats
   - File: `backtests/mocks/options_chain.py`

2. **Fix selector BAR path signal generation**
   - With default IV rank=50, the selector is too restrictive for backtesting
   - The raw IV fallback at `iv_rank==50` works but ATR ratios from real data are typically 0.005-0.015 (below thresholds)
   - Options: Lower ATR_MOD_THRESHOLD to 0.01, or add a new condition for moderate IV + range-bound
   - File: `options/selector.py`

3. **Run end-to-end backtest**
   ```bash
   python backtests/run_backtest.py --engine options --tickers AAPL NVDA TSLA --start 2026-04-07 --end 2026-04-11
   ```
   - Verify signals are captured
   - Verify trades are opened and closed
   - Verify P&L is realistic

4. **Run longer backtest (2+ weeks)**
   ```bash
   python backtests/run_backtest.py --engine options --tickers AAPL NVDA TSLA AMZN META --start 2026-03-20 --end 2026-04-11
   ```
   - Generate CSV reports
   - Analyze win rate by strategy type
   - Compute Sharpe ratio

### P1 — Validate Thresholds

5. **Test IV rank thresholds on historical data**
   - Seed IV tracker with 60+ days of IV readings from synthetic chain
   - Compare returns with IV rank filtering ON vs OFF
   - Tune IV_RANK_HIGH (currently 50) and IV_RANK_LOW (currently 30)

6. **Test exit management parameters**
   - Profit target: 50% credit, 80% debit — are these optimal?
   - Stop loss: 50% of max_risk — too tight? too loose?
   - DTE close: 10 days — compare with 7, 14

7. **Test strategy selection matrix**
   - Which strategies produce positive expectancy?
   - Which should be disabled?
   - What's the by-strategy breakdown (win rate, avg pnl, profit factor)?

### P2 — Production Hardening

8. **Live paper trading test**
   - Set `APCA_OPTIONS_KEY` and `APCA_OPTIONS_SECRET` env vars
   - Run `python run_monitor.py` with OPTIONS_PAPER_TRADING=true
   - Monitor for 1-2 trading sessions
   - Verify: signals generated, orders submitted, fills received, exits triggered

9. **Add `close_all_positions` to run_monitor.py EOD**
   - Call `options_engine.close_all_positions('eod_close')` at 3:30 PM ET
   - File: `run_monitor.py` (near the 3:30 PM check)

10. **IV tracker persistence**
    - Call `iv_tracker.save_to_file('data/iv_history.json')` at EOD
    - Call `iv_tracker.load_from_file('data/iv_history.json')` at startup
    - After 20+ trading days, IV rank becomes meaningful

### P3 — Improvements

11. **VIX regime detection**
    - Fetch VIX level via yfinance or Tradier
    - Low VIX (<15): favor credit strategies aggressively
    - High VIX (>25): favor debit/volatility strategies
    - File: new `options/vix_regime.py`

12. **Position sizing by Kelly criterion**
    - Current: flat $500/trade
    - Better: size based on estimated edge and probability
    - High win-rate credit spreads → larger size
    - Low win-rate directional → smaller size

13. **Correlation tracking**
    - Don't sell iron condors on 5 correlated tech names simultaneously
    - Track 30-day rolling correlation between held tickers
    - Block entry if portfolio correlation > 0.7

14. **Transaction cost model**
    - Add per-leg commission ($0.65 or percentage of premium)
    - Subtract from P&L in fill simulator and live engine

---

## V10 Exit Lifecycle (NEW — April 26)

### Architecture
```
Phase = f(DTE)           → OPEN (>21) / THETA (21-10) / CLOSE (≤10)
Override = f(moneyness)  → continuous pressure 0→1, escalates at >0.7
Behavior = f(Greeks)     → weighted risk score, strategy-aware fatal signals
```

### Per-Strategy Behavior
| | Credit | Debit | Volatility |
|---|---|---|---|
| Fatal signal | delta adverse + high pressure | vega adverse (IV crush) | vega adverse (IV revert) |
| Phase 1 stop | 2x credit received | 50% entry cost | 50% entry cost |
| Phase 2 stop | 1.5x credit (tighter) | 40% entry cost | 40% entry cost |
| Phase 1 profit | 50% max reward | 80% max reward | 60% max reward |
| Phase 2 profit | 40% (earlier) | 50% (lower) | 50% (lower) |
| Phase 3 | Roll if profitable + safe | Close all | Close all |

### PF Analysis
- Per-check snapshots every 2.5 min (19 fields: spot, Greeks, risk_score, etc.)
- `scripts/analyze_options_pf.py` — daily breakdown
- `scripts/weekly_options_review.py` — weekly calibration (auto-triggers Fridays)
- Calibration checks: score separation, monotonicity, threshold optimization

### Key Files
```
options/
├── options_exit_engine.py # V10: Phase-based lifecycle (NEW)
├── engine.py              # Wired lifecycle, close events, orphan recovery
├── broker.py              # Multi-leg partial fill detection
├── ...
scripts/
├── analyze_options_pf.py  # Daily PF analysis (NEW)
├── weekly_options_review.py # Weekly calibration (NEW)
lifecycle/
├── eod_report.py          # Friday auto-trigger for weekly review
├── adapters/
│   └── options_adapter.py # Orphan rebuild from broker contracts
```

## Architecture Reference

```
options/
├── options_exit_engine.py # V10: Phase-based exit lifecycle
├── engine.py              # Main orchestrator (entry + exit + monitoring)
├── selector.py            # Strategy selection (IV rank-based)
├── iv_tracker.py          # Historical IV rank/percentile
├── earnings_calendar.py   # Earnings date safety checks
├── risk.py                # Budget + position limits (tracks max_risk)
├── chain.py               # Alpaca option chain client (real API)
├── broker.py              # Alpaca order execution (real API)
├── portfolio_greeks.py    # Aggregate Greeks tracking
├── strategies/
│   ├── base.py            # OptionsTradeSpec, OptionLeg, BaseOptionsStrategy
│   ├── directional.py     # long_call, long_put
│   ├── vertical.py        # 4 vertical spreads
│   ├── volatility.py      # straddle, strangle
│   ├── neutral.py         # iron_condor, iron_butterfly (skew-aware)
│   ├── time_based.py      # calendar, diagonal
│   └── complex.py         # butterfly
│
scripts/
├── analyze_options_pf.py  # Daily PF analysis
├── weekly_options_review.py # Weekly calibration + threshold recommendations
│
backtests/
├── engine.py              # Backtest orchestrator (options wired in)
├── fill_simulator.py      # Options P&L model (profit/stop/DTE/theta)
├── adapters/
│   └── options_adapter.py # Wraps OptionsEngine with synthetic chain
├── mocks/
│   └── options_chain.py   # Synthetic chain with Black-Scholes Greeks
└── reporters/
    └── csv_reporter.py    # Trades + equity curve + summary CSVs
```

## Key Config (config.py + env vars)

```
ALPACA_OPTIONS_KEY        # Separate Alpaca account for options
ALPACA_OPTIONS_SECRET
OPTIONS_PAPER_TRADING     = true
OPTIONS_MAX_POSITIONS     = 5
OPTIONS_TRADE_BUDGET      = 500     # per trade max risk
OPTIONS_TOTAL_BUDGET      = 10000   # total capital
OPTIONS_ORDER_COOLDOWN    = 300     # seconds per ticker
OPTIONS_MIN_DTE           = 20
OPTIONS_MAX_DTE           = 45

# V10: Configurable via env vars (with safe defaults)
OPT_PROFIT_CREDIT        = 0.50    # credit profit target
OPT_PROFIT_DEBIT         = 0.80    # debit profit target
OPT_STOP_CREDIT          = 1.00    # credit stop (legacy; lifecycle uses 2x credit)
OPT_STOP_DEBIT           = 0.50    # debit stop
OPT_DTE_CLOSE            = 10      # DTE close threshold
OPT_DTE_ROLL             = 14      # DTE roll consideration
OPT_THETA_BLEED_PCT      = 0.60    # theta bleed exit %
READ_ONLY                = false   # block new entries (exits still run)
FF_OPTIONS_LIFECYCLE      = true    # feature flag: enable lifecycle engine
```

## Current Rating: 8.5/10

**What works**: Architecture, 13 strategies, IV rank, earnings calendar, V10 phase-based exit lifecycle, risk-regime management, full journey persistence, PF analysis pipeline, orphan recovery, 47 production hardening fixes.
**What's needed**: Live PF data (first week), entry quality scoring, Kelly sizing, VIX regime gating.
**Next milestone**: Week 1 PF review (auto-triggers Friday EOD).
