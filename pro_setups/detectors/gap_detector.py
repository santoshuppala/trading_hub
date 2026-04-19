"""GapDetector — gap-up / gap-down at open with go/fill context."""
from __future__ import annotations
from typing import Optional
import pandas as pd
from .base import BaseDetector, DetectorSignal


class GapDetector(BaseDetector):
    """
    Detects a gap between the previous close (last bar of rvol_df) and
    the current session open.  Falls back to comparing the first bar's open
    with the previous bar's close inside df when rvol_df is unavailable.

    Gap-and-go: gap is unfilled and price continues in the gap direction
    (last close still above/below gap level).

    Strength scales with gap size, capped at 1.0 at 5% gap.
    """
    name:         str   = 'gap'
    MIN_BARS:     int   = 5
    _MIN_GAP_PCT: float = 0.015    # minimum 1.5% gap to qualify (V9: was 0.5%, too noisy)
    _MAX_GAP_PCT: float = 0.15     # ignore extreme gaps > 15% (earnings/halts handled elsewhere)
    _NORM_GAP:    float = 0.05     # 5% gap → strength = 1.0

    def _detect(
        self,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame],
    ) -> DetectorSignal:
        # Resolve previous close — must be prior day's close, not an intra-session bar
        if rvol_df is not None and len(rvol_df) > 0:
            prev_close = float(rvol_df['close'].iloc[-1])
        else:
            # Without historical data we cannot determine the prior day's close.
            # Using an intra-session bar would produce false gap signals.
            return DetectorSignal.no_signal()

        session_open = float(df['open'].iloc[0])
        last_close   = float(df['close'].iloc[-1])

        if prev_close <= 0 or session_open <= 0:
            return DetectorSignal.no_signal()

        gap_pct = (session_open - prev_close) / prev_close

        if abs(gap_pct) < self._MIN_GAP_PCT or abs(gap_pct) > self._MAX_GAP_PCT:
            return DetectorSignal.no_signal()

        gap_up = gap_pct > 0

        # Gap-and-go: price has not filled the gap
        if gap_up:
            gap_fill_level = prev_close
            unfilled = last_close > gap_fill_level
            direction = 'long'
        else:
            gap_fill_level = prev_close
            unfilled = last_close < gap_fill_level
            direction = 'short'

        if not unfilled:
            return DetectorSignal.no_signal()

        strength = min(abs(gap_pct) / self._NORM_GAP, 1.0)

        return DetectorSignal(
            fired=True,
            direction=direction,
            strength=strength,
            metadata={
                'gap_pct':        gap_pct,
                'session_open':   session_open,
                'prev_close':     prev_close,
                'gap_fill_level': gap_fill_level,
                'unfilled':       unfilled,
            },
        )
