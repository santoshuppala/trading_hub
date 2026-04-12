"""
BaseProStrategy — contract for all 11 pro setup strategy modules.

Every strategy implements the four-method contract required by the
existing StrategyEngine interface, adapted for pro_setups:

    detect_signal(ticker, df, detector_outputs) → 'long' | 'short' | None
    generate_entry(ticker, df, direction, detector_outputs) → float
    generate_stop(entry_price, direction, atr, df) → float
    generate_exit(entry_price, stop_price, direction) → (target_1, target_2)

Tier constants (SL_ATR, PARTIAL_R, FULL_R) are defined at class level.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

import pandas as pd

from ..detectors.base import DetectorSignal


class BaseProStrategy(ABC):
    """
    Abstract base strategy.  Tier-specific subclasses override the class-level
    constants; strategies override ``detect_signal`` and ``generate_entry``.
    ``generate_stop`` and ``generate_exit`` use the tier constants and are
    rarely overridden.

    Tier constants
    --------------
    Tier 1: SL=0.4 ATR, partial=1R, full=2R   (high win-rate)
    Tier 2: SL=1.0 ATR, partial=1.5R, full=3R (moderate, higher R:R)
    Tier 3: SL=2.0 ATR, partial=3R,   full=6R  (low win-rate, high expectancy)
    """
    TIER:       int   = 0      # must be set by concrete class
    SL_ATR:     float = 1.0    # stop-loss ATR multiplier
    PARTIAL_R:  float = 1.5    # partial-exit at this multiple of 1R
    FULL_R:     float = 3.0    # full-exit at this multiple of 1R

    @abstractmethod
    def detect_signal(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> Optional[str]:
        """
        Final signal confirmation.  Returns 'long', 'short', or None.
        The classifier has already selected this strategy; this method
        verifies the specific conditions required for an entry.
        """
        ...

    @abstractmethod
    def generate_entry(
        self,
        ticker:           str,
        df:               pd.DataFrame,
        direction:        str,
        detector_outputs: Dict[str, DetectorSignal],
    ) -> float:
        """
        Returns the proposed entry price.  Typically the current close
        or a specific level derived from detector metadata.
        """
        ...

    def generate_stop(
        self,
        entry_price: float,
        direction:   str,
        atr:         float,
        df:          pd.DataFrame,
    ) -> float:
        """
        Tier-based stop loss.  Uses SL_ATR × ATR from entry.
        Subclasses may override for structure-based stops.
        """
        offset = self.SL_ATR * atr
        if direction == 'long':
            return round(max(entry_price - offset, 0.01), 4)
        else:
            return round(entry_price + offset, 4)

    def generate_exit(
        self,
        entry_price: float,
        stop_price:  float,
        direction:   str,
    ) -> Tuple[float, float]:
        """
        Tier-based exit targets.  Returns (target_1, target_2).
        Risk R = abs(entry_price - stop_price).
        """
        risk = abs(entry_price - stop_price)
        if direction == 'long':
            t1 = round(entry_price + self.PARTIAL_R * risk, 4)
            t2 = round(entry_price + self.FULL_R   * risk, 4)
        else:
            t1 = round(entry_price - self.PARTIAL_R * risk, 4)
            t2 = round(entry_price - self.FULL_R   * risk, 4)
        return t1, t2
