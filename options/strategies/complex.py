"""
Complex option strategies: Butterfly Spread.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseOptionsStrategy, OptionLeg, OptionsTradeSpec

log = logging.getLogger(__name__)


def _mid(contract) -> float:
    """Mid-price from bid/ask."""
    return (contract.bid + contract.ask) / 2.0


def _valid_quotes(*contracts) -> bool:
    """Return True if every contract has non-zero bid AND ask."""
    return all(c.bid > 0 and c.ask > 0 for c in contracts)


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
        """Build a butterfly spread.

        Buy 1 ITM call (lower), sell 2 ATM calls (middle), buy 1 OTM call (upper).
        All same expiry, equidistant wings. Profits from pinning at middle strike.
        """
        if chain_client is None:
            return None

        try:
            chain = chain_client.get_chain(ticker, min_dte, max_dte)
            if not chain:
                log.debug("ButterflySpread: empty chain for %s", ticker)
                return None

            # --- Find middle (ATM) strike ---
            middle = chain_client.find_atm(chain, 'call', underlying_price)
            if middle is None:
                log.debug("ButterflySpread: no ATM call for %s", ticker)
                return None

            # --- Determine wing width (2-5% of underlying) ---
            # Start at 3% and find the best available strikes
            target_width = underlying_price * 0.03
            calls = [c for c in chain
                     if c.option_type == 'call' and c.expiry_date == middle.expiry_date]
            if len(calls) < 3:
                log.debug("ButterflySpread: not enough calls for %s", ticker)
                return None

            # --- Find lower wing (ITM, below middle strike) ---
            lower_target = middle.strike - target_width
            lower_candidates = [c for c in calls if c.strike < middle.strike]
            if not lower_candidates:
                log.debug("ButterflySpread: no lower wing candidates for %s", ticker)
                return None
            lower = min(lower_candidates, key=lambda c: abs(c.strike - lower_target))

            # --- Find upper wing (OTM, above middle strike) ---
            upper_target = middle.strike + target_width
            upper_candidates = [c for c in calls if c.strike > middle.strike]
            if not upper_candidates:
                log.debug("ButterflySpread: no upper wing candidates for %s", ticker)
                return None
            upper = min(upper_candidates, key=lambda c: abs(c.strike - upper_target))

            # --- Verify wing symmetry: both wings within $1 of each other ---
            lower_width = middle.strike - lower.strike
            upper_width = upper.strike - middle.strike
            if abs(upper_width - lower_width) > 1.0:
                log.debug(
                    "ButterflySpread: asymmetric wings %.2f / %.2f for %s",
                    lower_width, upper_width, ticker,
                )
                return None

            wing_width = lower_width  # use lower for calculations

            # --- Validate quotes ---
            if not _valid_quotes(lower, middle, upper):
                log.debug("ButterflySpread: zero bid/ask for %s", ticker)
                return None

            # --- Compute prices ---
            lower_premium = _mid(lower)
            middle_premium = _mid(middle)
            upper_premium = _mid(upper)

            net_debit = (lower_premium + upper_premium) - 2.0 * middle_premium

            if net_debit <= 0:
                log.debug("ButterflySpread: non-positive debit %.4f for %s", net_debit, ticker)
                return None

            max_risk = net_debit * 100
            max_reward = (wing_width - net_debit) * 100

            if max_reward <= 0:
                log.debug("ButterflySpread: non-positive max reward for %s", ticker)
                return None

            legs = [
                OptionLeg(
                    symbol=lower.symbol,
                    side='BUY',
                    qty=1,
                    ratio=1,
                    limit_price=lower_premium,
                ),
                OptionLeg(
                    symbol=middle.symbol,
                    side='SELL',
                    qty=2,
                    ratio=2,
                    limit_price=middle_premium,
                ),
                OptionLeg(
                    symbol=upper.symbol,
                    side='BUY',
                    qty=1,
                    ratio=1,
                    limit_price=upper_premium,
                ),
            ]

            log.debug(
                "ButterflySpread built: %s strikes=%.0f/%.0f/%.0f exp=%s debit=%.2f",
                ticker, lower.strike, middle.strike, upper.strike,
                middle.expiry_date, net_debit,
            )

            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=middle.expiry_date,
                legs=legs,
                net_debit=round(net_debit * 100, 2),  # V9: scale to per-contract $
                max_risk=round(max_risk, 2),
                max_reward=round(max_reward, 2),
                reason=(
                    f"Butterfly on {ticker}: "
                    f"{lower.strike:.0f}/{middle.strike:.0f}/{upper.strike:.0f} "
                    f"exp {middle.expiry_date}, debit ${net_debit:.2f}, "
                    f"max reward ${max_reward:.0f} if pins at {middle.strike:.0f}"
                ),
            )

        except Exception:
            log.debug("ButterflySpread: error building for %s", ticker, exc_info=True)
            return None
