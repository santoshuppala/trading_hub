"""
Options Position Lifecycle — Risk-regime exit engine.

Replaces flat threshold exits with a 3-phase lifecycle driven by:
  Phase  = f(DTE)           — predictable baseline regime
  Override = f(moneyness)   — escalates phase when strike is threatened
  Behavior = f(Greeks)      — adaptive within each phase

Three phases:
  Phase 1: OPEN       (DTE > 21)   — thesis validation, normal management
  Phase 2: THETA      (21 ≥ DTE > 10) — theta accelerating, tighten targets
  Phase 3: CLOSE/ROLL (DTE ≤ 10)   — close or roll, no exceptions for debit

Greeks state machine (3 dimensions):
  vega_state:  FAVORABLE / ADVERSE  (IV helping or hurting since entry)
  theta_state: FAVORABLE / ADVERSE  (time decay helping or hurting)
  delta_state: FAVORABLE / ADVERSE  (underlying moving for or against)

  Each strategy type has a FATAL signal (one → EXIT) and TOLERABLE signals:
    Credit:     fatal=delta_adverse   tolerable=vega_adverse
    Debit:      fatal=vega_adverse    tolerable=theta_adverse
    Volatility: fatal=vega_adverse    tolerable=delta_adverse

Usage:
    lifecycle = OptionsPositionLifecycle(
        ticker='AAPL', strategy_type='iron_condor',
        entry_cost=-140.0, max_risk=360.0, max_reward=140.0,
        entry_iv=0.32, entry_delta=0.05, entry_underlying=185.0,
        short_strikes=(190.0, 175.0), atr=2.50, dte_at_entry=35,
    )

    decision = lifecycle.evaluate(
        spot=186.0, current_value=-80.0, dte=28,
        current_iv=0.35, current_delta=0.12, current_theta=-0.04,
        current_gamma=0.02, current_vega=0.15,
    )

    if decision.action == 'EXIT':
        close_position(reason=decision.reason)
    elif decision.action == 'ROLL':
        roll_position(reason=decision.reason)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Tuple

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE & DECISION
# ═══════════════════════════════════════════════════════════════════════════════

class OptionsPhase(IntEnum):
    PHASE1_OPEN = 1       # DTE > 21 — normal management
    PHASE2_THETA = 2      # 21 ≥ DTE > 10 — theta accelerating
    PHASE3_CLOSE = 3      # DTE ≤ 10 — close or roll


class GreeksState:
    """Discrete state for one Greeks dimension."""
    FAVORABLE = 'FAVORABLE'
    ADVERSE = 'ADVERSE'
    NEUTRAL = 'NEUTRAL'


@dataclass
class OptionsExitDecision:
    """Structured decision from lifecycle evaluation."""
    action: str            # 'HOLD', 'EXIT', 'TIGHTEN', 'ROLL'
    phase: int
    reason: str
    exit_price: float = 0.0
    new_stop_pnl: float = 0.0   # tightened stop loss P&L threshold
    roll_target_dte: int = 0     # target DTE for roll
    pnl: float = 0.0
    pnl_pct: float = 0.0        # P&L as % of max_risk
    dte: int = 0
    adverse_count: int = 0
    vega_state: str = ''
    theta_state: str = ''
    delta_state: str = ''


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY CATEGORIES
# ═══════════════════════════════════════════════════════════════════════════════

_CREDIT_STRATEGIES = {
    'bull_put_spread', 'bear_call_spread',
    'iron_condor', 'iron_butterfly',
}
_DEBIT_STRATEGIES = {
    'long_call', 'long_put',
    'bull_call_spread', 'bear_put_spread',
    'butterfly_spread',
}
_VOLATILITY_STRATEGIES = {
    'long_straddle', 'long_strangle',
}
_TIME_STRATEGIES = {
    'calendar_spread', 'diagonal_spread',
}

# Strategies where the underlying going AGAINST the position is fatal
_BULLISH_STRATEGIES = {
    'long_call', 'bull_call_spread', 'bull_put_spread',
}
_BEARISH_STRATEGIES = {
    'long_put', 'bear_put_spread', 'bear_call_spread',
}


# ═══════════════════════════════════════════════════════════════════════════════
# OPTIONS POSITION LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════════════

class OptionsPositionLifecycle:
    """Manages exit lifecycle for one open options position.

    Created when a position opens. Called on each monitoring pass with
    current Greeks, IV, and underlying price. Returns an OptionsExitDecision.
    """

    # DTE boundaries
    PHASE2_DTE = 21   # theta starts accelerating
    PHASE3_DTE = 10   # gamma risk, close/roll window

    # Moneyness pressure: continuous 0→1 gradient instead of binary threshold
    # 0.0 = safe (>1.5 ATR from strike), 1.0 = danger (at strike)
    MONEYNESS_PRESSURE_ATR = 1.5   # full danger zone width in ATR

    # Weighted risk score thresholds (replaces naive adverse count)
    RISK_SCORE_EXIT = 2.2      # exit
    RISK_SCORE_TIGHTEN = 1.2   # tighten stops

    def __init__(
        self,
        ticker: str,
        strategy_type: str,
        entry_cost: float,        # positive=debit, negative=credit
        max_risk: float,
        max_reward: float,
        entry_iv: float,          # IV at entry (for vega state tracking)
        entry_delta: float,       # net delta at entry
        entry_underlying: float,  # underlying price at entry
        short_strikes: Tuple[float, ...] = (),  # short strike(s) for moneyness
        long_strikes: Tuple[float, ...] = (),   # long strike(s)
        atr: float = 0.0,        # underlying ATR for moneyness scaling
        dte_at_entry: int = 30,
    ):
        self.ticker = ticker
        self.strategy_type = strategy_type
        self.entry_cost = entry_cost
        self.max_risk = max_risk if max_risk > 0 else abs(entry_cost) * 2
        self.max_reward = max_reward if max_reward > 0 else abs(entry_cost)
        self.entry_iv = entry_iv
        self.entry_delta = entry_delta
        self.entry_underlying = entry_underlying
        self.short_strikes = short_strikes
        self.long_strikes = long_strikes
        self.atr = atr if atr > 0 else entry_underlying * 0.01
        self.dte_at_entry = dte_at_entry

        # Derived
        self.is_credit = strategy_type in _CREDIT_STRATEGIES
        self.is_debit = strategy_type in _DEBIT_STRATEGIES
        self.is_volatility = strategy_type in _VOLATILITY_STRATEGIES
        self.is_time = strategy_type in _TIME_STRATEGIES
        self.is_bullish = strategy_type in _BULLISH_STRATEGIES
        self.is_bearish = strategy_type in _BEARISH_STRATEGIES

        # Credit received (positive value, for stop calculation)
        self.credit_received = abs(entry_cost) if self.is_credit else 0.0

        # R = risk unit for normalized move calculations
        self.R = max_risk if max_risk > 0 else self.atr * 100

        # State
        self.phase = OptionsPhase.PHASE1_OPEN
        self.checks_count = 0
        self.entry_mono = time.monotonic()
        self.best_pnl = 0.0       # high-water mark for trailing
        self.worst_pnl = 0.0      # low-water mark
        self.moneyness_pressure = 0.0  # continuous 0→1 gradient

        # Event log (significant events: phase transitions, exits, etc.)
        self._events: list = []
        # V10: Periodic snapshots for PF analysis (Greeks + risk score trajectory)
        self._snapshots: list = []

        log.info(
            "[OptionsLifecycle] %s OPENED | strategy=%s phase=1 "
            "entry_cost=$%.2f max_risk=$%.2f max_reward=$%.2f "
            "entry_iv=%.2f dte=%d short_strikes=%s",
            ticker, strategy_type, entry_cost, max_risk, max_reward,
            entry_iv, dte_at_entry, short_strikes,
        )

    # ── Main evaluation ──────────────────────────────────────────────────

    # V10: Per-check snapshots for PF analysis (every Nth check to limit size)
    _SNAPSHOT_EVERY = 5  # snapshot every 5th check (~2.5 min at 30s intervals)

    def evaluate(
        self,
        spot: float,              # current underlying price
        current_value: float,     # current mark-to-market of position
        dte: int,                 # days to expiry
        current_iv: float = 0.0,  # current implied volatility
        current_delta: float = 0.0,
        current_theta: float = 0.0,
        current_gamma: float = 0.0,
        current_vega: float = 0.0,
    ) -> OptionsExitDecision:
        """Evaluate exit decision based on current market state."""
        self.checks_count += 1

        # ── P&L calculation ──────────────────────────────────────────
        pnl = current_value - self.entry_cost
        pnl_pct = pnl / self.max_risk if self.max_risk > 0 else 0
        self.best_pnl = max(self.best_pnl, pnl)
        self.worst_pnl = min(self.worst_pnl, pnl)

        # ── Determine phase (DTE-based + moneyness pressure) ──────────
        base_phase = self._compute_phase(dte)
        moneyness_distance = self._moneyness_distance(spot)
        phase = base_phase

        # Continuous moneyness pressure: 0.0 (safe) → 1.0 (at strike)
        if self.short_strikes and self.atr > 0:
            self.moneyness_pressure = max(
                0.0, 1.0 - moneyness_distance / self.MONEYNESS_PRESSURE_ATR)
        else:
            self.moneyness_pressure = 0.0

        # Phase escalation: high pressure (>0.7 = within ~0.45 ATR) → Phase 3
        if self.moneyness_pressure > 0.7:
            phase = max(phase, OptionsPhase.PHASE3_CLOSE)
            if phase > base_phase:
                self._log_event('MONEYNESS_ESCALATION',
                                f'pressure={self.moneyness_pressure:.2f}, '
                                f'distance={moneyness_distance:.2f} ATR, '
                                f'base_phase={base_phase}→{phase}')

        if phase != self.phase:
            old = self.phase
            self.phase = phase
            self._log_event('PHASE_TRANSITION', f'{old}→{phase}')

        # ── Greeks state machine ─────────────────────────────────────
        vega_state = self._vega_state(current_iv)
        theta_state = self._theta_state(dte)
        delta_state = self._delta_state(spot, current_delta)

        # ── Weighted risk score (replaces naive adverse count) ───────
        risk_score = self._compute_risk_score(
            vega_state, theta_state, delta_state)

        # ── Strategy-aware fatal signal check ────────────────────────
        fatal = self._check_fatal_signal(vega_state, theta_state, delta_state)

        # Derive adverse_count for reporting (from risk_score)
        adverse_count = sum(1 for s in [vega_state, theta_state, delta_state]
                            if s == GreeksState.ADVERSE)

        # ── Universal stops (all phases) ─────────────────────────────
        # Credit: phase-aware stop (tighter as DTE decreases)
        if self.is_credit and self.credit_received > 0:
            credit_mult = 1.5 if phase >= OptionsPhase.PHASE2_THETA else 2.0
            # Moneyness pressure tightens stop further
            credit_mult -= self.moneyness_pressure * 0.3  # at pressure=1.0 → mult reduced by 0.3
            credit_stop = -credit_mult * self.credit_received
            if pnl <= credit_stop:
                self._log_event('CREDIT_STOP',
                                f'pnl=${pnl:.2f} <= {credit_mult:.1f}x credit=${credit_stop:.2f}')
                return self._decision('EXIT', f'CREDIT_STOP (pnl=${pnl:.2f})',
                                      pnl=pnl, pnl_pct=pnl_pct, dte=dte,
                                      adverse_count=adverse_count,
                                      vega_state=vega_state, theta_state=theta_state,
                                      delta_state=delta_state)

        # Debit: stop at fraction of entry cost
        if self.is_debit and self.entry_cost > 0:
            debit_stop_pct = 0.50 if phase <= OptionsPhase.PHASE1_OPEN else 0.40
            debit_stop = -self.entry_cost * debit_stop_pct
            if pnl <= debit_stop:
                self._log_event('DEBIT_STOP',
                                f'pnl=${pnl:.2f} <= {debit_stop_pct:.0%} of entry')
                return self._decision('EXIT', f'DEBIT_STOP (pnl=${pnl:.2f})',
                                      pnl=pnl, pnl_pct=pnl_pct, dte=dte,
                                      adverse_count=adverse_count,
                                      vega_state=vega_state, theta_state=theta_state,
                                      delta_state=delta_state)

        # Volatility: stop at 50% of entry cost
        if self.is_volatility and self.entry_cost > 0:
            vol_stop = -self.entry_cost * 0.50
            if pnl <= vol_stop:
                self._log_event('VOL_STOP', f'pnl=${pnl:.2f}')
                return self._decision('EXIT', f'VOL_STOP (pnl=${pnl:.2f})',
                                      pnl=pnl, pnl_pct=pnl_pct, dte=dte,
                                      adverse_count=adverse_count,
                                      vega_state=vega_state, theta_state=theta_state,
                                      delta_state=delta_state)

        # ── Fatal signal → EXIT immediately ──────────────────────────
        if fatal:
            self._log_event('FATAL_SIGNAL', fatal)
            return self._decision('EXIT', f'FATAL_{fatal}',
                                  pnl=pnl, pnl_pct=pnl_pct, dte=dte,
                                  adverse_count=adverse_count,
                                  vega_state=vega_state, theta_state=theta_state,
                                  delta_state=delta_state)

        # ── V10: Periodic snapshot for PF analysis ────────────────────
        # Captures full state every Nth check. On close, this data + the
        # exit event gives complete P&L curve + Greeks trajectory + risk
        # score history for threshold calibration.
        if self.checks_count % self._SNAPSHOT_EVERY == 0:
            self._snapshots.append({
                'check': self.checks_count,
                'ts': time.time(),
                'phase': int(phase),
                'dte': dte,
                'spot': round(spot, 4),
                'pnl': round(pnl, 2),
                'pnl_pct': round(pnl_pct, 4),
                'best_pnl': round(self.best_pnl, 2),
                'worst_pnl': round(self.worst_pnl, 2),
                'iv': round(current_iv, 4),
                'iv_change_pct': round((current_iv - self.entry_iv) / self.entry_iv, 4) if self.entry_iv > 0 else 0,
                'delta': round(current_delta, 4),
                'theta': round(current_theta, 4),
                'vega': round(current_vega, 4),
                'risk_score': round(risk_score, 3),
                'moneyness_pressure': round(self.moneyness_pressure, 4),
                'vega_state': vega_state,
                'theta_state': theta_state,
                'delta_state': delta_state,
            })
            # Cap at 200 snapshots (~16 hours of data at 5-min intervals)
            if len(self._snapshots) > 200:
                self._snapshots = self._snapshots[-200:]

        # ── Phase-specific logic ─────────────────────────────────────
        if phase == OptionsPhase.PHASE1_OPEN:
            return self._evaluate_phase1(
                spot, pnl, pnl_pct, dte, current_iv,
                vega_state, theta_state, delta_state, risk_score, adverse_count)

        elif phase == OptionsPhase.PHASE2_THETA:
            return self._evaluate_phase2(
                spot, pnl, pnl_pct, dte, current_iv,
                vega_state, theta_state, delta_state, risk_score, adverse_count)

        elif phase == OptionsPhase.PHASE3_CLOSE:
            return self._evaluate_phase3(
                spot, pnl, pnl_pct, dte,
                vega_state, theta_state, delta_state, risk_score, adverse_count)

        return self._decision('HOLD', 'default',
                              pnl=pnl, pnl_pct=pnl_pct, dte=dte,
                              adverse_count=adverse_count,
                              vega_state=vega_state, theta_state=theta_state,
                              delta_state=delta_state)

    # ── Phase 1: OPEN (DTE > 21) ────────────────────────────────────

    def _evaluate_phase1(self, spot, pnl, pnl_pct, dte, current_iv,
                          vega_state, theta_state, delta_state,
                          risk_score, adverse_count) -> OptionsExitDecision:
        """Normal management phase. Profit targets, fast failure, IV crush."""
        kw = dict(pnl=pnl, pnl_pct=pnl_pct, dte=dte,
                  adverse_count=adverse_count,
                  vega_state=vega_state, theta_state=theta_state,
                  delta_state=delta_state)

        # ── Fast failure: normalized by R (consistent across symbols) ─
        if self.checks_count <= 5 and self.R > 0:
            move_r = (spot - self.entry_underlying) / (self.R / 100)  # R is dollar-based
            if self.is_bullish and move_r < -1.0:
                self._log_event('FAST_FAILURE', f'move={move_r:.2f}R')
                return self._decision('EXIT', f'FAST_FAILURE_BEARISH (move={move_r:.1f}R)', **kw)
            elif self.is_bearish and move_r > 1.0:
                self._log_event('FAST_FAILURE', f'move={move_r:.2f}R')
                return self._decision('EXIT', f'FAST_FAILURE_BULLISH (move={move_r:.1f}R)', **kw)

        # ── IV crush exit (long vol / debit positions) ───────────────
        if (self.is_volatility or self.is_debit) and self.entry_iv > 0 and current_iv > 0:
            iv_change_pct = (current_iv - self.entry_iv) / self.entry_iv
            if iv_change_pct < -0.15:  # IV dropped >15% from entry
                self._log_event('IV_CRUSH',
                                f'iv={current_iv:.2f} vs entry={self.entry_iv:.2f} '
                                f'({iv_change_pct:.0%})')
                return self._decision('EXIT',
                    f'IV_CRUSH (iv={current_iv:.2f}, entry={self.entry_iv:.2f}, '
                    f'change={iv_change_pct:.0%})', **kw)

        # ── Profit target ────────────────────────────────────────────
        if self.is_credit:
            target = self.max_reward * 0.50
        elif self.is_volatility:
            target = self.max_reward * 0.60
        else:
            target = self.max_reward * 0.80

        if pnl >= target:
            self._log_event('PROFIT_TARGET', f'pnl=${pnl:.2f} >= target=${target:.2f}')
            return self._decision('EXIT', f'PROFIT_TARGET (pnl=${pnl:.2f})', **kw)

        # ── Weighted risk score → EXIT or TIGHTEN ────────────────────
        if risk_score >= self.RISK_SCORE_EXIT:
            if pnl > 0:
                self._log_event('RISK_EXIT_PROFIT',
                                f'risk_score={risk_score:.2f}, pnl=${pnl:.2f}')
                return self._decision('EXIT',
                    f'RISK_SCORE_EXIT (score={risk_score:.2f}, pnl=${pnl:.2f})', **kw)
            else:
                self._log_event('RISK_EXIT_LOSS',
                                f'risk_score={risk_score:.2f}, pnl=${pnl:.2f}')
                return self._decision('EXIT',
                    f'RISK_SCORE_EXIT (score={risk_score:.2f}, pnl=${pnl:.2f})', **kw)
        elif risk_score >= self.RISK_SCORE_TIGHTEN:
            return self._decision('TIGHTEN',
                f'RISK_TIGHTEN (score={risk_score:.2f})', **kw)

        # ── Stall detection (debit/vol: time passing, no progress) ───
        if (self.is_debit or self.is_volatility) and self.dte_at_entry > 0:
            bars_expected = max(1, self.dte_at_entry - dte)
            progress_pct = bars_expected / self.dte_at_entry  # 0→1 over life
            if progress_pct > 0.3 and pnl < self.max_reward * 0.1:
                # 30%+ of time used, less than 10% of target reached
                self._log_event('STALL',
                                f'progress={progress_pct:.0%}, pnl=${pnl:.2f}')
                return self._decision('TIGHTEN',
                    f'STALL_DEBIT (progress={progress_pct:.0%}, pnl=${pnl:.2f})', **kw)

        return self._decision('HOLD', f'phase1_normal (risk={risk_score:.2f})', **kw)

    # ── Phase 2: THETA (21 ≥ DTE > 10) ──────────────────────────────

    def _evaluate_phase2(self, spot, pnl, pnl_pct, dte, current_iv,
                          vega_state, theta_state, delta_state,
                          risk_score, adverse_count) -> OptionsExitDecision:
        """Theta acceleration phase. Tighter targets, bias to exit for debit."""
        kw = dict(pnl=pnl, pnl_pct=pnl_pct, dte=dte,
                  adverse_count=adverse_count,
                  vega_state=vega_state, theta_state=theta_state,
                  delta_state=delta_state)

        # ── Debit positions: theta is your enemy now ─────────────────
        if self.is_debit or self.is_volatility:
            target = self.max_reward * 0.50
            if pnl >= target:
                self._log_event('PROFIT_TARGET_THETA',
                                f'pnl=${pnl:.2f} >= target=${target:.2f} (theta phase)')
                return self._decision('EXIT',
                    f'PROFIT_TARGET_THETA (pnl=${pnl:.2f}, DTE={dte})', **kw)

            # IV crush tighter threshold in theta phase
            if self.entry_iv > 0 and current_iv > 0:
                iv_change_pct = (current_iv - self.entry_iv) / self.entry_iv
                if iv_change_pct < -0.10:
                    self._log_event('IV_CRUSH_THETA',
                                    f'iv_change={iv_change_pct:.0%} in theta phase')
                    return self._decision('EXIT',
                        f'IV_CRUSH_THETA (change={iv_change_pct:.0%}, DTE={dte})', **kw)

            # Debit: any risk + break-even or better → exit
            if risk_score >= self.RISK_SCORE_TIGHTEN and pnl >= 0:
                return self._decision('EXIT',
                    f'DEBIT_THETA_EXIT (risk={risk_score:.2f}, pnl=${pnl:.2f})', **kw)

            # Stall in theta phase is more urgent
            if self.dte_at_entry > 0:
                progress_pct = max(1, self.dte_at_entry - dte) / self.dte_at_entry
                if progress_pct > 0.4 and pnl < self.max_reward * 0.15:
                    return self._decision('EXIT',
                        f'STALL_THETA (progress={progress_pct:.0%}, pnl=${pnl:.2f})', **kw)

        # ── Credit positions: theta is your friend ───────────────────
        if self.is_credit:
            target = self.max_reward * 0.40
            if pnl >= target:
                self._log_event('PROFIT_TARGET_THETA',
                                f'credit pnl=${pnl:.2f} >= target=${target:.2f}')
                return self._decision('EXIT',
                    f'PROFIT_TARGET_THETA_CREDIT (pnl=${pnl:.2f})', **kw)

            # IV spike on credit → tighten
            if self.entry_iv > 0 and current_iv > 0:
                iv_change_pct = (current_iv - self.entry_iv) / self.entry_iv
                if iv_change_pct > 0.20:
                    self._log_event('IV_SPIKE_CREDIT',
                                    f'iv_change={iv_change_pct:.0%} in theta phase')
                    return self._decision('TIGHTEN',
                        f'IV_SPIKE_CREDIT (change={iv_change_pct:.0%})', **kw)

        # ── Risk score → exit in theta phase ─────────────────────────
        if risk_score >= self.RISK_SCORE_EXIT:
            self._log_event('RISK_EXIT_THETA', f'risk={risk_score:.2f}')
            return self._decision('EXIT',
                f'RISK_SCORE_THETA (score={risk_score:.2f}, DTE={dte})', **kw)

        return self._decision('HOLD', f'phase2_theta (DTE={dte}, risk={risk_score:.2f})', **kw)

    # ── Phase 3: CLOSE/ROLL (DTE ≤ 10) ──────────────────────────────

    def _evaluate_phase3(self, spot, pnl, pnl_pct, dte,
                          vega_state, theta_state, delta_state,
                          risk_score, adverse_count) -> OptionsExitDecision:
        """Close or roll window. Gamma risk too high for debit/vol.
        Credit may roll if profitable AND safe."""
        kw = dict(pnl=pnl, pnl_pct=pnl_pct, dte=dte,
                  adverse_count=adverse_count,
                  vega_state=vega_state, theta_state=theta_state,
                  delta_state=delta_state)

        # ── Debit / Volatility: close everything ─────────────────────
        if self.is_debit or self.is_volatility:
            self._log_event('PHASE3_CLOSE_DEBIT', f'DTE={dte} pnl=${pnl:.2f}')
            return self._decision('EXIT',
                f'DTE_CLOSE (DTE={dte}, strategy={self.strategy_type})', **kw)

        # ── Credit: roll quality check ───────────────────────────────
        # Only roll if: profitable enough AND safe distance from strikes
        # Otherwise you're just rolling losers forward (classic retail trap)
        if self.is_credit:
            moneyness_distance = self._moneyness_distance(spot)
            roll_worthy = (
                pnl > self.max_reward * 0.25                  # profitable enough
                and moneyness_distance > 1.0                  # safe from strikes
                and vega_state != GreeksState.ADVERSE          # IV not spiking against
            )
            if roll_worthy:
                self._log_event('PHASE3_ROLL',
                                f'DTE={dte} pnl=${pnl:.2f} distance={moneyness_distance:.1f} ATR')
                return self._decision('ROLL',
                    f'DTE_ROLL (DTE={dte}, pnl=${pnl:.2f}, dist={moneyness_distance:.1f}ATR)',
                    roll_target_dte=self.dte_at_entry, **kw)
            else:
                reason = []
                if pnl <= self.max_reward * 0.25:
                    reason.append(f'low_profit=${pnl:.2f}')
                if moneyness_distance <= 1.0:
                    reason.append(f'near_strike={moneyness_distance:.1f}ATR')
                if vega_state == GreeksState.ADVERSE:
                    reason.append('iv_adverse')
                self._log_event('PHASE3_CLOSE_CREDIT',
                                f'DTE={dte} pnl=${pnl:.2f} (roll rejected: {", ".join(reason)})')
                return self._decision('EXIT',
                    f'DTE_CLOSE_CREDIT (DTE={dte}, {", ".join(reason)})', **kw)

        # ── Time strategies (calendar/diagonal) ──────────────────────
        if self.is_time:
            self._log_event('PHASE3_CLOSE_TIME', f'DTE={dte}')
            return self._decision('EXIT',
                f'DTE_CLOSE_TIME (DTE={dte})', **kw)

        # Fallback
        return self._decision('EXIT', f'DTE_CLOSE_FALLBACK (DTE={dte})', **kw)

    # ── Greeks State Machine ─────────────────────────────────────────

    # Minimum IV move to be considered ADVERSE (prevents noise triggering fatal)
    _IV_ADVERSE_THRESHOLD = 0.08  # 8% relative IV change

    def _vega_state(self, current_iv: float) -> str:
        """Is IV helping or hurting since entry?

        Uses materiality threshold: small IV moves are NEUTRAL, not ADVERSE.
        This prevents a 2% IV dip from triggering a fatal exit on debit positions.
        """
        if self.entry_iv <= 0 or current_iv <= 0:
            return GreeksState.NEUTRAL

        iv_change_pct = (current_iv - self.entry_iv) / self.entry_iv

        if self.is_credit:
            # Credit: benefits from IV contraction
            if iv_change_pct < -self._IV_ADVERSE_THRESHOLD:
                return GreeksState.FAVORABLE
            elif iv_change_pct > self._IV_ADVERSE_THRESHOLD:
                return GreeksState.ADVERSE
            return GreeksState.NEUTRAL
        elif self.is_debit or self.is_volatility:
            # Debit/vol: benefits from IV expansion
            if iv_change_pct > self._IV_ADVERSE_THRESHOLD:
                return GreeksState.FAVORABLE
            elif iv_change_pct < -self._IV_ADVERSE_THRESHOLD:
                return GreeksState.ADVERSE
            return GreeksState.NEUTRAL
        return GreeksState.NEUTRAL

    def _theta_state(self, dte: int) -> str:
        """Is time decay helping or hurting?"""
        if self.is_credit:
            # Credit: theta always helps, but accelerates in Phase 2
            return GreeksState.FAVORABLE
        elif self.is_debit or self.is_volatility:
            # Debit/vol: theta always hurts, gets worse in Phase 2
            if dte <= self.PHASE2_DTE:
                return GreeksState.ADVERSE
            return GreeksState.NEUTRAL  # still hurts but not critical yet
        return GreeksState.NEUTRAL

    def _delta_state(self, spot: float, current_delta: float) -> str:
        """Is the underlying moving in your favor?"""
        move = spot - self.entry_underlying

        if self.is_bullish:
            return GreeksState.FAVORABLE if move >= 0 else GreeksState.ADVERSE
        elif self.is_bearish:
            return GreeksState.FAVORABLE if move <= 0 else GreeksState.ADVERSE
        elif self.is_volatility:
            # Volatility: big move in either direction is good
            if abs(move) > self.atr * 0.5:
                return GreeksState.FAVORABLE
            return GreeksState.NEUTRAL
        elif self.is_credit:
            # Credit (neutral): want underlying to STAY near entry
            # Adverse if moved > 1 ATR in either direction
            if abs(move) > self.atr:
                return GreeksState.ADVERSE
            return GreeksState.FAVORABLE

        return GreeksState.NEUTRAL

    def _compute_risk_score(self, vega_state, theta_state, delta_state) -> float:
        """Weighted risk score — replaces naive adverse count.

        Different weights by strategy type (not all adverse signals are equal).
        Moneyness pressure boosts delta weight for credit strategies.
        Returns 0.0 (safe) → 3.0+ (danger).
        """
        score = 0.0

        if self.is_credit:
            # Credit: delta is most dangerous (moneyness), vega is temporary
            delta_weight = 1.5 + self.moneyness_pressure * 0.5  # 1.5 → 2.0 as pressure rises
            vega_weight = 0.8
            theta_weight = 0.5  # theta rarely adverse for credit
        elif self.is_debit:
            # Debit: vega is most dangerous (IV crush), theta is expected
            delta_weight = 1.2
            vega_weight = 1.5
            theta_weight = 0.7  # expected cost, not surprising
        elif self.is_volatility:
            # Vol: vega is existential, delta is tolerable (neutral by design)
            delta_weight = 0.5
            vega_weight = 1.8
            theta_weight = 1.0
        else:
            # Time strategies / unknown
            delta_weight = 1.0
            vega_weight = 1.0
            theta_weight = 1.0

        if delta_state == GreeksState.ADVERSE:
            score += delta_weight
        if vega_state == GreeksState.ADVERSE:
            score += vega_weight
        if theta_state == GreeksState.ADVERSE:
            score += theta_weight

        return score

    def _check_fatal_signal(self, vega_state, theta_state, delta_state) -> Optional[str]:
        """Check for a single fatal signal that warrants immediate exit.

        Fatal = the ONE thing that kills this strategy type.
        Only fires when the adverse state is significant (materiality
        threshold already applied in _vega_state / _delta_state).
        """
        if self.is_credit:
            # Credit: delta adverse with high moneyness pressure = fatal
            if delta_state == GreeksState.ADVERSE and self.moneyness_pressure > 0.5:
                return f'DELTA_ADVERSE_CREDIT (pressure={self.moneyness_pressure:.2f})'

        elif self.is_debit:
            # Debit: vega adverse = fatal (IV crush kills premium)
            if vega_state == GreeksState.ADVERSE:
                return 'VEGA_ADVERSE_DEBIT (IV compressing)'

        elif self.is_volatility:
            # Volatility: vega adverse = fatal (IV mean-reverting)
            if vega_state == GreeksState.ADVERSE:
                return 'VEGA_ADVERSE_VOL (IV contracting)'

        return None

    # ── Moneyness ────────────────────────────────────────────────────

    def _moneyness_distance(self, spot: float) -> float:
        """Distance from underlying to nearest short strike, in ATR units.
        Returns float('inf') if no short strikes."""
        if not self.short_strikes or self.atr <= 0:
            return float('inf')

        min_distance = min(abs(spot - strike) for strike in self.short_strikes)
        return min_distance / self.atr

    def _compute_phase(self, dte: int) -> OptionsPhase:
        """Compute phase from DTE alone (before moneyness override)."""
        if dte <= self.PHASE3_DTE:
            return OptionsPhase.PHASE3_CLOSE
        elif dte <= self.PHASE2_DTE:
            return OptionsPhase.PHASE2_THETA
        return OptionsPhase.PHASE1_OPEN

    # ── Helpers ──────────────────────────────────────────────────────

    def _decision(self, action, reason, **kwargs) -> OptionsExitDecision:
        return OptionsExitDecision(
            action=action,
            phase=self.phase,
            reason=reason,
            pnl=kwargs.get('pnl', 0.0),
            pnl_pct=kwargs.get('pnl_pct', 0.0),
            dte=kwargs.get('dte', 0),
            adverse_count=kwargs.get('adverse_count', 0),
            vega_state=kwargs.get('vega_state', ''),
            theta_state=kwargs.get('theta_state', ''),
            delta_state=kwargs.get('delta_state', ''),
            new_stop_pnl=kwargs.get('new_stop_pnl', 0.0),
            roll_target_dte=kwargs.get('roll_target_dte', 0),
        )

    def _log_event(self, event_type: str, detail: str = ''):
        entry = {
            'check': self.checks_count,
            'phase': int(self.phase),
            'event': event_type,
            'detail': detail,
            'ts': time.time(),
        }
        self._events.append(entry)
        log.info("[OptionsLifecycle] %s | %s | %s", self.ticker, event_type, detail)

    def to_dict(self) -> dict:
        """Serialize for persistence."""
        return {
            'ticker': self.ticker,
            'strategy_type': self.strategy_type,
            'phase': int(self.phase),
            'checks_count': self.checks_count,
            'entry_cost': self.entry_cost,
            'max_risk': self.max_risk,
            'max_reward': self.max_reward,
            'entry_iv': self.entry_iv,
            'entry_delta': self.entry_delta,
            'entry_underlying': self.entry_underlying,
            'short_strikes': list(self.short_strikes),
            'long_strikes': list(self.long_strikes),
            'atr': self.atr,
            'dte_at_entry': self.dte_at_entry,
            'credit_received': self.credit_received,
            'best_pnl': self.best_pnl,
            'worst_pnl': self.worst_pnl,
            'moneyness_pressure': self.moneyness_pressure,
            'R': self.R,
            'events': self._events,
            'snapshots': self._snapshots,
        }
