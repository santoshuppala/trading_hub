"""
ConsoleReporter — Print formatted backtest results to console.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class ConsoleReporter:
    """Print backtest results in formatted table."""

    @staticmethod
    def report(result: Any) -> None:
        """Print summary and detailed results."""
        print("\n" + "=" * 80)
        print("BACKTEST RESULTS")
        print("=" * 80)

        # Summary line
        print(f"\nPeriod: {result.start_date} → {result.end_date}")
        print(f"Trades: {result.total_trades}  |  Win Rate: {result.win_rate:.1%}  |  P&L: ${result.total_pnl:,.2f}")
        print(f"Sharpe: {result.sharpe_ratio:.2f}  |  Max Drawdown: {result.max_drawdown_pct:.1%}")
        print(f"Avg Win: ${result.avg_win:.2f}  |  Avg Loss: ${result.avg_loss:.2f}  |  Profit Factor: {result.profit_factor:.2f}")

        # By layer
        if result.by_layer:
            print("\n--- By Layer ---")
            for layer_name, metrics in result.by_layer.items():
                print(f"\n{layer_name:12s}  Trades: {metrics.num_trades:3d}  Win: {metrics.win_rate:5.1%}  "
                      f"P&L: ${metrics.total_pnl:>9,.2f}  PF: {metrics.profit_factor:5.2f}")

        # By strategy
        if result.by_strategy:
            print("\n--- By Strategy ---")
            for strat_name, metrics in sorted(result.by_strategy.items()):
                print(f"{strat_name:30s}  Trades: {metrics.num_trades:3d}  Win: {metrics.win_rate:5.1%}  "
                      f"P&L: ${metrics.total_pnl:>9,.2f}")

        print("\n" + "=" * 80 + "\n")
