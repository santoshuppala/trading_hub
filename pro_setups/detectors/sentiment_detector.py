"""
V8 SentimentDetector — composite sentiment from ALL 8 alt data sources.

Reads from alt_data_cache.json (written by Data Collector every 10 min).
Does NOT make live API calls — purely cache-based, fast per-BAR evaluation.

Dual weight profiles: stocks (UOF 0.25) vs options (UOF 0.35).
F&G is a multiplier on the composite, not a weighted component.
"""
import logging
import time
from typing import Optional

import pandas as pd

from .base import BaseDetector, DetectorSignal

log = logging.getLogger(__name__)


class SentimentDetector(BaseDetector):
    """
    Composite sentiment detector using all available alt data sources.
    Returns a DetectorSignal with weighted composite strength and
    majority-vote direction from available sources.
    """

    name = 'sentiment'
    MIN_BARS = 5

    # Source weights — stocks vs options profiles
    STOCK_WEIGHTS = {
        'benzinga_sentiment':   0.20,
        'stocktwits_social':    0.15,
        'unusual_options_flow': 0.25,
        'finviz_insider':       0.05,
        'sec_filing':           0.05,
        'yahoo_recommendation': 0.10,
        'polygon_volume':       0.10,
    }

    OPTIONS_WEIGHTS = {
        'benzinga_sentiment':   0.15,
        'stocktwits_social':    0.10,
        'unusual_options_flow': 0.35,
        'finviz_insider':       0.05,
        'sec_filing':           0.05,
        'yahoo_recommendation': 0.15,
        'polygon_volume':       0.05,
    }

    def __init__(self, mode: str = 'stocks'):
        self._weights = self.OPTIONS_WEIGHTS if mode == 'options' else self.STOCK_WEIGHTS
        self._cache_hash = None  # skip recomputation if cache unchanged

    def _detect(
        self,
        ticker: str,
        df: pd.DataFrame,
        rvol_df: Optional[pd.DataFrame] = None,
    ) -> DetectorSignal:
        try:
            from data_sources.alt_data_reader import alt_data
        except ImportError:
            return DetectorSignal.no_signal()

        scores = {}
        direction_votes = {}

        # 1. Benzinga: headline sentiment delta (1h vs 24h)
        try:
            news = alt_data.news_sentiment(ticker)
            if news:
                delta = news.get('sentiment_delta', 0)
                scores['benzinga_sentiment'] = min(abs(delta), 1.0)
                if delta > 0.05:
                    direction_votes['benzinga'] = 'long'
                elif delta < -0.05:
                    direction_votes['benzinga'] = 'short'
        except Exception:
            pass

        # 2. StockTwits: social velocity + bullish/bearish skew
        try:
            social = alt_data.social_sentiment(ticker)
            if social:
                baseline = max(social.get('baseline', 100), 1)
                velocity = social.get('mention_velocity', 0) / baseline
                skew = social.get('bullish_pct', 0.5) - social.get('bearish_pct', 0.5)
                scores['stocktwits_social'] = min(velocity * abs(skew), 1.0)
                if skew > 0.1:
                    direction_votes['stocktwits'] = 'long'
                elif skew < -0.1:
                    direction_votes['stocktwits'] = 'short'
        except Exception:
            pass

        # 3. Unusual Options Flow: call/put ratio, sweep direction
        try:
            uof = alt_data.unusual_options_flow(ticker)
            if uof:
                # V8: Relative significance (premium / avg daily options volume)
                sweep_premium = uof.get('total_premium', 0)
                daily_opt_vol = uof.get('avg_daily_options_premium', 0)
                if daily_opt_vol <= 0:
                    # Hybrid: fall back to stock volume × 3% proxy
                    daily_stock_vol = uof.get('avg_daily_dollar_volume', 0)
                    daily_opt_vol = daily_stock_vol * 0.03

                if daily_opt_vol > 0:
                    significance = min(sweep_premium / daily_opt_vol, 1.0)
                else:
                    significance = 0.0

                # Boost for repeat sweeps
                if uof.get('repeat_sweeps', 0) >= 2:
                    significance = min(significance * 1.3, 1.0)

                scores['unusual_options_flow'] = significance
                call_ratio = uof.get('call_ratio', 0.5)
                if call_ratio > 0.6:
                    direction_votes['uof'] = 'long'
                elif call_ratio < 0.4:
                    direction_votes['uof'] = 'short'
        except Exception:
            pass

        # 4. Finviz: insider buys, analyst upgrades
        try:
            finviz = alt_data.short_float(ticker)
            if finviz:
                insider_buy = 1.0 if finviz.get('insider_buying') else 0.0
                analyst_upgrade = 1.0 if finviz.get('analyst_upgrade') else 0.0
                score = 0.5 * insider_buy + 0.5 * analyst_upgrade
                if score > 0:
                    scores['finviz_insider'] = score
                    direction_votes['finviz'] = 'long'
        except Exception:
            pass

        # 5. SEC EDGAR: insider filing activity
        try:
            sec = alt_data.sec_filings(ticker) if hasattr(alt_data, 'sec_filings') else None
            if sec:
                buy_count = sec.get('insider_buys_30d', 0)
                sell_count = sec.get('insider_sells_30d', 0)
                total = buy_count + sell_count
                if total > 0:
                    ratio = buy_count / total
                    scores['sec_filing'] = abs(ratio - 0.5) * 2
                    direction_votes['sec'] = 'long' if ratio > 0.5 else 'short'
        except Exception:
            pass

        # 6. Yahoo: earnings surprises, recommendation changes
        try:
            yahoo = alt_data.earnings(ticker)
            if yahoo:
                surprise = yahoo.get('earnings_surprise_pct', 0)
                if surprise:
                    scores['yahoo_recommendation'] = min(abs(surprise) / 10, 1.0)
                    direction_votes['yahoo'] = 'long' if surprise > 0 else 'short'
        except Exception:
            pass

        # 7. Polygon: prior-day volume anomaly
        try:
            polygon = alt_data.polygon_prev_day(ticker) if hasattr(alt_data, 'polygon_prev_day') else None
            if polygon:
                vol_ratio = polygon.get('volume_vs_avg', 1.0)
                if vol_ratio > 1:
                    scores['polygon_volume'] = min((vol_ratio - 1.0) / 3.0, 1.0)
        except Exception:
            pass

        # Composite score (weighted average of available sources)
        total_weight = sum(self._weights.get(k, 0) for k in scores)
        if total_weight == 0:
            return DetectorSignal.no_signal()

        composite = sum(scores.get(k, 0) * self._weights.get(k, 0)
                        for k in self._weights) / total_weight

        # F&G multiplier (market-wide baseline)
        try:
            fg = alt_data.fear_greed()
            if fg:
                fg_value = fg.get('value', 50) / 100.0
                fg_mult = 0.7 + fg_value * 0.6  # 0.7 (extreme fear) to 1.3 (extreme greed)
                composite *= fg_mult
                composite = min(composite, 1.0)
        except Exception:
            pass

        # Direction: majority vote
        long_votes = sum(1 for v in direction_votes.values() if v == 'long')
        short_votes = sum(1 for v in direction_votes.values() if v == 'short')
        if long_votes > short_votes:
            direction = 'long'
        elif short_votes > long_votes:
            direction = 'short'
        else:
            direction = 'neutral'

        fired = composite > 0.25  # 25% composite = meaningful signal

        return DetectorSignal(
            fired=fired,
            direction=direction,
            strength=min(composite, 1.0),
            metadata={
                'per_source': scores,
                'direction_votes': direction_votes,
                'composite': round(composite, 4),
                'sources_available': len(scores),
            },
        )
