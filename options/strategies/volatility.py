"""
Volatility option strategies: Long Straddle, Long Strangle.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseOptionsStrategy, OptionLeg, OptionsTradeSpec

log = logging.getLogger(__name__)


class LongStraddle(BaseOptionsStrategy):
    """Buy ATM call + ATM put (same strike, neutral volatility play)."""
    STRATEGY_TYPE = 'long_straddle'

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
        """Build a long straddle."""
        if chain_client is None:
            return None
        # TODO: Implement long straddle logic
        return None


class LongStrangle(BaseOptionsStrategy):
    """Buy OTM call + OTM put (different strikes, cheaper volatility play)."""
    STRATEGY_TYPE = 'long_strangle'

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
        """Build a long strangle."""
        if chain_client is None:
            return None
        # TODO: Implement long strangle logic
        return None
