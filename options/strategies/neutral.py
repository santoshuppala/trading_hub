"""
Neutral option strategies: Iron Condor, Iron Butterfly.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseOptionsStrategy, OptionLeg, OptionsTradeSpec

log = logging.getLogger(__name__)


class IronCondor(BaseOptionsStrategy):
    """Sell OTM call + OTM put, buy further OTM for protection (neutral, credit)."""
    STRATEGY_TYPE = 'iron_condor'

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
        """Build an iron condor."""
        if chain_client is None:
            return None
        # TODO: Implement iron condor logic
        return None


class IronButterfly(BaseOptionsStrategy):
    """Sell ATM call + ATM put, buy OTM wings (neutral, credit, defined risk)."""
    STRATEGY_TYPE = 'iron_butterfly'

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
        """Build an iron butterfly."""
        if chain_client is None:
            return None
        # TODO: Implement iron butterfly logic
        return None
