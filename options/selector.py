"""
OptionStrategySelector — maps signal/bar conditions to option strategy types.

Uses IV rank (not raw IV) for strategy selection. This is the critical difference
between a naive system and one with statistical edge:

  - IV rank > 50 → IV is HIGH relative to its own history → SELL premium
  - IV rank < 30 → IV is LOW relative to its own history → BUY premium
  - Without IV rank, we fall back to raw IV thresholds (less reliable)

Strategy selection principles:
  - High RVOL + low IV rank → buy premium (debit: long calls/puts, straddles)
  - Low RVOL  + high IV rank → sell premium (credit: iron condors, credit spreads)
  - Moderate conditions       → defined-risk spreads (verticals, butterflies)
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# ── Signal path thresholds ───────────────────────────────────────────────────
RVOL_HIGH_THRESHOLD   = 2.5
RVOL_MOD_THRESHOLD    = 1.2
RVOL_LOW_THRESHOLD    = 0.8

# ── Bar path thresholds ─────────────────────────────────────────────────────
ATR_SPIKE_THRESHOLD   = 0.035
ATR_MOD_THRESHOLD     = 0.015

# ── IV rank thresholds (0-100 scale) ────────────────────────────────────────
IV_RANK_HIGH          = 50    # above 50 → sell premium (IV is rich)
IV_RANK_LOW           = 30    # below 30 → buy premium (IV is cheap)
IV_RANK_VERY_HIGH     = 70    # above 70 → aggressive credit selling

# ── Raw IV fallbacks (used when IV rank unavailable / thin history) ───────────
IV_HIGH_THRESHOLD     = 0.25
IV_MOD_THRESHOLD      = 0.20
IV_LOW_THRESHOLD      = 0.18

# ── RSI thresholds ───────────────────────────────────────────────────────────
RSI_NEUTRAL_LOW       = 40
RSI_NEUTRAL_HIGH      = 60
RSI_VERY_NEUTRAL_LOW  = 42
RSI_VERY_NEUTRAL_HIGH = 58


class OptionStrategySelector:
    """
    Maps signal/bar conditions to the best options strategy type.
    Uses IV rank when available for statistically-backed selection.
    """

    def select_from_signal(
        self,
        action:      str,
        rvol:        float,
        rsi:         float,
        atr_value:   float,
        spot_price:  float,
        iv_estimate: float,
        iv_rank:     float = 50.0,
        ticker:      str = '',
    ) -> Optional[str]:
        """
        Signal-driven selection using IV rank.

        IV rank dictates regime:
          iv_rank >= 50 → premium is rich → favor credit strategies (sell)
          iv_rank < 30  → premium is cheap → favor debit strategies (buy)

        RVOL dictates conviction:
          high RVOL → strong move → more aggressive (single leg or wider spread)
          low RVOL  → weak move → only credit if IV rank supports it

        ETFs use lower RVOL thresholds (they never reach 2.5x).
        """
        action_upper = action.upper()
        is_buy = action_upper == 'BUY'
        is_sell = action_upper in ('SELL_STOP', 'SELL_TARGET', 'SELL_RSI', 'SELL_VWAP')

        if not is_buy and not is_sell:
            return None

        iv_is_rich = iv_rank >= IV_RANK_HIGH
        iv_is_cheap = iv_rank < IV_RANK_LOW
        iv_very_rich = iv_rank >= IV_RANK_VERY_HIGH

        # ETFs have structurally lower RVOL — scale thresholds down
        from monitor.sector_map import is_etf
        if is_etf(ticker):
            rvol_high = RVOL_HIGH_THRESHOLD * 0.35  # 2.5 → 0.875
            rvol_mod  = RVOL_MOD_THRESHOLD * 0.5    # 1.2 → 0.6
            rvol_low  = RVOL_LOW_THRESHOLD * 0.5    # 0.8 → 0.4
        else:
            rvol_high = RVOL_HIGH_THRESHOLD
            rvol_mod  = RVOL_MOD_THRESHOLD
            rvol_low  = RVOL_LOW_THRESHOLD

        # ── Tier 1: High RVOL — strong directional momentum ──────────
        if rvol >= rvol_high:
            if iv_is_cheap or (not iv_is_rich):
                # IV cheap/neutral: buy outright (premium is affordable)
                return 'long_call' if is_buy else 'long_put'
            else:
                # IV rich: use debit spread (reduce IV crush exposure)
                return 'bull_call_spread' if is_buy else 'bear_put_spread'

        # ── Tier 2: Moderate RVOL — spreads ───────────────────────────
        if rvol >= rvol_mod:
            if iv_is_rich:
                # IV rich: sell premium (credit spread)
                return 'bull_put_spread' if is_buy else 'bear_call_spread'
            else:
                # IV cheap/neutral: buy defined risk (debit spread)
                return 'bull_call_spread' if is_buy else 'bear_put_spread'

        # ── Tier 3: Low RVOL — only trade if IV rank gives clear edge ─
        if rvol >= rvol_low:
            if iv_very_rich:
                # IV very rich + low vol = ideal credit selling
                return 'bull_put_spread' if is_buy else 'bear_call_spread'
            elif iv_is_rich:
                return 'bull_put_spread' if is_buy else 'bear_call_spread'
            # IV not rich enough → skip
            return None

        # ── Tier 4: Very low RVOL — only if IV rank is extreme ────────
        if iv_very_rich:
            return 'bull_put_spread' if is_buy else 'bear_call_spread'

        return None

    def select_from_bar(
        self,
        atr_value:   float,
        spot_price:  float,
        iv_estimate: float,
        rsi:         float,
        rvol:        float,
        has_existing_position: bool,
        iv_rank:     float = 50.0,
        iv_history_days: int = 0,
    ) -> Optional[str]:
        """
        BAR-driven neutral/volatility selection using IV rank.

        Key insight: IV rank determines BUY vs SELL premium:
          - IV rank < 30 + ATR spike → BUY vol (straddle/strangle) — IV will expand
          - IV rank > 50 + range-bound → SELL vol (iron condor) — IV will contract
          - IV rank > 70 + neutral RSI → aggressive credit selling

        When IV history is thin (< 20 days), IV rank is blended toward neutral.
        With < 2 days, we fall back to raw IV thresholds entirely.
        """
        if spot_price <= 0:
            return None

        atr_ratio = atr_value / spot_price
        no_iv_history = iv_history_days < 2

        if no_iv_history:
            # No meaningful IV rank — use raw IV thresholds
            iv_is_rich = iv_estimate >= IV_HIGH_THRESHOLD
            iv_is_cheap = iv_estimate <= IV_LOW_THRESHOLD
            iv_very_rich = iv_estimate >= IV_HIGH_THRESHOLD + 0.10
        else:
            # IV rank available (possibly blended if < 20 days)
            iv_is_rich = iv_rank >= IV_RANK_HIGH
            iv_is_cheap = iv_rank < IV_RANK_LOW
            iv_very_rich = iv_rank >= IV_RANK_VERY_HIGH

        # ── 1. ATR spike + cheap/neutral IV → buy straddle ───────────
        if atr_ratio > ATR_SPIKE_THRESHOLD and not iv_is_rich:
            return 'long_straddle'

        # ── 2. Moderate ATR + cheap IV → buy strangle ────────────────
        if ATR_MOD_THRESHOLD < atr_ratio <= ATR_SPIKE_THRESHOLD:
            if iv_is_cheap or (no_iv_history and iv_estimate < IV_MOD_THRESHOLD):
                return 'long_strangle'

        # ── 3. Range-bound + rich IV → sell premium ──────────────────
        if RSI_NEUTRAL_LOW <= rsi <= RSI_NEUTRAL_HIGH:
            if (iv_very_rich or iv_is_rich) and not has_existing_position:
                if RSI_VERY_NEUTRAL_LOW <= rsi <= RSI_VERY_NEUTRAL_HIGH:
                    return 'iron_butterfly'
                else:
                    return 'iron_condor'

            if iv_is_rich and not has_existing_position:
                if atr_ratio < ATR_MOD_THRESHOLD:
                    return 'butterfly_spread'

        # ── 4. Calendar spread for existing positions with rich IV ────
        if has_existing_position and iv_is_rich:
            if RSI_NEUTRAL_LOW <= rsi <= RSI_NEUTRAL_HIGH:
                return 'calendar_spread'

        return None
