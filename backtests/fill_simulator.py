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


@dataclass
class OptionsPosition:
    """Represents an open options position (single leg or multi-leg)."""
    ticker: str
    strategy_type: str  # 'credit_spread' | 'debit_spread' | 'iron_condor' | etc.
    net_debit: float  # cost (positive) or credit received (negative)
    max_risk: float
    max_reward: float
    entry_ts: datetime
    entry_payload: Optional[Any] = None
    exit_price: Optional[float] = None
    exit_ts: Optional[datetime] = None
    exit_reason: Optional[str] = None


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
                    # Fill at open with slippage
                    fill_price = o * (1.0 + SPREAD_FACTOR) if payload.signal == 'BUY' else o * (1.0 - SPREAD_FACTOR)
                    pos = OpenPosition(
                        ticker=ticker,
                        layer=layer_name,
                        strategy_name=payload.strategy_name if hasattr(payload, 'strategy_name') else 'unknown',
                        side=PositionSide.LONG if payload.signal == 'BUY' else PositionSide.SHORT,
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

        # 2. Check for options entries (simplified: fill at next bar open)
        new_pending_options = []
        for payload in self.pending_options:
            if payload.ticker == ticker:
                opt_pos = OptionsPosition(
                    ticker=ticker,
                    strategy_type=payload.option_type if hasattr(payload, 'option_type') else 'unknown',
                    net_debit=payload.net_debit,
                    max_risk=payload.max_risk,
                    max_reward=payload.max_reward,
                    entry_ts=None,
                    entry_payload=payload,
                )
                self.open_positions['options'].append(opt_pos)
                log.debug(f"[FillSim] Filled options entry: {ticker}")
            else:
                new_pending_options.append(payload)
        self.pending_options = new_pending_options

        # 3. Check stops and targets for equity positions
        for layer_name in ['pro', 'pop']:
            positions = self.open_positions[layer_name]
            remaining = []
            for pos in positions:
                if pos.ticker != ticker or not pos.fill_pending:
                    # Different ticker or already exited
                    if pos.ticker == ticker and pos.exit_reason is not None:
                        remaining.append(pos)
                    elif pos.ticker != ticker:
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

        # 4. Check options exit (simplified: 50% max_reward or 0% max_risk)
        options_remaining = []
        for opt_pos in self.open_positions.get('options', []):
            if opt_pos.ticker != ticker or opt_pos.exit_reason is not None:
                options_remaining.append(opt_pos)
                continue
            # Options exit at close: target 50% of max_reward or loss of max_risk
            # Simplified: exit at close
            if is_eod:
                exit_price = c
                pnl = (exit_price - opt_pos.net_debit) if opt_pos.net_debit > 0 else (opt_pos.net_debit - exit_price)
                self._close_options_position(opt_pos, 'eod')
            else:
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

    def _close_options_position(self, opt_pos: OptionsPosition, reason: str) -> None:
        """Close an options position and record the trade."""
        pnl = 0.0  # Simplified
        opt_pos.exit_reason = reason
        self.closed_options.append(opt_pos)
        self.cumulative_pnl += pnl
        self.current_equity += pnl

        # Call close callbacks
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
