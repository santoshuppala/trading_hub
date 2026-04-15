"""
CNN Fear & Greed Index — market sentiment gauge (0-100).

No API key required. Scrapes CNN's public endpoint.

Values:
  0-25:   Extreme Fear (contrarian buy signal)
  25-45:  Fear
  45-55:  Neutral
  55-75:  Greed
  75-100: Extreme Greed (contrarian sell signal / don't sell premium)

Usage:
    from data_sources.fear_greed import FearGreedSource
    fg = FearGreedSource()
    current = fg.get_current()
    print(f"Fear & Greed: {current['value']} ({current['label']})")
"""
import json
import logging
import time
from typing import Optional, Dict

import requests

log = logging.getLogger(__name__)

_CACHE_TTL = 1800  # 30 minutes (index updates ~hourly)
_API_URL = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata'


class FearGreedSource:
    """CNN Fear & Greed Index — 0 (extreme fear) to 100 (extreme greed)."""

    def __init__(self):
        self._cache: Optional[Dict] = None
        self._cache_time: float = 0

    def get_current(self) -> Optional[Dict]:
        """Get current Fear & Greed Index value.

        Returns dict:
            value: float (0-100)
            label: str ('Extreme Fear', 'Fear', 'Neutral', 'Greed', 'Extreme Greed')
            previous_close: float
            one_week_ago: float
            one_month_ago: float
            one_year_ago: float
            updated_at: str (ISO timestamp)
        """
        now = time.time()
        if self._cache and (now - self._cache_time) < _CACHE_TTL:
            return self._cache

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 TradingHub/1.0',
                'Accept': 'application/json',
            }
            resp = requests.get(_API_URL, headers=headers, timeout=10)

            if resp.status_code != 200:
                log.warning("[FearGreed] HTTP %d", resp.status_code)
                return self._cache

            data = resp.json()
            fg_data = data.get('fear_and_greed', {})

            value = float(fg_data.get('score', 50))
            previous = float(fg_data.get('previous_close', value))
            week_ago = float(data.get('fear_and_greed_historical', {}).get('one_week_ago', {}).get('score', value))
            month_ago = float(data.get('fear_and_greed_historical', {}).get('one_month_ago', {}).get('score', value))
            year_ago = float(data.get('fear_and_greed_historical', {}).get('one_year_ago', {}).get('score', value))

            label = self._value_to_label(value)

            result = {
                'value': round(value, 1),
                'label': label,
                'previous_close': round(previous, 1),
                'one_week_ago': round(week_ago, 1),
                'one_month_ago': round(month_ago, 1),
                'one_year_ago': round(year_ago, 1),
                'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
            }

            self._cache = result
            self._cache_time = now
            log.info("[FearGreed] Index: %.0f (%s)", value, label)
            return result

        except Exception as exc:
            log.warning("[FearGreed] Fetch failed: %s", exc)
            return self._cache

    def is_extreme_fear(self) -> bool:
        """Returns True if index <= 25 (contrarian buy signal)."""
        current = self.get_current()
        return current is not None and current['value'] <= 25

    def is_extreme_greed(self) -> bool:
        """Returns True if index >= 75 (contrarian sell signal)."""
        current = self.get_current()
        return current is not None and current['value'] >= 75

    def regime(self) -> str:
        """Return current market regime based on Fear & Greed.

        Returns: 'extreme_fear', 'fear', 'neutral', 'greed', 'extreme_greed'
        """
        current = self.get_current()
        if current is None:
            return 'neutral'
        return current['label'].lower().replace(' ', '_')

    @staticmethod
    def _value_to_label(value: float) -> str:
        if value <= 25:
            return 'Extreme Fear'
        elif value <= 45:
            return 'Fear'
        elif value <= 55:
            return 'Neutral'
        elif value <= 75:
            return 'Greed'
        else:
            return 'Extreme Greed'
