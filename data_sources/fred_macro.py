"""
FRED (Federal Reserve) data source — macro regime detection.

Requires: FRED_API_KEY (free at https://fred.stlouisfed.org/docs/api/api_key.html)

Provides:
  - Interest rate regime (hiking/cutting/holding)
  - Yield curve status (normal/inverted/flat)
  - VIX level and regime
  - Economic indicators (unemployment, CPI)
  - Macro regime classification for strategy filtering

Usage:
    from data_sources.fred_macro import FREDMacroSource
    fred = FREDMacroSource(api_key='your_key')
    regime = fred.macro_regime()
    print(f"Market regime: {regime['regime']}")
"""
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

_CACHE_TTL = 14400  # 4 hours (macro data updates daily at most)
_BASE_URL = 'https://api.stlouisfed.org/fred/series/observations'


@dataclass
class MacroRegime:
    """Market macro regime classification."""
    regime: str              # 'risk_on', 'risk_off', 'neutral', 'crisis'
    fed_rate: float          # current Fed Funds Rate
    fed_trend: str           # 'hiking', 'cutting', 'holding'
    yield_curve: float       # 10Y-2Y spread (negative = inverted)
    yield_status: str        # 'normal', 'inverted', 'flat'
    vix: float               # current VIX
    vix_regime: str          # 'low' (<15), 'normal' (15-25), 'elevated' (25-35), 'crisis' (>35)
    updated_at: str


class FREDMacroSource:
    """FRED API data source for macro regime detection."""

    def __init__(self, api_key: str = None):
        self._api_key = api_key or os.getenv('FRED_API_KEY', '')
        self._session = requests.Session()
        self._cache: Dict[str, tuple] = {}

        if not self._api_key:
            log.info("[FRED] No API key — macro data unavailable. "
                     "Get free key: https://fred.stlouisfed.org/docs/api/api_key.html")

    def _fetch_series(self, series_id: str, limit: int = 5) -> Optional[List[Dict]]:
        """Fetch recent observations for a FRED series."""
        if not self._api_key:
            return None

        cache_key = f'{series_id}_{limit}'
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < _CACHE_TTL:
            return cached[1]

        try:
            resp = self._session.get(_BASE_URL, params={
                'series_id': series_id,
                'api_key': self._api_key,
                'file_type': 'json',
                'sort_order': 'desc',
                'limit': limit,
            }, timeout=10)

            if resp.status_code != 200:
                log.warning("[FRED] HTTP %d for %s", resp.status_code, series_id)
                return None

            data = resp.json()
            observations = data.get('observations', [])
            # Filter out missing values
            valid = [o for o in observations if o.get('value', '.') != '.']
            self._cache[cache_key] = (time.time(), valid)
            return valid

        except Exception as exc:
            log.warning("[FRED] Fetch failed for %s: %s", series_id, exc)
            return None

    def fed_funds_rate(self) -> Optional[float]:
        """Get current Federal Funds Rate."""
        obs = self._fetch_series('DFF', limit=3)
        if obs:
            return float(obs[0]['value'])
        return None

    def yield_curve_spread(self) -> Optional[float]:
        """Get 10Y-2Y Treasury spread. Negative = inverted."""
        obs = self._fetch_series('T10Y2Y', limit=3)
        if obs:
            return float(obs[0]['value'])
        return None

    def vix(self) -> Optional[float]:
        """Get current VIX level."""
        obs = self._fetch_series('VIXCLS', limit=3)
        if obs:
            return float(obs[0]['value'])
        return None

    def unemployment_rate(self) -> Optional[float]:
        """Get current unemployment rate."""
        obs = self._fetch_series('UNRATE', limit=2)
        if obs:
            return float(obs[0]['value'])
        return None

    def cpi_yoy(self) -> Optional[float]:
        """Get year-over-year CPI change."""
        obs = self._fetch_series('CPIAUCSL', limit=13)
        if obs and len(obs) >= 2:
            current = float(obs[0]['value'])
            year_ago = float(obs[-1]['value'])
            return round((current - year_ago) / year_ago * 100, 2)
        return None

    def fed_trend(self) -> str:
        """Determine if the Fed is hiking, cutting, or holding."""
        obs = self._fetch_series('DFF', limit=5)
        if not obs or len(obs) < 3:
            return 'unknown'

        rates = [float(o['value']) for o in obs]
        # Compare current vs 3 observations ago
        if rates[0] > rates[-1] + 0.1:
            return 'hiking'
        elif rates[0] < rates[-1] - 0.1:
            return 'cutting'
        else:
            return 'holding'

    def macro_regime(self) -> MacroRegime:
        """Classify current macro regime for strategy filtering.

        Regime classification:
          risk_on:  low VIX + normal yield curve + cutting/holding
          risk_off: elevated VIX OR inverted yield curve
          neutral:  moderate conditions
          crisis:   VIX > 35 OR multiple red flags
        """
        rate = self.fed_funds_rate() or 5.0
        spread = self.yield_curve_spread() or 0.5
        vix_val = self.vix() or 20.0
        trend = self.fed_trend()

        # VIX regime
        if vix_val < 15:
            vix_regime = 'low'
        elif vix_val < 25:
            vix_regime = 'normal'
        elif vix_val < 35:
            vix_regime = 'elevated'
        else:
            vix_regime = 'crisis'

        # Yield curve status
        if spread > 0.25:
            yield_status = 'normal'
        elif spread > -0.1:
            yield_status = 'flat'
        else:
            yield_status = 'inverted'

        # Overall regime
        if vix_regime == 'crisis':
            regime = 'crisis'
        elif vix_regime == 'elevated' or yield_status == 'inverted':
            regime = 'risk_off'
        elif vix_regime == 'low' and yield_status == 'normal' and trend in ('cutting', 'holding'):
            regime = 'risk_on'
        else:
            regime = 'neutral'

        result = MacroRegime(
            regime=regime,
            fed_rate=rate,
            fed_trend=trend,
            yield_curve=spread,
            yield_status=yield_status,
            vix=vix_val,
            vix_regime=vix_regime,
            updated_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
        )

        log.info("[FRED] Macro regime: %s | rate=%.2f (%s) | yield=%.2f (%s) | VIX=%.1f (%s)",
                 regime, rate, trend, spread, yield_status, vix_val, vix_regime)
        return result

    def should_reduce_risk(self) -> bool:
        """Quick check: should we reduce position sizes / avoid new entries?"""
        regime = self.macro_regime()
        return regime.regime in ('risk_off', 'crisis')
