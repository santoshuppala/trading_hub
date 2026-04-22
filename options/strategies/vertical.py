"""
Vertical spread option strategies: Bull/Bear Call Spreads, Bull/Bear Put Spreads.

Debit spreads:
  - BullCallSpread: buy lower-strike call, sell higher-strike call
  - BearPutSpread:  buy higher-strike put, sell lower-strike put

Credit spreads:
  - BullPutSpread:  sell higher-strike put, buy lower-strike put (protection)
  - BearCallSpread: sell lower-strike call, buy higher-strike call (protection)
"""
from __future__ import annotations

import logging
from typing import Optional

from .base import BaseOptionsStrategy, OptionLeg, OptionsTradeSpec

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MIN_SPREAD_WIDTH = 1.0
MAX_SPREAD_WIDTH = 20.0


def _mid(contract) -> float:
    """Mid-price from bid/ask."""
    return round((contract.bid + contract.ask) / 2, 2)


def _is_liquid(contract) -> bool:
    """Return True if the contract has a valid bid and ask."""
    return contract.bid > 0 and contract.ask > 0


# ---------------------------------------------------------------------------
# 1. BullCallSpread  (debit, bullish)
# ---------------------------------------------------------------------------

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
        try:
            if chain_client is None:
                return None

            contracts = chain_client.get_chain(ticker, min_dte, max_dte)
            if not contracts:
                log.debug("BullCallSpread(%s): empty chain", ticker)
                return None

            # Long call — higher delta (closer to ATM)
            long_leg = chain_client.find_by_delta(contracts, 'call', delta_directional)
            # Short call — lower delta (further OTM, higher strike)
            short_leg = chain_client.find_by_delta(contracts, 'call', delta_spread)

            if long_leg is None or short_leg is None:
                log.debug("BullCallSpread(%s): could not find legs", ticker)
                return None

            # Liquidity check
            if not _is_liquid(long_leg) or not _is_liquid(short_leg):
                log.debug("BullCallSpread(%s): illiquid legs", ticker)
                return None

            # Long call must have the lower strike
            if long_leg.strike >= short_leg.strike:
                log.debug("BullCallSpread(%s): long strike %.2f >= short strike %.2f",
                          ticker, long_leg.strike, short_leg.strike)
                return None

            spread_width = short_leg.strike - long_leg.strike
            if spread_width < MIN_SPREAD_WIDTH or spread_width > MAX_SPREAD_WIDTH:
                log.debug("BullCallSpread(%s): spread width $%.2f out of range", ticker, spread_width)
                return None

            long_premium = _mid(long_leg)
            short_premium = _mid(short_leg)
            net_debit = round(long_premium - short_premium, 2)
            max_risk = round(net_debit * 100, 2)
            max_reward = round((spread_width - net_debit) * 100, 2)

            if max_risk <= 0 or max_reward <= 0:
                log.debug("BullCallSpread(%s): invalid risk/reward (risk=%.2f, reward=%.2f)",
                          ticker, max_risk, max_reward)
                return None

            legs = [
                OptionLeg(symbol=long_leg.symbol, side='BUY', qty=1, ratio=1, limit_price=long_premium),
                OptionLeg(symbol=short_leg.symbol, side='SELL', qty=1, ratio=1, limit_price=short_premium),
            ]

            reason = (f"BullCallSpread {ticker}: buy {long_leg.strike}C / sell {short_leg.strike}C "
                      f"exp {long_leg.expiry_date}, width ${spread_width:.2f}, "
                      f"debit ${net_debit:.2f}")
            log.debug(reason)

            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=str(long_leg.expiry_date),
                legs=legs,
                net_debit=round(net_debit * 100, 2),  # V9: scale to per-contract $
                max_risk=max_risk,
                max_reward=max_reward,
                reason=reason,
            )
        except Exception:
            log.exception("BullCallSpread(%s): unexpected error", ticker)
            return None


