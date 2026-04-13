"""
Vertical spread option strategies: Bull/Bear Call Spreads, Bull/Bear Put Spreads.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseOptionsStrategy, OptionLeg, OptionsTradeSpec

log = logging.getLogger(__name__)


class BullCallSpread(BaseOptionsStrategy):
    """Buy lower strike call, sell higher strike call (debit spread, bullish)."""
    STRATEGY_TYPE = 'bull_call_spread'

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
        """Build a bull call spread."""
        if chain_client is None:
            return None
        # TODO: Implement bull call spread logic
        return None


class BearPutSpread(BaseOptionsStrategy):
    """Buy higher strike put, sell lower strike put (debit spread, bearish)."""
    STRATEGY_TYPE = 'bear_put_spread'

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
        """Build a bear put spread."""
        if chain_client is None:
            return None
        # TODO: Implement bear put spread logic
        return None


class BullPutSpread(BaseOptionsStrategy):
    """Sell higher strike put, buy lower strike put (credit spread, bullish)."""
    STRATEGY_TYPE = 'bull_put_spread'

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
        """Build a bull put spread."""
        if chain_client is None:
            return None
        # TODO: Implement bull put spread logic
        return None


class BearCallSpread(BaseOptionsStrategy):
    """Sell lower strike call, buy higher strike call (credit spread, bearish)."""
    STRATEGY_TYPE = 'bear_call_spread'

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
        """Build a bear call spread."""
        if chain_client is None:
            return None
        # TODO: Implement bear call spread logic
        return None
