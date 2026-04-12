"""
pop_screener/features.py — Feature engineering pipeline
========================================================
Given raw ingestion outputs (NewsData, SocialData, MarketDataSlice) this
module computes a normalised EngineeredFeatures object that every downstream
rule-based stage (screener, classifier, engines) consumes.

All arithmetic is deterministic and side-effect-free — no I/O, no randomness.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from pop_screener import config as cfg
from pop_screener.models import (
    EngineeredFeatures, FloatCategory, MarketDataSlice, NewsData,
    OHLCVBar, SocialData,
)

ET = ZoneInfo('America/New_York')


class FeatureEngineer:
    """
    Stateless feature-engineering layer.

    Typical usage
    -------------
        engineer = FeatureEngineer()
        features = engineer.compute(
            symbol='AAPL',
            news_1h=news_source.get_news('AAPL', window_hours=1),
            news_24h=news_source.get_news('AAPL', window_hours=24),
            social=social_source.get_social('AAPL'),
            market=market_source.get_market_slice('AAPL'),
            social_baseline_velocity=50.0,   # 7-day avg mention/hr
            headline_baseline_velocity=2.0,  # 7-day avg headlines/hr
        )
    """

    def compute(
        self,
        symbol: str,
        news_1h:   List[NewsData],
        news_24h:  List[NewsData],
        social:    SocialData,
        market:    MarketDataSlice,
        social_baseline_velocity:   float = 100.0,
        headline_baseline_velocity: float = 2.0,
    ) -> EngineeredFeatures:
        """
        Compute all engineered features for one symbol.

        Parameters
        ----------
        symbol                    : ticker
        news_1h                   : headlines from the last 1 hour
        news_24h                  : headlines from the last 24 hours
        social                    : social data for the last 1 hour
        market                    : intraday MarketDataSlice
        social_baseline_velocity  : 7-day average mention velocity (mentions/hr)
                                    used to normalise social_velocity
        headline_baseline_velocity: 7-day average headline rate (headlines/hr)
                                    used to normalise headline_velocity

        Returns
        -------
        EngineeredFeatures
        """
        now = datetime.now(ET)

        # ── Sentiment ────────────────────────────────────────────────────────
        sentiment_score_1h  = _mean_sentiment(news_1h)
        sentiment_score_24h = _mean_sentiment(news_24h)
        sentiment_delta     = sentiment_score_1h - sentiment_score_24h

        # ── Headline velocity ─────────────────────────────────────────────────
        # Ratio of current 1-hr count vs 7-day hourly baseline
        baseline_hl = max(headline_baseline_velocity, 0.01)
        headline_velocity = len(news_1h) / baseline_hl

        # ── Social ────────────────────────────────────────────────────────────
        baseline_sv = max(social_baseline_velocity, 0.01)
        social_velocity       = social.mention_velocity / baseline_sv
        social_sentiment_skew = social.bullish_pct - social.bearish_pct

        # ── Market-based features ─────────────────────────────────────────────
        bars = market.bars
        if not bars:
            # Degenerate case — return zeros
            return _zero_features(symbol, now, market.float_category)

        last_bar  = bars[-1]
        last_price = last_bar.close

        # RVOL: use latest value from the series
        rvol = market.rvol_series[-1] if market.rvol_series else 1.0

        # Volatility score: ATR(14) / price
        atr_value = _compute_atr(bars, period=cfg.ATR_PERIOD)
        volatility_score = atr_value / last_price if last_price > 0 else 0.0

        # Price momentum: (close_now - close_N_bars_ago) / close_N_bars_ago
        lookback = cfg.PRICE_MOMENTUM_BARS
        if len(bars) > lookback:
            prior = bars[-(lookback + 1)].close
            price_momentum = (last_price - prior) / prior if prior > 0 else 0.0
        else:
            price_momentum = 0.0

        # Gap size from MarketDataSlice
        gap_size = market.gap_size

        # Float category
        float_category = market.float_category

        # VWAP distance
        current_vwap = market.vwap_series[-1] if market.vwap_series else last_price
        vwap_distance = ((last_price - current_vwap) / current_vwap
                         if current_vwap > 0 else 0.0)

        # Trend cleanliness score
        trend_cleanliness_score = _compute_trend_cleanliness(
            bars, current_vwap, atr_value, lookback=cfg.TREND_CLEAN_LOOKBACK_BARS
        )

        return EngineeredFeatures(
            symbol=symbol,
            timestamp=now,
            sentiment_score=round(sentiment_score_1h, 4),
            sentiment_delta=round(sentiment_delta, 4),
            headline_velocity=round(headline_velocity, 4),
            social_velocity=round(social_velocity, 4),
            social_sentiment_skew=round(social_sentiment_skew, 4),
            rvol=round(rvol, 4),
            volatility_score=round(volatility_score, 6),
            price_momentum=round(price_momentum, 6),
            gap_size=round(gap_size, 6),
            float_category=float_category,
            vwap_distance=round(vwap_distance, 6),
            trend_cleanliness_score=round(trend_cleanliness_score, 4),
            atr_value=round(atr_value, 4),
            last_price=round(last_price, 4),
            current_vwap=round(current_vwap, 4),
        )


# ── Private helpers ────────────────────────────────────────────────────────────

def _mean_sentiment(news: List[NewsData]) -> float:
    """Average sentiment_score across all items; 0.0 if list is empty."""
    if not news:
        return 0.0
    return statistics.mean(n.sentiment_score for n in news)


def _compute_atr(bars: List[OHLCVBar], period: int = 14) -> float:
    """
    Compute ATR(period) using Wilder's smoothing.

    Falls back to the simple average of true ranges when fewer bars are
    available than the period.  Returns 0.0 for lists with fewer than 2 bars.
    """
    if len(bars) < 2:
        return 0.0

    true_ranges: List[float] = []
    for i in range(1, len(bars)):
        curr = bars[i]
        prev = bars[i - 1]
        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev.close),
            abs(curr.low  - prev.close),
        )
        true_ranges.append(tr)

    if not true_ranges:
        return 0.0

    n = min(period, len(true_ranges))
    # Seed with simple mean of first n values
    atr = statistics.mean(true_ranges[:n])
    # Wilder smoothing for subsequent values
    alpha = 1.0 / n
    for tr in true_ranges[n:]:
        atr = atr * (1.0 - alpha) + tr * alpha

    return atr


def _compute_trend_cleanliness(
    bars:         List[OHLCVBar],
    current_vwap: float,
    atr_value:    float,
    lookback:     int = 10,
) -> float:
    """
    Rule-based trend cleanliness score [0, 1].

    Components
    ----------
    1. Higher-highs fraction   — fraction of consecutive bar pairs where
                                  high_i > high_{i-1}  (weight 0.35)
    2. Higher-lows fraction    — same for lows          (weight 0.35)
    3. Trend/noise ratio       — (close[-1] − close[-N]) / (N × ATR)
                                  capped to [0, 1]        (weight 0.15)
    4. EMA20 proximity         — |close − EMA20| / (close)
                                  inverted, capped [0, 1]  (weight 0.15)

    Returns 0.0 if fewer than 3 bars are available.
    """
    n = min(lookback, len(bars))
    if n < 3:
        return 0.5   # neutral when insufficient data

    window = bars[-n:]

    # 1 & 2 — higher highs and higher lows
    hh = sum(
        1 for i in range(1, len(window)) if window[i].high > window[i - 1].high
    )
    hl = sum(
        1 for i in range(1, len(window)) if window[i].low > window[i - 1].low
    )
    pairs = len(window) - 1
    hh_frac = hh / pairs if pairs > 0 else 0.5
    hl_frac = hl / pairs if pairs > 0 else 0.5

    # 3 — trend / noise ratio
    if atr_value > 0 and n > 1:
        price_move = abs(window[-1].close - window[0].close)
        trend_noise = min(price_move / (n * atr_value), 1.0)
    else:
        trend_noise = 0.0

    # 4 — proximity to EMA20 (short-term EMA of closes)
    ema_period = min(20, len(bars))
    ema20 = _ema(bars, period=ema_period)
    last_close = bars[-1].close
    if ema20 > 0 and last_close > 0:
        dist_pct = abs(last_close - ema20) / last_close
        # Closer to EMA = better; 2 % tolerance = perfect
        ema_proximity = max(0.0, 1.0 - dist_pct / 0.02)
        ema_proximity = min(ema_proximity, 1.0)
    else:
        ema_proximity = 0.5

    score = (
        0.35 * hh_frac
        + 0.35 * hl_frac
        + 0.15 * trend_noise
        + 0.15 * ema_proximity
    )
    return round(min(max(score, 0.0), 1.0), 4)


def _ema(bars: List[OHLCVBar], period: int) -> float:
    """
    Compute EMA of close prices over the last *period* bars.
    Returns the last bar's close if period < 2.
    """
    if not bars:
        return 0.0
    closes = [b.close for b in bars]
    n = min(period, len(closes))
    if n < 2:
        return closes[-1]
    alpha = 2.0 / (n + 1)
    ema = closes[0]
    for c in closes[1:]:
        ema = alpha * c + (1 - alpha) * ema
    return ema


def _zero_features(
    symbol: str, ts: datetime, float_cat: FloatCategory
) -> EngineeredFeatures:
    """Return a zero-valued EngineeredFeatures for edge cases."""
    return EngineeredFeatures(
        symbol=symbol, timestamp=ts,
        sentiment_score=0.0, sentiment_delta=0.0,
        headline_velocity=0.0, social_velocity=0.0,
        social_sentiment_skew=0.0, rvol=1.0,
        volatility_score=0.0, price_momentum=0.0,
        gap_size=0.0, float_category=float_cat,
        vwap_distance=0.0, trend_cleanliness_score=0.5,
        atr_value=0.0, last_price=0.0, current_vwap=0.0,
    )