# ---------------------------------------------------------------------------
# 2. BearPutSpread  (debit, bearish)
# ---------------------------------------------------------------------------

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
        try:
            if chain_client is None:
                return None

            contracts = chain_client.get_chain(ticker, min_dte, max_dte)
            if not contracts:
                log.debug("BearPutSpread(%s): empty chain", ticker)
                return None

            # Long put — higher delta (closer to ATM, higher strike)
            long_leg = chain_client.find_by_delta(contracts, 'put', delta_directional)
            # Short put — lower delta (further OTM, lower strike)
            short_leg = chain_client.find_by_delta(contracts, 'put', delta_spread)

            if long_leg is None or short_leg is None:
                log.debug("BearPutSpread(%s): could not find legs", ticker)
                return None

            if not _is_liquid(long_leg) or not _is_liquid(short_leg):
                log.debug("BearPutSpread(%s): illiquid legs", ticker)
                return None

            # Long put must have the higher strike
            if long_leg.strike <= short_leg.strike:
                log.debug("BearPutSpread(%s): long strike %.2f <= short strike %.2f",
                          ticker, long_leg.strike, short_leg.strike)
                return None

            spread_width = long_leg.strike - short_leg.strike
            if spread_width < MIN_SPREAD_WIDTH or spread_width > MAX_SPREAD_WIDTH:
                log.debug("BearPutSpread(%s): spread width $%.2f out of range", ticker, spread_width)
                return None

            long_premium = _mid(long_leg)
            short_premium = _mid(short_leg)
            net_debit = round(long_premium - short_premium, 2)
            max_risk = round(net_debit * 100, 2)
            max_reward = round((spread_width - net_debit) * 100, 2)

            if max_risk <= 0 or max_reward <= 0:
                log.debug("BearPutSpread(%s): invalid risk/reward (risk=%.2f, reward=%.2f)",
                          ticker, max_risk, max_reward)
                return None

            legs = [
                OptionLeg(symbol=long_leg.symbol, side='BUY', qty=1, ratio=1, limit_price=long_premium),
                OptionLeg(symbol=short_leg.symbol, side='SELL', qty=1, ratio=1, limit_price=short_premium),
            ]

            reason = (f"BearPutSpread {ticker}: buy {long_leg.strike}P / sell {short_leg.strike}P "
                      f"exp {long_leg.expiry_date}, width ${spread_width:.2f}, "
                      f"debit ${net_debit:.2f}")
            log.debug(reason)

            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=str(long_leg.expiry_date),
                legs=legs,
                net_debit=round(net_debit * 100, 2),  # V9: scale to per-contract $
                max_risk=max_risk,
                max_reward=max_reward,
                reason=reason,
            )
        except Exception:
            log.exception("BearPutSpread(%s): unexpected error", ticker)
            return None


# ---------------------------------------------------------------------------
# 3. BullPutSpread  (credit, bullish)
# ---------------------------------------------------------------------------

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
        """Build a bull put spread (credit spread)."""
        try:
            if chain_client is None:
                return None

            contracts = chain_client.get_chain(ticker, min_dte, max_dte)
            if not contracts:
                log.debug("BullPutSpread(%s): empty chain", ticker)
                return None

            # Short put — delta ~delta_spread (higher strike, closer to ATM)
            short_leg = chain_client.find_by_delta(contracts, 'put', delta_spread)
            # Long put — further OTM for protection, delta ~0.10
            long_leg = chain_client.find_by_delta(contracts, 'put', 0.10)

            if short_leg is None or long_leg is None:
                log.debug("BullPutSpread(%s): could not find legs", ticker)
                return None

            if not _is_liquid(short_leg) or not _is_liquid(long_leg):
                log.debug("BullPutSpread(%s): illiquid legs", ticker)
                return None

            # Short put must have the higher strike
            if short_leg.strike <= long_leg.strike:
                log.debug("BullPutSpread(%s): short strike %.2f <= long strike %.2f",
                          ticker, short_leg.strike, long_leg.strike)
                return None

            spread_width = short_leg.strike - long_leg.strike
            if spread_width < MIN_SPREAD_WIDTH or spread_width > MAX_SPREAD_WIDTH:
                log.debug("BullPutSpread(%s): spread width $%.2f out of range", ticker, spread_width)
                return None

            short_premium = _mid(short_leg)
            long_premium = _mid(long_leg)
            net_credit = round(short_premium - long_premium, 2)
            max_risk = round((spread_width - net_credit) * 100, 2)
            max_reward = round(net_credit * 100, 2)

            if max_risk <= 0 or max_reward <= 0:
                log.debug("BullPutSpread(%s): invalid risk/reward (risk=%.2f, reward=%.2f)",
                          ticker, max_risk, max_reward)
                return None

            legs = [
                OptionLeg(symbol=short_leg.symbol, side='SELL', qty=1, ratio=1, limit_price=short_premium),
                OptionLeg(symbol=long_leg.symbol, side='BUY', qty=1, ratio=1, limit_price=long_premium),
            ]

            # net_debit is negative for credit spreads
            net_debit_value = round(-net_credit, 2)

            reason = (f"BullPutSpread {ticker}: sell {short_leg.strike}P / buy {long_leg.strike}P "
                      f"exp {short_leg.expiry_date}, width ${spread_width:.2f}, "
                      f"credit ${net_credit:.2f}")
            log.debug(reason)

            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=str(short_leg.expiry_date),
                legs=legs,
                net_debit=round(net_debit_value * 100, 2),  # V9: scale to per-contract $
                max_risk=max_risk,
                max_reward=max_reward,
                reason=reason,
            )
        except Exception:
            log.exception("BullPutSpread(%s): unexpected error", ticker)
            return None


