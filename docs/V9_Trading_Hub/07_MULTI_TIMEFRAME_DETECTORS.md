# V9 Multi-Timeframe Detector Architecture

## Overview

V9 introduces multi-timeframe bar aggregation for Pro strategy detectors. 6 of 13 detectors now operate on 5-minute bars for pattern detection, while 7 detectors remain on 1-minute bars where that timeframe is correct.

The 1-minute bars are aggregated to 5-minute bars locally via `df.resample('5min')` — no additional API calls needed.

## Why Multi-Timeframe?

On 1-minute bars, structure-based detectors (SR, Trend, Fib, Flag, Inside Bar) produce 80%+ false positives because intrabar noise looks like real patterns. On 5-minute bars, noise is filtered and only meaningful structures remain.

| Issue | 1-min bars | 5-min bars |
|-------|-----------|-----------|
| SR pivot detection | Noise pivots every 5 min (20-30/day) | Real structure pivots (3-5/day) |
| Inside bar pattern | Happens every 30 seconds (50+/day) | Rare genuine coils (5-8/day) |
| EMA-50 trend | 50 min = incomplete session trend | 250 min = full session trend |
| ATR-14 | $0.08 (meaningless for options) | $0.40 (realistic volatility) |
| Flag pattern | Micro-moves look like poles (15-20/day) | Real directional thrusts (2-4/day) |
| Fib swings | 40 min of noise oscillation | 200 min of real session structure |

## Timeframe Assignment

### 1-Minute Detectors (unchanged)

| Detector | Why 1-min is correct |
|----------|---------------------|
| **VWAP Reclaim** | Cumulative session VWAP needs every bar for precision |
| **ORB** | Opening range = first 15 1-min bars (standard definition) |
| **Momentum** | RSI + volume surges are per-bar events |
| **Liquidity** | Wick analysis is per-candle structure |
| **Volatility** | Bollinger squeeze on 1-min for intrabar compression |
| **Sentiment** | Reads alt_data_cache.json, not bars |
| **NewsVelocity** | News events are tick-level |
| **Gap** | Overnight gap (prev close vs open) — timeframe-agnostic |

### 5-Minute Detectors (V9)

| Detector | `_MIN_5M_BARS` | First Active | Daily Fallback | What changes on 5-min |
|----------|---------------|-------------|----------------|----------------------|
| **SR Flip** | 10 (50 min) | **9:45 AM** (Layer 1) | Daily pivots PP/R1/R2/S1/S2 | Pivots from real structure, not noise |
| **Fib Retracement** | 10 (50 min) | **9:45 AM** (Phase 1) | Yesterday's H/L range | Structural swings over 200 min |
| **Inside Bar** | 4 (20 min) | ~9:50 AM | None | Rare meaningful coils, not constant noise |
| **Flag/Pennant** | 10 (50 min) | ~10:20 AM | None | Real poles (40 min) + real consolidation (50 min) |
| **Trend** | 12 (60 min) | ~10:30 AM | None | EMA50 = 250 min = full session trend |

### Options Engine (V9)

| Indicator | 1-min (V8) | 5-min (V9) | Impact |
|-----------|-----------|-----------|--------|
| ATR-14 | 14 min ($0.08) | 70 min ($0.40) | Realistic for strike width + P&L targets |
| RSI-14 | 14 min (noisy) | 70 min (stable) | Meaningful neutral/extreme readings |
| RVOL | Stays on 1-min | Same | Cumulative volume is timeframe-agnostic |

## Implementation

### Data Flow

```
Tradier API → 1-min OHLCV bars (fetch_batch_bars)
                    │
                    ├──→ 1-min consumers: VWAP, ORB, Momentum, Liquidity, News
                    │    Direct df passed to detector.detect()
                    │
                    └──→ df.resample('5min').agg(...)  [in ProSetupEngine._on_bar()]
                         │
                         ├──→ precomputed['df_5min']     → SR, InsideBar, Flag, Fib, Trend
                         ├──→ precomputed['ema_9_5m']    → Trend detector
                         ├──→ precomputed['ema_21_5m']   → Trend, InsideBar, Fib
                         ├──→ precomputed['ema_50_5m']   → Trend detector
                         ├──→ precomputed['atr_5m']      → Flag detector
                         └──→ precomputed['rsi_5m']      → (available for future use)
```

