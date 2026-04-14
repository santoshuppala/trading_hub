"""
Volatility option strategies: Long Straddle, Long Strangle.
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseOptionsStrategy, OptionLeg, OptionsTradeSpec

log = logging.getLogger(__name__)


def _mid(contract) -> float:
    """Mid-price from bid/ask."""
    return round((contract.bid + contract.ask) / 2, 2)


def _valid_quote(contract) -> bool:
    """Return True if both bid and ask are positive (liquid contract)."""
    return contract.bid > 0 and contract.ask > 0


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
        """Build a long straddle: buy ATM call + buy ATM put."""
        if chain_client is None:
            return None
        try:
            contracts = chain_client.get_chain(ticker, min_dte, max_dte)
            if not contracts:
                log.debug("LongStraddle: no contracts for %s", ticker)
                return None

            # Find ATM call and put
            atm_call = chain_client.find_atm(contracts, 'call', underlying_price)
            atm_put = chain_client.find_atm(contracts, 'put', underlying_price)

            if atm_call is None or atm_put is None:
                log.debug("LongStraddle: could not find ATM call/put for %s", ticker)
                return None

            # Validate quotes
            if not _valid_quote(atm_call) or not _valid_quote(atm_put):
                log.debug("LongStraddle: invalid bid/ask on ATM contracts for %s", ticker)
                return None

            # Ensure same expiry — prefer the one closest to 30 DTE
            # If expiries differ, filter chain to the closer-to-30-DTE expiry and retry
            if atm_call.expiry_date != atm_put.expiry_date:
                target_dte = 30
                call_diff = abs(atm_call.dte - target_dte)
                put_diff = abs(atm_put.dte - target_dte)
                chosen_expiry = atm_call.expiry_date if call_diff <= put_diff else atm_put.expiry_date
                filtered = [c for c in contracts if c.expiry_date == chosen_expiry]
                atm_call = chain_client.find_atm(filtered, 'call', underlying_price)
                atm_put = chain_client.find_atm(filtered, 'put', underlying_price)
                if atm_call is None or atm_put is None:
                    log.debug("LongStraddle: no same-expiry ATM pair for %s", ticker)
                    return None
                if not _valid_quote(atm_call) or not _valid_quote(atm_put):
                    log.debug("LongStraddle: invalid bid/ask after expiry filter for %s", ticker)
                    return None

            call_premium = _mid(atm_call)
            put_premium = _mid(atm_put)
            net_debit = round(call_premium + put_premium, 2)
            max_risk = round(net_debit * 100, 2)
            max_reward = round(net_debit * 5 * 100, 2)  # unlimited, estimate 5x

            legs = [
                OptionLeg(
                    symbol=atm_call.symbol,
                    side='buy',
                    qty=1,
                    ratio=1,
                    limit_price=call_premium,
                ),
                OptionLeg(
                    symbol=atm_put.symbol,
                    side='buy',
                    qty=1,
                    ratio=1,
                    limit_price=put_premium,
                ),
            ]

            reason = (
                f"Long straddle on {ticker} @ strike {atm_call.strike}: "
                f"buy ATM call + ATM put for ${net_debit:.2f} debit, "
                f"break-even at {atm_call.strike - net_debit:.2f} / "
                f"{atm_call.strike + net_debit:.2f}"
            )

            log.debug("LongStraddle: %s", reason)
            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=atm_call.expiry_date,
                legs=legs,
                net_debit=net_debit,
                max_risk=max_risk,
                max_reward=max_reward,
                reason=reason,
            )
        except Exception:
            log.debug("LongStraddle: error building for %s", ticker, exc_info=True)
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
        """Build a long strangle: buy OTM call + buy OTM put (3% OTM)."""
        if chain_client is None:
            return None
        try:
            contracts = chain_client.get_chain(ticker, min_dte, max_dte)
            if not contracts:
                log.debug("LongStrangle: no contracts for %s", ticker)
                return None

            # Find 3% OTM call and put
            otm_call = chain_client.find_otm(contracts, 'call', underlying_price, otm_pct=0.03)
            otm_put = chain_client.find_otm(contracts, 'put', underlying_price, otm_pct=0.03)

            if otm_call is None or otm_put is None:
                log.debug("LongStrangle: could not find OTM call/put for %s", ticker)
                return None

            if not _valid_quote(otm_call) or not _valid_quote(otm_put):
                log.debug("LongStrangle: invalid bid/ask on OTM contracts for %s", ticker)
                return None

            # Ensure same expiry
            if otm_call.expiry_date != otm_put.expiry_date:
                target_dte = 30
                call_diff = abs(otm_call.dte - target_dte)
                put_diff = abs(otm_put.dte - target_dte)
                chosen_expiry = otm_call.expiry_date if call_diff <= put_diff else otm_put.expiry_date
                filtered = [c for c in contracts if c.expiry_date == chosen_expiry]
                otm_call = chain_client.find_otm(filtered, 'call', underlying_price, otm_pct=0.03)
                otm_put = chain_client.find_otm(filtered, 'put', underlying_price, otm_pct=0.03)
                if otm_call is None or otm_put is None:
                    log.debug("LongStrangle: no same-expiry OTM pair for %s", ticker)
                    return None
                if not _valid_quote(otm_call) or not _valid_quote(otm_put):
                    log.debug("LongStrangle: invalid bid/ask after expiry filter for %s", ticker)
                    return None

            call_premium = _mid(otm_call)
            put_premium = _mid(otm_put)
            net_debit = round(call_premium + put_premium, 2)
            max_risk = round(net_debit * 100, 2)
            max_reward = round(net_debit * 5 * 100, 2)  # unlimited, estimate 5x

            legs = [
                OptionLeg(
                    symbol=otm_call.symbol,
                    side='buy',
                    qty=1,
                    ratio=1,
                    limit_price=call_premium,
                ),
                OptionLeg(
                    symbol=otm_put.symbol,
                    side='buy',
                    qty=1,
                    ratio=1,
                    limit_price=put_premium,
                ),
            ]

            reason = (
                f"Long strangle on {ticker}: buy {otm_call.strike}C + "
                f"{otm_put.strike}P for ${net_debit:.2f} debit (3% OTM)"
            )

            log.debug("LongStrangle: %s", reason)
            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=otm_call.expiry_date,
                legs=legs,
                net_debit=net_debit,
                max_risk=max_risk,
                max_reward=max_reward,
                reason=reason,
            )
        except Exception:
            log.debug("LongStrangle: error building for %s", ticker, exc_info=True)
            return None