# ---------------------------------------------------------------------------
# 4. BearCallSpread  (credit, bearish)
# ---------------------------------------------------------------------------

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
        """Build a bear call spread (credit spread)."""
        try:
            if chain_client is None:
                return None

            contracts = chain_client.get_chain(ticker, min_dte, max_dte)
            if not contracts:
                log.debug("BearCallSpread(%s): empty chain", ticker)
                return None

            # Short call — delta ~delta_spread (lower strike, closer to ATM)
            short_leg = chain_client.find_by_delta(contracts, 'call', delta_spread)
            # Long call — further OTM for protection, delta ~0.10
            long_leg = chain_client.find_by_delta(contracts, 'call', 0.10)

            if short_leg is None or long_leg is None:
                log.debug("BearCallSpread(%s): could not find legs", ticker)
                return None

            if not _is_liquid(short_leg) or not _is_liquid(long_leg):
                log.debug("BearCallSpread(%s): illiquid legs", ticker)
                return None

            # Long call must have the higher strike (further OTM)
            if long_leg.strike <= short_leg.strike:
                log.debug("BearCallSpread(%s): long strike %.2f <= short strike %.2f",
                          ticker, long_leg.strike, short_leg.strike)
                return None

            spread_width = long_leg.strike - short_leg.strike
            if spread_width < MIN_SPREAD_WIDTH or spread_width > MAX_SPREAD_WIDTH:
                log.debug("BearCallSpread(%s): spread width $%.2f out of range", ticker, spread_width)
                return None

            short_premium = _mid(short_leg)
            long_premium = _mid(long_leg)
            net_credit = round(short_premium - long_premium, 2)
            max_risk = round((spread_width - net_credit) * 100, 2)
            max_reward = round(net_credit * 100, 2)

            if max_risk <= 0 or max_reward <= 0:
                log.debug("BearCallSpread(%s): invalid risk/reward (risk=%.2f, reward=%.2f)",
                          ticker, max_risk, max_reward)
                return None

            legs = [
                OptionLeg(symbol=short_leg.symbol, side='SELL', qty=1, ratio=1, limit_price=short_premium),
                OptionLeg(symbol=long_leg.symbol, side='BUY', qty=1, ratio=1, limit_price=long_premium),
            ]

            net_debit_value = round(-net_credit, 2)

            reason = (f"BearCallSpread {ticker}: sell {short_leg.strike}C / buy {long_leg.strike}C "
                      f"exp {short_leg.expiry_date}, width ${spread_width:.2f}, "
                      f"credit ${net_credit:.2f}")
            log.debug(reason)

            return OptionsTradeSpec(
                strategy_type=self.STRATEGY_TYPE,
                ticker=ticker,
                expiry_date=str(short_leg.expiry_date),
                legs=legs,
                net_debit=round(net_debit_value * 100, 2),  # V9: scale to per-contract $
                max_risk=max_risk,
                max_reward=max_reward,
                reason=reason,
            )
        except Exception:
            log.exception("BearCallSpread(%s): unexpected error", ticker)
            return None
