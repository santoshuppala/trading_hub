"""
Base classes and contracts for options strategy builders.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger(__name__)


@dataclass
class OptionLeg:
    """Single leg of a multi-leg options order."""
    symbol:      str    # OCC option symbol e.g. AAPL240119C00180000
    side:        str    # 'buy' | 'sell'
    qty:         int    # number of contracts
    ratio:       int    # leg ratio in combo (1 for single, 2 for iron fly body)
    limit_price: float  # mid-price for execution (debit legs) or 0 for market (credit)


@dataclass
class OptionsTradeSpec:
    """
    Fully specified options trade — output of BaseOptionsStrategy.build().
    Passed to AlpacaOptionsBroker for execution.
    """
    strategy_type:  str             # OptionStrategyType.value
    ticker:         str             # underlying symbol
    expiry_date:    str             # 'YYYY-MM-DD'
    legs:           List[OptionLeg] # all legs (buy + sell)
    net_debit:      float           # positive=cost, negative=credit received
    max_risk:       float           # maximum dollar loss (always > 0)
    max_reward:     float           # maximum dollar gain (always > 0)
    reason:         str             # human-readable description


class BaseOptionsStrategy(ABC):
    """
    Contract for all 13 options strategy builders.

    Each concrete strategy implements build(), which selects option contracts
    from the chain and returns an OptionsTradeSpec (or None if no suitable
    contracts exist).
    """

    STRATEGY_TYPE: str = ''   # must be set by subclass to OptionStrategyType.value

    @abstractmethod
    def build(
        self,
        ticker:           str,
        underlying_price: float,
        chain_client,                  # AlpacaOptionChainClient
        min_dte:          int,
        max_dte:          int,
        delta_directional: float = 0.35,
        delta_spread:      float = 0.175,
    ) -> Optional[OptionsTradeSpec]:
        """
        Fetch the option chain and build the strategy's legs.

        Returns OptionsTradeSpec on success, None if no suitable contracts
        exist or chain is too illiquid.

        Args:
            ticker: stock symbol (e.g., 'AAPL')
            underlying_price: current bid/ask mid price
            chain_client: AlpacaOptionChainClient instance
            min_dte: minimum days-to-expiry (e.g., 20)
            max_dte: maximum days-to-expiry (e.g., 45)
            delta_directional: target delta for directional legs (0.30-0.40)
            delta_spread: target delta for spread legs (0.15-0.20)
        """
        ...