### Per-Ticker Cache

5-min bars are cached per-ticker in `ProSetupEngine._5min_cache`. Recomputed only when `len(df)` changes (new 1-min bar arrived). Saves ~80% of resample operations across 183 tickers.

```python
# Pro engine caching logic
cached_len = self._5min_cache_len.get(ticker, 0)
if len(df) != cached_len:
    df_5min = df.resample('5min').agg(...)  # recompute
    self._5min_cache[ticker] = df_5min
    self._5min_cache_len[ticker] = len(df)
else:
    df_5min = self._5min_cache[ticker]      # use cached
```

Cache is in-memory only — resets to empty on restart. No stale data risk.

### Detector Pattern

Each 5-min detector reads from `precomputed['df_5min']` with fallback to original `df`:

```python
def _detect(self, ticker, df, rvol_df, precomputed=None, **kw):
    # V9: Use 5-min bars for structure detection
    work_df = precomputed.get('df_5min', df) if precomputed else df
    if len(work_df) < self._MIN_5M_BARS:
        return DetectorSignal.no_signal()
    # ... pattern detection on work_df ...
    
    # Use latest 1-min close for entry timing (most current price)
    last_close = float(df['close'].iloc[-1])
```

Key design: **Detection on 5-min structure, entry timing on 1-min precision.** Pivots/swings/trends identified from 5-min bars, but proximity/breakout checks use the latest 1-min close for the most current price.

## Activation Timeline

```
9:30 AM   Market opens. Streaming starts.
          │
9:45 AM   ═══ TRADING WINDOW OPENS ═══
          Active: Momentum, Liquidity, Volatility, Sentiment, News, Gap
          + SR Layer 1 (daily pivots from yesterday H/L/C — 7 levels)
          + Fib Phase 1 (yesterday's range — 5 Fib levels)
          │
9:50 AM   + Inside Bar (5-min, _MIN_5M_BARS=4)
          │
10:00 AM  + VWAP Reclaim (1-min, MIN_BARS=30)
          │
10:20 AM  + SR Layer 2 (intraday 5-min pivots added on top of daily)
          + Flag (5-min, _MIN_5M_BARS=10)
          + Fib Phase 2 (intraday 5-min swings replace yesterday's range)
          │
10:22 AM  + Pro classifier gate open (52 1-min bars)
          │
10:30 AM  + Trend (5-min, _MIN_5M_BARS=12)
          │ ALL 13 DETECTORS NOW ACTIVE
          │
10:40 AM  + Options 5-min ATR/RSI active
          │ ALL ENGINES AT FULL CAPABILITY
          │
15:00 PM  ═══ TRADING WINDOW CLOSES ═══
```

## Detector Details

### SR Flip — Two-Layer S/R Detection

**Layer 1: Daily Pivots (available from 9:45 AM)**
- Computed from yesterday's High / Low / Close via `rvol_df`
- Standard institutional pivot levels: PP, R1, R2, S1, S2 + yesterday's H/L
- Every institutional desk watches these — available from the first bar
- 7 levels generated per session

**Layer 2: Intraday Pivots (available from ~10:20 AM)**
- Computed from 5-min bar pivot highs/lows (`_MIN_5M_BARS=10`)
- Lookback: 40 5-min bars = 200 min of session structure
- Pivot window: 3 bars each side (15 min qualification)
- Adds session-level structure on top of daily pivots

**Both layers coexist** — daily pivots remain valid all session. Nearest level from either layer triggers the signal.

- **Proximity**: 0.6% of price
- **S/R flip logic**: Previous resistance now acting as support (or vice versa) gets +0.2 strength bonus
- **Source tracking**: metadata reports `daily_pivot` or `intraday_5m`
- **V8 → V9**: False pivots reduced from 20-30/day to 3-5/day

