"""
Complex option strategies: Butterfly Spread.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseOptionsStrategy, OptionLeg, OptionsTradeSpec

log = logging.getLogger(__name__)


class ButterflySpread(BaseOptionsStrategy):
    """Buy 1 ITM, sell 2 ATM, buy 1 OTM (equal wing width, defined risk, low premium)."""
    STRATEGY_TYPE = 'butterfly_spread'

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
        """Build a butterfly spread."""
        if chain_client is None:
            return None
        # TODO: Implement butterfly spread logic
        return None
