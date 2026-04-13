"""
Alpaca options chain client for fetching and filtering option contracts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

log = logging.getLogger(__name__)


@dataclass
class OptionContract:
    """Single option contract returned from Alpaca options chain."""
    symbol:       str     # OCC symbol e.g. AAPL240119C00180000
    underlying:   str     # stock symbol
    expiry_date:  date    # expiration date
    strike:       float   # strike price
    option_type:  str     # 'call' | 'put'
    delta:        float   # delta value
    gamma:        float   # gamma value
    theta:        float   # theta value
    vega:         float   # vega value
    iv:           float   # implied volatility
    bid:          float   # bid price
    ask:          float   # ask price
    dte:          int     # days to expiry


class AlpacaOptionChainClient:
    """
    Wraps Alpaca's options data API to fetch chains and find contracts.

    Uses APCA_OPTIONS_KEY credentials (not the main trading key).
    Fetches via alpaca-py SDK.
    """

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        # TODO: Initialize alpaca-py OptionHistoricalDataClient
        log.info(f"[AlpacaOptionChainClient] initialized with API key ***{api_key[-4:]}")

    def get_chain(
        self,
        ticker: str,
        min_dte: int = 20,
        max_dte: int = 45,
    ) -> List[OptionContract]:
        """
        Fetch all option contracts for ticker within the DTE window.

        Returns empty list on API error (caller must handle gracefully).
        """
        # TODO: Implement via alpaca-py OptionChainRequest
        return []

    def find_by_delta(
        self,
        contracts: List[OptionContract],
        option_type: str,         # 'call' | 'put'
        target_delta: float,      # e.g. 0.35
        tolerance: float = 0.05,
    ) -> Optional[OptionContract]:
        """
        Select the contract whose delta is closest to target_delta within tolerance.

        Returns None if no suitable contract found.
        """
        if not contracts:
            return None

        candidates = [
            c for c in contracts
            if c.option_type == option_type
            and abs(c.delta - target_delta) <= tolerance
        ]
        if not candidates:
            return None

        # Return closest match
        return min(candidates, key=lambda c: abs(c.delta - target_delta))

    def find_atm(
        self,
        contracts: List[OptionContract],
        option_type: str,
        underlying_price: float,
    ) -> Optional[OptionContract]:
        """Select the contract with strike closest to underlying_price."""
        if not contracts:
            return None

        candidates = [c for c in contracts if c.option_type == option_type]
        if not candidates:
            return None

        return min(candidates, key=lambda c: abs(c.strike - underlying_price))

    def find_otm(
        self,
        contracts: List[OptionContract],
        option_type: str,
        underlying_price: float,
        otm_pct: float = 0.03,     # how far OTM as fraction of price
    ) -> Optional[OptionContract]:
        """
        Select contract approximately otm_pct% OTM from underlying_price.

        For calls: strike = underlying * (1 + otm_pct).
        For puts:  strike = underlying * (1 - otm_pct).
        """
        if not contracts:
            return None

        if option_type == 'call':
            target_strike = underlying_price * (1 + otm_pct)
        elif option_type == 'put':
            target_strike = underlying_price * (1 - otm_pct)
        else:
            return None

        candidates = [c for c in contracts if c.option_type == option_type]
        if not candidates:
            return None

        return min(candidates, key=lambda c: abs(c.strike - target_strike))

    def find_leaps(
        self,
        ticker: str,
        option_type: str,
        target_delta: float,
        min_dte: int = 300,
    ) -> Optional[OptionContract]:
        """Fetch a LEAPS contract for calendar/diagonal spread long leg."""
        # TODO: Implement LEAPS fetch
        return None
