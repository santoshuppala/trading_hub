"""
FillSimulator — Realistic position entry/exit simulation for backtesting.

Tracks pending and open positions. Called once per bar per ticker:
1. Fill pending entries at next bar's open (with SPREAD_FACTOR slippage)
2. Check stops: bar.low <= stop_price → exit at stop
3. Partial target exit: bar.close >= target_1 → exit 50% of size
4. Full target exit: bar.close >= target_2 → exit remaining
5. EOD close: last bar of session → close all positions at close

Maintains separate lists for Pro/Pop/Options positions and notifies
adapters via callbacks when positions are entered or closed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

SPREAD_FACTOR = 0.0002  # 2 basis points slippage on entry


class PositionSide(Enum):
    LONG = "long"
    SHORT = "short"


@dataclass
class OpenPosition:
    """Represents an open position from a single strategy signal."""
    ticker: str
    layer: str  # 'pro' | 'pop' | 'options'
    strategy_name: str  # e.g., 'momentum_ignition', 'orb'
    side: PositionSide
    entry_bar_ts: datetime
    entry_price: float  # overwritten at next-bar open fill
    stop_price: float
    target_1: float
    target_2: float
    qty: int
    fill_pending: bool = True  # True = fill on next bar's open
    partial_exited: bool = False  # True = target_1 hit, 50% exited
    exit_price: Optional[float] = None  # set on close
    exit_reason: Optional[str] = None  # 'open', 'stop', 'target_1', 'target_2', 'eod'


@dataclass
class ClosedTrade:
    """Record of a completed trade for metrics reporting."""
    ticker: str
    layer: str
    strategy_name: str
    entry_ts: datetime
    exit_ts: datetime
    side: PositionSide
    entry_price: float
    exit_price: float
    qty: int
    exit_reason: str
    pnl: float  # gross P&L before commissions
    pnl_pct: float  # (exit_price - entry_price) / entry_price
    commission: float = 0.0  # per-trade cost


# Options strategy categories for exit logic
_CREDIT_STRATEGIES = {
    'bull_put_spread', 'bear_call_spread', 'iron_condor', 'iron_butterfly',
}
_DEBIT_STRATEGIES = {
    'long_call', 'long_put', 'bull_call_spread', 'bear_put_spread', 'butterfly_spread',
}
_VOLATILITY_STRATEGIES = {'long_straddle', 'long_strangle'}


@dataclass
class OptionsPosition:
    """Represents an open options position (single leg or multi-leg)."""
    ticker: str
    strategy_type: str
    net_debit: float       # positive=paid, negative=credit received
    max_risk: float        # always positive
    max_reward: float      # always positive
    entry_ts: Optional[datetime] = None
    entry_spot: float = 0.0       # underlying price at entry
    entry_payload: Optional[Any] = None
    exit_price: Optional[float] = None
    exit_ts: Optional[datetime] = None
    exit_reason: Optional[str] = None
    bars_held: int = 0
    total_dte: int = 30           # DTE at entry
    pnl: float = 0.0

    @property
    def is_credit(self) -> bool:
        return self.strategy_type in _CREDIT_STRATEGIES

    @property
    def is_debit(self) -> bool:
        return self.strategy_type in _DEBIT_STRATEGIES

    @property
    def profit_target_pct(self) -> float:
        if self.is_credit:
            return 0.50   # close at 50% of max_reward
        elif self.strategy_type in _VOLATILITY_STRATEGIES:
            return 0.60
        return 0.80       # debit: close at 80%

    @property
    def stop_loss_pct(self) -> float:
        return 0.50       # close at 50% of max_risk loss


class _VwapSignalWrapper:
    """Adapts SignalPayload (VWAP) to the interface FillSimulator expects."""
    def __init__(self, signal_payload, qty: int):
        self.ticker = signal_payload.ticker
        self.strategy_name = 'vwap_reclaim'
        self.direction = 'long' if str(signal_payload.action) == 'BUY' else 'short'
        self.signal = 'BUY' if self.direction == 'long' else 'SELL'
        self.entry_price = signal_payload.current_price
        self.stop_price = signal_payload.stop_price
        self.target_1 = signal_payload.half_target
        self.target_2 = signal_payload.target_price
        self.qty = qty


class FillSimulator:
    """
    Simulates order fills and position management for backtesting.

    Maintains pending and open positions per layer (pro/pop/options).
    Called per bar to execute entries, exits, stop/target fills.
    """

    def __init__(self, trade_budget: float = 1000.0):
        self.trade_budget = trade_budget
        self.starting_equity = trade_budget
        self.current_equity = trade_budget
        self.cash = trade_budget

        # Pending entries (signal received, awaiting next bar open)
        self.pending_pro: List[tuple[Any, int]] = []  # (signal_payload, qty)
        self.pending_pop: List[tuple[Any, int]] = []
        self.pending_options: List[tuple[Any]] = []

        # Open positions by layer
        self.open_positions: Dict[str, List[OpenPosition]] = {
            'pro': [],
            'pop': [],
            'options': [],
        }

        # Closed trades (for metrics)
        self.closed_trades: List[ClosedTrade] = []
        self.closed_options: List[OptionsPosition] = []

        # Callbacks: (ticker: str) -> None
        self._entry_callbacks: Dict[str, List[Callable]] = {}  # layer → [callables]
        self._close_callbacks: Dict[str, List[Callable]] = {}

        # Track cumulative P&L for equity curve
        self.cumulative_pnl = 0.0

    def queue_from_vwap(self, payload: Any, qty: Optional[int] = None) -> None:
        """Queue a VWAP SIGNAL (BUY) for entry on next bar open."""
        if qty is None:
            price = getattr(payload, 'current_price', getattr(payload, 'ask_price', 100))
            qty = max(1, int(self.trade_budget / price)) if price > 0 else 1
        # Wrap VWAP signal to match the interface expected by fill logic
        wrapped = _VwapSignalWrapper(payload, qty)
        self.pending_pro.append((wrapped, qty))
        log.debug("[FillSim] Queued VWAP entry: %s %d shares @ $%.2f",
                  payload.ticker, qty, price)

    def queue_from_pro(self, payload: Any, qty: Optional[int] = None) -> None:
        """Queue a PRO_STRATEGY_SIGNAL for entry on next bar open."""
        if qty is None:
            qty = getattr(payload, 'qty', 1)
        self.pending_pro.append((payload, qty))
        log.debug(f"[FillSim] Queued PRO entry: {payload.ticker} {qty} shares")

    def queue_from_pop(self, payload: Any, qty: Optional[int] = None) -> None:
        """Queue a POP_SIGNAL for entry on next bar open."""
        if qty is None:
            qty = getattr(payload, 'qty', 1)
        self.pending_pop.append((payload, qty))
        log.debug(f"[FillSim] Queued POP entry: {payload.ticker} {qty} shares")

    def queue_from_options(self, payload: Any) -> None:
        """Queue an OPTIONS_SIGNAL for entry on next bar open."""
        self.pending_options.append(payload)
        log.debug(f"[FillSim] Queued OPTIONS entry: {payload.ticker}")

    def register_entry_callback(self, layer: str, fn: Callable[[str], None]) -> None:
        """Register callback when a position is opened."""
        if layer not in self._entry_callbacks:
            self._entry_callbacks[layer] = []
        self._entry_callbacks[layer].append(fn)

    def register_close_callback(self, layer: str, fn: Callable[[str], None]) -> None:
        """Register callback when a position is closed."""
        if layer not in self._close_callbacks:
            self._close_callbacks[layer] = []
        self._close_callbacks[layer].append(fn)

    def process_bar(self, ticker: str, bar: Any, is_eod: bool = False) -> None:
        """
        Process one bar for a ticker.
        Bar object has: open, high, low, close, volume (attrs or dict access).
        is_eod: True if this is the last bar of the trading session.
        """
        # Extract OHLC
        try:
            o = float(bar['open']) if isinstance(bar, dict) else float(bar.open)
            h = float(bar['high']) if isinstance(bar, dict) else float(bar.high)
            l = float(bar['low']) if isinstance(bar, dict) else float(bar.low)
            c = float(bar['close']) if isinstance(bar, dict) else float(bar.close)
        except (KeyError, AttributeError, TypeError) as e:
            log.warning(f"[FillSim] Could not extract OHLC from bar for {ticker}: {e}")
            return

        # 1. Fill pending entries at next bar's open
        for layer_name, pending_list in [('pro', self.pending_pro), ('pop', self.pending_pop)]:
            new_pending = []
            for payload, qty in pending_list:
                if payload.ticker == ticker:
                    # Determine side: support both 'signal' (sync_bus) and 'direction' (live payload)
                    is_long = True
                    if hasattr(payload, 'signal'):
                        is_long = payload.signal == 'BUY'
                    elif hasattr(payload, 'direction'):
                        is_long = payload.direction == 'long'

                    # Fill at open with slippage
                    fill_price = o * (1.0 + SPREAD_FACTOR) if is_long else o * (1.0 - SPREAD_FACTOR)
                    pos = OpenPosition(
                        ticker=ticker,
                        layer=layer_name,
                        strategy_name=payload.strategy_name if hasattr(payload, 'strategy_name') else 'unknown',
                        side=PositionSide.LONG if is_long else PositionSide.SHORT,
                        entry_bar_ts=None,  # will be set by BacktestEngine
                        entry_price=fill_price,
                        stop_price=payload.stop_price,
                        target_1=payload.target_1,
                        target_2=payload.target_2,
                        qty=qty,
                        fill_pending=False,
                    )
                    self.open_positions[layer_name].append(pos)
                    log.debug(f"[FillSim] Filled {layer_name} entry: {ticker} @ {fill_price:.2f}")
                    # Call entry callbacks
                    for cb in self._entry_callbacks.get(layer_name, []):
                        cb(ticker)
                else:
                    new_pending.append((payload, qty))
            if layer_name == 'pro':
                self.pending_pro = new_pending
            else:
                self.pending_pop = new_pending

        # 2. Fill pending options entries
        new_pending_options = []
        for payload in self.pending_options:
            if payload.ticker == ticker:
                strategy_type = str(payload.strategy_type) if hasattr(payload, 'strategy_type') else 'unknown'
                dte = 30
                if hasattr(payload, 'expiry_date') and payload.expiry_date:
                    try:
                        from datetime import date as _date
                        exp = _date.fromisoformat(str(payload.expiry_date))
                        dte = max((exp - _date.today()).days, 1)
                    except (ValueError, TypeError):
                        dte = 30

                opt_pos = OptionsPosition(
                    ticker=ticker,
                    strategy_type=strategy_type,
                    net_debit=payload.net_debit,
                    max_risk=payload.max_risk,
                    max_reward=payload.max_reward,
                    entry_ts=None,
                    entry_spot=c,  # underlying price at entry
                    entry_payload=payload,
                    total_dte=dte,
                )
                self.open_positions['options'].append(opt_pos)
                log.debug("[FillSim] Filled options entry: %s %s cost=$%.2f",
                          ticker, strategy_type, payload.net_debit)
                for cb in self._entry_callbacks.get('options', []):
                    cb(ticker)
            else:
                new_pending_options.append(payload)
        self.pending_options = new_pending_options

        # 3. Check stops and targets for equity positions
        for layer_name in ['pro', 'pop']:
            positions = self.open_positions[layer_name]
            remaining = []
            for pos in positions:
                if pos.ticker != ticker:
                    remaining.append(pos)
                    continue
                if pos.fill_pending or pos.exit_reason is not None:
                    # Still pending fill or already exited
                    remaining.append(pos)
                    continue

                # Check stop
                if (pos.side == PositionSide.LONG and l <= pos.stop_price) or \
                   (pos.side == PositionSide.SHORT and h >= pos.stop_price):
                    # Stop hit
                    exit_price = pos.stop_price
                    self._close_position(pos, exit_price, 'stop', layer_name)
                    continue

                # Check target_1 (partial exit at 50%)
                if not pos.partial_exited:
                    if (pos.side == PositionSide.LONG and c >= pos.target_1) or \
                       (pos.side == PositionSide.SHORT and c <= pos.target_1):
                        # Partial exit
                        pos.partial_exited = True
                        log.debug(f"[FillSim] Partial exit (target_1) for {ticker}: {pos.qty//2} @ {pos.target_1}")

                # Check target_2 (full exit)
                if (pos.side == PositionSide.LONG and c >= pos.target_2) or \
                   (pos.side == PositionSide.SHORT and c <= pos.target_2):
                    # Full exit
                    exit_price = pos.target_2
                    self._close_position(pos, exit_price, 'target_2', layer_name)
                    continue

                remaining.append(pos)

            self.open_positions[layer_name] = remaining

        # 4. Check options exits with realistic P&L model
        options_remaining = []
        for opt_pos in self.open_positions.get('options', []):
            if opt_pos.ticker != ticker or opt_pos.exit_reason is not None:
                options_remaining.append(opt_pos)
                continue

            opt_pos.bars_held += 1

            # Estimate current P&L based on underlying move + time decay
            current_pnl = self._estimate_options_pnl(opt_pos, c)

            # Check profit target
            target = opt_pos.max_reward * opt_pos.profit_target_pct
            if current_pnl >= target:
                opt_pos.pnl = current_pnl
                self._close_options_position(opt_pos, 'profit_target')
                continue

            # Check stop loss
            max_loss = opt_pos.max_risk * opt_pos.stop_loss_pct
            if current_pnl <= -max_loss:
                opt_pos.pnl = current_pnl
                self._close_options_position(opt_pos, 'stop_loss')
                continue

            # Check DTE (close at 10 DTE equivalent in bars: ~10*6.5hrs*60min = 3900 bars)
            # Simplified: close after holding 75% of total_dte in bars (390 bars/day)
            bars_per_day = 390
            dte_remaining = opt_pos.total_dte - (opt_pos.bars_held / bars_per_day)
            if dte_remaining <= 10:
                opt_pos.pnl = current_pnl
                self._close_options_position(opt_pos, 'dte_close')
                continue

            # EOD close on last bar (for intraday backtest)
            if is_eod:
                # Don't force close — options span multiple days
                # Only close if this is the last session day
                pass

            options_remaining.append(opt_pos)
        self.open_positions['options'] = options_remaining

        # 5. EOD close
        if is_eod:
            for layer_name in ['pro', 'pop']:
                positions = self.open_positions[layer_name]
                remaining = []
                for pos in positions:
                    if pos.ticker == ticker and pos.exit_reason is None:
                        # EOD close at close price
                        self._close_position(pos, c, 'eod', layer_name)
                    else:
                        remaining.append(pos)
                self.open_positions[layer_name] = remaining

    def _close_position(self, pos: OpenPosition, exit_price: float, reason: str, layer: str) -> None:
        """Close an equity position and record the trade."""
        pnl = (exit_price - pos.entry_price) * pos.qty if pos.side == PositionSide.LONG \
              else (pos.entry_price - exit_price) * pos.qty
        pnl_pct = (exit_price - pos.entry_price) / pos.entry_price if pos.side == PositionSide.LONG \
                  else (pos.entry_price - exit_price) / pos.entry_price
        commission = 0.0  # Simplified: no commission

        pos.exit_price = exit_price
        pos.exit_reason = reason

        trade = ClosedTrade(
            ticker=pos.ticker,
            layer=layer,
            strategy_name=pos.strategy_name,
            entry_ts=pos.entry_bar_ts,
            exit_ts=None,  # set by BacktestEngine
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            qty=pos.qty,
            exit_reason=reason,
            pnl=pnl,
            pnl_pct=pnl_pct,
            commission=commission,
        )
        self.closed_trades.append(trade)
        self.cumulative_pnl += pnl
        self.current_equity += pnl
        self.cash += pnl

        log.debug(f"[FillSim] Closed {layer} {pos.ticker}: {reason} @ {exit_price:.2f}, pnl=${pnl:.2f}")

        # Call close callbacks
        for cb in self._close_callbacks.get(layer, []):
            cb(pos.ticker)

    def _estimate_options_pnl(self, opt_pos: OptionsPosition, current_spot: float) -> float:
        """
        Estimate current P&L for an options position based on underlying movement + time decay.

        Simplified model (no full Black-Scholes repricing):
        - Directional component: how much did underlying move relative to position's strikes
        - Time decay component: linear theta decay over DTE
        - Combined to approximate position value
        """
        if opt_pos.entry_spot <= 0:
            return 0.0

        move_pct = (current_spot - opt_pos.entry_spot) / opt_pos.entry_spot
        bars_per_day = 390
        days_held = opt_pos.bars_held / bars_per_day
        time_decay_pct = min(days_held / max(opt_pos.total_dte, 1), 1.0)

        if opt_pos.is_credit:
            # Credit strategy profits from: time decay + underlying staying in range
            # Loses from: large move in either direction
            theta_gain = opt_pos.max_reward * time_decay_pct * 0.7  # 70% of linear decay
            # Direction penalty: larger moves = more loss
            direction_penalty = abs(move_pct) * opt_pos.max_risk * 3.0
            return theta_gain - direction_penalty

        elif opt_pos.strategy_type in _VOLATILITY_STRATEGIES:
            # Volatility plays profit from large moves, lose from time decay
            direction_gain = abs(move_pct) * opt_pos.max_reward * 2.0
            theta_loss = opt_pos.max_risk * time_decay_pct * 0.5
            return direction_gain - theta_loss

        else:
            # Debit/directional: profit from move in right direction, lose from theta
            if opt_pos.strategy_type in ('long_call', 'bull_call_spread'):
                direction_gain = max(move_pct, 0) * opt_pos.max_reward * 2.5
            elif opt_pos.strategy_type in ('long_put', 'bear_put_spread'):
                direction_gain = max(-move_pct, 0) * opt_pos.max_reward * 2.5
            else:
                direction_gain = abs(move_pct) * opt_pos.max_reward * 1.5

            theta_loss = opt_pos.max_risk * time_decay_pct * 0.4
            return direction_gain - theta_loss

    def _close_options_position(self, opt_pos: OptionsPosition, reason: str) -> None:
        """Close an options position and record the trade."""
        pnl = opt_pos.pnl
        opt_pos.exit_reason = reason

        # Also record as ClosedTrade for unified metrics
        trade = ClosedTrade(
            ticker=opt_pos.ticker,
            layer='options',
            strategy_name=opt_pos.strategy_type,
            entry_ts=opt_pos.entry_ts,
            exit_ts=opt_pos.exit_ts,
            side=PositionSide.LONG,  # options don't have simple long/short
            entry_price=abs(opt_pos.net_debit),
            exit_price=abs(opt_pos.net_debit) + pnl,
            qty=1,
            exit_reason=reason,
            pnl=pnl,
            pnl_pct=pnl / opt_pos.max_risk if opt_pos.max_risk > 0 else 0,
        )
        self.closed_trades.append(trade)
        self.closed_options.append(opt_pos)
        self.cumulative_pnl += pnl
        self.current_equity += pnl

        log.debug("[FillSim] Closed options %s %s: %s pnl=$%.2f",
                  opt_pos.ticker, opt_pos.strategy_type, reason, pnl)

        for cb in self._close_callbacks.get('options', []):
            cb(opt_pos.ticker)

    def reset_session(self) -> None:
        """Reset for a new trading session (daily)."""
        # Carry forward any unfilled pending orders
        # In reality, these would expire; for now keep them
        pass

    def equity_value(self) -> float:
        """Return current portfolio equity."""
        return self.current_equity

    def metrics_snapshot(self) -> Dict[str, Any]:
        """Return current metrics snapshot."""
        return {
            'current_equity': self.current_equity,
            'cumulative_pnl': self.cumulative_pnl,
            'total_trades': len(self.closed_trades),
            'open_positions': sum(len(pos_list) for pos_list in self.open_positions.values()),
        }
