"""
pop_screener/models.py — Data models for the pop-stock multi-strategy subsystem
================================================================================
All dataclasses and enums used as the contracts between pipeline stages.

Stage flow:
  ingestion  → NewsData / SocialData / MarketDataSlice
  features   → EngineeredFeatures
  screener   → PopCandidate
  classifier → StrategyAssignment
  router     → EntrySignal / ExitSignal
  engine     → TradeCandidate  (assembled result)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


# ── Enums ──────────────────────────────────────────────────────────────────────

class StrategyType(str, Enum):
    """Six deterministic strategy types the classifier can assign.
    VWAP Reclaim delegates to the T4 SignalAnalyzer (monitor/signals.py)."""
    VWAP_RECLAIM           = 'VWAP_RECLAIM'
    ORB                    = 'ORB'
    HALT_RESUME_BREAKOUT   = 'HALT_RESUME_BREAKOUT'
    PARABOLIC_REVERSAL     = 'PARABOLIC_REVERSAL'
    EMA_TREND_CONTINUATION = 'EMA_TREND_CONTINUATION'
    BREAKOUT_PULLBACK      = 'BREAKOUT_PULLBACK'
    MOMENTUM_ENTRY         = 'MOMENTUM_ENTRY'

    def __str__(self) -> str:
        return self.value


class PopReason(str, Enum):
    """Why a symbol was promoted to a pop candidate."""
    MODERATE_NEWS    = 'MODERATE_NEWS'
    HIGH_IMPACT_NEWS = 'HIGH_IMPACT_NEWS'
    SENTIMENT_POP    = 'SENTIMENT_POP'
    UNUSUAL_VOLUME   = 'UNUSUAL_VOLUME'
    LOW_FLOAT        = 'LOW_FLOAT'
    EARNINGS         = 'EARNINGS'

    def __str__(self) -> str:
        return self.value


class FloatCategory(str, Enum):
    """Share-float size bucket used in screener and classifier rules."""
    LOW_FLOAT = 'LOW_FLOAT'
    NORMAL    = 'NORMAL'

    def __str__(self) -> str:
        return self.value


class ExitReason(str, Enum):
    """Reason an exit signal was generated."""
    STOP            = 'STOP'
    TARGET_1        = 'TARGET_1'
    TARGET_2        = 'TARGET_2'
    VWAP_BREAK      = 'VWAP_BREAK'
    RSI_OVERBOUGHT  = 'RSI_OVERBOUGHT'
    EOD             = 'EOD'
    MOMENTUM_FADE   = 'MOMENTUM_FADE'
    TREND_BREAK     = 'TREND_BREAK'
    REVERSAL        = 'REVERSAL'
    BELOW_EMA       = 'BELOW_EMA'
    BELOW_PRIOR_HIGH = 'BELOW_PRIOR_HIGH'

    def __str__(self) -> str:
        return self.value


# ── Ingestion layer models ─────────────────────────────────────────────────────

@dataclass
class NewsData:
    """
    Single news headline with sentiment metadata.

    Produced by : NewsSentimentSource.get_news()
    Consumed by : FeatureEngineer (headline_velocity, sentiment_score)
    """
    timestamp:       datetime
    headline:        str
    sentiment_score: float   # [-1.0, +1.0]; positive = bullish
    source:          str     # e.g. 'reuters', 'benzinga', 'mock'


@dataclass
class SocialData:
    """
    Aggregated social-media mention metrics for one symbol over a window.

    Produced by : SocialSentimentSource.get_social()
    Consumed by : FeatureEngineer (social_velocity, social_sentiment_skew)
    """
    symbol:           str
    mention_count:    int    # total mentions in the window
    mention_velocity: float  # mentions per hour (current window)
    bullish_pct:      float  # fraction of mentions that are bullish [0, 1]
    bearish_pct:      float  # fraction that are bearish [0, 1]
    newest_message_time: str = ''  # ISO timestamp of newest message
    oldest_message_time: str = ''  # ISO timestamp of oldest message in batch


@dataclass
class OHLCVBar:
    """One intraday 1-minute OHLCV bar."""
    timestamp: datetime
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    int


@dataclass
class MarketDataSlice:
    """
    Intraday market data for one symbol, enriched with derived series.

    Produced by : MarketBehaviorSource.get_market_slice()
    Consumed by : FeatureEngineer, strategy engines

    Fields
    ------
    bars         : chronological list of 1-min OHLCV bars for today
    vwap_series  : one VWAP value per bar (same length as bars)
    rvol_series  : one RVOL ratio per bar (same length as bars)
    gap_size     : (today_open − prior_close) / prior_close; positive = gap-up
    float_category : LOW_FLOAT or NORMAL
    earnings_flag  : True if the symbol has an earnings event today / after-hours
    """
    symbol:         str
    bars:           List[OHLCVBar]
    vwap_series:    List[float]
    rvol_series:    List[float]
    gap_size:       float
    float_category: FloatCategory
    earnings_flag:  bool
    prior_close:    float = 0.0   # used to compute gap_size; 0 if unknown


# ── Feature layer model ────────────────────────────────────────────────────────

@dataclass
class EngineeredFeatures:
    """
    All engineered features for one symbol at one point in time.

    Produced by : FeatureEngineer.compute()
    Consumed by : PopScreener, StrategyClassifier, strategy engines

    Feature descriptions
    --------------------
    sentiment_score         Average sentiment of recent headlines [-1, +1]
    sentiment_delta         sentiment_score(1 h) − sentiment_score(24 h)
    headline_velocity       (headlines in last 1 h) / (avg headlines/h over 7 d)
    social_velocity         current mention_count / avg_mention_count over 7 d
    social_sentiment_skew   bullish_pct − bearish_pct
    rvol                    current_volume / avg_volume at this time of day
    volatility_score        ATR(14) / last_price  (normalised volatility)
    price_momentum          (close_now − close_N_bars_ago) / close_N_bars_ago
    gap_size                (today_open − prior_close) / prior_close
    float_category          LOW_FLOAT or NORMAL
    vwap_distance           (last_price − vwap) / vwap; negative = below VWAP
    trend_cleanliness_score Rule-based score [0, 1] — higher = cleaner trend
    atr_value               Raw ATR value in dollars
    last_price              Most recent close price
    current_vwap            Current VWAP value
    """
    symbol:                 str
    timestamp:              datetime
    sentiment_score:        float
    sentiment_delta:        float
    headline_velocity:      float
    social_velocity:        float
    social_sentiment_skew:  float
    rvol:                   float
    volatility_score:       float
    price_momentum:         float
    gap_size:               float
    float_category:         FloatCategory
    vwap_distance:          float
    trend_cleanliness_score: float
    atr_value:              float
    last_price:             float
    current_vwap:           float
    earnings_flag:          bool = False


# ── Screener layer model ───────────────────────────────────────────────────────

@dataclass
class PopCandidate:
    """
    A symbol that passed at least one pop-detection rule.

    Produced by : PopScreener.screen()
    Consumed by : StrategyClassifier.classify()
    """
    symbol:     str
    features:   EngineeredFeatures
    pop_reason: PopReason
    raw_scores: Dict[str, float]   # key metrics that triggered the rule


# ── Classifier layer model ─────────────────────────────────────────────────────

@dataclass
class StrategyAssignment:
    """
    Deterministic mapping of a pop candidate to one primary strategy plus
    optional secondary strategies.

    Produced by : StrategyClassifier.classify()
    Consumed by : StrategyRouter

    Fields
    ------
    primary_strategy         Best-fit strategy for this pop type
    secondary_strategies     Ordered list of fallback / complementary strategies
    vwap_compatibility_score [0, 1] — 0 means VWAP_RECLAIM is explicitly disallowed
    strategy_confidence      Overall classifier confidence [0, 1]
    notes                    Human-readable explanation of the assignment decision
    """
    symbol:                   str
    primary_strategy:         StrategyType
    secondary_strategies:     List[StrategyType]
    vwap_compatibility_score: float   # [0, 1]
    strategy_confidence:      float   # [0, 1]
    notes:                    str


# ── Signal models ──────────────────────────────────────────────────────────────

@dataclass
class EntrySignal:
    """
    Entry signal emitted by a strategy engine.

    Produced by : any strategy engine
    Consumed by : PopStrategyEngine → EventBus (POP_SIGNAL + SIGNAL)

    Fields
    ------
    side        : 'buy' (long) or 'sell' (short — PARABOLIC_REVERSAL only)
    entry_price : reference price at signal time (close of trigger bar)
    stop_loss   : hard stop loss price
    target_1    : first profit target (partial exit, ~50 % of position)
    target_2    : full profit target
    strategy_type : which engine generated this signal
    size_hint   : optional share-count hint (None = let RiskEngine size)
    metadata    : strategy-specific diagnostics (atr, rvol, indicator values)
    """
    symbol:        str
    side:          str            # 'buy' | 'sell'
    entry_price:   float
    stop_loss:     float
    target_1:      float
    target_2:      float
    strategy_type: StrategyType
    size_hint:     Optional[int] = None
    metadata:      Dict[str, float] = field(default_factory=dict)


@dataclass
class ExitSignal:
    """
    Exit signal emitted by a strategy engine when it detects an exit condition
    on an *already open* position.

    Produced by : any strategy engine's check_exits() method
    Consumed by : PopStrategyEngine → SIGNAL(action=sell_*) → RiskEngine passthrough

    Note: Exit signals are informational; actual exit orders flow through the
    existing PositionManager which monitors stop/target prices.  These signals
    are used for immediate forced-exit conditions (EOD, reversal, etc.).
    """
    symbol:        str
    side:          str            # 'sell' for longs, 'buy' to cover shorts
    exit_price:    float          # reference price (current close)
    reason:        ExitReason
    strategy_type: StrategyType
    metadata:      Dict[str, float] = field(default_factory=dict)


# ── Assembled result ───────────────────────────────────────────────────────────

@dataclass
class TradeCandidate:
    """
    Full pipeline result for one pop candidate — pop detection through signal
    generation.  Used by the demo script and for audit logging.
    """
    pop_candidate:       PopCandidate
    strategy_assignment: StrategyAssignment
    entry_signals:       List[EntrySignal]
    exit_signals:        List[ExitSignal]
