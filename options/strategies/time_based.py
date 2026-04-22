"""
Time-based option strategies: Calendar Spread, Diagonal Spread.
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


class CalendarSpread(BaseOptionsStrategy):
    """Sell near-term, buy far-term call (same strike, horizontal spread)."""
    STRATEGY_TYPE = 'calendar_spread'

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
        """Build a calendar spread.

        Sell near-term ATM call, buy far-term ATM call at the same strike.
        Profits from theta differential (near decays faster than far).
        """
        if chain_client is None:
            return None

        try:
            # --- Fetch two separate chains ---
            near_chain = chain_client.get_chain(ticker, min_dte, min_dte + 10)
            far_chain = chain_client.get_chain(ticker, max_dte - 5, max_dte + 10)

            if not near_chain or not far_chain:
                log.debug("CalendarSpread: empty chain for %s", ticker)
                return None

            # --- Find ATM calls on each chain ---
            near_atm = chain_client.find_atm(near_chain, 'call', underlying_price)
            far_atm = chain_client.find_atm(far_chain, 'call', underlying_price)

            if near_atm is None or far_atm is None:
                log.debug("CalendarSpread: could not find ATM calls for %s", ticker)
                return None

            # --- Match strikes: find far-chain contract closest to near ATM strike ---
            far_calls = [c for c in far_chain if c.option_type == 'call']
            if not far_calls:
                log.debug("CalendarSpread: no far-chain calls for %s", ticker)
                return None

            far_leg = min(far_calls, key=lambda c: abs(c.strike - near_atm.strike))

            # Verify strikes are within $1 of each other
            if abs(far_leg.strike - near_atm.strike) > 1.0:
                log.debug(
                    "CalendarSpread: strike mismatch %.2f vs %.2f for %s",
                    near_atm.strike, far_leg.strike, ticker,
                )
                return None

            # --- Validate quotes ---
            if not _valid_quotes(near_atm, far_leg):
                log.debug("CalendarSpread: zero bid/ask for %s", ticker)
                return None

            # --- Compute prices ---
            near_premium = _mid(near_atm)
            far_premium = _mid(far_leg)
            net_debit = far_premium - near_premium  # always positive: far costs more

            if net_debit <= 0:
                log.debug("CalendarSpread: non-positive debit %.4f for %s", net_debit, ticker)
                return None

            max_risk = net_debit * 100
            max_reward = near_premium * 100  # approximate, at near expiry if ATM

            legs = [
                OptionLeg(
                    symbol=near_atm.symbol,
                    side='SELL',
                    qty=1,
                    ratio=1,
                    limit_price=near_premium,
                ),
                OptionLeg(
                    symbol=far_leg.symbol,
                    side='BUY',
                    qty=1,
                    ratio=1,
                    limit_price=far_premium,
                ),
            ]

            log.debug(
                "CalendarSpread built: %s strike=%.2f near_exp=%s far_exp=%s debit=%.2f",
                ticker, near_atm.strike, near_atm.expiry_date, far_leg.expiry_date, net_debit,
            )

            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=far_leg.expiry_date,
                legs=legs,
                net_debit=round(net_debit * 100, 2),  # V9: scale to per-contract $
                max_risk=round(max_risk, 2),
                max_reward=round(max_reward, 2),
                reason=(
                    f"Calendar spread on {ticker} @ {near_atm.strike:.0f} strike: "
                    f"sell {near_atm.expiry_date} / buy {far_leg.expiry_date}, "
                    f"debit ${net_debit:.2f}, theta differential play"
                ),
            )

        except Exception:
            log.debug("CalendarSpread: error building for %s", ticker, exc_info=True)
            return None


class DiagonalSpread(BaseOptionsStrategy):
    """Buy LEAPS call, sell near-term OTM call (diagonal spread, synthetic covered call)."""
    STRATEGY_TYPE = 'diagonal_spread'

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
        """Build a diagonal spread (poor man's covered call).

        Buy deep ITM LEAPS call (delta ~0.80, 300+ DTE).
        Sell near-term OTM call (delta ~0.30) to collect premium.
        """
        if chain_client is None:
            return None

        try:
            # --- Buy deep ITM LEAPS call ---
            leaps = chain_client.find_leaps(ticker, 'call', target_delta=0.80, min_dte=300)
            if leaps is None:
                log.debug("DiagonalSpread: no LEAPS found for %s", ticker)
                return None

            # --- Sell near-term OTM call ---
            near_chain = chain_client.get_chain(ticker, min_dte, min_dte + 10)
            if not near_chain:
                log.debug("DiagonalSpread: empty near-term chain for %s", ticker)
                return None

            short_call = chain_client.find_by_delta(near_chain, 'call', target_delta=0.30, tolerance=0.05)
            if short_call is None:
                log.debug("DiagonalSpread: no ~0.30 delta call found for %s", ticker)
                return None

            # --- Validate quotes ---
            if not _valid_quotes(leaps, short_call):
                log.debug("DiagonalSpread: zero bid/ask for %s", ticker)
                return None

            # --- Compute prices ---
            leaps_premium = _mid(leaps)
            short_premium = _mid(short_call)
            net_debit = leaps_premium - short_premium

            if net_debit <= 0:
                log.debug("DiagonalSpread: non-positive debit %.4f for %s", net_debit, ticker)
                return None

            max_risk = net_debit * 100
            max_reward = (short_call.strike - leaps.strike + short_premium) * 100

            if max_reward <= 0:
                log.debug("DiagonalSpread: non-positive max reward for %s", ticker)
                return None

            legs = [
                OptionLeg(
                    symbol=leaps.symbol,
                    side='BUY',
                    qty=1,
                    ratio=1,
                    limit_price=leaps_premium,
                ),
                OptionLeg(
                    symbol=short_call.symbol,
                    side='SELL',
                    qty=1,
                    ratio=1,
                    limit_price=short_premium,
                ),
            ]

            log.debug(
                "DiagonalSpread built: %s LEAPS=%.2f/%s short=%.2f/%s debit=%.2f",
                ticker, leaps.strike, leaps.expiry_date,
                short_call.strike, short_call.expiry_date, net_debit,
            )

            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=leaps.expiry_date,
                legs=legs,
                net_debit=round(net_debit * 100, 2),  # V9: scale to per-contract $
                max_risk=round(max_risk, 2),
                max_reward=round(max_reward, 2),
                reason=(
                    f"Diagonal spread on {ticker}: "
                    f"buy {leaps.strike:.0f} LEAPS {leaps.expiry_date} / "
                    f"sell {short_call.strike:.0f} {short_call.expiry_date}, "
                    f"debit ${net_debit:.2f}, synthetic covered call"
                ),
            )

        except Exception:
            log.debug("DiagonalSpread: error building for %s", ticker, exc_info=True)
            return None
