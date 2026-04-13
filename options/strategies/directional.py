"""
Directional option strategies: LongCall, LongPut.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseOptionsStrategy, OptionLeg, OptionsTradeSpec

log = logging.getLogger(__name__)


class LongCall(BaseOptionsStrategy):
    """Buy a single call at delta ~0.35 (directional bullish)."""
    STRATEGY_TYPE = 'long_call'

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
        """Buy a single call at target delta."""
        if chain_client is None:
            return None

        try:
            contracts = chain_client.get_chain(ticker, min_dte, max_dte)
            if not contracts:
                log.debug(f"[LongCall] no contracts for {ticker} {min_dte}-{max_dte} DTE")
                return None

            call = chain_client.find_by_delta(
                contracts, 'call', delta_directional, tolerance=0.05
            )
            if not call:
                log.debug(f"[LongCall] no call at delta {delta_directional} for {ticker}")
                return None

            premium = (call.ask + call.bid) / 2.0
            max_risk = premium * 100  # cost of 1 contract
            max_reward = max(underlying_price - call.strike - premium, 0) * 100 * 10  # capped at 10x

            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=call.expiry_date.isoformat(),
                legs=[
                    OptionLeg(
                        symbol=call.symbol,
                        side='buy',
                        qty=1,
                        ratio=1,
                        limit_price=premium,
                    )
                ],
                net_debit=premium * 100,
                max_risk=max_risk,
                max_reward=max_reward,
                reason=f"Long call {call.symbol} delta={call.delta:.2f}",
            )
        except Exception as e:
            log.warning(f"[LongCall] error building for {ticker}: {e}")
            return None


class LongPut(BaseOptionsStrategy):
    """Buy a single put at delta ~0.35 (directional bearish)."""
    STRATEGY_TYPE = 'long_put'

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
        """Buy a single put at target delta."""
        if chain_client is None:
            return None

        try:
            contracts = chain_client.get_chain(ticker, min_dte, max_dte)
            if not contracts:
                log.debug(f"[LongPut] no contracts for {ticker} {min_dte}-{max_dte} DTE")
                return None

            put = chain_client.find_by_delta(
                contracts, 'put', delta_directional, tolerance=0.05
            )
            if not put:
                log.debug(f"[LongPut] no put at delta {delta_directional} for {ticker}")
                return None

            premium = (put.ask + put.bid) / 2.0
            max_risk = premium * 100  # cost of 1 contract
            max_reward = max(put.strike - underlying_price - premium, 0) * 100 * 10  # capped at 10x

            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=put.expiry_date.isoformat(),
                legs=[
                    OptionLeg(
                        symbol=put.symbol,
                        side='buy',
                        qty=1,
                        ratio=1,
                        limit_price=premium,
                    )
                ],
                net_debit=premium * 100,
                max_risk=max_risk,
                max_reward=max_reward,
                reason=f"Long put {put.symbol} delta={put.delta:.2f}",
            )
        except Exception as e:
            log.warning(f"[LongPut] error building for {ticker}: {e}")
            return None