### Inside Bar (5-min)

- **Pattern**: Current 5-min bar high ≤ previous high AND low ≥ previous low
- **Tightness**: Ratio of inside bar range to mother bar range (tighter = stronger signal)
- **Direction**: 5-min EMA20 slope (100 min trend bias)
- **V8 → V9**: False signals reduced from 50+/day to 5-8/day

### Flag/Pennant (5-min)

- **Pole**: 8 5-min bars (40 min) with move ≥ 3× ATR-14 (5-min ATR = 70 min)
- **Flag**: 10 5-min bars (50 min) with range ≤ 1.5× ATR
- **Breakout**: Close exceeds flag by 0.3× ATR
- **V8 → V9**: ATR on 5-min is ~2x larger, so only real directional thrusts qualify as poles

### Fib Retracement — Two-Phase Detection

**Phase 1: Yesterday's Range (available from 9:45 AM)**
- Fib levels computed from yesterday's High → Low via `rvol_df`
- Answers: "Where does the overnight gap retrace to?" — standard practice
- Active as fallback when Phase 2 has insufficient data

**Phase 2: Intraday 5-min Swings (available from ~10:20 AM)**
- Swing high/low computed from 5-min bars (`_MIN_5M_BARS=10`)
- Lookback: 40 5-min bars = 200 min of real session structure
- **Replaces Phase 1** once mature (intraday structure takes priority)

Phase 2 is tried first; Phase 1 activates only if Phase 2 produces `s_high <= s_low` (insufficient swings).

- **Levels**: 23.6%, 38.2%, 50%, 61.8%, 78.6%
- **Proximity**: 0.25% of price
- **Primary bonus**: 38.2% and 61.8% get +0.2 strength
- **Direction**: 5-min EMA21 slope (or 1-min EMA20 when 5-min unavailable)
- **Source tracking**: metadata reports `daily_prev` or `intraday_5m`
- **V8 → V9**: Swings capture the full session move, not 40 minutes of noise

### Trend / EMA Alignment (5-min)

- **EMA9**: 45 min (short-term acceleration)
- **EMA21**: 105 min (intermediate swing)
- **EMA50**: 250 min (full session trend — 4+ hours)
- **Structure**: 20 5-min bars (100 min) of HH/HL or LH/LL confirmation
- **Min strength**: 45% of bars must be in trending structure
- **V8 → V9**: EMA50 now captures the REAL session trend, not just the last hour

## Files Modified

| File | Change |
|------|--------|
| `pro_setups/engine.py` | 5-min resample + precomputed indicators + per-ticker cache |
| `pro_setups/detectors/sr_detector.py` | Read `df_5min`, `_MIN_5M_BARS=10` |
| `pro_setups/detectors/inside_bar_detector.py` | Read `df_5min`, `_MIN_5M_BARS=4` |
| `pro_setups/detectors/flag_detector.py` | Read `df_5min` + 5-min ATR, `_MIN_5M_BARS=10` |
| `pro_setups/detectors/fib_detector.py` | Read `df_5min` + 5-min EMA, `_MIN_5M_BARS=10` |
| `pro_setups/detectors/trend_detector.py` | Precomputed 5-min EMAs, `_MIN_5M_BARS=12` |
| `options/engine.py` | 5-min resample for ATR/RSI entry decisions |

## What Does NOT Change

- Data fetching (still 1-min bars from Tradier)
- BAR event structure (1-min df in payload)
- VWAP/ORB/Momentum/Liquidity/News/Gap detectors (stay on 1-min)
- Base detector interface (`precomputed` dict already supported)
- Classifier logic (receives DetectorSignal regardless of source timeframe)
- Stop/target computation (uses ATR from signal, independent of detection timeframe)
- Streaming exit monitoring (QUOTE-based, not bar-based)
- FillLedger, BrokerRegistry, EquityTracker (position tracking layer unaffected)
