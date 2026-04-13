"""
pop_screener/config.py — All configurable thresholds and parameters
====================================================================
Every numeric threshold used by the screener, classifier, and strategy engines
is defined here.  Change a value in this file and all downstream logic updates
automatically — no hunting for magic numbers across modules.

Override at runtime via environment variables or by patching this module before
importing the screener (useful for backtesting with different regimes).
"""
import os

# ── Float threshold ────────────────────────────────────────────────────────────
# Shares outstanding below this value → FloatCategory.LOW_FLOAT
FLOAT_LOW_THRESHOLD_SHARES: int = 20_000_000   # 20 M shares

# ── Screener thresholds ────────────────────────────────────────────────────────

# moderate_news_pop
SENTIMENT_DELTA_MODERATE: float   = 0.20   # sentiment improved by at least 0.20
HEADLINE_VELOCITY_MIN:    float   = 1.5    # 1.5× baseline headline rate
HEADLINE_VELOCITY_MAX:    float   = 8.0    # cap — above this → high_impact territory
GAP_SIZE_MODERATE_MAX:    float   = 0.04   # |gap| < 4 % → moderate (not gapping out)

# high_impact_news_pop
SENTIMENT_DELTA_HIGH:     float   = 0.40   # strong positive sentiment shift
HEADLINE_VELOCITY_HIGH:   float   = 8.0    # headline storm (8× baseline)
GAP_SIZE_HIGH_IMPACT_MIN: float   = 0.04   # |gap| ≥ 4 %

# sentiment_pop (social)
SOCIAL_VELOCITY_THRESHOLD: float  = 3.0    # 3× normal mention rate
SOCIAL_SKEW_THRESHOLD:     float  = 0.20   # bullish_pct − bearish_pct > 20 %

# unusual_volume_pop
RVOL_UNUSUAL_THRESHOLD: float     = 3.0    # 3× average volume at this time of day
MOMENTUM_MIN:           float     = 0.02   # price up ≥ 2 % from N bars ago

# low_float_pop
RVOL_LOW_FLOAT_THRESHOLD: float   = 4.0    # 4× volume for low-float names
GAP_SIZE_LOW_FLOAT_MIN:   float   = 0.05   # must gap ≥ 5 %

# earnings_pop
GAP_SIZE_EARNINGS_MIN: float      = 0.05   # ≥ 5 % gap on earnings

# ── Classifier thresholds ──────────────────────────────────────────────────────

# Classifier thresholds (VWAP Reclaim handled exclusively by T4 monitor layer)
RVOL_VWAP_RECLAIM_MIN:         float    = 1.5    # retained for _vwap_compat_score()
RVOL_VWAP_RECLAIM_MAX:         float    = 5.0    # retained for _vwap_compat_score()
TREND_CLEAN_VWAP_MIN:          float    = 0.55   # cleanliness score threshold

# EMA_TREND_CONTINUATION
TREND_CLEAN_EMA_MIN:            float   = 0.60
VWAP_DISTANCE_EMA_MAX:          float   = 0.03

# Earnings gap boundary
GAP_SIZE_EARNINGS_VWAP_MAX:     float   = 0.08   # < 8% gap → EMA_TREND; ≥ 8% → ORB

# High-impact secondary classification
VOLATILITY_EXTREME_THRESHOLD:   float   = 0.25   # volatility_score > 0.25 → add HALT/PARABOLIC
GAP_EXTREME_THRESHOLD:          float   = 0.15   # gap > 15 % → extremely gapped

# Confidence scoring weights
_WEIGHT_TREND:    float = 0.35
_WEIGHT_RVOL:     float = 0.30
_WEIGHT_VWAP:     float = 0.35

# ── ORB Engine ─────────────────────────────────────────────────────────────────
OR_MINUTES:              int   = 15    # first N minutes define the opening range
OR_BREAKOUT_VOL_MULT:    float = 1.5   # need 1.5× OR avg volume to confirm breakout
OR_STOP_BUFFER_PCT:      float = 0.003 # stop = or_low − 0.3 % of price
OR_R_MULT_1:             float = 1.0   # target_1 = entry + 1R (R = entry − or_low)
OR_R_MULT_2:             float = 2.0   # target_2 = entry + 2R
OR_FADE_VOL_MULT:        float = 0.60  # if volume < 60 % of OR avg → momentum fade

# ── Halt/Resume Breakout Engine ────────────────────────────────────────────────
HALT_RETURN_THRESHOLD:          float = 0.10  # ≥ 10 % single-bar move → halt-like
HALT_VOL_MULT:                  float = 3.0   # ≥ 3× avg volume
CONSOLIDATION_RANGE_MAX_ATR:    float = 0.50  # bar range < 0.5× ATR → consolidating
CONSOLIDATION_MIN_BARS:         int   = 2
CONSOLIDATION_BREAKOUT_VOL_MULT: float = 1.5
CONSOLIDATION_STOP_BUFFER_PCT:  float = 0.003
HALT_R_MULT_1:                  float = 1.0
HALT_R_MULT_2:                  float = 2.5
REVERSAL_RETURN_THRESHOLD:      float = 0.07  # 7 % reversal from high → exit

# ── Parabolic Reversal Engine ──────────────────────────────────────────────────
PARABOLIC_MOVE_THRESHOLD:  float = 0.50   # ≥ 50 % intraday move low→high
N_EXTENDED_BARS:           int   = 3      # need at least 3 extended candles
EXTENDED_RANGE_MULT:       float = 1.5    # bar range > 1.5× ATR
WICK_RATIO_MIN:            float = 0.40   # upper-wick ≥ 40 % of total bar range
EXHAUSTION_VOL_MULT:       float = 2.0    # ≥ 2× avg volume on exhaustion candle
REVERSAL_STOP_BUFFER_PCT:  float = 0.003  # stop above exhaustion high
REVERSAL_R_MULT_1:         float = 1.0
REVERSAL_R_MULT_2:         float = 2.0

# ── EMA Trend Continuation Engine ─────────────────────────────────────────────
EMA_FAST_PERIOD:           int   = 9
EMA_MID_PERIOD:            int   = 20
EMA_SLOW_PERIOD:           int   = 50
EMA_PULLBACK_VOL_MULT:     float = 1.2
EMA_STOP_BUFFER_PCT:       float = 0.003
EMA_R_MULT_1:              float = 1.5
EMA_R_MULT_2:              float = 3.0
EMA_CONSECUTIVE_BELOW:     int   = 2     # N bars below EMA20 → exit
TREND_CLEAN_EXIT_MIN:      float = 0.40  # exit if cleanliness drops below this

# ── Breakout-Pullback (BOPB) Engine ───────────────────────────────────────────
BOPB_LOOKBACK_BARS:        int   = 20    # prior high window
BOPB_BREAKOUT_VOL_MULT:    float = 1.5
BOPB_PULLBACK_TOLERANCE:   float = 0.005 # price within 0.5 % of prior high
BOPB_CONFIRM_VOL_MULT:     float = 1.2
BOPB_STOP_BUFFER_PCT:      float = 0.003
BOPB_R_MULT_1:             float = 1.0
BOPB_R_MULT_2:             float = 2.0
BOPB_BELOW_PRIOR_HIGH_BARS: int  = 2    # N bars below prior_high → exit

# ── Feature engineering windows ───────────────────────────────────────────────
PRICE_MOMENTUM_BARS:       int   = 10    # look-back bars for price_momentum
TREND_CLEAN_LOOKBACK_BARS: int   = 10   # bars to evaluate higher-highs/lows
ATR_PERIOD:                int   = 14
RSI_PERIOD:                int   = 14
