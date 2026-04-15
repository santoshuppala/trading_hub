"""
Compute per-ticker sentiment baselines from historical NewsDataSnapshot
and SocialDataSnapshot events in the event_store.

Replaces the hardcoded headline_baseline=2.0 and social_baseline=100.0
with real per-ticker averages computed from the last N days of data.
"""
import logging
from collections import defaultdict
from typing import Dict, Optional

log = logging.getLogger(__name__)


class SentimentBaselineEngine:
    """Compute per-ticker headline and social velocity baselines from DB history."""

    # Defaults when no history exists for a ticker
    DEFAULT_HEADLINE_BASELINE = 2.0
    DEFAULT_SOCIAL_BASELINE = 100.0

    def __init__(self, lookback_days: int = 7):
        self._lookback_days = lookback_days
        # {ticker: float} — avg headlines_1h per snapshot
        self._headline_baselines: Dict[str, float] = {}
        # {ticker: float} — avg mention_velocity per snapshot
        self._social_baselines: Dict[str, float] = {}
        self._loaded = False

    def load_from_db(self, db_url: str = None) -> None:
        """Query event_store for historical baselines. Call once at session start."""
        if db_url is None:
            try:
                from config import DATABASE_URL
                db_url = DATABASE_URL
            except ImportError:
                log.warning("[SentimentBaseline] No DATABASE_URL — using defaults")
                return

        try:
            import psycopg2
            conn = psycopg2.connect(db_url, connect_timeout=5)
            cur = conn.cursor()

            # Headline baselines: avg headlines_1h per ticker over last N days
            cur.execute("""
                SELECT
                    event_payload->>'ticker' as ticker,
                    AVG((event_payload->>'headlines_1h')::float) as avg_headlines,
                    COUNT(*) as sample_count
                FROM event_store
                WHERE event_type = 'NewsDataSnapshot'
                  AND event_time >= NOW() - INTERVAL '%s days'
                GROUP BY event_payload->>'ticker'
                HAVING COUNT(*) >= 5
            """, (self._lookback_days,))

            for ticker, avg_hl, count in cur.fetchall():
                if avg_hl is not None and avg_hl > 0:
                    self._headline_baselines[ticker] = round(float(avg_hl), 4)

            # Social baselines: avg mention_velocity per ticker
            cur.execute("""
                SELECT
                    event_payload->>'ticker' as ticker,
                    AVG((event_payload->>'mention_velocity')::float) as avg_velocity,
                    COUNT(*) as sample_count
                FROM event_store
                WHERE event_type = 'SocialDataSnapshot'
                  AND event_time >= NOW() - INTERVAL '%s days'
                GROUP BY event_payload->>'ticker'
                HAVING COUNT(*) >= 5
            """, (self._lookback_days,))

            for ticker, avg_vel, count in cur.fetchall():
                if avg_vel is not None and avg_vel > 0:
                    self._social_baselines[ticker] = round(float(avg_vel), 4)

            conn.close()
            self._loaded = True
            log.info(
                "[SentimentBaseline] Loaded baselines: %d tickers with headline data, "
                "%d tickers with social data (lookback=%d days)",
                len(self._headline_baselines),
                len(self._social_baselines),
                self._lookback_days,
            )

        except Exception as exc:
            log.warning("[SentimentBaseline] DB query failed (using defaults): %s", exc)

    def headline_baseline(self, ticker: str) -> float:
        """Get headline velocity baseline for a ticker."""
        return self._headline_baselines.get(ticker, self.DEFAULT_HEADLINE_BASELINE)

    def social_baseline(self, ticker: str) -> float:
        """Get social mention velocity baseline for a ticker."""
        return self._social_baselines.get(ticker, self.DEFAULT_SOCIAL_BASELINE)

    def update_intraday(self, ticker: str, headlines_1h: int, mention_velocity: float) -> None:
        """Update baselines with today's data (running average).

        Called during the session to build up baseline data for new tickers
        that have no DB history yet. Uses exponential moving average so
        recent observations weigh more.
        """
        alpha = 0.1  # EMA smoothing factor

        if headlines_1h > 0:
            current = self._headline_baselines.get(ticker)
            if current is not None:
                self._headline_baselines[ticker] = round(current * (1 - alpha) + headlines_1h * alpha, 4)
            else:
                self._headline_baselines[ticker] = float(headlines_1h)

        if mention_velocity > 0:
            current = self._social_baselines.get(ticker)
            if current is not None:
                self._social_baselines[ticker] = round(current * (1 - alpha) + mention_velocity * alpha, 4)
            else:
                self._social_baselines[ticker] = mention_velocity

    def summary(self) -> dict:
        """Return baseline stats for logging."""
        return {
            'headline_tickers': len(self._headline_baselines),
            'social_tickers': len(self._social_baselines),
            'loaded_from_db': self._loaded,
            'lookback_days': self._lookback_days,
        }
