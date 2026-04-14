"""Institutional risk metrics computed from trade history."""
import logging
import numpy as np
from collections import defaultdict

log = logging.getLogger(__name__)


class RiskMetricsEngine:
    """Compute institutional risk metrics from trade history."""

    def __init__(self, risk_free_rate: float = 0.05):
        self.rf = risk_free_rate

    def compute(self, trades: list) -> dict:
        """Compute risk metrics from trade log.

        Args:
            trades: list of trade dicts with 'pnl', 'time', 'ticker' keys

        Returns dict with all metrics. Handles edge cases (<2 trades, all wins, etc.)
        """
        # Metrics to compute:
        # sharpe_ratio (annualized), sortino_ratio (annualized), calmar_ratio
        # daily_var_95, daily_var_99 (parametric)
        # max_drawdown, max_drawdown_pct, recovery_factor
        # win_rate, profit_factor, avg_rr (risk/reward)
        # expectancy, kelly_fraction
        # consecutive_losses_max, consecutive_wins_max
        # trade_count

        if not trades or len(trades) < 2:
            return self._empty_metrics(len(trades) if trades else 0)

        pnls = np.array([t['pnl'] for t in trades], dtype=float)
        n = len(pnls)

        # Basic stats
        wins = pnls[pnls >= 0]
        losses = pnls[pnls < 0]
        total_pnl = float(pnls.sum())
        win_rate = len(wins) / n if n > 0 else 0
        loss_rate = 1 - win_rate
        avg_win = float(wins.mean()) if len(wins) > 0 else 0
        avg_loss = float(losses.mean()) if len(losses) > 0 else 0
        gross_wins = float(wins.sum()) if len(wins) > 0 else 0
        gross_losses = float(abs(losses.sum())) if len(losses) > 0 else 0

        # Profit factor
        profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')

        # Risk/reward
        avg_rr = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

        # Expectancy
        expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)

        # Kelly fraction
        if avg_loss != 0 and avg_rr != 0:
            kelly = win_rate - (loss_rate / avg_rr)
        else:
            kelly = 0

        # Cumulative P&L for drawdown
        cum_pnl = np.cumsum(pnls)
        running_max = np.maximum.accumulate(cum_pnl)
        drawdowns = cum_pnl - running_max
        max_dd = float(drawdowns.min())
        max_dd_pct = float(max_dd / running_max[drawdowns.argmin()]) if running_max[drawdowns.argmin()] != 0 else 0

        # Recovery factor
        recovery = total_pnl / abs(max_dd) if max_dd != 0 else float('inf')

        # Sharpe (treat each trade as a "daily return")
        mean_ret = float(pnls.mean())
        std_ret = float(pnls.std(ddof=1)) if n > 1 else 1
        daily_rf = self.rf / 252
        sharpe = ((mean_ret - daily_rf) / std_ret * np.sqrt(252)) if std_ret > 0 else 0

        # Sortino (downside deviation only)
        downside = pnls[pnls < 0]
        downside_std = float(downside.std(ddof=1)) if len(downside) > 1 else std_ret
        sortino = ((mean_ret - daily_rf) / downside_std * np.sqrt(252)) if downside_std > 0 else 0

        # Calmar
        annualized_return = mean_ret * 252
        calmar = annualized_return / abs(max_dd) if max_dd != 0 else float('inf')

        # VaR (parametric)
        var_95 = float(mean_ret - 1.645 * std_ret)
        var_99 = float(mean_ret - 2.326 * std_ret)

        # Consecutive wins/losses
        cons_wins, cons_losses = self._consecutive_streaks(pnls)

        return {
            'sharpe_ratio': round(sharpe, 3),
            'sortino_ratio': round(sortino, 3),
            'calmar_ratio': round(calmar, 3),
            'daily_var_95': round(var_95, 2),
            'daily_var_99': round(var_99, 2),
            'max_drawdown': round(max_dd, 2),
            'max_drawdown_pct': round(max_dd_pct * 100, 2),
            'recovery_factor': round(recovery, 3),
            'win_rate': round(win_rate * 100, 1),
            'profit_factor': round(profit_factor, 3),
            'avg_rr': round(avg_rr, 3),
            'expectancy': round(expectancy, 3),
            'kelly_fraction': round(kelly, 3),
            'consecutive_wins_max': cons_wins,
            'consecutive_losses_max': cons_losses,
            'trade_count': n,
            'total_pnl': round(total_pnl, 2),
        }

    def rolling_sharpe(self, trades: list, window: int = 20) -> list:
        """Rolling Sharpe ratio over last N trades."""
        if len(trades) < window:
            return []
        pnls = [t['pnl'] for t in trades]
        result = []
        daily_rf = self.rf / 252
        for i in range(window, len(pnls) + 1):
            chunk = np.array(pnls[i-window:i])
            mean_r = chunk.mean()
            std_r = chunk.std(ddof=1)
            s = ((mean_r - daily_rf) / std_r * np.sqrt(252)) if std_r > 0 else 0
            result.append(round(float(s), 3))
        return result

    def rolling_var(self, trades: list, window: int = 20, confidence: float = 0.95) -> list:
        """Rolling VaR over last N trades."""
        if len(trades) < window:
            return []
        z = 1.645 if confidence == 0.95 else 2.326
        pnls = [t['pnl'] for t in trades]
        result = []
        for i in range(window, len(pnls) + 1):
            chunk = np.array(pnls[i-window:i])
            v = float(chunk.mean() - z * chunk.std(ddof=1))
            result.append(round(v, 2))
        return result

    def sector_exposure(self, positions: dict) -> dict:
        """Current portfolio exposure by GICS sector."""
        from monitor.sector_map import get_sector
        exposure = defaultdict(lambda: {'count': 0, 'notional': 0.0})
        total_notional = 0
        for ticker, pos in positions.items():
            sector = get_sector(ticker)
            qty = pos.get('quantity', 0)
            price = pos.get('entry_price', 0)
            notional = qty * price
            exposure[sector]['count'] += 1
            exposure[sector]['notional'] += notional
            total_notional += notional
        # Add percentage
        for sector in exposure:
            exposure[sector]['pct'] = round(
                exposure[sector]['notional'] / total_notional * 100, 1
            ) if total_notional > 0 else 0
            exposure[sector]['notional'] = round(exposure[sector]['notional'], 2)
        return dict(exposure)

    def _consecutive_streaks(self, pnls):
        max_wins = max_losses = cur_wins = cur_losses = 0
        for p in pnls:
            if p >= 0:
                cur_wins += 1
                cur_losses = 0
            else:
                cur_losses += 1
                cur_wins = 0
            max_wins = max(max_wins, cur_wins)
            max_losses = max(max_losses, cur_losses)
        return max_wins, max_losses

    def _empty_metrics(self, n):
        return {k: 0 for k in [
            'sharpe_ratio', 'sortino_ratio', 'calmar_ratio',
            'daily_var_95', 'daily_var_99', 'max_drawdown', 'max_drawdown_pct',
            'recovery_factor', 'win_rate', 'profit_factor', 'avg_rr',
            'expectancy', 'kelly_fraction', 'consecutive_wins_max',
            'consecutive_losses_max',
        ]} | {'trade_count': n, 'total_pnl': 0}
