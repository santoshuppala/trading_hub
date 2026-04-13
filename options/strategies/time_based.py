"""
Time-based option strategies: Calendar Spread, Diagonal Spread.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseOptionsStrategy, OptionLeg, OptionsTradeSpec

log = logging.getLogger(__name__)


class CalendarSpread(BaseOptionsStrategy):
    """Sell near-term, buy far-term call (same strike, horizontal spread)."""
    STRATEGY_TYPE = 'calendar_spread'

    def build(
        self,
        ticker: str,
        underlying_price: float,
        chain_client,
        min_dte: int,
        max_dte: int,
        delta_directional: float = 0.35,
        delta_spread: float = 0.175,
    ) -> Optional[OptionsTradeSpec]:
        """Build a calendar spread."""
        if chain_client is None:
            return None
        # TODO: Implement calendar spread logic
        return None


class DiagonalSpread(BaseOptionsStrategy):
    """Buy LEAPS call, sell near-term OTM call (diagonal spread, synthetic covered call)."""
    STRATEGY_TYPE = 'diagonal_spread'

    def build(
        self,
        ticker: str,
        underlying_price: float,
        chain_client,
        min_dte: int,
        max_dte: int,
        delta_directional: float = 0.35,
        delta_spread: float = 0.175,
    ) -> Optional[OptionsTradeSpec]:
        """Build a diagonal spread."""
        if chain_client is None:
            return None
        # TODO: Implement diagonal spread logic
        return None
