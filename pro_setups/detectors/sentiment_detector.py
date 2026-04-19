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

        # 4. Finviz: analyst rating + relative volume + short float
        # ACTUAL fields: analyst_rating (1-5), relative_volume, short_float, change_pct
        try:
            finviz = alt_data.finviz_data(ticker)
            if finviz:
                # Analyst rating: 1.0=Strong Buy, 5.0=Strong Sell
                rating = float(finviz.get('analyst_rating', 3.0) or 3.0)
                bullish_analyst = max(0, (3.0 - rating) / 2.0)  # 1.0→1.0, 3.0→0, 5.0→-1.0
                # Relative volume (>1.5 = unusual activity)
                rvol = float(finviz.get('relative_volume', 1.0) or 1.0)
                rvol_score = min(max(rvol - 1.0, 0) / 2.0, 1.0)  # 1→0, 3→1
                # Combined
                score = max(bullish_analyst, rvol_score)
                if score > 0.1:
                    scores['finviz_insider'] = score
                    if bullish_analyst > 0.3:
                        direction_votes['finviz'] = 'long'
                    elif bullish_analyst < -0.3:
                        direction_votes['finviz'] = 'short'
        except Exception:
            pass

        # 5. SEC EDGAR: filing activity (higher count = more corporate activity)
        # ACTUAL fields: count, days
        try:
            sec = alt_data.sec_filings(ticker) if hasattr(alt_data, 'sec_filings') else None
            if sec:
                filing_count = int(sec.get('count', 0) or 0)
                days = int(sec.get('days', 30) or 30)
                # >5 filings in 30 days = high activity (M&A, offerings, insider reports)
                if filing_count > 5:
                    scores['sec_filing'] = min(filing_count / 15.0, 1.0)
                    # Can't determine direction from filing count alone
        except Exception:
            pass

        # 6. Yahoo: earnings proximity (days_until_earnings)
        # ACTUAL fields: days_until_earnings, next_earnings_date, earnings_before_open
        try:
            yahoo = alt_data.earnings(ticker)
            if yahoo:
                days = yahoo.get('days_until_earnings')
                if days is not None and isinstance(days, (int, float)) and days <= 7:
                    # Earnings within 7 days = high attention, potential catalyst
                    scores['yahoo_recommendation'] = max(0.3, 1.0 - days / 7.0)
                    # Can't determine direction from proximity alone
        except Exception:
            pass

        # 7. Polygon: prior-day price change as momentum signal
        # ACTUAL fields: change_pct, volume, vwap, close
        try:
            polygon = alt_data.polygon_prev_day(ticker) if hasattr(alt_data, 'polygon_prev_day') else None
            if polygon:
                change = float(polygon.get('change_pct', 0) or 0)
                volume = float(polygon.get('volume', 0) or 0)
                # Big prev-day move = momentum continuation potential
                if abs(change) > 2.0 and volume > 500000:
                    scores['polygon_volume'] = min(abs(change) / 10.0, 1.0)
                    direction_votes['polygon'] = 'long' if change > 0 else 'short'
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
