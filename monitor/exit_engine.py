"""
V10 Exit Engine — Phase-based Position Lifecycle Manager.

Replaces panic-based exits (RSI > 70 → SELL ALL) with a structured
4-phase lifecycle that manages each position from entry to exit.

Phases:
    Phase 0: ENTRY VALIDATION (0-30s)  — thesis check, fast exit on rejection
    Phase 1: PROTECTION (1-3 min)      — stop only, no RSI/VWAP exits
    Phase 2: BREAKEVEN (0.5R)          — move stop to entry, RSI tightens trail
    Phase 3: PROFIT HARVEST (1R)       — partial exit, trail under structure
    Phase 4: RUNNER (2R+)              — trail under structure only, let it run

Per-strategy exit profiles define different parameters for each detector.
Phase 0 uses detector-provided invalidation levels for thesis validation.

Usage:
    lifecycle = PositionLifecycle(
        ticker='AAPL', strategy='sr_flip',
        entry_price=150.0, entry_bar_num=100,
        key_level=149.50, invalidation=149.00,
        structure_trail=149.20, atr=0.50,
        profile=EXIT_PROFILES['sr_flip'],
    )

    # Every bar:
    decision = lifecycle.evaluate(bar, rsi=65.0, vwap=149.80)
    if decision.action == 'EXIT':
        sell(ticker, reason=decision.reason)
    elif decision.action == 'PARTIAL':
        sell_partial(ticker, pct=decision.partial_pct)
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional, Dict, Any

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# EXIT PROFILES — per-strategy parameters
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ExitProfile:
    """Exit parameters for a specific strategy."""
    # Phase 0: Entry validation
    phase0_min_bars: int = 1         # minimum bars to validate thesis
    phase0_max_bars: int = 3         # maximum bars for validation window
    phase0_atr_adaptive: bool = True # scale window by ATR (high ATR → fewer bars)

    # Phase 1: Protection
    phase1_bars: int = 3             # bars of protection (no RSI/VWAP exits)

    # Stop loss
    stop_type: str = 'atr'           # 'atr', 'structure', 'fixed'
    stop_atr_mult: float = 1.0      # ATR multiplier for initial stop

    # Phase 2: Breakeven
    breakeven_trigger_r: float = 0.5 # move stop to entry at this R-multiple
    breakeven_cushion_r: float = 0.0 # 0.0 = true breakeven, 0.1 = small cushion for structure
    is_impulse: bool = False         # impulse (true BE) vs structure (cushion)
    dead_trade_bars: int = 30        # bars of flatness before "dead trade" exit
    dead_trade_r_range: float = 0.2  # within this R range = "flat"
    # RSI trail widths per phase (progressive tightening)
    rsi_phase2_trail_r: float = 0.5  # Phase 2: mild tightening
    rsi_phase3_trail_r: float = 0.3  # Phase 3: normal tightening
    rsi_phase4_trail_r: float = 0.2  # Phase 4: tight
    # VWAP behavior in Phase 2
    vwap_phase2_action: str = 'exit' # 'exit' for VWAP strategies, 'tighten' for others
    vwap_tighten_r: float = 0.2      # trail tighten width when VWAP breaks (non-VWAP strategies)

    # Phase 3: Profit harvest
    partial_at_r: float = 1.0       # take partial at this R-multiple
    partial_pct: float = 0.50       # sell this % of position
    trail_type: str = 'structure'    # 'structure', 'atr', 'higher_lows', 'bollinger_mid'
    trail_r_width: float = 0.5      # trail width in R (fallback when no structure)
    trail_atr_mult: float = 1.0     # trail width in ATR (for atr trail type)

    # Phase 4: Runner
    runner_trail_type: str = 'structure'  # structure-only for runners

    # RSI behavior
    rsi_tighten: bool = True         # does RSI tighten the trail?
    rsi_tighten_threshold: float = 70.0  # RSI level to start tightening
    rsi_hard_exit: bool = False      # never True in V10 — RSI doesn't exit

    # VWAP behavior
    vwap_exit: str = '2bar'          # '2bar', 'tighten', or None
    vwap_confirm_bars: int = 2       # bars below VWAP to confirm breakdown

    # Time limits
    max_hold_bars: int = 0           # 0 = no limit


# Per-strategy exit profiles
EXIT_PROFILES: Dict[str, ExitProfile] = {
    'sr_flip': ExitProfile(
        phase0_min_bars=1, phase0_max_bars=3,
        phase1_bars=3,
        stop_type='structure', stop_atr_mult=1.0,
        breakeven_trigger_r=0.5, breakeven_cushion_r=0.1, is_impulse=False,
        dead_trade_bars=30,
        rsi_phase2_trail_r=0.5, rsi_phase3_trail_r=0.3, rsi_phase4_trail_r=0.2,
        vwap_phase2_action='tighten', vwap_tighten_r=0.2,
        partial_at_r=1.0, partial_pct=0.50,
        trail_type='structure', trail_r_width=0.5,
        rsi_tighten=True, rsi_tighten_threshold=72,
        vwap_exit='2bar',
        max_hold_bars=0,
    ),
    'trend_pullback': ExitProfile(
        phase0_min_bars=1, phase0_max_bars=2,
        phase1_bars=2,
        stop_type='structure', stop_atr_mult=0.8,
        breakeven_trigger_r=0.5, breakeven_cushion_r=0.1, is_impulse=False,
        dead_trade_bars=30,
        rsi_phase2_trail_r=0.5, rsi_phase3_trail_r=0.3, rsi_phase4_trail_r=0.2,
        vwap_phase2_action='tighten', vwap_tighten_r=0.2,
        partial_at_r=1.0, partial_pct=0.50,
        trail_type='higher_lows', trail_r_width=0.5,
        rsi_tighten=False,
        vwap_exit='2bar',
        max_hold_bars=0,
    ),
    'momentum_ignition': ExitProfile(
        phase0_min_bars=1, phase0_max_bars=3,
        phase1_bars=3,
        stop_type='atr', stop_atr_mult=2.0,
        breakeven_trigger_r=1.0, breakeven_cushion_r=0.0, is_impulse=True,
        dead_trade_bars=15,
        rsi_phase2_trail_r=0.5, rsi_phase3_trail_r=0.3, rsi_phase4_trail_r=0.2,
        vwap_phase2_action='tighten', vwap_tighten_r=0.3,
        partial_at_r=2.0, partial_pct=0.33,
        trail_type='atr', trail_atr_mult=1.5, trail_r_width=1.0,
        rsi_tighten=False,
        vwap_exit=None,
        max_hold_bars=30,
    ),
    'orb': ExitProfile(
        phase0_min_bars=1, phase0_max_bars=3,
        phase1_bars=3,
        stop_type='structure', stop_atr_mult=1.5,
        breakeven_trigger_r=0.5, breakeven_cushion_r=0.1, is_impulse=False,
        dead_trade_bars=30,
        rsi_phase2_trail_r=0.5, rsi_phase3_trail_r=0.3, rsi_phase4_trail_r=0.2,
        vwap_phase2_action='tighten', vwap_tighten_r=0.2,
        partial_at_r=1.0, partial_pct=0.50,
        trail_type='structure', trail_r_width=0.5,
        rsi_tighten=True, rsi_tighten_threshold=70,
        vwap_exit='2bar',
        max_hold_bars=60,
    ),
    'inside_bar': ExitProfile(
        phase0_min_bars=1, phase0_max_bars=3,
        phase1_bars=3,
        stop_type='structure', stop_atr_mult=1.0,
        breakeven_trigger_r=0.5, breakeven_cushion_r=0.1, is_impulse=False,
        dead_trade_bars=30,
        rsi_phase2_trail_r=0.5, rsi_phase3_trail_r=0.3, rsi_phase4_trail_r=0.2,
        vwap_phase2_action='tighten', vwap_tighten_r=0.2,
        partial_at_r=1.0, partial_pct=0.50,
        trail_type='structure', trail_r_width=0.5,
        rsi_tighten=True, rsi_tighten_threshold=70,
        vwap_exit='2bar',
        max_hold_bars=60,
    ),
    'bollinger_squeeze': ExitProfile(
        phase0_min_bars=2, phase0_max_bars=4,
        phase1_bars=4,
        stop_type='atr', stop_atr_mult=2.0,
        breakeven_trigger_r=1.0, breakeven_cushion_r=0.1, is_impulse=False,
        dead_trade_bars=30,
        rsi_phase2_trail_r=0.5, rsi_phase3_trail_r=0.3, rsi_phase4_trail_r=0.2,
        vwap_phase2_action='tighten', vwap_tighten_r=0.3,
        partial_at_r=1.5, partial_pct=0.50,
        trail_type='atr', trail_atr_mult=1.5, trail_r_width=1.0,
        rsi_tighten=True, rsi_tighten_threshold=75,
        vwap_exit=None,
        max_hold_bars=45,
    ),
    'fib_confluence': ExitProfile(
        phase0_min_bars=1, phase0_max_bars=3,
        phase1_bars=3,
        stop_type='structure', stop_atr_mult=1.2,
        breakeven_trigger_r=0.7, breakeven_cushion_r=0.1, is_impulse=False,
        dead_trade_bars=30,
        rsi_phase2_trail_r=0.5, rsi_phase3_trail_r=0.3, rsi_phase4_trail_r=0.2,
        vwap_phase2_action='tighten', vwap_tighten_r=0.2,
        partial_at_r=1.0, partial_pct=0.50,
        trail_type='structure', trail_r_width=0.6,
        rsi_tighten=True, rsi_tighten_threshold=70,
        vwap_exit='2bar',
        max_hold_bars=0,
    ),
    'liquidity_sweep': ExitProfile(
        phase0_min_bars=2, phase0_max_bars=4,
        phase1_bars=3,
        stop_type='structure', stop_atr_mult=2.5,
        breakeven_trigger_r=1.0, breakeven_cushion_r=0.0, is_impulse=True,
        dead_trade_bars=15,
        rsi_phase2_trail_r=0.5, rsi_phase3_trail_r=0.3, rsi_phase4_trail_r=0.2,
        vwap_phase2_action='tighten', vwap_tighten_r=0.3,
        partial_at_r=1.5, partial_pct=0.50,
        trail_type='structure', trail_r_width=1.0,
        rsi_tighten=False,
        vwap_exit=None,
        max_hold_bars=0,
    ),
    'gap_and_go': ExitProfile(
        phase0_min_bars=1, phase0_max_bars=2,
        phase1_bars=2,
        stop_type='structure', stop_atr_mult=1.0,
        breakeven_trigger_r=0.5, breakeven_cushion_r=0.0, is_impulse=True,
        dead_trade_bars=15,
        rsi_phase2_trail_r=0.5, rsi_phase3_trail_r=0.3, rsi_phase4_trail_r=0.2,
        vwap_phase2_action='tighten', vwap_tighten_r=0.2,
        partial_at_r=1.0, partial_pct=0.50,
        trail_type='structure', trail_r_width=0.5,
        rsi_tighten=True, rsi_tighten_threshold=70,
        vwap_exit='2bar',
        max_hold_bars=30,
    ),
    'vwap_reclaim': ExitProfile(
        phase0_min_bars=1, phase0_max_bars=3,
        phase1_bars=3,
        stop_type='atr', stop_atr_mult=0.5,
        breakeven_trigger_r=0.5, breakeven_cushion_r=0.1, is_impulse=False,
        dead_trade_bars=30,
        rsi_phase2_trail_r=0.5, rsi_phase3_trail_r=0.3, rsi_phase4_trail_r=0.2,
        vwap_phase2_action='exit', vwap_tighten_r=0.2,  # VWAP IS the thesis
        partial_at_r=1.0, partial_pct=0.50,
        trail_type='atr', trail_atr_mult=1.0, trail_r_width=0.5,
        rsi_tighten=True, rsi_tighten_threshold=70,
        vwap_exit='2bar',
        max_hold_bars=0,
    ),
}

# Default profile for unknown strategies
DEFAULT_PROFILE = ExitProfile()


def get_exit_profile(strategy: str) -> ExitProfile:
    """Get exit profile for a strategy. Falls back to DEFAULT_PROFILE."""
    # Strip 'pro:' prefix if present
    clean = strategy.replace('pro:', '').split(':')[0] if strategy else ''
    return EXIT_PROFILES.get(clean, DEFAULT_PROFILE)


# ═══════════════════════════════════════════════════════════════════════════════
# DECISION OBJECTS
# ═══════════════════════════════════════════════════════════════════════════════

class Phase(IntEnum):
    PHASE0_VALIDATION = 0
    PHASE1_PROTECTION = 1
    PHASE2_BREAKEVEN = 2
    PHASE3_HARVEST = 3
    PHASE4_RUNNER = 4


@dataclass
class ExitDecision:
    """Structured decision from lifecycle evaluation."""
    action: str              # 'HOLD', 'EXIT', 'PARTIAL', 'TIGHTEN_TRAIL'
    phase: int               # current phase
    reason: str              # human-readable reason
    exit_price: float = 0.0  # suggested exit price (for stops/trails)
    partial_pct: float = 0.0 # % to sell if action=PARTIAL
    new_stop: float = 0.0    # updated stop price if trail tightened
    bars_in_phase: int = 0
    unrealized_r: float = 0.0  # current unrealized in R-multiples
    phase0_passed: bool = True
    updated_invalidation: float = 0.0
    updated_structure_trail: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# POSITION LIFECYCLE MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class PositionLifecycle:
    """Manages the exit lifecycle for one open position.

    Created when a position opens. Called every bar with current price data.
    Returns an ExitDecision that the caller acts on.
    """

    def __init__(
        self,
        ticker: str,
        strategy: str,
        entry_price: float,
        entry_bar_num: int,
        key_level: float,           # from detector: the level that defines the thesis
        invalidation: float,        # from detector: below this = thesis dead
        structure_trail: float,     # from detector: trail anchor for Phase 3/4
        atr: float,
        profile: ExitProfile = None,
        median_atr: float = 0.0,    # market median ATR for adaptive Phase 0
        confluence_count: int = 2,  # number of detectors that agreed on this signal
    ):
        self.ticker = ticker
        self.strategy = strategy
        self.entry_price = entry_price
        self.entry_bar_num = entry_bar_num
        self.key_level = key_level
        self.invalidation = invalidation
        self.structure_trail = structure_trail
        self.atr = atr if atr > 0 else entry_price * 0.005
        self.profile = profile or get_exit_profile(strategy)
        self.median_atr = median_atr or self.atr
        self._confluence_count = confluence_count

        # Computed values
        self.R = abs(entry_price - invalidation) if invalidation > 0 else self.atr
        self.stop_price = invalidation if invalidation > 0 else entry_price - self.atr * self.profile.stop_atr_mult

        # State
        self.phase = Phase.PHASE0_VALIDATION
        self.bars_held = 0
        self.phase0_window = self._compute_phase0_window()
        self.running_high = entry_price
        self.running_low = entry_price
        self.partial_done = False
        self.vwap_below_count = 0    # consecutive bars below VWAP
        self.trail_stop = self.stop_price
        self.phase_entry_bar = 0     # bar when current phase started

        # V10: Event log — captures every phase transition and decision for DB/ML
        self._events: list = []  # list of dicts, one per significant event

        # V10.1: Rolling buffers for trade quality scorer (deque for O(1) eviction)
        self._bar_opens: deque = deque(maxlen=10)
        self._bar_highs: deque = deque(maxlen=10)
        self._bar_lows: deque = deque(maxlen=10)
        self._bar_closes: deque = deque(maxlen=10)
        self._bar_volumes: deque = deque(maxlen=10)
        self._vwap_history: deque = deque(maxlen=10)
        self._prev_trend_score: float | None = None
        self._prev_failure_score: float | None = None
        self._prev_trend_state: str | None = None
        self._latest_score: dict | None = None

        # Phase-specific rolling state
        self._phase1_lows: list = []
        self._phase4_lows: list = []

        log.info(
            "[Lifecycle] %s OPENED | strategy=%s phase=0 entry=$%.2f "
            "stop=$%.2f R=$%.2f key=$%.2f invalidation=$%.2f "
            "phase0_window=%d",
            ticker, strategy, entry_price, self.stop_price, self.R,
            key_level, invalidation, self.phase0_window,
        )

    def _compute_phase0_window(self) -> int:
        """ATR-adaptive Phase 0 validation window."""
        if not self.profile.phase0_atr_adaptive:
            return self.profile.phase0_max_bars

        # High ATR (relative to median) → fewer bars needed
        # Low ATR → more bars needed for thesis to validate
        if self.median_atr > 0:
            ratio = self.atr / self.median_atr
        else:
            ratio = 1.0

        if ratio > 1.5:
            window = self.profile.phase0_min_bars      # high ATR → fast validation
        elif ratio < 0.5:
            window = self.profile.phase0_max_bars       # low ATR → slow validation
        else:
            window = max(
                self.profile.phase0_min_bars,
                min(self.profile.phase0_max_bars, round(2.0 / ratio))
            )

        return window

    # ── Main evaluation ──────────────────────────────────────────────────

    def evaluate(self, bar_high: float, bar_low: float, bar_close: float,
                 bar_volume: float = 0, entry_bar_volume: float = 0,
                 rsi: float = 50.0, vwap: float = 0.0,
                 current_bar_num: int = 0,
                 bar_open: float = 0.0,
                 is_quote: bool = False) -> ExitDecision:
        """Evaluate exit decision for current bar or real-time quote.

        Args:
            bar_high/low/close: current bar OHLC (or bid for quotes)
            bar_volume: current bar volume
            entry_bar_volume: volume on the entry bar (for momentum validation)
            rsi: current RSI value
            vwap: current session VWAP
            current_bar_num: monotonic bar counter
            bar_open: bar open price (for directional volume + closing strength)
            is_quote: True for QUOTE-driven evaluation. Skips bars_held increment,
                      buffer appends, and scorer to prevent poisoning from
                      high-frequency identical data points.

        Returns:
            ExitDecision with action and reasoning
        """
        # Running high/low always update (real-time price tracking)
        self.running_high = max(self.running_high, bar_high)
        self.running_low = min(self.running_low, bar_low)

        # Only count actual bars, not quotes
        if not is_quote:
            self.bars_held += 1

        unrealized_r = (bar_close - self.entry_price) / self.R if self.R > 0 else 0

        # ── V10.1: Update rolling buffers for scorer (BAR only) ──────
        # All buffers append unconditionally to keep lengths aligned for scorer.
        if not is_quote:
            self._bar_opens.append(bar_open if bar_open > 0 else bar_close)
            self._bar_highs.append(bar_high)
            self._bar_lows.append(bar_low)
            self._bar_closes.append(bar_close)
            self._bar_volumes.append(bar_volume)
            self._vwap_history.append(vwap if vwap > 0 else (
                self._vwap_history[-1] if self._vwap_history else bar_close))

        # ── V10.1: Score trade quality (BAR only, logging only) ──────
        if not is_quote and self.bars_held >= 3:
            try:
                from .trade_scorer import score_trade
                self._latest_score = score_trade(
                    bar_open=bar_open or (self._bar_closes[-2] if len(self._bar_closes) >= 2 else bar_close),
                    bar_high=bar_high, bar_low=bar_low,
                    bar_close=bar_close, bar_volume=bar_volume,
                    entry_price=self.entry_price, atr=self.atr, R=self.R,
                    running_high=self.running_high, running_low=self.running_low,
                    vwap=vwap, rsi=rsi,
                    bar_opens=list(self._bar_opens),
                    bar_highs=list(self._bar_highs), bar_lows=list(self._bar_lows),
                    bar_closes=list(self._bar_closes), bar_volumes=list(self._bar_volumes),
                    vwap_history=list(self._vwap_history),
                    prev_trend_score=self._prev_trend_score,
                    prev_failure_score=self._prev_failure_score,
                )
                self._prev_trend_score = self._latest_score['trend_score_smooth']
                self._prev_failure_score = self._latest_score['failure_score_smooth']
                # Update state with hysteresis
                self._prev_trend_state = self._latest_score['trend_state']

                self._log_event(
                    'SCORE',
                    f"trend={self._latest_score['trend_score']:.2f} "
                    f"fail={self._latest_score['failure_score']:.2f} "
                    f"state={self._latest_score['trend_state']}",
                    score=self._latest_score,
                )
            except Exception as exc:
                # Scorer failure must NEVER affect exit logic
                log.debug("[Lifecycle] Scorer failed for %s: %s", self.ticker, exc)

        # ── STOP LOSS: always active, every phase ────────────────────
        if bar_low <= self.stop_price:
            return ExitDecision(
                action='EXIT', phase=self.phase, reason=f'STOP_LOSS_phase{self.phase}',
                exit_price=self.stop_price, bars_in_phase=self.bars_held,
                unrealized_r=unrealized_r,
            )

        # ── TRAIL STOP: active in Phase 2+ ───────────────────────────
        if self.phase >= Phase.PHASE2_BREAKEVEN and bar_low <= self.trail_stop:
            return ExitDecision(
                action='EXIT', phase=self.phase, reason=f'TRAIL_STOP_phase{self.phase}',
                exit_price=self.trail_stop, bars_in_phase=self.bars_held,
                unrealized_r=unrealized_r,
            )

        # ── MAX HOLD: exit if held too long ──────────────────────────
        if self.profile.max_hold_bars > 0 and self.bars_held >= self.profile.max_hold_bars:
            return ExitDecision(
                action='EXIT', phase=self.phase, reason='MAX_HOLD_BARS',
                exit_price=bar_close, bars_in_phase=self.bars_held,
                unrealized_r=unrealized_r,
            )

        # ── Phase-specific logic ─────────────────────────────────────
        # Phase 0: thesis validation on BARS only (quotes could fail on bid noise)
        # Stop loss above still fires on quotes — protects against real crashes.
        if self.phase == Phase.PHASE0_VALIDATION:
            if is_quote:
                return ExitDecision(action='HOLD', phase=0, reason='phase0_quote_hold',
                                    bars_in_phase=self.bars_held, unrealized_r=unrealized_r)
            return self._evaluate_phase0(bar_high, bar_low, bar_close,
                                          bar_volume, entry_bar_volume,
                                          rsi, vwap, unrealized_r)

        elif self.phase == Phase.PHASE1_PROTECTION:
            return self._evaluate_phase1(bar_close, bar_low, unrealized_r, is_quote)

        elif self.phase == Phase.PHASE2_BREAKEVEN:
            return self._evaluate_phase2(bar_high, bar_close, rsi, vwap, unrealized_r, is_quote)

        elif self.phase == Phase.PHASE3_HARVEST:
            return self._evaluate_phase3(bar_high, bar_close, rsi, vwap, unrealized_r, is_quote)

        elif self.phase == Phase.PHASE4_RUNNER:
            return self._evaluate_phase4(bar_high, bar_close, rsi, vwap, unrealized_r, is_quote)

        return ExitDecision(action='HOLD', phase=self.phase, reason='default',
                            unrealized_r=unrealized_r)

    # ── Phase 0: Entry Validation ────────────────────────────────────

    def _evaluate_phase0(self, bar_high, bar_low, bar_close,
                          bar_volume, entry_bar_volume,
                          rsi, vwap, unrealized_r) -> ExitDecision:
        """Validate that the entry thesis survived the first bars."""
        strategy = self.strategy.replace('pro:', '').split(':')[0]

        # Check thesis invalidation based on strategy type
        thesis_failed = False
        fail_reason = ''

        if strategy == 'sr_flip':
            # SR flip: price must stay above the flipped level
            if bar_close < self.key_level:
                thesis_failed = True
                fail_reason = 'sr_level_rejected'

        elif strategy == 'trend_pullback':
            # Trend pullback: price must hold above pullback low
            if bar_close < self.invalidation:
                thesis_failed = True
                fail_reason = 'pullback_broken'

        elif strategy == 'momentum_ignition':
            # Momentum: volume must confirm, bar range must confirm
            if entry_bar_volume > 0 and bar_volume < entry_bar_volume * 0.4:
                thesis_failed = True
                fail_reason = 'momentum_volume_died'
            elif bar_high - bar_low < self.atr * 0.3:
                thesis_failed = True
                fail_reason = 'momentum_range_collapsed'

        elif strategy == 'orb':
            # ORB: price must stay above opening range breakout level
            if bar_close < self.key_level:
                thesis_failed = True
                fail_reason = 'orb_reenter_range'

        elif strategy == 'inside_bar':
            # Inside bar: price must stay outside mother bar range
            if bar_close < self.key_level:
                thesis_failed = True
                fail_reason = 'inside_bar_reenter'

        elif strategy == 'bollinger_squeeze':
            # Squeeze: price must stay outside squeeze bands
            if bar_close < self.key_level:
                thesis_failed = True
                fail_reason = 'squeeze_failed'

        elif strategy == 'liquidity_sweep':
            # Sweep: price must not make new low below sweep
            if bar_low < self.invalidation:
                thesis_failed = True
                fail_reason = 'sweep_new_low'

        elif strategy == 'fib_confluence':
            # Fib: price must hold above fib cluster
            if bar_close < self.invalidation:
                thesis_failed = True
                fail_reason = 'fib_cluster_broken'

        elif strategy == 'gap_and_go':
            # Gap: price must hold above gap fill level
            if bar_close < self.key_level:
                thesis_failed = True
                fail_reason = 'gap_filled'

        else:
            # Default: check invalidation level
            if self.invalidation > 0 and bar_close < self.invalidation:
                thesis_failed = True
                fail_reason = 'invalidation_breached'

        if thesis_failed:
            self._log_event('PHASE0_FAIL', fail_reason, price=bar_close,
                            unrealized_r=unrealized_r)
            log.warning(
                "[Lifecycle] %s PHASE0 FAILED | reason=%s bar=%d close=$%.2f "
                "key=$%.2f invalidation=$%.2f",
                self.ticker, fail_reason, self.bars_held, bar_close,
                self.key_level, self.invalidation,
            )
            return ExitDecision(
                action='EXIT', phase=0, reason=f'PHASE0_{fail_reason}',
                exit_price=bar_close, bars_in_phase=self.bars_held,
                unrealized_r=unrealized_r, phase0_passed=False,
            )

        # Phase 0 window complete → advance to Phase 1
        if self.bars_held >= self.phase0_window:
            self._log_event('PHASE0_PASS', f'thesis_valid bars={self.bars_held}',
                            price=bar_close, unrealized_r=unrealized_r)
            self._advance_phase(Phase.PHASE1_PROTECTION)
            log.info(
                "[Lifecycle] %s PHASE0 PASSED → Phase 1 | bars=%d close=$%.2f",
                self.ticker, self.bars_held, bar_close,
            )
            return ExitDecision(
                action='HOLD', phase=1, reason='phase0_passed',
                bars_in_phase=self.bars_held, unrealized_r=unrealized_r,
                phase0_passed=True,
                updated_invalidation=self.invalidation,
                updated_structure_trail=self.structure_trail,
            )

        return ExitDecision(
            action='HOLD', phase=0, reason='phase0_validating',
            bars_in_phase=self.bars_held, unrealized_r=unrealized_r,
        )

    # ── Phase 1: Protection ──────────────────────────────────────────

    def _evaluate_phase1(self, bar_close, bar_low, unrealized_r,
                          is_quote: bool = False) -> ExitDecision:
        """Protection phase: stop only, no indicator exits.

        Three ways to advance to Phase 2:
        1. Price-based: unrealized >= 0.5R
        2. Time-based: phase1_bars elapsed
        3. Structure-based: higher low formed (price building support)
        """
        phase_bars = self.bars_held - self.phase_entry_bar

        # Transition 1: Price-based — reached breakeven trigger
        if unrealized_r >= self.profile.breakeven_trigger_r:
            self._advance_phase(Phase.PHASE2_BREAKEVEN)
            self.trail_stop = self.entry_price  # move to breakeven
            log.info("[Lifecycle] %s → Phase 2 (price: %.1fR) | bars=%d",
                     self.ticker, unrealized_r, phase_bars)
            return ExitDecision(
                action='TIGHTEN_TRAIL', phase=2, reason='breakeven_reached',
                new_stop=self.entry_price, unrealized_r=unrealized_r,
            )

        # Transition 3: Structure-based — higher low forming
        # Only track actual bar lows (quotes would poison with identical bid values)
        if not is_quote:
            self._phase1_lows.append(bar_low)
            if len(self._phase1_lows) > 10:
                self._phase1_lows = self._phase1_lows[-10:]

        if len(self._phase1_lows) >= 2:
            prev_low = self._phase1_lows[-2]
            curr_low = self._phase1_lows[-1]
            # Higher low: current bar's low is above previous bar's low,
            # and price is above entry (building structure)
            if curr_low > prev_low and bar_close > self.entry_price:
                self._advance_phase(Phase.PHASE2_BREAKEVEN)
                log.info("[Lifecycle] %s → Phase 2 (higher low) | bars=%d close=$%.2f",
                         self.ticker, phase_bars, bar_close)
                return ExitDecision(
                    action='HOLD', phase=2, reason='higher_low_formed',
                    bars_in_phase=phase_bars, unrealized_r=unrealized_r,
                )

        # Transition 2: Time-based — phase1_bars elapsed
        if phase_bars >= self.profile.phase1_bars:
            self._advance_phase(Phase.PHASE2_BREAKEVEN)
            log.info("[Lifecycle] %s → Phase 2 (time) | bars=%d unrealized=%.1fR",
                     self.ticker, phase_bars, unrealized_r)

        return ExitDecision(
            action='HOLD', phase=self.phase, reason='phase1_protecting',
            bars_in_phase=phase_bars, unrealized_r=unrealized_r,
        )

    # ── Phase 2: Breakeven ───────────────────────────────────────────

    def _evaluate_phase2(self, bar_high, bar_close, rsi, vwap, unrealized_r,
                          is_quote: bool = False) -> ExitDecision:
        """Breakeven phase: move stop to entry (with cushion), RSI tightens trail mildly.

        V10: Strategy-aware behavior:
        - Impulse setups: true breakeven (0 cushion)
        - Structure setups: breakeven - 0.1R cushion (absorbs noise pullbacks)
        - RSI tightening is MILD in Phase 2 (0.5R, not 0.3R)
        - VWAP: full exit for VWAP strategies, tighten-only for non-VWAP
        - Dead trade: 15 bars for impulse, 30 bars for structure
        """
        phase_bars = self.bars_held - self.phase_entry_bar

        # Move stop to breakeven (with cushion) if reached trigger
        if unrealized_r >= self.profile.breakeven_trigger_r:
            be_price = self.entry_price - self.R * self.profile.breakeven_cushion_r
            if self.trail_stop < be_price:
                self._log_event('BREAKEVEN', f'stop→${be_price:.2f}',
                                price=bar_close, unrealized_r=unrealized_r)
            self.trail_stop = max(self.trail_stop, be_price)

        # RSI trail tightening — MILD in Phase 2
        if self.profile.rsi_tighten and rsi > self.profile.rsi_tighten_threshold:
            tightened = self.running_high - self.R * self.profile.rsi_phase2_trail_r
            if tightened > self.trail_stop:
                self._log_event('RSI_TIGHTEN', f'rsi={rsi:.0f} trail→${tightened:.2f}',
                                price=bar_close, unrealized_r=unrealized_r, rsi=rsi)
            self.trail_stop = max(self.trail_stop, tightened)

        # VWAP monitoring — BAR only (rapid quotes could false-trigger 2-bar breakdown)
        if not is_quote:
            self._check_vwap(bar_close, vwap)
        if self._vwap_breakdown_confirmed():
            if self.profile.vwap_phase2_action == 'exit':
                # VWAP IS the thesis (vwap_reclaim) → full exit
                return ExitDecision(
                    action='EXIT', phase=2, reason='VWAP_BREAKDOWN_thesis',
                    exit_price=bar_close, unrealized_r=unrealized_r,
                )
            else:
                # Non-VWAP strategy → tighten trail, don't exit
                tightened = bar_close - self.R * self.profile.vwap_tighten_r
                self.trail_stop = max(self.trail_stop, tightened)
                self.vwap_below_count = 0  # reset — we tightened, not exited
                self._log_event('VWAP_TIGHTEN', f'trail→${self.trail_stop:.2f}',
                                price=bar_close, unrealized_r=unrealized_r)
                log.info("[Lifecycle] %s Phase 2 VWAP tighten (not exit) | trail=$%.2f",
                         self.ticker, self.trail_stop)

        # Dead trade check — strategy-dependent duration
        if phase_bars >= self.profile.dead_trade_bars:
            if abs(unrealized_r) < self.profile.dead_trade_r_range:
                return ExitDecision(
                    action='EXIT', phase=2, reason='DEAD_TRADE',
                    exit_price=bar_close, unrealized_r=unrealized_r,
                )

        # Phase 3 transition — confluence-aware partial sizing
        if unrealized_r >= self.profile.partial_at_r:
            self._advance_phase(Phase.PHASE3_HARVEST)

            # Confluence-aware partial %:
            # High confluence (3+) → smaller partial (let more ride)
            # Normal (2) → baseline
            # Weak (1) → larger partial (lock more)
            if self._confluence_count >= 3:
                partial_pct = max(0.33, self.profile.partial_pct - 0.17)
            elif self._confluence_count <= 1:
                partial_pct = min(0.66, self.profile.partial_pct + 0.16)
            else:
                partial_pct = self.profile.partial_pct

            # Post-partial stop — confluence-aware:
            # High conviction → looser (entry + 0.25R), let structure trail work
            # Normal → entry + 0.5R
            if self._confluence_count >= 3:
                post_partial_stop = self.entry_price + self.R * 0.25
            else:
                post_partial_stop = self.entry_price + self.R * 0.5

            self._log_event('PARTIAL_EXIT', f'{partial_pct*100:.0f}% at {unrealized_r:.1f}R',
                            price=0, unrealized_r=unrealized_r,
                            partial_pct=partial_pct, confluence=self._confluence_count)
            log.info("[Lifecycle] %s → Phase 3 (HARVEST) | unrealized=%.1fR "
                     "partial=%.0f%% confluence=%d stop=$%.2f",
                     self.ticker, unrealized_r, partial_pct * 100,
                     self._confluence_count, post_partial_stop)
            return ExitDecision(
                action='PARTIAL', phase=3, reason='reached_partial_target',
                partial_pct=partial_pct, unrealized_r=unrealized_r,
                new_stop=post_partial_stop,
            )

        return ExitDecision(
            action='HOLD', phase=2, reason='phase2_managing',
            new_stop=self.trail_stop, unrealized_r=unrealized_r,
        )

    # ── Phase 3: Profit Harvest ──────────────────────────────────────

    def _evaluate_phase3(self, bar_high, bar_close, rsi, vwap, unrealized_r,
                          is_quote: bool = False) -> ExitDecision:
        """Profit harvest: trail under structure, RSI tightens.

        V10: Confluence-aware behavior:
        - Trail width: impulse=0.5R, structure=0.7R min (structure-based preferred)
        - RSI tightening: progressive (Phase 3 = 0.3R)
        - VWAP: full exit for all strategies with vwap_exit configured (profit locked)
        - Phase 4 transition: 2R default, 1.5R for high-conviction trades
        """
        # Trail width — strategy-dependent
        if self.profile.is_impulse:
            # Impulse: tight trail, monetize fast
            trail_width = self.profile.trail_r_width  # 0.5R default
        else:
            # Structure: wider trail, never tighter than 0.7R
            trail_width = max(0.7, self.profile.trail_r_width)

        # Confluence-aware: high conviction → even wider trail
        if self._confluence_count >= 3:
            trail_width = max(trail_width, 0.8)

        base_trail = self.running_high - self.R * trail_width
        # Also consider structure trail from detector
        if self.structure_trail > 0:
            base_trail = max(base_trail, self.structure_trail)
        self.trail_stop = max(self.trail_stop, base_trail)

        # RSI tightening — normal in Phase 3
        if self.profile.rsi_tighten:
            if rsi > 75:
                tight = self.running_high - self.R * self.profile.rsi_phase3_trail_r
                self.trail_stop = max(self.trail_stop, tight)
            elif rsi > self.profile.rsi_tighten_threshold:
                tight = self.running_high - self.R * (self.profile.rsi_phase3_trail_r + 0.1)
                self.trail_stop = max(self.trail_stop, tight)

        # VWAP monitoring — BAR only (Phase 3: profit locked, full exit on confirmed breakdown)
        if not is_quote:
            self._check_vwap(bar_close, vwap)
        if self._vwap_breakdown_confirmed():
            return ExitDecision(
                action='EXIT', phase=3, reason='VWAP_BREAKDOWN_phase3',
                exit_price=bar_close, unrealized_r=unrealized_r,
            )

        # Phase 4 transition — confluence-aware threshold
        runner_threshold = 2.0
        if self._confluence_count >= 3:
            runner_threshold = 1.5  # high conviction → enter runner mode earlier
        if unrealized_r >= runner_threshold:
            self._advance_phase(Phase.PHASE4_RUNNER)
            log.info("[Lifecycle] %s → Phase 4 (RUNNER) | unrealized=%.1fR confluence=%d",
                     self.ticker, unrealized_r, self._confluence_count)

        return ExitDecision(
            action='HOLD', phase=3, reason='phase3_trailing',
            new_stop=self.trail_stop, unrealized_r=unrealized_r,
        )

    # ── Phase 4: Runner ──────────────────────────────────────────────

    def _evaluate_phase4(self, bar_high, bar_close, rsi, vwap, unrealized_r,
                          is_quote: bool = False) -> ExitDecision:
        """Runner phase: let winners run. Structure trail only.

        V10 Runner spec:
        - Structure trail: 5-bar swing low (only ratchets up, never down)
        - VWAP floor: if VWAP > swing low, use VWAP as floor
        - R-based floor: trail never tighter than 0.7R from running high
        - Final trail = WIDEST of (swing low, VWAP floor, R floor)
        - RSI: structure strategies = ignore. Impulse = tighten to 0.5R at RSI>80.
        - No additional partials, no VWAP exits, no fixed targets
        - EOD: force close per existing rules (Phase 4 does NOT override)
        """
        # ── 1. Track 5-bar swing low (only ratchet up) ───────────────
        # Only append actual bar closes (quotes would flood with identical bids)
        if not is_quote:
            self._phase4_lows.append(bar_close)  # use close, not low (less noisy)
            if len(self._phase4_lows) > 5:
                self._phase4_lows = self._phase4_lows[-5:]

        swing_low = min(self._phase4_lows) if self._phase4_lows else bar_close

        # ── 2. VWAP floor (if above swing low, use VWAP) ─────────────
        vwap_floor = 0
        if vwap > 0 and vwap > swing_low:
            vwap_floor = vwap

        # ── 3. R-based floor (never tighter than 0.7R) ──────────────
        r_floor = self.running_high - self.R * max(0.7, self.profile.trail_r_width)

        # ── 4. Structure trail from detector ─────────────────────────
        detector_trail = self.structure_trail if self.structure_trail > 0 else 0

        # ── 5. Final trail = WIDEST (most room to breathe) ───────────
        candidates = [swing_low, r_floor]
        if vwap_floor > 0:
            candidates.append(vwap_floor)
        if detector_trail > 0:
            candidates.append(detector_trail)

        best_trail = min(candidates)  # widest = lowest stop = most room
        # But never move trail DOWN — only ratchet up
        self.trail_stop = max(self.trail_stop, best_trail)

        # ── 6. RSI behavior — strategy-dependent ─────────────────────
        if self.profile.is_impulse:
            # Impulse (momentum, sweep, gap): RSI > 80 tightens to 0.5R
            if rsi > 80:
                tight = self.running_high - self.R * 0.5
                self.trail_stop = max(self.trail_stop, tight)
        # else: structure strategies → RSI irrelevant in Phase 4

        # No VWAP exits, no partials, no fixed targets in Phase 4
        return ExitDecision(
            action='HOLD', phase=4, reason='phase4_running',
            new_stop=self.trail_stop, unrealized_r=unrealized_r,
        )

    # ── Helpers ───────────────────────────────────────────────────────

    def _log_event(self, event_type: str, detail: str = '', **kwargs):
        """Record a lifecycle event for DB persistence and ML training."""
        entry = {
            'bar': self.bars_held,
            'phase': int(self.phase),
            'event': event_type,
            'detail': detail,
            'price': kwargs.get('price', 0),
            'trail_stop': round(self.trail_stop, 4),
            'unrealized_r': round(kwargs.get('unrealized_r', 0), 3),
        }
        entry.update({k: v for k, v in kwargs.items()
                      if k not in ('price', 'unrealized_r')})
        self._events.append(entry)

    def _advance_phase(self, new_phase: Phase):
        old = self.phase
        self.phase = new_phase
        self.phase_entry_bar = self.bars_held
        self._log_event('PHASE_TRANSITION', f'{old}→{int(new_phase)}',
                        from_phase=int(old), to_phase=int(new_phase))

    def _check_vwap(self, bar_close: float, vwap: float):
        """Track consecutive bars below VWAP."""
        if vwap <= 0:
            return
        if bar_close < vwap:
            self.vwap_below_count += 1
        else:
            self.vwap_below_count = 0

    def _vwap_breakdown_confirmed(self) -> bool:
        """Check if VWAP breakdown is confirmed (per profile)."""
        if self.profile.vwap_exit is None:
            return False
        if self.profile.vwap_exit == '2bar':
            return self.vwap_below_count >= self.profile.vwap_confirm_bars
        return False

    def update_structure(self, new_key_level: float = 0, new_trail: float = 0):
        """Update structure levels as new bars form (e.g., higher lows)."""
        if new_key_level > 0:
            self.key_level = new_key_level
        if new_trail > 0 and new_trail > self.structure_trail:
            self.structure_trail = new_trail
            # Tighten trail to new structure
            self.trail_stop = max(self.trail_stop, new_trail)

    def to_dict(self) -> dict:
        """Serialize for logging/persistence."""
        return {
            'ticker': self.ticker,
            'strategy': self.strategy,
            'phase': self.phase,
            'bars_held': self.bars_held,
            'entry_price': self.entry_price,
            'stop_price': self.stop_price,
            'trail_stop': self.trail_stop,
            'running_high': self.running_high,
            'R': self.R,
            'key_level': self.key_level,
            'invalidation': self.invalidation,
            'partial_done': self.partial_done,
            'confluence_count': self._confluence_count,
            'is_impulse': self.profile.is_impulse,
            'events': self._events,  # full decision log for ML
        }
