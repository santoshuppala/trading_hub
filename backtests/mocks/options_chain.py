"""
SyntheticOptionChainClient — Generate realistic synthetic option chains for backtesting.

IV formula: iv = (ATR / spot) * sqrt(252)
Clamped to [0.10, 1.50].
Consistent with OptionsEngine._estimate_iv fallback pattern.

Delta approximation: N(d1) where d1 = log(S/K) / (iv * sqrt(T))
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class OptionContract:
    """Synthetic option contract."""
    symbol: str
    right: str  # 'C' for call, 'P' for put
    strike: float
    expiration: str  # 'YYYY-MM-DD'
    bid: float
    ask: float
    iv: float
    delta: float
    theta: float
    gamma: float
    vega: float


class SyntheticOptionChainClient:
    """
    Generate synthetic option chains on demand.

    Tracks ATR and spot price per ticker to compute realistic IV.
    Generates option contracts with realistic Greeks via Black-Scholes approximation.
    """

    def __init__(self):
        self.bar_state: Dict[str, tuple[float, float]] = {}  # ticker → (atr, spot)
        log.info("[SyntheticOptionChainClient] Initialized")

    def update_bar(self, ticker: str, atr: float, spot: float) -> None:
        """Update ATR and spot price for IV computation."""
        self.bar_state[ticker] = (atr, spot)

    def get_chain(
        self,
        ticker: str,
        min_dte: int = 20,
        max_dte: int = 45,
    ) -> List[OptionContract]:
        """
        Generate synthetic option chain for a ticker.

        Args:
            ticker: symbol
            min_dte: minimum days to expiration
            max_dte: maximum days to expiration

        Returns:
            list of OptionContract objects
        """
        if ticker not in self.bar_state:
            log.warning(f"[SyntheticOptionChainClient] No bar data for {ticker}")
            return []

        atr, spot = self.bar_state[ticker]
        if spot <= 0:
            return []

        # Compute IV from ATR
        iv = (atr / spot) * math.sqrt(252)
        iv = max(0.10, min(iv, 1.50))  # clamp to [0.10, 1.50]

        contracts = []

        # Generate strikes: ATM ± 5% in 1% increments, rounded to nearest $5
        strikes = self._generate_strikes(spot)

        # Generate expirations in 5-day steps
        today = datetime.now()
        for dte in range(min_dte, max_dte + 1, 5):
            expiration_date = (today.replace(hour=0, minute=0, second=0, microsecond=0) +
                              __import__('datetime').timedelta(days=dte))
            expiry_str = expiration_date.strftime('%Y-%m-%d')
            t_years = dte / 365.0

            # Generate call and put for each strike
            for strike in strikes:
                # Call
                call_contract = self._generate_contract(
                    ticker=ticker,
                    right='C',
                    strike=strike,
                    expiration=expiry_str,
                    spot=spot,
                    iv=iv,
                    t_years=t_years,
                )
                contracts.append(call_contract)

                # Put
                put_contract = self._generate_contract(
                    ticker=ticker,
                    right='P',
                    strike=strike,
                    expiration=expiry_str,
                    spot=spot,
                    iv=iv,
                    t_years=t_years,
                )
                contracts.append(put_contract)

        return contracts

    def find_by_delta(
        self,
        contracts: List[OptionContract],
        option_type: str,  # 'C' or 'P'
        target_delta: float,
        tolerance: float = 0.05,
    ) -> Optional[OptionContract]:
        """Find a contract with delta closest to target."""
        matches = [c for c in contracts if c.right == option_type]
        if not matches:
            return None

        best = min(matches, key=lambda c: abs(c.delta - target_delta))
        if abs(best.delta - target_delta) > tolerance:
            return None

        return best

    def find_atm(
        self,
        contracts: List[OptionContract],
        option_type: str,
        underlying_price: float,
    ) -> Optional[OptionContract]:
        """Find at-the-money contract."""
        return self.find_by_delta(contracts, option_type, 0.50)

    # Private helpers

    def _generate_strikes(self, spot: float) -> List[float]:
        """Generate strike prices: ATM ± 5% in 1% increments."""
        strikes_set = set()
        for pct in range(-5, 6):
            strike = spot * (1.0 + pct / 100.0)
            # Round to nearest $5
            strike = round(strike / 5.0) * 5.0
            strikes_set.add(strike)
        return sorted(strikes_set)

    def _generate_contract(
        self,
        ticker: str,
        right: str,
        strike: float,
        expiration: str,
        spot: float,
        iv: float,
        t_years: float,
    ) -> OptionContract:
        """Generate a single option contract with Greeks."""
        # Black-Scholes d1, d2
        d1 = (math.log(spot / strike) + 0.5 * iv * iv * t_years) / (iv * math.sqrt(t_years))
        d2 = d1 - iv * math.sqrt(t_years)

        # Cumulative normal distribution (simplified)
        N_d1 = self._norm_cdf(d1)
        N_d2 = self._norm_cdf(d2)
        n_d1 = self._norm_pdf(d1)

        # Price and Greeks
        if right == 'C':
            price = spot * N_d1 - strike * math.exp(-0.05 * t_years) * N_d2
            delta = N_d1
        else:  # 'P'
            price = strike * math.exp(-0.05 * t_years) * (1 - N_d2) - spot * (1 - N_d1)
            delta = -N_d1 + 1

        gamma = n_d1 / (spot * iv * math.sqrt(t_years))
        vega = spot * n_d1 * math.sqrt(t_years)  # per 1% IV change
        theta = -(spot * n_d1 * iv) / (2 * math.sqrt(t_years))

        mid_price = max(price, 0.01)
        bid = mid_price * 0.99
        ask = mid_price * 1.01

        return OptionContract(
            symbol=ticker,
            right=right,
            strike=strike,
            expiration=expiration,
            bid=bid,
            ask=ask,
            iv=iv,
            delta=delta,
            theta=theta,
            gamma=gamma,
            vega=vega,
        )

    @staticmethod
    def _norm_cdf(x: float) -> float:
        """Approximation of cumulative normal distribution."""
        return 0.5 * (1.0 + math.tanh(math.sqrt(2 / math.pi) * (x + 0.044715 * x ** 3)))

    @staticmethod
    def _norm_pdf(x: float) -> float:
        """Probability density of normal distribution."""
        return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
