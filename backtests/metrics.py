"""
MetricsEngine — Compute backtest performance metrics.

Tracks equity curve and trade performance.
Computes: P&L, Sharpe ratio, max drawdown, win rate, profit factor.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class StrategyMetrics:
    """Per-strategy performance metrics."""
    name: str
    num_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0


@dataclass
class BacktestResult:
    """Complete backtest results."""
    start_date: datetime
    end_date: datetime
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    calmar_ratio: float = 0.0
    by_strategy: Dict[str, StrategyMetrics] = None
    by_layer: Dict[str, StrategyMetrics] = None
    equity_curve: List[tuple[datetime, float]] = None
    closed_trades: List[Any] = None


class MetricsEngine:
    """Compute backtest metrics."""

    def __init__(self):
        self.equity_curve: List[tuple[datetime, float]] = []
        self.daily_returns: List[float] = []

    def record(self, timestamp: datetime, equity: float) -> None:
        """Record a point on the equity curve."""
        self.equity_curve.append((timestamp, equity))

    def compute(self, closed_trades: List[Any], all_signals: List[tuple[str, Any]]) -> BacktestResult:
        """
        Compute comprehensive backtest metrics.

        Args:
            closed_trades: list of ClosedTrade objects from FillSimulator
            all_signals: list of captured signals (('pro'|'pop'|'options', payload))

        Returns:
            BacktestResult with all metrics
        """
        if not self.equity_curve:
            log.warning("[MetricsEngine] No equity curve data")
            return BacktestResult(start_date=datetime.now(), end_date=datetime.now())

        start_ts, start_equity = self.equity_curve[0]
        end_ts, end_equity = self.equity_curve[-1]

        # Trade metrics
        result = self._compute_trade_metrics(closed_trades, start_equity)
        result.start_date = start_ts
        result.end_date = end_ts
        result.equity_curve = self.equity_curve
        result.closed_trades = closed_trades

        # Return metrics
        if len(self.equity_curve) > 1:
            result.sharpe_ratio = self._compute_sharpe(self.equity_curve)
            result.max_drawdown, result.max_drawdown_pct = self._compute_max_drawdown(self.equity_curve)
            if result.max_drawdown != 0:
                result.calmar_ratio = abs(result.total_pnl / result.max_drawdown)

        # Strategy breakdown
        result.by_strategy = self._compute_by_strategy(closed_trades)
        result.by_layer = self._compute_by_layer(closed_trades)

        log.info(f"[MetricsEngine] Backtest complete: {result.total_trades} trades, "
                 f"P&L ${result.total_pnl:.2f} ({result.win_rate:.1%}), "
                 f"Sharpe {result.sharpe_ratio:.2f}")

        return result

    def _compute_trade_metrics(self, closed_trades: List[Any], starting_equity: float) -> BacktestResult:
        """Compute P&L and win rate from closed trades."""
        if not closed_trades:
            return BacktestResult(start_date=datetime.now(), end_date=datetime.now())

        winning = [t for t in closed_trades if t.pnl > 0]
        losing = [t for t in closed_trades if t.pnl < 0]
        breakeven = [t for t in closed_trades if t.pnl == 0]

        total_pnl = sum(t.pnl for t in closed_trades)
        total_pnl_pct = total_pnl / starting_equity if starting_equity != 0 else 0.0

        win_amount = sum(t.pnl for t in winning) if winning else 0.0
        loss_amount = sum(t.pnl for t in losing) if losing else 0.0
        avg_win = win_amount / len(winning) if winning else 0.0
        avg_loss = loss_amount / len(losing) if losing else 0.0

        profit_factor = win_amount / abs(loss_amount) if loss_amount != 0 else float('inf') if win_amount > 0 else 0.0

        win_rate = len(winning) / len(closed_trades) if closed_trades else 0.0

        result = BacktestResult(
            start_date=datetime.now(),
            end_date=datetime.now(),
            total_trades=len(closed_trades),
            winning_trades=len(winning),
            losing_trades=len(losing),
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl_pct,
            profit_factor=profit_factor if profit_factor != float('inf') else 999.9,
        )

        return result

    def _compute_sharpe(self, equity_curve: List[tuple[datetime, float]], risk_free_rate: float = 0.04) -> float:
        """Compute Sharpe ratio (annualized)."""
        if len(equity_curve) < 2:
            return 0.0

        returns = []
        prev_equity = equity_curve[0][1]
        for ts, equity in equity_curve[1:]:
            ret = (equity - prev_equity) / prev_equity if prev_equity != 0 else 0.0
            returns.append(ret)
            prev_equity = equity

        if not returns or np.std(returns) == 0:
            return 0.0

        excess_return = np.mean(returns) - (risk_free_rate / 252)  # daily risk-free
        sharpe = excess_return / np.std(returns) * np.sqrt(252)  # annualized

        return float(sharpe) if not np.isnan(sharpe) else 0.0

    def _compute_max_drawdown(self, equity_curve: List[tuple[datetime, float]]) -> tuple[float, float]:
        """Compute maximum drawdown in dollars and percentage."""
        if not equity_curve:
            return 0.0, 0.0

        equities = [eq for _, eq in equity_curve]
        running_max = equities[0]
        max_dd = 0.0
        max_dd_pct = 0.0

        for eq in equities:
            if eq > running_max:
                running_max = eq
            dd = eq - running_max
            dd_pct = dd / running_max if running_max != 0 else 0.0
            if dd < max_dd:
                max_dd = dd
                max_dd_pct = dd_pct

        return max_dd, abs(max_dd_pct)

    def _compute_by_strategy(self, closed_trades: List[Any]) -> Dict[str, StrategyMetrics]:
        """Group metrics by strategy name."""
        strategies: Dict[str, List[Any]] = {}
        for trade in closed_trades:
            key = trade.strategy_name
            if key not in strategies:
                strategies[key] = []
            strategies[key].append(trade)

        result = {}
        for strategy_name, trades in strategies.items():
            winning = [t for t in trades if t.pnl > 0]
            losing = [t for t in trades if t.pnl < 0]
            total_pnl = sum(t.pnl for t in trades)
            win_amount = sum(t.pnl for t in winning) if winning else 0.0
            loss_amount = sum(t.pnl for t in losing) if losing else 0.0
            profit_factor = win_amount / abs(loss_amount) if loss_amount != 0 else 0.0

            result[strategy_name] = StrategyMetrics(
                name=strategy_name,
                num_trades=len(trades),
                winning_trades=len(winning),
                losing_trades=len(losing),
                avg_win=win_amount / len(winning) if winning else 0.0,
                avg_loss=loss_amount / len(losing) if losing else 0.0,
                total_pnl=total_pnl,
                win_rate=len(winning) / len(trades) if trades else 0.0,
                profit_factor=profit_factor,
            )

        return result

    def _compute_by_layer(self, closed_trades: List[Any]) -> Dict[str, StrategyMetrics]:
        """Group metrics by layer (pro/pop/options)."""
        layers: Dict[str, List[Any]] = {}
        for trade in closed_trades:
            layer = trade.layer
            if layer not in layers:
                layers[layer] = []
            layers[layer].append(trade)

        result = {}
        for layer, trades in layers.items():
            winning = [t for t in trades if t.pnl > 0]
            losing = [t for t in trades if t.pnl < 0]
            total_pnl = sum(t.pnl for t in trades)
            win_amount = sum(t.pnl for t in winning) if winning else 0.0
            loss_amount = sum(t.pnl for t in losing) if losing else 0.0
            profit_factor = win_amount / abs(loss_amount) if loss_amount != 0 else 0.0

            result[layer] = StrategyMetrics(
                name=layer,
                num_trades=len(trades),
                winning_trades=len(winning),
                losing_trades=len(losing),
                avg_win=win_amount / len(winning) if winning else 0.0,
                avg_loss=loss_amount / len(losing) if losing else 0.0,
                total_pnl=total_pnl,
                win_rate=len(winning) / len(trades) if trades else 0.0,
                profit_factor=profit_factor,
            )

        return result
