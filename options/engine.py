"""
T3.7 — Options Engine

Main orchestrator for options trading subsystem.
Handles entry selection, execution, position monitoring, and exit management.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

from monitor.event_bus import Event, EventBus, EventType
from monitor.events import OptionsSignalPayload, SignalAction
from monitor.position_registry import registry

from .broker import AlpacaOptionsBroker
from .chain import AlpacaOptionChainClient
from .earnings_calendar import EarningsCalendar
from .iv_tracker import IVTracker
from .risk import OptionsRiskGate
from .selector import OptionStrategySelector
from .strategies import STRATEGY_REGISTRY
from .strategies.base import OptionsTradeSpec

log = logging.getLogger(__name__)

_MIN_BARS: int = 52

# ── Exit management thresholds ──────────────────────────────────────────────
# Profit targets (fraction of max_reward to capture before closing)
PROFIT_TARGET_CREDIT  = 0.50   # credit strategies: close at 50% of max reward
PROFIT_TARGET_DEBIT   = 0.80   # debit strategies: close at 80% of max reward (don't be greedy)
PROFIT_TARGET_VOLAT   = 0.60   # volatility strategies: close at 60% of max reward

# Stop losses (fraction of max_risk at which to cut)
STOP_LOSS_FRACTION    = 0.50   # close if unrealised loss >= 50% of max_risk (was 80%, too late)

# DTE thresholds
DTE_CLOSE_THRESHOLD   = 10    # close any position with <= 10 DTE (was 7, gamma risk)
DTE_ROLL_THRESHOLD    = 14    # consider rolling at 14 DTE (credit strategies)

# Theta bleed: close if position lost > X% of entry cost with DTE still remaining
THETA_BLEED_PCT       = 0.60   # close if lost 60% of value with 15+ DTE left
THETA_BLEED_MIN_DTE   = 15

# Position monitoring interval (seconds) — checked on every BAR
_MONITOR_COOLDOWN     = 30.0   # don't re-check same ticker faster than 30s

# Strategy categories for exit logic
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


@dataclass
class OptionsPosition:
    """Tracked open options position with full lifecycle data."""
    ticker:           str
    strategy_type:    str
    spec:             OptionsTradeSpec
    entry_time:       float            # monotonic timestamp
    entry_cost:       float            # net_debit (positive=paid, negative=credit)
    max_risk:         float
    max_reward:       float
    expiry_date:      str              # ISO date 'YYYY-MM-DD'

    # Updated on each monitoring pass
    current_value:    float = 0.0      # current mid-market value of the position
    current_pnl:      float = 0.0      # unrealised P&L
    last_check_time:  float = 0.0      # monotonic timestamp of last exit check
    portfolio_delta:  float = 0.0      # net delta of this position
    portfolio_theta:  float = 0.0      # net theta (daily decay)
    portfolio_vega:   float = 0.0      # net vega (IV sensitivity)

    @property
    def is_credit(self) -> bool:
        return self.strategy_type in _CREDIT_STRATEGIES

    @property
    def is_debit(self) -> bool:
        return self.strategy_type in _DEBIT_STRATEGIES

    @property
    def is_volatility(self) -> bool:
        return self.strategy_type in _VOLATILITY_STRATEGIES

    @property
    def dte(self) -> int:
        try:
            exp = date.fromisoformat(self.expiry_date)
            return max((exp - date.today()).days, 0)
        except (ValueError, TypeError):
            return 999

    @property
    def profit_target(self) -> float:
        if self.is_credit:
            return PROFIT_TARGET_CREDIT
        elif self.is_volatility:
            return PROFIT_TARGET_VOLAT
        return PROFIT_TARGET_DEBIT

    @property
    def holding_minutes(self) -> float:
        return (time.monotonic() - self.entry_time) / 60.0


class OptionsEngine:
    """
    T3.7 — Options Engine with full lifecycle management.

    Entry:  SIGNAL path (directional) + BAR path (neutral/volatility)
    Exit:   Profit target, stop loss, DTE management, theta bleed, EOD close
    Monitor: Greeks tracking, position valuation on every bar
    """

    def __init__(
        self,
        bus:            EventBus,
        options_key:    Optional[str] = None,
        options_secret: Optional[str] = None,
        paper:          bool          = True,
        max_positions:  int           = 5,
        trade_budget:   float         = 500.0,
        total_budget:   float         = 10_000.0,
        order_cooldown: int           = 300,
        min_dte:        int           = 20,
        max_dte:        int           = 45,
        leaps_dte:      int           = 365,
        delta_directional: float      = 0.35,
        delta_spread:      float      = 0.175,
        alert_email:    Optional[str] = None,
    ) -> None:
        self._bus              = bus
        self._min_dte          = min_dte
        self._max_dte          = max_dte
        self._leaps_dte        = leaps_dte
        self._delta_directional = delta_directional
        self._delta_spread     = delta_spread
        self._alert_email      = alert_email

        # ── Option chain client ────────────────────────────────────────
        if options_key and options_secret:
            self._chain = AlpacaOptionChainClient(options_key, options_secret)
        else:
            self._chain = None
            log.warning("[OptionsEngine] No options keys — chain client unavailable; paper mode only")

        # ── Risk gate ──────────────────────────────────────────────────
        self._risk = OptionsRiskGate(
            max_positions=max_positions,
            trade_budget=trade_budget,
            total_budget=total_budget,
            order_cooldown=order_cooldown,
        )

        # ── IV tracker (historical IV rank/percentile) ─────────────────
        self._iv_tracker = IVTracker()

        # ── Earnings calendar (block premium selling near earnings) ───
        self._earnings = EarningsCalendar()

        # ── Selector ──────────────────────────────────────────────────
        self._selector = OptionStrategySelector()

        # ── Broker ─────────────────────────────────────────────────────
        if options_key and options_secret:
            try:
                from alpaca.trading.client import TradingClient
                trading_client = TradingClient(
                    api_key=options_key,
                    secret_key=options_secret,
                    paper=paper
                )
                self._broker = AlpacaOptionsBroker(trading_client, alert_email)
            except ImportError:
                log.error("[OptionsEngine] alpaca-py not installed; broker unavailable")
                self._broker = None
        else:
            self._broker = None

        # ── Open positions (full lifecycle tracking) ───────────────────
        self._positions: Dict[str, OptionsPosition] = {}
        self._positions_lock = threading.Lock()

        # ── Portfolio-level Greeks ─────────────────────────────────────
        self._portfolio_delta = 0.0
        self._portfolio_theta = 0.0
        self._portfolio_vega  = 0.0
        self._portfolio_lock  = threading.Lock()

        # ── Daily stats ────────────────────────────────────────────────
        self._daily_entries  = 0
        self._daily_exits    = 0
        self._daily_pnl      = 0.0

        # ── Subscribe ──────────────────────────────────────────────────
        bus.subscribe(EventType.SIGNAL,     self._on_signal,     priority=3)
        bus.subscribe(EventType.BAR,        self._on_bar,        priority=3)
        bus.subscribe(EventType.POP_SIGNAL, self._on_pop_signal, priority=3)

        log.info(
            "[OptionsEngine] ready | max_pos=%d | budget=$%.0f/trade | total=$%.0f | "
            "DTE=%d-%d | delta_dir=%.2f | delta_spread=%.2f | paper=%s | chain=%s | broker=%s",
            max_positions, trade_budget, total_budget,
            min_dte, max_dte, delta_directional, delta_spread,
            paper, "OK" if self._chain else "NONE", "OK" if self._broker else "NONE",
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # SIGNAL HANDLER — Directional entries
    # ═══════════════════════════════════════════════════════════════════════════

    def _on_signal(self, event: Event) -> None:
        p = event.payload
        action = str(p.action)

        if action not in (
            SignalAction.BUY.value,
            SignalAction.SELL_STOP.value,
            SignalAction.SELL_TARGET.value,
            SignalAction.SELL_RSI.value,
            SignalAction.SELL_VWAP.value,
        ):
            return

        if self._chain is None:
            return

        iv_estimate = self._estimate_iv(p.ticker, p.current_price)

        # Update IV tracker with current reading
        self._iv_tracker.update(p.ticker, iv_estimate)
        iv_rank = self._iv_tracker.iv_rank(p.ticker)

        strategy_type = self._selector.select_from_signal(
            action=action,
            rvol=p.rvol,
            rsi=p.rsi_value,
            atr_value=p.atr_value,
            spot_price=p.current_price,
            iv_estimate=iv_estimate,
            iv_rank=iv_rank,
            ticker=p.ticker,
        )

        if strategy_type is None:
            return

        # Earnings safety check: block credit strategies near earnings
        if strategy_type in _CREDIT_STRATEGIES:
            if not self._earnings.is_earnings_safe(p.ticker, min_days=7):
                dte = self._earnings.days_to_earnings(p.ticker)
                log.info(
                    "[OptionsEngine] EARNINGS BLOCK %s %s | earnings in %s days",
                    p.ticker, strategy_type, dte,
                )
                return

        log.info(
            "[OptionsEngine] SIGNAL → %s %s | rvol=%.2f iv=%.2f iv_rank=%.0f",
            p.ticker, strategy_type, p.rvol, iv_estimate, iv_rank,
        )

        self._execute_entry(
            ticker=p.ticker,
            spot_price=p.current_price,
            strategy_type=strategy_type,
            atr_value=p.atr_value,
            rvol=p.rvol,
            rsi_value=p.rsi_value,
            source='signal',
            source_event=event,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # POP_SIGNAL HANDLER — News/sentiment-driven options entries
    # ═══════════════════════════════════════════════════════════════════════════

    def _on_pop_signal(self, event: Event) -> None:
        """
        Convert a pop screener candidate into an options trade.

        Pop candidates are news/sentiment-driven — ideal for options because:
          - Catalyst provides directional conviction (long call/put)
          - Defined risk via options (vs unlimited risk on equity short)
          - If IV is already elevated from the news, use debit spreads to reduce cost

        Strategy selection:
          - Bullish pop (MODERATE_NEWS, SENTIMENT_POP, UNUSUAL_VOLUME, EARNINGS gap up)
            → long_call or bull_call_spread (depending on IV rank)
          - Bearish or high-impact with gap down
            → long_put or bear_put_spread
          - IV rich after news spike (IV rank > 50)
            → debit spread (cap IV crush risk)
        """
        p = event.payload
        if self._chain is None:
            return

        ticker = p.symbol
        entry_price = float(p.entry_price)
        rvol = float(p.rvol) if hasattr(p, 'rvol') else 1.0
        atr = float(p.atr_value) if hasattr(p, 'atr_value') else entry_price * 0.02

        # Determine direction from pop reason and features
        pop_reason = str(getattr(p, 'pop_reason', '')).upper()
        features_json = getattr(p, 'features_json', '{}')
        try:
            import json
            feats = json.loads(features_json) if isinstance(features_json, str) else features_json
        except Exception as exc:
            log.debug("[OptionsEngine] POP_SIGNAL features_json parse failed for %s: %s", ticker, exc)
            feats = {}

        sentiment_delta = float(feats.get('sentiment_delta', 0))
        gap_size = float(feats.get('gap_size', 0))

        # Infer direction: bullish if positive sentiment + positive gap
        is_bullish = sentiment_delta >= 0 or gap_size >= 0

        # Get IV context
        iv_estimate = self._estimate_iv(ticker, entry_price)
        self._iv_tracker.update(ticker, iv_estimate)
        iv_rank = self._iv_tracker.iv_rank(ticker)
        iv_is_rich = iv_rank >= 50

        # Earnings safety: block credit strategies near earnings
        earnings_safe = self._earnings.is_earnings_safe(ticker, min_days=7)

        # Strategy selection based on direction + IV regime
        if is_bullish:
            if iv_is_rich:
                # IV rich after news → use spread to cap IV crush
                strategy_type = 'bull_call_spread'
            elif rvol >= 2.5:
                # Strong momentum + cheap IV → outright call
                strategy_type = 'long_call'
            else:
                # Moderate conviction → spread
                strategy_type = 'bull_call_spread'
        else:
            if iv_is_rich:
                strategy_type = 'bear_put_spread'
            elif rvol >= 2.5:
                strategy_type = 'long_put'
            else:
                strategy_type = 'bear_put_spread'

        # RSI estimate from features (or default neutral)
        rsi_value = float(feats.get('rsi', 50.0))

        log.info(
            "[OptionsEngine] POP_SIGNAL → %s %s | reason=%s sent_delta=%.2f gap=%.3f "
            "rvol=%.2f iv=%.2f iv_rank=%.0f direction=%s",
            ticker, strategy_type, pop_reason, sentiment_delta, gap_size,
            rvol, iv_estimate, iv_rank, 'BULL' if is_bullish else 'BEAR',
        )

        self._execute_entry(
            ticker=ticker,
            spot_price=entry_price,
            strategy_type=strategy_type,
            atr_value=atr,
            rvol=rvol,
            rsi_value=rsi_value,
            source='pop_signal',
            source_event=event,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # BAR HANDLER — Exit monitoring FIRST, then neutral/volatility scan
    # ═══════════════════════════════════════════════════════════════════════════

    def _on_bar(self, event: Event) -> None:
        p = event.payload
        df = p.df

        if len(df) < _MIN_BARS:
            return

        spot = float(df['close'].iloc[-1])

        # ── STEP 1: Monitor existing positions for exits ──────────────
        self._monitor_positions(p.ticker, spot)

        # ── STEP 2: Scan for new neutral/volatility entries ───────────
        if self._chain is None:
            return

        try:
            from pro_setups.detectors._compute import compute_atr, compute_rsi, compute_rvol
            atr_value = compute_atr(df)
            rsi_value = compute_rsi(df)
            rvol = compute_rvol(df, p.rvol_df)
        except (ImportError, Exception):
            atr_value = 0.5
            rsi_value = 50.0
            rvol = 1.0

        # ── Pre-filter: skip expensive chain API call if no strategy possible ─
        # The selector needs ATR/RSI/RVOL to pass basic thresholds.
        # If none can pass, don't waste an API call on _estimate_iv().
        atr_ratio = atr_value / spot if spot > 0 else 0
        has_pos = p.ticker in self._positions
        from options.selector import (
            ATR_MOD_THRESHOLD, RSI_NEUTRAL_LOW, RSI_NEUTRAL_HIGH, IV_RANK_HIGH,
        )
        might_trade = (
            atr_ratio > ATR_MOD_THRESHOLD                          # straddle/strangle
            or (RSI_NEUTRAL_LOW <= rsi_value <= RSI_NEUTRAL_HIGH)  # iron condor/butterfly
            or has_pos                                              # calendar spread
        )
        if not might_trade:
            return

        iv_estimate = self._estimate_iv(p.ticker, spot)

        # Update IV tracker
        self._iv_tracker.update(p.ticker, iv_estimate)
        iv_rank = self._iv_tracker.iv_rank(p.ticker)

        strategy_type = self._selector.select_from_bar(
            atr_value=atr_value,
            spot_price=spot,
            iv_estimate=iv_estimate,
            rsi=rsi_value,
            rvol=rvol,
            has_existing_position=has_pos,
            iv_rank=iv_rank,
        )

        if strategy_type is None:
            return

        # Earnings safety check for credit strategies
        if strategy_type in _CREDIT_STRATEGIES:
            if not self._earnings.is_earnings_safe(p.ticker, min_days=7):
                dte = self._earnings.days_to_earnings(p.ticker)
                log.info(
                    "[OptionsEngine] EARNINGS BLOCK %s %s | earnings in %s days",
                    p.ticker, strategy_type, dte,
                )
                return

        log.info(
            "[OptionsEngine] BAR → %s %s | atr_ratio=%.4f rsi=%.1f iv=%.2f iv_rank=%.0f",
            p.ticker, strategy_type, atr_value / spot if spot > 0 else 0,
            rsi_value, iv_estimate, iv_rank,
        )

        self._execute_entry(
            ticker=p.ticker,
            spot_price=spot,
            strategy_type=strategy_type,
            atr_value=atr_value,
            rvol=rvol,
            rsi_value=rsi_value,
            source='bar_scan',
            source_event=event,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # POSITION MONITORING — Exit management on every bar
    # ═══════════════════════════════════════════════════════════════════════════

    def _monitor_positions(self, ticker: str, spot: float) -> None:
        """Check if any open position on this ticker needs exit."""
        # Take a snapshot under lock to avoid race conditions
        with self._positions_lock:
            pos = self._positions.get(ticker)
            if pos is None:
                return

            now = time.monotonic()
            if now - pos.last_check_time < _MONITOR_COOLDOWN:
                return
            pos.last_check_time = now

            # Copy reference — pos is still in dict, safe to evaluate outside lock
            # because only this thread processes this ticker's bars

        self._update_position_greeks(pos)
        exit_reason = self._evaluate_exit(pos, spot)

        if exit_reason:
            # Re-check under lock that position still exists before closing
            with self._positions_lock:
                if ticker not in self._positions:
                    return
            self._close_position(pos, exit_reason, spot)

    def _evaluate_exit(self, pos: OptionsPosition, spot: float) -> Optional[str]:
        """
        Evaluate all exit conditions for a position. Returns exit reason or None.

        Exit priority (first match wins):
        1. DTE ≤ 7 → close to avoid gamma risk / assignment
        2. Profit target hit → take profits
        3. Stop loss hit → cut losses
        4. Theta bleed → position decaying without recovery
        """
        # ── 1. DTE management ─────────────────────────────────────────
        dte = pos.dte
        if dte <= DTE_CLOSE_THRESHOLD:
            return f"dte_expiry (DTE={dte})"

        # ── 2. Get current mark-to-market ─────────────────────────────
        close_value = self._get_position_mark(pos)
        if close_value is None:
            return None

        pos.current_value = close_value

        # P&L = what you get closing now - what you paid opening
        # Debit entry: entry_cost=+$200, close_value=+$300 → pnl=+$100 (profit)
        # Credit entry: entry_cost=-$140, close_value=-$80  → pnl=+$60 (kept $60 of credit)
        # Credit entry: entry_cost=-$140, close_value=-$250 → pnl=-$110 (loss, costs more to close)
        pos.current_pnl = close_value - pos.entry_cost

        # ── 3. Profit target ──────────────────────────────────────────
        target_pnl = pos.max_reward * pos.profit_target
        if pos.current_pnl >= target_pnl:
            return f"profit_target (pnl=${pos.current_pnl:.2f} >= target=${target_pnl:.2f})"

        # ── 4. Stop loss ──────────────────────────────────────────────
        max_loss = pos.max_risk * STOP_LOSS_FRACTION
        if pos.current_pnl <= -max_loss:
            return f"stop_loss (pnl=${pos.current_pnl:.2f} <= -${max_loss:.2f})"

        # ── 5. Theta bleed (debit positions losing value over time) ───
        if pos.is_debit and dte >= THETA_BLEED_MIN_DTE and pos.entry_cost > 0 and close_value > 0:
            remaining_value_pct = close_value / pos.entry_cost
            if remaining_value_pct <= (1.0 - THETA_BLEED_PCT):
                return f"theta_bleed (only {remaining_value_pct:.0%} of value left, {dte} DTE)"

        # ── 6. DTE rolling threshold (credit strategies) ──────────────
        if pos.is_credit and dte <= DTE_ROLL_THRESHOLD:
            if pos.current_pnl > 0:
                return f"dte_roll_profit (DTE={dte}, pnl=${pos.current_pnl:.2f})"

        # ── 7. Greeks-based exit triggers ──────────────────────────────
        greeks_exit = self._evaluate_greeks_exit(pos, dte)
        if greeks_exit:
            return greeks_exit

        return None

    def _evaluate_greeks_exit(
        self, pos: OptionsPosition, dte: int,
    ) -> Optional[str]:
        """
        Evaluate Greeks-based exit conditions.

        Checks gamma spike, theta decay acceleration, and delta drift.
        Returns exit reason string or None. Never raises.
        """
        if not self._chain:
            return None

        try:
            current_greeks = self._chain.get_greeks(pos.spec.legs)

            if not current_greeks:
                return None

            total_delta = sum(
                g['delta'] * g['qty'] * (1 if g['side'].upper() == 'BUY' else -1)
                for g in current_greeks
            )
            total_gamma = sum(
                abs(g['gamma'] * g['qty'])
                for g in current_greeks
            )
            total_theta = sum(
                g['theta'] * g['qty'] * (1 if g['side'].upper() == 'BUY' else -1)
                for g in current_greeks
            )

            # Exit: Gamma spike — position becomes unmanageable near expiry
            if total_gamma > 0.10 and dte <= 14:
                log.info(
                    "[OptionsEngine] GREEKS EXIT %s | gamma_spike | "
                    "gamma=%.4f dte=%d",
                    pos.ticker, total_gamma, dte,
                )
                return f"gamma_spike (gamma={total_gamma:.4f}, DTE={dte})"

            # Exit: Theta decay acceleration — bleeding too fast
            position_value = abs(pos.current_value) if pos.current_value != 0.0 else abs(
                pos.entry_cost if pos.entry_cost != 0 else pos.max_risk or 1
            )
            if position_value > 0 and abs(total_theta) / position_value > 0.05:
                log.info(
                    "[OptionsEngine] GREEKS EXIT %s | theta_bleed_fast | "
                    "theta=%.4f pos_value=%.2f ratio=%.4f",
                    pos.ticker, total_theta, position_value,
                    abs(total_theta) / position_value,
                )
                return (
                    f"theta_bleed_fast (theta={total_theta:.4f}, "
                    f"pos_value=${position_value:.2f}, "
                    f"ratio={abs(total_theta) / position_value:.4f})"
                )

            # Exit: Delta drift — neutral position has become directional
            _NEUTRAL_STRATEGIES = {
                'iron_condor', 'iron_butterfly', 'long_straddle',
                'long_strangle', 'butterfly_spread',
            }
            if pos.strategy_type in _NEUTRAL_STRATEGIES and abs(total_delta) > 0.30:
                log.info(
                    "[OptionsEngine] GREEKS EXIT %s | delta_drift | "
                    "delta=%.4f strategy=%s",
                    pos.ticker, total_delta, pos.strategy_type,
                )
                return (
                    f"delta_drift (delta={total_delta:.4f}, "
                    f"strategy={pos.strategy_type})"
                )

        except Exception as exc:
            log.debug(
                "Greeks fetch failed for %s (non-fatal): %s",
                pos.ticker, exc,
            )

        return None

    def _get_position_mark(self, pos: OptionsPosition) -> Optional[float]:
        """
        Get current mark-to-market value of the position.

        Returns the NET value if you closed RIGHT NOW:
        - Positive = you'd receive money (position is worth something to sell)
        - Negative = you'd pay money (position costs to close)

        For a long call worth $3.00 → returns +300
        For a short put costing $2.00 to close → returns -200
        For a bull call spread (long $3, short $1.50) → returns +150
        For an iron condor (cost to close $0.40 credit) → returns -40
        """
        if not self._chain:
            return None

        close_value = 0.0
        for leg in pos.spec.legs:
            quote = self._chain.get_quote(leg.symbol)
            if quote is None:
                return None

            bid, ask = quote

            if leg.side.upper() == 'BUY':
                # We own this: to close we sell at bid
                close_value += bid * leg.qty * 100
            else:
                # We're short this: to close we buy at ask
                close_value -= ask * leg.qty * 100

        return close_value

    def _update_position_greeks(self, pos: OptionsPosition) -> None:
        """Update Greeks for a single position from current chain data."""
        if not self._chain:
            return

        net_delta = 0.0
        net_theta = 0.0
        net_vega  = 0.0

        for leg in pos.spec.legs:
            quote = self._chain.get_quote(leg.symbol)
            if quote is None:
                continue

            # get_quote returns (bid, ask) — we need Greeks from the contract
            contracts = self._chain.get_chain(pos.ticker, min_dte=1, max_dte=500)
            contract = next((c for c in contracts if c.symbol == leg.symbol), None)
            if contract is None:
                continue

            multiplier = leg.qty * (1 if leg.side.upper() == 'BUY' else -1)
            net_delta += contract.delta * multiplier * 100
            net_theta += contract.theta * multiplier * 100
            net_vega  += contract.vega * multiplier * 100

        pos.portfolio_delta = net_delta
        pos.portfolio_theta = net_theta
        pos.portfolio_vega  = net_vega

    # ═══════════════════════════════════════════════════════════════════════════
    # POSITION CLOSE — Execute exit and clean up
    # ═══════════════════════════════════════════════════════════════════════════

    def _close_position(
        self, pos: OptionsPosition, reason: str, spot: float,
    ) -> None:
        """Close an open position and clean up all state."""
        ticker = pos.ticker

        log.info(
            "[OptionsEngine] CLOSING %s %s | reason=%s | pnl=$%.2f | held=%.1fmin | dte=%d",
            ticker, pos.strategy_type, reason,
            pos.current_pnl, pos.holding_minutes, pos.dte,
        )

        # Execute close via broker
        success = True
        if self._broker:
            success = self._broker.close_position(ticker, pos.spec.legs)

        if success:
            # Update daily stats
            self._daily_exits += 1
            self._daily_pnl += pos.current_pnl

            # Release from risk gate
            self._risk.release(ticker)

            # Remove from positions
            with self._positions_lock:
                self._positions.pop(ticker, None)

            # Update portfolio Greeks
            self._recalculate_portfolio_greeks()

            log.info(
                "[OptionsEngine] CLOSED %s %s | realised_pnl=$%.2f | "
                "daily_pnl=$%.2f | daily_trades=%d/%d",
                ticker, pos.strategy_type, pos.current_pnl,
                self._daily_pnl, self._daily_exits, self._daily_entries,
            )
        else:
            log.error(
                "[OptionsEngine] CLOSE FAILED %s %s | reason=%s — position remains open",
                ticker, pos.strategy_type, reason,
            )

    def close_all_positions(self, reason: str = 'eod_close') -> None:
        """Close all open positions (called at end of day or emergency)."""
        with self._positions_lock:
            tickers = list(self._positions.keys())

        for ticker in tickers:
            pos = self._positions.get(ticker)
            if pos:
                self._close_position(pos, reason, spot=0.0)

        log.info(
            "[OptionsEngine] ALL POSITIONS CLOSED | reason=%s | "
            "daily_pnl=$%.2f | entries=%d exits=%d",
            reason, self._daily_pnl, self._daily_entries, self._daily_exits,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # ENTRY EXECUTION
    # ═══════════════════════════════════════════════════════════════════════════

    def _execute_entry(
        self,
        ticker:        str,
        spot_price:    float,
        strategy_type: str,
        atr_value:     float,
        rvol:          float,
        rsi_value:     float,
        source:        str,
        source_event:  Event,
    ) -> None:
        """
        Full entry pipeline:
        1. Risk gate check
        2. Build trade spec via strategy
        3. Risk gate verify with actual max_risk
        4. Emit OPTIONS_SIGNAL (durable)
        5. Acquire position in risk gate
        6. Execute via broker
        7. Track position for monitoring
        """
        # Step 1: Pre-check risk gate
        reject_reason = self._risk.check(ticker, max_risk=self._risk._trade_budget)
        if reject_reason:
            log.debug("[OptionsEngine] %s %s BLOCKED: %s", ticker, strategy_type, reject_reason)
            return

        # Step 2: Get strategy class and build
        if strategy_type not in STRATEGY_REGISTRY:
            log.warning("[OptionsEngine] unknown strategy type: %s", strategy_type)
            return

        if not self._chain:
            return

        strategy = STRATEGY_REGISTRY[strategy_type]()
        trade_spec = strategy.build(
            ticker=ticker,
            underlying_price=spot_price,
            chain_client=self._chain,
            min_dte=self._min_dte,
            max_dte=self._max_dte,
            delta_directional=self._delta_directional,
            delta_spread=self._delta_spread,
        )

        if trade_spec is None:
            log.debug("[OptionsEngine] %s %s build returned None", ticker, strategy_type)
            return

        # Step 3: Verify risk with actual max_risk
        reject_reason = self._risk.check(ticker, max_risk=trade_spec.max_risk)
        if reject_reason:
            log.debug("[OptionsEngine] %s %s BLOCKED after build: %s", ticker, strategy_type, reject_reason)
            return

        # Step 4: Emit durable OPTIONS_SIGNAL
        self._emit_options_signal(
            ticker=ticker, spot_price=spot_price, strategy_type=strategy_type,
            trade_spec=trade_spec, atr_value=atr_value, rvol=rvol,
            rsi_value=rsi_value, source=source, source_event=source_event,
        )

        # Step 5: Acquire position in risk gate (track max_risk, not just cost)
        self._risk.acquire(ticker, cost=trade_spec.net_debit, max_risk=trade_spec.max_risk)

        # Step 6: Execute trade
        success = True
        if self._broker:
            success = self._broker.execute(trade_spec, source_event)

        if not success:
            log.error("[OptionsEngine] execution failed for %s; releasing position", ticker)
            self._risk.release(ticker)
            return

        # Step 7: Track position for monitoring
        pos = OptionsPosition(
            ticker=ticker,
            strategy_type=strategy_type,
            spec=trade_spec,
            entry_time=time.monotonic(),
            entry_cost=trade_spec.net_debit,
            max_risk=trade_spec.max_risk,
            max_reward=trade_spec.max_reward,
            expiry_date=trade_spec.expiry_date,
            current_value=abs(trade_spec.net_debit),
            last_check_time=time.monotonic(),
        )

        with self._positions_lock:
            self._positions[ticker] = pos

        self._daily_entries += 1
        self._recalculate_portfolio_greeks()

        log.info(
            "[OptionsEngine] ENTERED %s %s | cost=$%.2f | max_risk=$%.2f | "
            "max_reward=$%.2f | dte=%d | positions=%d",
            ticker, strategy_type, trade_spec.net_debit,
            trade_spec.max_risk, trade_spec.max_reward,
            pos.dte, len(self._positions),
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # PORTFOLIO GREEKS
    # ═══════════════════════════════════════════════════════════════════════════

    def _recalculate_portfolio_greeks(self) -> None:
        """Recalculate aggregate portfolio Greeks from all open positions."""
        with self._portfolio_lock:
            self._portfolio_delta = 0.0
            self._portfolio_theta = 0.0
            self._portfolio_vega  = 0.0

            with self._positions_lock:
                for pos in self._positions.values():
                    self._portfolio_delta += pos.portfolio_delta
                    self._portfolio_theta += pos.portfolio_theta
                    self._portfolio_vega  += pos.portfolio_vega

    @property
    def portfolio_greeks(self) -> Dict[str, float]:
        """Current portfolio-level Greeks summary."""
        with self._portfolio_lock:
            return {
                'delta': self._portfolio_delta,
                'theta': self._portfolio_theta,
                'vega':  self._portfolio_vega,
            }

    @property
    def daily_stats(self) -> Dict[str, float]:
        """Current day's trading statistics."""
        return {
            'entries': self._daily_entries,
            'exits':   self._daily_exits,
            'pnl':     self._daily_pnl,
            'open':    len(self._positions),
        }

    # ═══════════════════════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════════════════════

    def _emit_options_signal(
        self,
        ticker: str, spot_price: float, strategy_type: str,
        trade_spec: OptionsTradeSpec, atr_value: float, rvol: float,
        rsi_value: float, source: str, source_event: Event,
    ) -> None:
        legs_list = [
            {
                'symbol': l.symbol, 'side': l.side,
                'qty': l.qty, 'ratio': l.ratio, 'limit_price': l.limit_price,
            }
            for l in trade_spec.legs
        ]
        payload = OptionsSignalPayload(
            ticker=ticker,
            strategy_type=strategy_type,
            underlying_price=spot_price,
            expiry_date=trade_spec.expiry_date,
            net_debit=trade_spec.net_debit,
            max_risk=trade_spec.max_risk,
            max_reward=trade_spec.max_reward,
            atr_value=atr_value,
            rvol=rvol,
            rsi_value=rsi_value,
            legs_json=json.dumps(legs_list),
            source=source,
        )
        self._bus.emit(Event(EventType.OPTIONS_SIGNAL, payload), durable=True)
        log.info(
            "[OptionsEngine] emitted OPTIONS_SIGNAL | %s %s | $%.2f debit | max_risk $%.2f",
            ticker, strategy_type, trade_spec.net_debit, trade_spec.max_risk,
        )

    def _estimate_iv(self, ticker: str, spot_price: float) -> float:
        if not self._chain:
            return 0.25
        try:
            contracts = self._chain.get_chain(ticker, min_dte=self._min_dte, max_dte=self._max_dte)
            if not contracts:
                return 0.25
            atm_call = self._chain.find_atm(contracts, 'call', spot_price)
            if atm_call and atm_call.iv > 0:
                return atm_call.iv
            return 0.25
        except Exception as e:
            log.debug("[OptionsEngine] IV estimate error for %s: %s", ticker, e)
            return 0.25

    def reset_daily(self) -> None:
        """Reset daily counters (called at start of new trading day)."""
        self._daily_entries = 0
        self._daily_exits   = 0
        self._daily_pnl     = 0.0
        log.info("[OptionsEngine] daily stats reset")
