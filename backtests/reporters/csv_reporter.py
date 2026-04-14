"""
CsvReporter — Export backtest results to CSV files.

Generates three CSV files per backtest run:
  - trades_{timestamp}.csv        — all closed trades
  - equity_curve_{timestamp}.csv  — timestamped equity values
  - summary_{timestamp}.csv       — single-row summary + by-strategy breakdown
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from typing import Any, Dict

log = logging.getLogger(__name__)


class CsvReporter:
    """Export backtest results to CSV files."""

    @staticmethod
    def report(result: Any, output_dir: str = "backtest_results") -> Dict[str, str]:
        """
        Write backtest results to three CSV files.

        Args:
            result: BacktestResult dataclass from MetricsEngine.compute()
            output_dir: directory for output files (created if missing)

        Returns:
            dict mapping logical name → absolute file path for each CSV created
        """
        os.makedirs(output_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        paths: Dict[str, str] = {}

        # --- trades CSV ---
        trades_path = os.path.join(output_dir, f"trades_{ts}.csv")
        paths["trades"] = os.path.abspath(trades_path)
        _write_trades(trades_path, result.closed_trades or [])

        # --- equity curve CSV ---
        equity_path = os.path.join(output_dir, f"equity_curve_{ts}.csv")
        paths["equity_curve"] = os.path.abspath(equity_path)
        _write_equity_curve(equity_path, result.equity_curve or [])

        # --- summary CSV ---
        summary_path = os.path.join(output_dir, f"summary_{ts}.csv")
        paths["summary"] = os.path.abspath(summary_path)
        _write_summary(summary_path, result)

        log.info(f"[CsvReporter] Wrote {len(paths)} files to {output_dir}/")
        return paths


# ── internal helpers ──────────────────────────────────────────────────────────

_TRADE_COLUMNS = [
    "ticker", "layer", "strategy_name",
    "entry_ts", "exit_ts", "side",
    "entry_price", "exit_price", "qty",
    "exit_reason", "pnl", "pnl_pct",
]


def _write_trades(path: str, closed_trades: list) -> None:
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_TRADE_COLUMNS)
        writer.writeheader()
        for t in closed_trades:
            writer.writerow({
                "ticker": t.ticker,
                "layer": t.layer,
                "strategy_name": t.strategy_name,
                "entry_ts": _fmt_ts(t.entry_ts),
                "exit_ts": _fmt_ts(t.exit_ts),
                "side": t.side.value if hasattr(t.side, "value") else str(t.side),
                "entry_price": f"{t.entry_price:.4f}",
                "exit_price": f"{t.exit_price:.4f}",
                "qty": t.qty,
                "exit_reason": t.exit_reason,
                "pnl": f"{t.pnl:.2f}",
                "pnl_pct": f"{t.pnl_pct:.4f}",
            })


def _write_equity_curve(path: str, equity_curve: list) -> None:
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "equity_value"])
        for ts, equity in equity_curve:
            writer.writerow([_fmt_ts(ts), f"{equity:.2f}"])


def _write_summary(path: str, result: Any) -> None:
    """Write a single summary row followed by per-strategy rows."""
    summary_cols = [
        "total_trades", "win_rate", "total_pnl", "max_drawdown",
        "sharpe_ratio", "profit_factor", "avg_win", "avg_loss",
    ]

    # Per-strategy breakdown columns (appended after summary columns)
    strat_cols = [
        "strategy", "strat_trades", "strat_win_rate",
        "strat_pnl", "strat_profit_factor",
    ]

    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)

        # -- summary row --
        writer.writerow(summary_cols)
        writer.writerow([
            result.total_trades,
            f"{result.win_rate:.4f}",
            f"{result.total_pnl:.2f}",
            f"{result.max_drawdown:.2f}",
            f"{result.sharpe_ratio:.4f}",
            f"{result.profit_factor:.4f}",
            f"{result.avg_win:.2f}",
            f"{result.avg_loss:.2f}",
        ])

        # -- by-strategy breakdown --
        if result.by_strategy:
            writer.writerow([])  # blank separator
            writer.writerow(strat_cols)
            for name, metrics in sorted(result.by_strategy.items()):
                writer.writerow([
                    name,
                    metrics.num_trades,
                    f"{metrics.win_rate:.4f}",
                    f"{metrics.total_pnl:.2f}",
                    f"{metrics.profit_factor:.4f}",
                ])


def _fmt_ts(ts: Any) -> str:
    """Format a timestamp; return empty string for None."""
    if ts is None:
        return ""
    if isinstance(ts, datetime):
        return ts.strftime("%Y-%m-%d %H:%M:%S")
    return str(ts)
