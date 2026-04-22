"""
Neutral option strategies: Iron Condor, Iron Butterfly.

Strike selection is skew-aware: uses OTM percentage from spot
instead of raw delta, so put skew doesn't create asymmetric risk.
"""
from __future__ import annotations

import logging
from typing import List, Optional

from .base import BaseOptionsStrategy, OptionLeg, OptionsTradeSpec

log = logging.getLogger(__name__)


def _mid(contract) -> float:
    """Mid-price from bid/ask."""
    return round((contract.bid + contract.ask) / 2, 2)


def _valid_quote(contract) -> bool:
    """Return True if both bid and ask are positive (liquid contract)."""
    return contract.bid > 0 and contract.ask > 0


def _filter_to_best_expiry(contracts, target_dte: int = 30) -> list:
    """Filter contracts to the single expiry closest to target_dte."""
    if not contracts:
        return []
    expiries = {}
    for c in contracts:
        expiries.setdefault(c.expiry_date, abs(c.dte - target_dte))
    best_expiry = min(expiries, key=expiries.get)
    return [c for c in contracts if c.expiry_date == best_expiry]


def _find_by_strike(
    contracts: List,
    option_type: str,
    target_strike: float,
) -> Optional[object]:
    """
    Find the contract of a given type whose strike is closest to target_strike.

    Returns None if no candidates exist for the requested option_type.
    """
    candidates = [c for c in contracts if c.option_type == option_type]
    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(c.strike - target_strike))


