"""
Post-trade analytics: P&L attribution, performance metrics,
exit efficiency, and signal accuracy.

All computation uses pandas + numpy only.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TRADING_DAYS_PER_YEAR = 252
RISK_FREE_RATE = 0.045  # 4.5 %
DEFAULT_BETA = 1.0

# Signal-type -> factor category mapping
MOMENTUM_SIGNALS = frozenset({
    "momentum", "breakout", "trend_follow", "confirmed_crossover",
})
MEAN_REVERSION_SIGNALS = frozenset({
    "rsi", "vwap", "vwap_reclaim", "mean_reversion", "bollinger",
})


class PostTradeAnalytics:
    """Hedge-fund-grade post-trade analytics engine."""

    # ------------------------------------------------------------------
    # 1. P&L Attribution
    # ------------------------------------------------------------------
    @staticmethod
    def compute_attribution(
        trades_df: pd.DataFrame,
        spy_df: pd.DataFrame,
        portfolio_beta: float = DEFAULT_BETA,
    ) -> pd.DataFrame:
        """Decompose each trade's return into alpha / beta / factor / residual.

        Parameters
        ----------
        trades_df : DataFrame
            Closed trades with at least: entry_ts, exit_ts, entry_price,
            exit_price, strategy_name.
        spy_df : DataFrame
            SPY 1-min bars with columns ``ts`` and ``close``.
        portfolio_beta : float
            Beta to use for market-return attribution (default 1.0).

        Returns
        -------
        DataFrame
            Copy of *trades_df* with added columns: trade_return,
            spy_return, beta_contribution, alpha, factor_momentum,
            factor_mean_reversion, factor_total, residual.
        """
        if trades_df.empty:
            for col in (
                "trade_return", "spy_return", "beta_contribution", "alpha",
                "factor_momentum", "factor_mean_reversion", "factor_total",
                "residual",
            ):
                trades_df[col] = np.nan
            return trades_df.copy()

        df = trades_df.copy()

        # -- Trade return ------------------------------------------------
        df["trade_return"] = np.where(
            df["entry_price"] != 0,
            (df["exit_price"] - df["entry_price"]) / df["entry_price"],
            np.nan,
        )

        # -- SPY return over each holding period -------------------------
        spy = spy_df.copy()
        spy["ts"] = pd.to_datetime(spy["ts"])
        spy = spy.sort_values("ts").set_index("ts")

        def _spy_return(row: pd.Series) -> float:
            entry_ts = pd.Timestamp(row["entry_ts"])
            exit_ts = pd.Timestamp(row["exit_ts"])
            # Use nearest available bar (method='nearest' would require
            # a sorted DatetimeIndex which we already have).
            idx = spy.index
            entry_idx = idx.searchsorted(entry_ts, side="left")
            exit_idx = idx.searchsorted(exit_ts, side="left")
            entry_idx = min(entry_idx, len(idx) - 1)
            exit_idx = min(exit_idx, len(idx) - 1)
            if len(idx) == 0:
                return np.nan
            spy_entry = spy["close"].iloc[entry_idx]
            spy_exit = spy["close"].iloc[exit_idx]
            if spy_entry == 0:
                return np.nan
            return (spy_exit - spy_entry) / spy_entry

        df["spy_return"] = df.apply(_spy_return, axis=1)

        # -- Beta contribution -------------------------------------------
        df["beta_contribution"] = portfolio_beta * df["spy_return"]

        # -- Alpha -------------------------------------------------------
        df["alpha"] = df["trade_return"] - df["beta_contribution"]

        # -- Factor contributions ----------------------------------------
        strategy_lower = df["strategy_name"].fillna("").str.lower()

        is_momentum = strategy_lower.isin(MOMENTUM_SIGNALS) | strategy_lower.str.contains(
            "momentum|breakout|trend", case=False, na=False,
        )
        is_mean_rev = strategy_lower.isin(MEAN_REVERSION_SIGNALS) | strategy_lower.str.contains(
            "rsi|vwap|mean_rev|bollinger", case=False, na=False,
        )

        df["factor_momentum"] = np.where(is_momentum, df["alpha"] * 0.3, 0.0)
        df["factor_mean_reversion"] = np.where(is_mean_rev, df["alpha"] * 0.3, 0.0)
        df["factor_total"] = df["factor_momentum"] + df["factor_mean_reversion"]

        # -- Residual ----------------------------------------------------
        df["residual"] = df["alpha"] - df["factor_total"]

        return df

    # ------------------------------------------------------------------
    # 2. Performance Metrics
    # ------------------------------------------------------------------
    @staticmethod
    def compute_performance(trades_df: pd.DataFrame) -> dict:
        """Compute aggregate performance metrics.

        Parameters
        ----------
        trades_df : DataFrame
            Closed trades with at least: pnl, entry_ts, exit_ts,
            strategy_name, ticker.  Optionally ``session_phase``.

        Returns
        -------
        dict  with keys for every metric described in the spec.
        """
        result: dict = {}

        if trades_df.empty:
            return {
                "sharpe": np.nan, "sortino": np.nan, "calmar": np.nan,
                "max_drawdown_pct": np.nan, "max_drawdown_dollar": np.nan,
                "rolling_sharpe_5d": np.nan, "rolling_sharpe_20d": np.nan,
                "win_rate": np.nan, "profit_factor": np.nan,
                "expected_value_per_trade": np.nan,
                "avg_holding_period": np.nan, "recovery_factor": np.nan,
                "win_rate_by_strategy": {}, "win_rate_by_ticker": {},
                "win_rate_by_session_phase": {},
            }

        pnl = trades_df["pnl"].astype(float)
        n = len(pnl)

        # -- Returns series (per-trade) ----------------------------------
        returns = pnl.values.copy()
        mean_ret = np.nanmean(returns)
        std_ret = np.nanstd(returns, ddof=1) if n > 1 else np.nan

        # -- Sharpe (annualised) -----------------------------------------
        if std_ret and std_ret > 0:
            daily_rf = RISK_FREE_RATE / TRADING_DAYS_PER_YEAR
            result["sharpe"] = (
                (mean_ret - daily_rf) / std_ret
            ) * np.sqrt(TRADING_DAYS_PER_YEAR)
        else:
            result["sharpe"] = np.nan

        # -- Sortino (annualised) ----------------------------------------
        downside = returns[returns < 0]
        if len(downside) > 1:
            downside_std = np.nanstd(downside, ddof=1)
            if downside_std > 0:
                daily_rf = RISK_FREE_RATE / TRADING_DAYS_PER_YEAR
                result["sortino"] = (
                    (mean_ret - daily_rf) / downside_std
                ) * np.sqrt(TRADING_DAYS_PER_YEAR)
            else:
                result["sortino"] = np.nan
        else:
            result["sortino"] = np.nan

        # -- Drawdown (cumulative equity curve) --------------------------
        cum_pnl = np.cumsum(returns)
        running_max = np.maximum.accumulate(cum_pnl)
        drawdowns = cum_pnl - running_max
        max_dd_dollar = float(np.nanmin(drawdowns)) if len(drawdowns) > 0 else 0.0
        result["max_drawdown_dollar"] = max_dd_dollar

        # Percentage drawdown relative to running peak equity
        with np.errstate(divide="ignore", invalid="ignore"):
            peak_equity = running_max.copy()
            peak_equity[peak_equity <= 0] = np.nan
            dd_pct = drawdowns / peak_equity
        result["max_drawdown_pct"] = (
            float(np.nanmin(dd_pct)) if len(dd_pct) > 0 else np.nan
        )

        # -- Calmar ------------------------------------------------------
        total_pnl = float(np.nansum(returns))
        if max_dd_dollar < 0:
            result["calmar"] = total_pnl / abs(max_dd_dollar)
        else:
            result["calmar"] = np.nan

        # -- Rolling Sharpe (5 / 20 trade windows) -----------------------
        pnl_series = pd.Series(returns)
        for window, key in ((5, "rolling_sharpe_5d"), (20, "rolling_sharpe_20d")):
            if n >= window:
                roll_mean = pnl_series.rolling(window).mean()
                roll_std = pnl_series.rolling(window).std(ddof=1)
                rolling = roll_mean / roll_std.replace(0, np.nan)
                result[key] = rolling.dropna().tolist()
            else:
                result[key] = []

        # -- Win rate (overall) ------------------------------------------
        wins = (pnl > 0).sum()
        result["win_rate"] = wins / n if n > 0 else np.nan

        # -- Win rate sliced by strategy / ticker / session_phase --------
        def _win_rate_by(col: str) -> dict:
            if col not in trades_df.columns:
                return {}
            grouped = trades_df.groupby(col)["pnl"]
            out = {}
            for name, group in grouped:
                total = len(group)
                w = (group.astype(float) > 0).sum()
                out[str(name)] = round(w / total, 4) if total > 0 else np.nan
            return out

        result["win_rate_by_strategy"] = _win_rate_by("strategy_name")
        result["win_rate_by_ticker"] = _win_rate_by("ticker")
        result["win_rate_by_session_phase"] = _win_rate_by("session_phase")

        # -- Profit factor -----------------------------------------------
        gross_wins = float(pnl[pnl > 0].sum())
        gross_losses = float(pnl[pnl < 0].sum())
        result["profit_factor"] = (
            gross_wins / abs(gross_losses) if gross_losses != 0 else np.nan
        )

        # -- Expected value per trade ------------------------------------
        result["expected_value_per_trade"] = float(mean_ret)

        # -- Average holding period --------------------------------------
        if "entry_ts" in trades_df.columns and "exit_ts" in trades_df.columns:
            entry = pd.to_datetime(trades_df["entry_ts"])
            exit_ = pd.to_datetime(trades_df["exit_ts"])
            holding = (exit_ - entry).dt.total_seconds()
            result["avg_holding_period_seconds"] = float(holding.mean())
            result["avg_holding_period"] = str(pd.Timedelta(seconds=holding.mean()))
        else:
            result["avg_holding_period_seconds"] = np.nan
            result["avg_holding_period"] = "N/A"

        # -- Recovery factor ---------------------------------------------
        if max_dd_dollar < 0:
            result["recovery_factor"] = total_pnl / abs(max_dd_dollar)
        else:
            result["recovery_factor"] = np.nan

        return result

    # ------------------------------------------------------------------
    # 3. Exit Efficiency
    # ------------------------------------------------------------------
    @staticmethod
    def compute_exit_efficiency(trades_df: pd.DataFrame) -> pd.DataFrame:
        """Compute exit and stop efficiency for each closed trade.

        Parameters
        ----------
        trades_df : DataFrame
            Must include: pnl, mfe (max favourable excursion, $),
            mae (max adverse excursion, $).  Optionally ``max_risk``.

        Returns
        -------
        DataFrame with added columns ``exit_efficiency`` and
        ``stop_efficiency``.
        """
        if trades_df.empty:
            trades_df["exit_efficiency"] = np.nan
            trades_df["stop_efficiency"] = np.nan
            return trades_df.copy()

        df = trades_df.copy()

        pnl = df["pnl"].astype(float)
        mfe = df["mfe"].astype(float)
        mae = df["mae"].astype(float).abs()  # ensure positive

        # exit_efficiency = realized_pnl / MFE
        with np.errstate(divide="ignore", invalid="ignore"):
            df["exit_efficiency"] = np.where(mfe != 0, pnl / mfe, np.nan)

        # stop_efficiency = 1 - (MAE / max_risk)
        if "max_risk" in df.columns:
            max_risk = df["max_risk"].astype(float).abs()
        else:
            # Fallback: use entry_price * qty * stop_pct if available,
            # otherwise approximate max_risk as entry_price * qty * 0.02
            if {"entry_price", "qty"}.issubset(df.columns):
                max_risk = (df["entry_price"].astype(float)
                            * df["qty"].astype(float)
                            * 0.02)
            else:
                max_risk = pd.Series(np.nan, index=df.index)

        with np.errstate(divide="ignore", invalid="ignore"):
            df["stop_efficiency"] = np.where(
                max_risk != 0, 1.0 - (mae / max_risk), np.nan,
            )

        return df

    # ------------------------------------------------------------------
    # 4. Signal Accuracy
    # ------------------------------------------------------------------
    @staticmethod
    def compute_signal_accuracy(
        signals_df: pd.DataFrame,
        trades_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """Per-signal-type accuracy, precision, and edge.

        Parameters
        ----------
        signals_df : DataFrame
            All generated signals with at least: signal_id, signal_type,
            executed (bool).
        trades_df : DataFrame
            Closed trades linked via ``signal_id`` with ``pnl`` column.

        Returns
        -------
        DataFrame indexed by signal_type with columns: total_signals,
        executed_signals, trades_profitable, accuracy, precision,
        edge_per_signal.
        """
        if signals_df.empty:
            return pd.DataFrame(
                columns=[
                    "signal_type", "total_signals", "executed_signals",
                    "trades_profitable", "accuracy", "precision",
                    "edge_per_signal",
                ],
            )

        sig = signals_df.copy()
        trd = trades_df.copy() if not trades_df.empty else pd.DataFrame(
            columns=["signal_id", "pnl"],
        )

        # Ensure executed flag exists
        if "executed" not in sig.columns:
            sig["executed"] = sig["signal_id"].isin(trd["signal_id"])

        # Merge pnl onto signals
        merged = sig.merge(
            trd[["signal_id", "pnl"]].drop_duplicates("signal_id"),
            on="signal_id",
            how="left",
        )
        merged["pnl"] = merged["pnl"].fillna(0.0).astype(float)

        groups = merged.groupby("signal_type")

        rows = []
        for stype, grp in groups:
            total = len(grp)
            executed = grp["executed"].sum()
            profitable = (grp["pnl"] > 0).sum()
            total_pnl = grp["pnl"].sum()

            # accuracy = profitable trades / executed trades
            accuracy = profitable / executed if executed > 0 else np.nan

            # precision = true positives / (true positives + false positives)
            # TP = executed & pnl > 0; FP = executed & pnl <= 0
            executed_mask = grp["executed"].astype(bool)
            tp = ((executed_mask) & (grp["pnl"] > 0)).sum()
            fp = ((executed_mask) & (grp["pnl"] <= 0)).sum()
            precision = tp / (tp + fp) if (tp + fp) > 0 else np.nan

            # edge = avg pnl per signal (blocked signals count as $0)
            edge = total_pnl / total if total > 0 else np.nan

            rows.append({
                "signal_type": stype,
                "total_signals": int(total),
                "executed_signals": int(executed),
                "trades_profitable": int(profitable),
                "accuracy": round(accuracy, 4) if not np.isnan(accuracy) else np.nan,
                "precision": round(precision, 4) if not np.isnan(precision) else np.nan,
                "edge_per_signal": round(edge, 4) if not np.isnan(edge) else np.nan,
            })

        return pd.DataFrame(rows)

    # ------------------------------------------------------------------
    # 5. Full Report
    # ------------------------------------------------------------------
    def full_report(
        self,
        trades_df: pd.DataFrame,
        spy_df: pd.DataFrame,
        signals_df: pd.DataFrame | None = None,
        portfolio_beta: float = DEFAULT_BETA,
    ) -> dict:
        """Run all analytics and return a single dict.

        Parameters
        ----------
        trades_df : DataFrame  (closed trades)
        spy_df : DataFrame     (SPY 1-min bars)
        signals_df : DataFrame (optional, all generated signals)
        portfolio_beta : float

        Returns
        -------
        dict with keys: attribution, performance, exit_efficiency,
        signal_accuracy.
        """
        attribution_df = self.compute_attribution(
            trades_df, spy_df, portfolio_beta=portfolio_beta,
        )
        performance = self.compute_performance(trades_df)
        efficiency_df = self.compute_exit_efficiency(trades_df)

        signal_accuracy = pd.DataFrame()
        if signals_df is not None and not signals_df.empty:
            signal_accuracy = self.compute_signal_accuracy(signals_df, trades_df)

        return {
            "attribution": attribution_df,
            "performance": performance,
            "exit_efficiency": efficiency_df,
            "signal_accuracy": signal_accuracy,
        }
