"""
OptionStrategySelector maps signal/bar conditions to option strategy types.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Strategy selection thresholds
RVOL_HIGH_THRESHOLD   = 3.0   # high RVOL → directional single leg
RVOL_MOD_THRESHOLD    = 1.5   # moderate RVOL → spread
ATR_SPIKE_THRESHOLD   = 0.04  # ATR/price > 4% → straddle
ATR_MOD_THRESHOLD     = 0.02  # ATR/price > 2% → strangle
IV_HIGH_THRESHOLD     = 0.35  # IV > 35% → credit spread or neutral
IV_MOD_THRESHOLD      = 0.20  # IV > 20% → debit spread


class OptionStrategySelector:
    """
    Maps SignalPayload fields and BAR indicators to the appropriate
    OptionsStrategy type.

    Called by OptionsEngine._on_signal() and OptionsEngine._on_bar().
    Returns a strategy type string (OptionStrategyType.value) or None.
    """

    def select_from_signal(
        self,
        action:      str,    # SignalAction.value
        rvol:        float,
        rsi:         float,
        atr_value:   float,
        spot_price:  float,
        iv_estimate: float,  # from option chain mid-market IV
    ) -> Optional[str]:
        """
        Signal-driven selection logic:

        BUY signals:
          rvol >= RVOL_HIGH  → long_call
          RVOL_MOD <= rvol < RVOL_HIGH and IV < IV_HIGH → bull_call_spread
          IV >= IV_HIGH or rvol < RVOL_MOD              → bull_put_spread (credit)

        SELL signals (sell_stop, sell_target, sell_rsi, sell_vwap):
          rvol >= RVOL_HIGH  → long_put
          RVOL_MOD <= rvol < RVOL_HIGH                  → bear_put_spread
          IV >= IV_HIGH                                 → bear_call_spread (credit)

        Returns OptionStrategyType.value string or None.
        """
        if action == 'buy':
            if rvol >= RVOL_HIGH_THRESHOLD:
                return 'long_call'
            elif RVOL_MOD_THRESHOLD <= rvol < RVOL_HIGH_THRESHOLD:
                if iv_estimate < IV_HIGH_THRESHOLD:
                    return 'bull_call_spread'
                else:
                    return 'bull_put_spread'
            else:
                return None  # rvol too low

        elif action in ('sell_stop', 'sell_target', 'sell_rsi', 'sell_vwap'):
            if rvol >= RVOL_HIGH_THRESHOLD:
                return 'long_put'
            elif RVOL_MOD_THRESHOLD <= rvol < RVOL_HIGH_THRESHOLD:
                if iv_estimate < IV_HIGH_THRESHOLD:
                    return 'bear_put_spread'
                else:
                    return 'bear_call_spread'
            else:
                return None  # rvol too low

        return None

    def select_from_bar(
        self,
        atr_value:   float,
        spot_price:  float,
        iv_estimate: float,
        rsi:         float,
        rvol:        float,
        has_existing_position: bool,
    ) -> Optional[str]:
        """
        BAR-driven neutral/volatility scan:

        ATR/price > ATR_SPIKE_THRESHOLD → long_straddle
        ATR_MOD <= ATR/price <= ATR_SPIKE → long_strangle
        RSI in [40, 60] and IV > IV_HIGH   → iron_condor
        RSI in [45, 55] and IV > IV_HIGH   → iron_butterfly (more neutral)
        has_existing_position + term structure → calendar_spread

        Returns OptionStrategyType.value string or None.
        """
        if spot_price <= 0:
            return None

        atr_ratio = atr_value / spot_price

        # High volatility spike → straddle
        if atr_ratio > ATR_SPIKE_THRESHOLD:
            return 'long_straddle'

        # Moderate volatility → strangle
        if ATR_MOD_THRESHOLD <= atr_ratio <= ATR_SPIKE_THRESHOLD:
            return 'long_strangle'

        # Range-bound, high IV → iron condor / butterfly
        if 40 <= rsi <= 60 and iv_estimate > IV_HIGH_THRESHOLD:
            if 45 <= rsi <= 55:
                return 'iron_butterfly'  # very neutral
            else:
                return 'iron_condor'

        # Calendar/diagonal spreads for existing positions
        if has_existing_position:
            # TODO: Check term structure for calendar spreads
            pass

        return None
