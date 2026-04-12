"""
Base classes for all pro_setups detectors.
Each detector receives a BarPayload DataFrame and returns a DetectorSignal.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class DetectorSignal:
    """
    Normalised output from any detector.  All detector outputs share this
    schema so the StrategyClassifier can consume them uniformly.

    Attributes
    ----------
    fired     : True if the detector's pattern was detected
    direction : 'long', 'short', or 'neutral'
    strength  : confidence/quality score in [0.0, 1.0]
    metadata  : detector-specific key/value data (e.g. levels, prices)
    """
    fired:     bool
    direction: str          # 'long' | 'short' | 'neutral'
    strength:  float        # [0.0, 1.0]
    metadata:  Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction not in ('long', 'short', 'neutral'):
            raise ValueError(
                f"direction must be 'long', 'short', or 'neutral'; got {self.direction!r}"
            )
        self.strength = max(0.0, min(1.0, float(self.strength)))

    @classmethod
    def no_signal(cls) -> 'DetectorSignal':
        return cls(fired=False, direction='neutral', strength=0.0)


class BaseDetector(ABC):
    """
    Abstract base for all pro_setups detectors.

    Subclasses implement ``_detect()``.  The public ``detect()`` wrapper
    enforces the minimum bar count and silently absorbs exceptions so a single
    buggy detector never crashes the whole BAR handler.
    """
    name:     str = 'base'
    MIN_BARS: int = 20

    def detect(
        self,
        ticker:   str,
        df:       pd.DataFrame,
        rvol_df:  Optional[pd.DataFrame] = None,
    ) -> DetectorSignal:
        """
        Public entry point.  Returns ``DetectorSignal.no_signal()`` if there
        are fewer bars than ``MIN_BARS`` or if the inner implementation raises.
        """
        if len(df) < self.MIN_BARS:
            return DetectorSignal.no_signal()
        try:
            return self._detect(ticker, df, rvol_df)
        except Exception as exc:
            log.debug("[%s][%s] detector error: %s", self.name, ticker, exc)
            return DetectorSignal.no_signal()

    @abstractmethod
    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
    ) -> DetectorSignal:
        ...
