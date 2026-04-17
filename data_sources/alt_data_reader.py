"""
V7.1 Alternative Data Reader — shared cache for all engines.

Reads data/alt_data_cache.json (written by run_data_collector.py) using
SafeStateFile shared lock. Any engine can import and query.

Usage:
    from data_sources.alt_data_reader import alt_data

    # Market regime
    fg = alt_data.fear_greed()          # {'value': 72, 'label': 'Greed', ...}
    regime = alt_data.macro_regime()    # {'regime': 'risk_on', 'vix': 15.2, ...}

    # Per-ticker
    earnings = alt_data.earnings('AAPL')  # {'days_until_earnings': 12, ...} or None
    short_float = alt_data.short_float('GME')  # 0.24 or None
    insider = alt_data.insider_activity('AAPL')  # {...} or None

    # Discovery
    new_tickers = alt_data.discovered_tickers()  # ['CRWV', 'XYZ', ...]

    # Staleness
    alt_data.is_fresh()  # True if data < 15 min old
"""
import logging
import os
from typing import Dict, List, Optional

from lifecycle.safe_state import SafeStateFile

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALT_CACHE_PATH = os.path.join(PROJECT_ROOT, 'data', 'alt_data_cache.json')


class AltDataReader:
    """Read-only interface to alternative data shared cache."""

    def __init__(self, cache_path: str = None):
        self._sf = SafeStateFile(
            cache_path or ALT_CACHE_PATH,
            max_age_seconds=900.0,  # 15 min — collector writes every 10 min
        )
        self._cached: Optional[dict] = None
        self._last_version = 0

    def _load(self) -> dict:
        """Load cache if changed since last read."""
        data, fresh, version = self._sf.read_if_changed(self._last_version)
        if data is not None:
            self._cached = data
            self._last_version = version
        return self._cached or {}

    def is_fresh(self) -> bool:
        """Check if alt data cache has been updated within 15 minutes."""
        return not self._sf.is_stale()

    # ── Market-wide data ──────────────────────────────────────────────

    def fear_greed(self) -> Optional[dict]:
        """Get Fear & Greed Index. Returns dict with 'value', 'label', etc."""
        data = self._load()
        return data.get('fear_greed')

    def fear_greed_value(self) -> Optional[int]:
        """Get Fear & Greed numeric value (0-100). None if unavailable."""
        fg = self.fear_greed()
        if fg:
            return int(fg.get('value', fg.get('score', 0)) or 0)
        return None

    def macro_regime(self) -> Optional[dict]:
        """Get FRED macro regime. Returns dict with 'regime', 'vix', 'yield_curve', etc."""
        data = self._load()
        return data.get('macro_regime')

    def vix(self) -> Optional[float]:
        """Get current VIX level. None if unavailable."""
        regime = self.macro_regime()
        if regime:
            v = regime.get('vix')
            return float(v) if v else None
        return None

    # ── Per-ticker data ───────────────────────────────────────────────

    def earnings(self, ticker: str) -> Optional[dict]:
        """Get earnings info for ticker. Returns dict with 'days_until_earnings', etc."""
        data = self._load()
        return data.get('earnings', {}).get(ticker)

    def days_until_earnings(self, ticker: str) -> Optional[int]:
        """Days until next earnings. None if unknown."""
        e = self.earnings(ticker)
        if e:
            d = e.get('days_until_earnings')
            return int(d) if d is not None else None
        return None

    def short_float(self, ticker: str) -> Optional[float]:
        """Get short float percentage. None if unavailable."""
        data = self._load()
        screener = data.get('finviz_screener', {}).get(ticker, {})
        sf = screener.get('short_float')
        return float(sf) if sf else None

    def finviz_data(self, ticker: str) -> Optional[dict]:
        """Get full Finviz screener data for ticker."""
        data = self._load()
        return data.get('finviz_screener', {}).get(ticker)

    def sec_filings(self, ticker: str) -> Optional[dict]:
        """Get SEC filing data for ticker."""
        data = self._load()
        return data.get('sec_filings', {}).get(ticker)

    def polygon_prev(self, ticker: str) -> Optional[dict]:
        """Get Polygon previous day data for ticker."""
        data = self._load()
        return data.get('polygon_prev', {}).get(ticker)

    # ── Unusual Options Flow ─────────────────────────────────────────

    def unusual_options_flow(self, ticker: str) -> Optional[dict]:
        """Get unusual options flow for ticker. Returns dict with signal, confidence, etc."""
        data = self._load()
        return data.get('unusual_options_flow', {}).get(ticker)

    def all_unusual_flow(self) -> Dict[str, dict]:
        """All unusual options flow data keyed by ticker."""
        data = self._load()
        return data.get('unusual_options_flow', {})

    # ── V8: News & Social Sentiment ────────────────────────────────────

    def news_sentiment(self, ticker: str) -> Optional[dict]:
        """V8: Get Benzinga news sentiment for ticker.
        Returns dict with headlines_1h, sentiment_delta, headline_baseline, etc."""
        data = self._load()
        return data.get('benzinga_news', {}).get(ticker)

    def social_sentiment(self, ticker: str) -> Optional[dict]:
        """V8: Get StockTwits social sentiment for ticker.
        Returns dict with mention_count, mention_velocity, bullish_pct, etc."""
        data = self._load()
        return data.get('stocktwits_social', {}).get(ticker)

    def sentiment(self, ticker: str) -> Optional[dict]:
        """V8: Combined sentiment score for a ticker.
        Returns dict with 'score' (0.0-1.0) from news + social combined."""
        news = self.news_sentiment(ticker)
        social = self.social_sentiment(ticker)
        if not news and not social:
            return None
        score = 0.0
        count = 0
        if news:
            score += abs(news.get('sentiment_delta', 0))
            count += 1
        if social:
            skew = social.get('bullish_pct', 0.5) - social.get('bearish_pct', 0.5)
            score += abs(skew)
            count += 1
        return {'score': round(score / max(count, 1), 4), 'ticker': ticker}

    def recent_headlines(self, window_minutes: int = 5) -> Optional[dict]:
        """V8: Get tickers with high recent headline counts.
        Returns {ticker: count} for tickers with >= 2 headlines."""
        data = self._load()
        news = data.get('benzinga_news', {})
        result = {}
        for ticker, info in news.items():
            count = info.get('headlines_1h', 0)
            if count >= 2:
                result[ticker] = count
        return result if result else None

    def get_all(self) -> Optional[dict]:
        """V8: Get full cache for Pop periodic scanner."""
        data = self._load()
        return data if data else None

    def polygon_prev_day(self, ticker: str) -> Optional[dict]:
        """Get Polygon previous-day data for ticker."""
        data = self._load()
        return data.get('polygon_prev', {}).get(ticker)

    # ── Discovery ─────────────────────────────────────────────────────

    def discovered_tickers(self) -> List[str]:
        """Get list of newly discovered tickers from all sources."""
        data = self._load()
        return data.get('discovered_tickers', [])

    # ── Bulk access ───────────────────────────────────────────────────

    def all_earnings(self) -> Dict[str, dict]:
        """All earnings data keyed by ticker."""
        data = self._load()
        return data.get('earnings', {})

    def all_finviz(self) -> Dict[str, dict]:
        """All Finviz screener data keyed by ticker."""
        data = self._load()
        return data.get('finviz_screener', {})

    def snapshot(self) -> dict:
        """Full cache snapshot (for dashboards/debugging)."""
        return self._load()


# Module-level singleton (like position_registry)
alt_data = AltDataReader()