def _equalize_widths(
    short_put_strike: float,
    long_put_strike: float,
    short_call_strike: float,
    long_call_strike: float,
    contracts: List,
    tolerance: float = 0.20,
):
    """
    Verify put spread width ~ call spread width.

    If the difference exceeds *tolerance* (default 20%), widen the narrower
    side so both spreads have the same width.  Returns the (possibly updated)
    long_put and long_call contracts.
    """
    put_width = short_put_strike - long_put_strike
    call_width = long_call_strike - short_call_strike

    if put_width <= 0 or call_width <= 0:
        return None, None  # invalid geometry

    wider = max(put_width, call_width)
    narrower = min(put_width, call_width)

    if (wider - narrower) / narrower <= tolerance:
        # Within tolerance — look up the original contracts by strike
        lp = _find_by_strike(contracts, 'put', long_put_strike)
        lc = _find_by_strike(contracts, 'call', long_call_strike)
        return lp, lc

    # Widen the narrower side to match the wider
    target_width = wider
    if put_width < call_width:
        new_long_put_strike = short_put_strike - target_width
        lp = _find_by_strike(contracts, 'put', new_long_put_strike)
        lc = _find_by_strike(contracts, 'call', long_call_strike)
    else:
        new_long_call_strike = short_call_strike + target_width
        lp = _find_by_strike(contracts, 'put', long_put_strike)
        lc = _find_by_strike(contracts, 'call', new_long_call_strike)

    return lp, lc


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
        """
        Build an iron condor using skew-aware OTM-percentage strike placement.

        Short strikes are placed at spot +/- 5%.
        Wing width is ~2.5% of spot, giving long strikes at ~7.5% OTM.
        Spread widths are equalized if they differ by more than 20%.
        """
        if chain_client is None:
            return None
        try:
            contracts = chain_client.get_chain(ticker, min_dte, max_dte)
            if not contracts:
                log.debug("IronCondor: no contracts for %s", ticker)
                return None

            # Filter to single expiry closest to 30 DTE
            filtered = _filter_to_best_expiry(contracts, target_dte=30)
            if not filtered:
                log.debug("IronCondor: no contracts after expiry filter for %s", ticker)
                return None

            spot = underlying_price

            # --- Skew-aware strike placement ---
            put_otm_pct = 0.05    # 5% below spot
            call_otm_pct = 0.05   # 5% above spot
            wing_pct = 0.025      # 2.5% wing width

            short_put_target = spot * (1 - put_otm_pct)
            short_call_target = spot * (1 + call_otm_pct)
            wing_width = spot * wing_pct
            long_put_target = short_put_target - wing_width
            long_call_target = short_call_target + wing_width

            # Find closest available contracts to each target strike
            short_put = _find_by_strike(filtered, 'put', short_put_target)
            short_call = _find_by_strike(filtered, 'call', short_call_target)
            long_put = _find_by_strike(filtered, 'put', long_put_target)
            long_call = _find_by_strike(filtered, 'call', long_call_target)

            if any(c is None for c in [short_put, short_call, long_put, long_call]):
                log.debug("IronCondor: could not find all 4 legs for %s", ticker)
                return None

            # --- Width validation: equalize put/call spread widths ---
            long_put, long_call = _equalize_widths(
                short_put.strike,
                long_put.strike,
                short_call.strike,
                long_call.strike,
                filtered,
            )

            if long_put is None or long_call is None:
                log.debug("IronCondor: width equalization failed for %s", ticker)
                return None

            # Validate all quotes -- all 4 legs must have bid > 0
            if not all(_valid_quote(c) for c in [short_put, short_call, long_put, long_call]):
                log.debug("IronCondor: invalid bid/ask on one or more legs for %s", ticker)
                return None

            short_put_premium = _mid(short_put)
            short_call_premium = _mid(short_call)
            long_put_premium = _mid(long_put)
            long_call_premium = _mid(long_call)

            net_credit = round(
                (short_put_premium + short_call_premium) - (long_put_premium + long_call_premium),
                2,
            )
            if net_credit <= 0:
                log.debug("IronCondor: no net credit for %s (%.2f)", ticker, net_credit)
                return None

            # Spread widths
            put_spread_width = abs(short_put.strike - long_put.strike)
            call_spread_width = abs(long_call.strike - short_call.strike)
            max_spread_width = max(call_spread_width, put_spread_width)

            max_reward = round(net_credit * 100, 2)      # per-contract dollars
            max_risk = round(max_spread_width * 100 - max_reward, 2)
            net_debit_scaled = round(-net_credit * 100, 2)  # V9: scale to match close_value

            if max_risk <= 0:
                log.debug("IronCondor: non-positive max_risk for %s", ticker)
                return None

            legs = [
                OptionLeg(
                    symbol=short_put.symbol,
                    side='sell',
                    qty=1,
                    ratio=1,
                    limit_price=short_put_premium,
                ),
                OptionLeg(
                    symbol=long_put.symbol,
                    side='buy',
                    qty=1,
                    ratio=1,
                    limit_price=long_put_premium,
                ),
                OptionLeg(
                    symbol=short_call.symbol,
                    side='sell',
                    qty=1,
                    ratio=1,
                    limit_price=short_call_premium,
                ),
                OptionLeg(
                    symbol=long_call.symbol,
                    side='buy',
                    qty=1,
                    ratio=1,
                    limit_price=long_call_premium,
                ),
            ]

            reason = (
                f"Iron condor on {ticker}: sell {short_put.strike}P/{short_call.strike}C, "
                f"buy {long_put.strike}P/{long_call.strike}C for ${net_credit:.2f} credit, "
                f"max risk ${max_risk:.2f}"
            )

            log.debug("IronCondor: %s", reason)
            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=short_put.expiry_date,
                legs=legs,
                net_debit=net_debit_scaled,  # V9: scaled by 100 to match close_value
                max_risk=max_risk,
                max_reward=max_reward,
                reason=reason,
            )
        except Exception:
            log.debug("IronCondor: error building for %s", ticker, exc_info=True)
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
        """
        Build an iron butterfly with slightly-OTM body for better fills.

        Short put is placed at spot - 0.5%, short call at spot + 0.5%.
        Wings use OTM-percentage placement (~5% from spot).
        Spread widths are equalized if they differ by more than 20%.
        """
        if chain_client is None:
            return None
        try:
            contracts = chain_client.get_chain(ticker, min_dte, max_dte)
            if not contracts:
                log.debug("IronButterfly: no contracts for %s", ticker)
                return None

            # Filter to single expiry closest to 30 DTE
            filtered = _filter_to_best_expiry(contracts, target_dte=30)
            if not filtered:
                log.debug("IronButterfly: no contracts after expiry filter for %s", ticker)
                return None

            spot = underlying_price

            # --- Slightly-OTM body for better fills ---
            body_offset_pct = 0.005   # 0.5% from ATM
            wing_otm_pct = 0.05       # 5% from spot for wings

            short_put_target = spot * (1 - body_offset_pct)
            short_call_target = spot * (1 + body_offset_pct)
            wing_put_target = spot * (1 - wing_otm_pct)
            wing_call_target = spot * (1 + wing_otm_pct)

            # Find closest available contracts to each target strike
            atm_put = _find_by_strike(filtered, 'put', short_put_target)
            atm_call = _find_by_strike(filtered, 'call', short_call_target)
            wing_put = _find_by_strike(filtered, 'put', wing_put_target)
            wing_call = _find_by_strike(filtered, 'call', wing_call_target)

            if any(c is None for c in [atm_call, atm_put, wing_call, wing_put]):
                log.debug("IronButterfly: could not find all 4 legs for %s", ticker)
                return None

            # --- Width validation: equalize put/call wing widths ---
            wing_put, wing_call = _equalize_widths(
                atm_put.strike,
                wing_put.strike,
                atm_call.strike,
                wing_call.strike,
                filtered,
            )

            if wing_put is None or wing_call is None:
                log.debug("IronButterfly: width equalization failed for %s", ticker)
                return None

            # Validate all quotes
            if not all(_valid_quote(c) for c in [atm_call, atm_put, wing_call, wing_put]):
                log.debug("IronButterfly: invalid bid/ask on one or more legs for %s", ticker)
                return None

            atm_call_premium = _mid(atm_call)
            atm_put_premium = _mid(atm_put)
            wing_call_premium = _mid(wing_call)
            wing_put_premium = _mid(wing_put)

            net_credit = round(
                (atm_call_premium + atm_put_premium) - (wing_call_premium + wing_put_premium),
                2,
            )
            if net_credit <= 0:
                log.debug("IronButterfly: no net credit for %s (%.2f)", ticker, net_credit)
                return None

            # Wing width = distance from body strike to wing strike
            # Use the wider of the two wings for max risk calculation
            call_wing_width = abs(wing_call.strike - atm_call.strike)
            put_wing_width = abs(atm_put.strike - wing_put.strike)
            wing_width = max(call_wing_width, put_wing_width)

            max_reward = round(net_credit * 100, 2)   # per-contract dollars
            max_risk = round(wing_width * 100 - max_reward, 2)
            net_debit_scaled = round(-net_credit * 100, 2)  # V9: scale to match close_value

            if max_risk <= 0:
                log.debug("IronButterfly: non-positive max_risk for %s", ticker)
                return None

            legs = [
                OptionLeg(
                    symbol=atm_call.symbol,
                    side='sell',
                    qty=1,
                    ratio=1,
                    limit_price=atm_call_premium,
                ),
                OptionLeg(
                    symbol=atm_put.symbol,
                    side='sell',
                    qty=1,
                    ratio=1,
                    limit_price=atm_put_premium,
                ),
                OptionLeg(
                    symbol=wing_call.symbol,
                    side='buy',
                    qty=1,
                    ratio=1,
                    limit_price=wing_call_premium,
                ),
                OptionLeg(
                    symbol=wing_put.symbol,
                    side='buy',
                    qty=1,
                    ratio=1,
                    limit_price=wing_put_premium,
                ),
            ]

            reason = (
                f"Iron butterfly on {ticker}: sell {atm_call.strike}C/{atm_put.strike}P, "
                f"buy wings {wing_call.strike}C/{wing_put.strike}P for ${net_credit:.2f} credit, "
                f"max risk ${max_risk:.2f}"
            )

            log.debug("IronButterfly: %s", reason)
            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=atm_call.expiry_date,
                legs=legs,
                net_debit=net_debit_scaled,  # V9: scaled by 100 to match close_value
                max_risk=max_risk,
                max_reward=max_reward,
                reason=reason,
            )
        except Exception:
            log.debug("IronButterfly: error building for %s", ticker, exc_info=True)
            return None
