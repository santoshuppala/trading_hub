"""
pop_screener/strategy_router.py — Routes StrategyAssignment to the correct engine
==================================================================================
Given a StrategyAssignment, builds the appropriate engine instance, calls
generate_signals(), and returns the combined entry/exit signal lists.

If the primary engine produces no signals, the router tries each secondary
strategy in order and returns the first non-empty result.

Usage
-----
    router = StrategyRouter()
    entry_signals, exit_signals = router.route(
        symbol      = 'AAPL',
        bars        = market_slice.bars,
        vwap_series = market_slice.vwap_series,
        features    = engineered_features,
        assignment  = strategy_assignment,
    )
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple, Type

from pop_screener import config as cfg
from pop_screener.models import (
    EngineeredFeatures, EntrySignal, ExitSignal,
    OHLCVBar, StrategyAssignment, StrategyType,
)
from pop_screener.strategies.vwap_reclaim_engine    import VWAPReclaimEngine
from pop_screener.strategies.orb_engine             import ORBEngine
from pop_screener.strategies.halt_resume_engine     import HaltResumeEngine
from pop_screener.strategies.parabolic_reversal_engine import ParabolicReversalEngine
from pop_screener.strategies.ema_trend_engine       import EMATrendEngine
from pop_screener.strategies.bopb_engine            import BOPBEngine
from pop_screener.strategies.momentum_entry_engine  import MomentumEntryEngine


# V7 P4-3: Strategy Registry — single place to add new Pop strategies.
# To add a new strategy:
#   1. Create engine class in pop_screener/strategies/
#   2. Add StrategyType enum entry to models.py
#   3. Add entry here: STRATEGY_REGISTRY[StrategyType.NEW_TYPE] = NewEngine
# No other code changes required — router discovers from registry.
STRATEGY_REGISTRY: Dict[StrategyType, object] = {
    StrategyType.VWAP_RECLAIM:           VWAPReclaimEngine(),
    StrategyType.ORB:                    ORBEngine(),
    StrategyType.HALT_RESUME_BREAKOUT:   HaltResumeEngine(),
    StrategyType.PARABOLIC_REVERSAL:     ParabolicReversalEngine(),
    StrategyType.EMA_TREND_CONTINUATION: EMATrendEngine(),
    StrategyType.BREAKOUT_PULLBACK:      BOPBEngine(),
    StrategyType.MOMENTUM_ENTRY:         MomentumEntryEngine(),
}


class StrategyRouter:
    """
    Routes a StrategyAssignment to the correct engine and returns signals.

    V7 P4-3: Uses STRATEGY_REGISTRY for pluggable strategy discovery.
    To add a new strategy, add to STRATEGY_REGISTRY above — no router changes.

    Parameters
    ----------
    try_secondaries : bool
        If True (default), when the primary engine fires no entry signals
        the router tries each secondary strategy in order and returns the
        first non-empty result.  Set to False to only run the primary engine.
    """

    def __init__(self, try_secondaries: bool = True):
        self._try_secondaries = try_secondaries
        # V7: Engine instances from registry (pluggable, not hardcoded)
        self._engines = dict(STRATEGY_REGISTRY)

    def route(
        self,
        symbol:      str,
        bars:        List[OHLCVBar],
        vwap_series: List[float],
        features:    EngineeredFeatures,
        assignment:  StrategyAssignment,
    ) -> Tuple[List[EntrySignal], List[ExitSignal]]:
        """
        Run the engine for the primary strategy; fall back to secondaries if
        the primary produces no entry signals and try_secondaries=True.

        Parameters
        ----------
        symbol       : ticker
        bars         : chronological 1-min OHLCV bars
        vwap_series  : per-bar VWAP values (same length as bars)
        features     : EngineeredFeatures
        assignment   : StrategyAssignment from StrategyClassifier

        Returns
        -------
        (entry_signals, exit_signals)
        Both lists may be empty if no conditions are met.
        """
        # Build EMA series for engines that need them
        # (vwap_series is already provided; EMA is computed internally by engines)

        all_entries: List[EntrySignal] = []
        all_exits:   List[ExitSignal]  = []

        strategies_to_try = [assignment.primary_strategy]
        if self._try_secondaries:
            strategies_to_try += assignment.secondary_strategies

        for strategy_type in strategies_to_try:
            engine = self._engines.get(strategy_type)
            if engine is None:
                continue

            entries, exits = engine.generate_signals(
                symbol=symbol,
                bars=bars,
                vwap_series=vwap_series,
                features=features,
                assignment=assignment,
            )

            all_exits.extend(exits)

            if entries:
                all_entries.extend(entries)
                # Got entry signals from this strategy — stop trying others
                break

        # Last resort: try simple momentum entry if all strategies rejected
        if not all_entries and StrategyType.MOMENTUM_ENTRY not in strategies_to_try:
            momentum_engine = self._engines.get(StrategyType.MOMENTUM_ENTRY)
            if momentum_engine:
                entries, exits = momentum_engine.generate_signals(
                    symbol=symbol,
                    bars=bars,
                    vwap_series=vwap_series,
                    features=features,
                    assignment=assignment,
                )
                all_exits.extend(exits)
                all_entries.extend(entries)

        return all_entries, all_exits

    def route_all(
        self,
        symbol:      str,
        bars:        List[OHLCVBar],
        vwap_series: List[float],
        features:    EngineeredFeatures,
        assignments: List[StrategyAssignment],
    ) -> Tuple[List[EntrySignal], List[ExitSignal]]:
        """
        Run the router for multiple StrategyAssignments for the same symbol
        (e.g. when a symbol matches both primary and secondary classifiers).

        Returns the union of all entry and exit signals across assignments.
        Deduplicates entry signals by (strategy_type, side) to avoid double
        entries from the same strategy.
        """
        seen_entry_keys = set()
        all_entries: List[EntrySignal] = []
        all_exits:   List[ExitSignal]  = []

        for assignment in assignments:
            entries, exits = self.route(
                symbol=symbol,
                bars=bars,
                vwap_series=vwap_series,
                features=features,
                assignment=assignment,
            )
            all_exits.extend(exits)
            for e in entries:
                key = (e.strategy_type, e.side)
                if key not in seen_entry_keys:
                    seen_entry_keys.add(key)
                    all_entries.append(e)

        return all_entries, all_exits

    def get_engine(self, strategy_type: StrategyType):
        """Return the engine instance for a given StrategyType (for testing)."""
        return self._engines.get(strategy_type)
