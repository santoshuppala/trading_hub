"""Aggregate Greeks tracking across all open options positions."""
import logging
import time
from collections import defaultdict

log = logging.getLogger(__name__)


class PortfolioGreeksTracker:
    """Track aggregate Greeks across all open options positions."""

    def __init__(self, chain_client=None):
        self._chain = chain_client
        self._cache = {}        # {contract_symbol: {delta, gamma, theta, vega, ts}}
        self._cache_ttl = 60    # seconds
        self._last_snapshot = {}

    def update(self, positions: list) -> dict:
        """Fetch fresh Greeks for all open options positions.

        Args:
            positions: list of options position objects/dicts with legs info

        Returns dict:
            total_delta, total_gamma, total_theta, total_vega
            delta_dollars (dollar exposure per $1 move in underlying)
            per_position: list of per-position Greeks breakdowns
        """
        if not positions:
            self._last_snapshot = self._empty_snapshot()
            return self._last_snapshot

        total_delta = 0.0
        total_gamma = 0.0
        total_theta = 0.0
        total_vega = 0.0
        per_position = []

        for pos in positions:
            ticker = getattr(pos, 'ticker', None) or pos.get('ticker', '?')
            legs = getattr(pos, 'legs', None) or pos.get('legs', [])
            underlying_price = getattr(pos, 'underlying_price', None) or pos.get('underlying_price', 0)

            pos_delta = 0.0
            pos_gamma = 0.0
            pos_theta = 0.0
            pos_vega = 0.0

            for leg in legs:
                symbol = leg.get('symbol', '') if isinstance(leg, dict) else getattr(leg, 'symbol', '')
                qty = leg.get('qty', 1) if isinstance(leg, dict) else getattr(leg, 'qty', 1)
                side = leg.get('side', 'long') if isinstance(leg, dict) else getattr(leg, 'side', 'long')
                sign = 1 if side == 'long' else -1

                greeks = self._get_cached_greeks(symbol)
                if greeks:
                    pos_delta += greeks['delta'] * qty * sign
                    pos_gamma += abs(greeks['gamma'] * qty)
                    pos_theta += greeks['theta'] * qty * sign
                    pos_vega += greeks['vega'] * qty * sign

            total_delta += pos_delta
            total_gamma += pos_gamma
            total_theta += pos_theta
            total_vega += pos_vega

            per_position.append({
                'ticker': ticker,
                'delta': round(pos_delta, 4),
                'gamma': round(pos_gamma, 4),
                'theta': round(pos_theta, 4),
                'vega': round(pos_vega, 4),
                'underlying_price': underlying_price,
            })

        self._last_snapshot = {
            'total_delta': round(total_delta, 4),
            'total_gamma': round(total_gamma, 4),
            'total_theta': round(total_theta, 4),
            'total_vega': round(total_vega, 4),
            'delta_dollars': round(total_delta * 100, 2),  # per $1 move
            'per_position': per_position,
            'updated_at': time.time(),
        }
        return self._last_snapshot

    def snapshot(self) -> dict:
        """Return last computed Greeks without fetching."""
        return self._last_snapshot or self._empty_snapshot()

    def _get_cached_greeks(self, symbol: str) -> dict | None:
        """Get Greeks from cache, fetch if expired."""
        now = time.time()
        if symbol in self._cache and (now - self._cache[symbol].get('ts', 0)) < self._cache_ttl:
            return self._cache[symbol]

        # Try to fetch fresh Greeks
        if self._chain is None:
            return None
        try:
            greeks = self._chain.get_greeks_for_symbol(symbol)
            if greeks:
                self._cache[symbol] = {**greeks, 'ts': now}
                return self._cache[symbol]
        except Exception:
            pass
        return self._cache.get(symbol)

    def _empty_snapshot(self):
        return {
            'total_delta': 0.0, 'total_gamma': 0.0,
            'total_theta': 0.0, 'total_vega': 0.0,
            'delta_dollars': 0.0, 'per_position': [],
            'updated_at': 0,
        }
