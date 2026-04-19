"""
Backtest Results Dashboard — Streamlit UI

Loads CSV results from backtest_results/ directory and visualizes:
  - Summary metrics (P&L, Sharpe, drawdown, win rate)
  - Equity curve with drawdown
  - Per-strategy breakdown (P&L, win rate, profit factor)
  - Ticker performance (top/bottom)
  - P&L distribution histogram
  - Exit reason analysis
  - Strategy comparison scatter
  - Win rate vs trade count

Launch: streamlit run dashboards/backtest_dashboard.py
"""
import os
import sys
import glob
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import streamlit as st
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# ── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Backtest Results",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

RESULTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'backtest_results')


# ══════════════════════════════════════════════════════════════════════════════
# Data loading
# ══════════════════════════════════════════════════════════════════════════════

@st.cache_data
def load_backtest_run(run_id: str) -> dict:
    """Load trades, equity curve, and summary for a backtest run."""
    trades_path = os.path.join(RESULTS_DIR, f'trades_{run_id}.csv')
    equity_path = os.path.join(RESULTS_DIR, f'equity_curve_{run_id}.csv')
    summary_path = os.path.join(RESULTS_DIR, f'summary_{run_id}.csv')

    result = {'run_id': run_id}

    if os.path.exists(trades_path):
        df = pd.read_csv(trades_path)
        for col in ['entry_ts', 'exit_ts']:
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors='coerce')
        result['trades'] = df
    else:
        result['trades'] = pd.DataFrame()

    if os.path.exists(equity_path):
        df = pd.read_csv(equity_path)
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
        result['equity'] = df
    else:
        result['equity'] = pd.DataFrame()

    if os.path.exists(summary_path):
        result['summary'] = pd.read_csv(summary_path, nrows=1)
    else:
        result['summary'] = pd.DataFrame()

    return result


def get_available_runs() -> list:
    """Find all backtest runs in results directory."""
    if not os.path.exists(RESULTS_DIR):
        return []
    files = glob.glob(os.path.join(RESULTS_DIR, 'trades_*.csv'))
    runs = []
    for f in files:
        basename = os.path.basename(f)
        run_id = basename.replace('trades_', '').replace('.csv', '')
        runs.append(run_id)
    return sorted(runs, reverse=True)


# ══════════════════════════════════════════════════════════════════════════════
# Chart helpers
# ══════════════════════════════════════════════════════════════════════════════

def _style_axis(ax):
    """Apply dark theme to matplotlib axis."""
    ax.set_facecolor('#0e1117')
    ax.tick_params(colors='#ccc', labelsize=9)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['bottom'].set_color('#444')
    ax.spines['left'].set_color('#444')
    ax.grid(axis='y', alpha=0.12, color='white')


def plot_equity_curve(equity_df: pd.DataFrame) -> plt.Figure:
    """Equity curve with drawdown."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 6), height_ratios=[3, 1], sharex=True)
    fig.patch.set_facecolor('#0e1117')

    eq = equity_df['equity_value'].values
    x = range(len(eq))

    ax1.plot(x, eq, color='#00d4aa', linewidth=1.2)
    ax1.fill_between(x, eq[0], eq, alpha=0.1, color='#00d4aa')
    ax1.axhline(y=eq[0], color='#666', linestyle='--', alpha=0.5, label=f'Start: ${eq[0]:,.0f}')
    ax1.set_ylabel('Equity ($)', color='#ccc')
    ax1.set_title('Equity Curve', color='white', fontsize=14, fontweight='bold')
    ax1.legend(facecolor='#1a1a2e', edgecolor='#333', labelcolor='white')
    _style_axis(ax1)

    # Drawdown
    running_max = np.maximum.accumulate(eq)
    dd_pct = (eq - running_max) / running_max * 100
    ax2.fill_between(x, 0, dd_pct, color='#ff4444', alpha=0.5)
    ax2.set_ylabel('Drawdown %', color='#ccc')
    ax2.set_xlabel('Bar', color='#ccc')
    _style_axis(ax2)

    plt.tight_layout()
    return fig


def plot_pnl_distribution(trades_df: pd.DataFrame) -> plt.Figure:
    """Histogram of trade P&L."""
    fig, ax = plt.subplots(figsize=(8, 4))
    fig.patch.set_facecolor('#0e1117')

    pnl = trades_df['pnl']
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]

    # Clip extremes for better visualization
    clip_low, clip_high = pnl.quantile(0.02), pnl.quantile(0.98)
    pnl_clipped = pnl.clip(clip_low, clip_high)

    bins = min(60, max(15, len(pnl_clipped) // 100))
    ax.hist(pnl_clipped[pnl_clipped > 0], bins=bins, color='#00d4aa', alpha=0.7,
            label=f'Wins ({len(wins)})')
    ax.hist(pnl_clipped[pnl_clipped <= 0], bins=bins, color='#ff4444', alpha=0.7,
            label=f'Losses ({len(losses)})')
    ax.axvline(x=0, color='white', linestyle='--', alpha=0.4)
    ax.axvline(x=pnl.mean(), color='#ffd700', linestyle='-', alpha=0.8,
               label=f'Mean: ${pnl.mean():.2f}')
    ax.axvline(x=pnl.median(), color='#ff8800', linestyle=':', alpha=0.8,
               label=f'Median: ${pnl.median():.2f}')

    ax.set_xlabel('P&L ($)', color='#ccc')
    ax.set_ylabel('Count', color='#ccc')
    ax.set_title('P&L Distribution', color='white', fontsize=14, fontweight='bold')
    ax.legend(facecolor='#1a1a2e', edgecolor='#333', labelcolor='white', fontsize=8)
    _style_axis(ax)
    plt.tight_layout()
    return fig


def plot_strategy_performance(trades_df: pd.DataFrame) -> plt.Figure:
    """Horizontal bar chart of P&L by strategy."""
    strat_pnl = trades_df.groupby('strategy_name').agg(
        total_pnl=('pnl', 'sum'),
        num_trades=('pnl', 'count'),
        win_rate=('pnl', lambda x: (x > 0).mean()),
    ).sort_values('total_pnl')

    fig, ax = plt.subplots(figsize=(10, max(4, len(strat_pnl) * 0.6)))
    fig.patch.set_facecolor('#0e1117')

    colors = ['#00d4aa' if p >= 0 else '#ff4444' for p in strat_pnl['total_pnl']]
    bars = ax.barh(range(len(strat_pnl)), strat_pnl['total_pnl'], color=colors, alpha=0.85)

    labels = [f"{name}  ({int(row.num_trades)}t, {row.win_rate:.0%})"
              for name, row in strat_pnl.iterrows()]
    ax.set_yticks(range(len(strat_pnl)))
    ax.set_yticklabels(labels, fontsize=10, color='white')
    ax.axvline(x=0, color='#666', linestyle='-', alpha=0.5)

    # Add value labels on bars
    for i, (_, row) in enumerate(strat_pnl.iterrows()):
        ax.text(row.total_pnl, i, f' ${row.total_pnl:,.0f}',
                va='center', ha='left' if row.total_pnl >= 0 else 'right',
                color='white', fontsize=9, fontweight='bold')

    ax.set_xlabel('Total P&L ($)', color='#ccc')
    ax.set_title('Strategy Performance', color='white', fontsize=14, fontweight='bold')
    _style_axis(ax)
    plt.tight_layout()
    return fig


def plot_strategy_scatter(trades_df: pd.DataFrame) -> plt.Figure:
    """Scatter: win rate vs avg P&L per strategy, sized by trade count."""
    strat = trades_df.groupby('strategy_name').agg(
        win_rate=('pnl', lambda x: (x > 0).mean() * 100),
        avg_pnl=('pnl', 'mean'),
        total_pnl=('pnl', 'sum'),
        count=('pnl', 'count'),
    )

    fig, ax = plt.subplots(figsize=(8, 5))
    fig.patch.set_facecolor('#0e1117')

    sizes = (strat['count'] / strat['count'].max() * 300).clip(30, 500)
    colors = ['#00d4aa' if p >= 0 else '#ff4444' for p in strat['total_pnl']]

    ax.scatter(strat['win_rate'], strat['avg_pnl'], s=sizes, c=colors,
               alpha=0.8, edgecolors='white', linewidth=0.5)

    for name, row in strat.iterrows():
        ax.annotate(name, (row.win_rate, row.avg_pnl),
                    fontsize=8, color='white', ha='center', va='bottom',
                    xytext=(0, 8), textcoords='offset points')

    ax.axhline(y=0, color='#666', linestyle='--', alpha=0.4)
    ax.axvline(x=50, color='#666', linestyle='--', alpha=0.4)
    ax.set_xlabel('Win Rate (%)', color='#ccc')
    ax.set_ylabel('Avg P&L ($)', color='#ccc')
    ax.set_title('Strategy: Win Rate vs Avg P&L (size = trade count)',
                 color='white', fontsize=13, fontweight='bold')
    _style_axis(ax)
    plt.tight_layout()
    return fig


def plot_exit_reasons(trades_df: pd.DataFrame) -> plt.Figure:
    """Donut chart of exit reasons with P&L breakdown."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4))
    fig.patch.set_facecolor('#0e1117')

    counts = trades_df['exit_reason'].value_counts()
    color_map = {
        'stop': '#ff4444', 'target_2': '#00d4aa', 'target_1': '#00aa88',
        'eod': '#ffd700', 'profit_target': '#00d4aa', 'stop_loss': '#ff4444',
        'dte_close': '#ff8800',
    }
    colors = [color_map.get(r, '#888888') for r in counts.index]

    # Donut chart
    wedges, texts, autotexts = ax1.pie(
        counts, labels=counts.index, autopct='%1.0f%%',
        colors=colors, textprops={'color': 'white', 'fontsize': 10},
        pctdistance=0.8, wedgeprops={'width': 0.4},
    )
    ax1.set_title('Exit Reasons (count)', color='white', fontsize=13, fontweight='bold')

    # P&L by exit reason bar chart
    reason_pnl = trades_df.groupby('exit_reason')['pnl'].agg(['sum', 'mean', 'count'])
    reason_pnl = reason_pnl.sort_values('sum')
    bar_colors = [color_map.get(r, '#888') for r in reason_pnl.index]
    ax2.barh(range(len(reason_pnl)), reason_pnl['sum'], color=bar_colors, alpha=0.85)
    labels = [f"{r}  (avg ${reason_pnl.loc[r, 'mean']:.2f})" for r in reason_pnl.index]
    ax2.set_yticks(range(len(reason_pnl)))
    ax2.set_yticklabels(labels, fontsize=10, color='white')
    ax2.axvline(x=0, color='#666', linestyle='-', alpha=0.5)
    ax2.set_xlabel('Total P&L ($)', color='#ccc')
    ax2.set_title('P&L by Exit Reason', color='white', fontsize=13, fontweight='bold')
    _style_axis(ax2)

    plt.tight_layout()
    return fig


def plot_ticker_performance(trades_df: pd.DataFrame, top_n: int = 20) -> plt.Figure:
    """Top/bottom tickers by P&L."""
    ticker_pnl = trades_df.groupby('ticker').agg(
        total_pnl=('pnl', 'sum'),
        trades=('pnl', 'count'),
        win_rate=('pnl', lambda x: (x > 0).mean()),
    ).sort_values('total_pnl')

    if len(ticker_pnl) > top_n:
        bottom = ticker_pnl.head(top_n // 2)
        top = ticker_pnl.tail(top_n // 2)
        ticker_pnl = pd.concat([bottom, top])

    fig, ax = plt.subplots(figsize=(10, max(4, len(ticker_pnl) * 0.35)))
    fig.patch.set_facecolor('#0e1117')

    colors = ['#00d4aa' if p >= 0 else '#ff4444' for p in ticker_pnl['total_pnl']]
    ax.barh(range(len(ticker_pnl)), ticker_pnl['total_pnl'], color=colors, alpha=0.85)

    labels = [f"{t}  ({int(r.trades)}t, {r.win_rate:.0%})" for t, r in ticker_pnl.iterrows()]
    ax.set_yticks(range(len(ticker_pnl)))
    ax.set_yticklabels(labels, fontsize=9, color='white')
    ax.axvline(x=0, color='#666', linestyle='-', alpha=0.5)
    ax.set_xlabel('Total P&L ($)', color='#ccc')
    ax.set_title('Ticker Performance (Top/Bottom)', color='white', fontsize=14, fontweight='bold')
    _style_axis(ax)
    plt.tight_layout()
    return fig


def plot_cumulative_pnl(trades_df: pd.DataFrame) -> plt.Figure:
    """Cumulative P&L by trade number, colored by strategy."""
    fig, ax = plt.subplots(figsize=(14, 5))
    fig.patch.set_facecolor('#0e1117')

    # Overall cumulative
    cum_pnl = trades_df['pnl'].cumsum()
    ax.plot(range(len(cum_pnl)), cum_pnl, color='#00d4aa', linewidth=1.5,
            label=f'All ({len(trades_df)} trades)', alpha=0.9)
    ax.fill_between(range(len(cum_pnl)), 0, cum_pnl,
                     where=cum_pnl >= 0, color='#00d4aa', alpha=0.08)
    ax.fill_between(range(len(cum_pnl)), 0, cum_pnl,
                     where=cum_pnl < 0, color='#ff4444', alpha=0.08)

    # Per-strategy cumulative (top 5 by trade count)
    top_strats = trades_df['strategy_name'].value_counts().head(5).index
    cmap = plt.cm.Set2(np.linspace(0, 1, len(top_strats)))
    for strat, color in zip(top_strats, cmap):
        mask = trades_df['strategy_name'] == strat
        strat_cum = trades_df.loc[mask, 'pnl'].cumsum()
        ax.plot(trades_df.index[mask], strat_cum, linewidth=1, alpha=0.7,
                color=color, label=strat)

    ax.axhline(y=0, color='#666', linestyle='--', alpha=0.4)
    ax.set_xlabel('Trade #', color='#ccc')
    ax.set_ylabel('Cumulative P&L ($)', color='#ccc')
    ax.set_title('Cumulative P&L by Trade Sequence', color='white', fontsize=14, fontweight='bold')
    ax.legend(facecolor='#1a1a2e', edgecolor='#333', labelcolor='white',
              fontsize=8, ncol=3, loc='upper left')
    _style_axis(ax)
    plt.tight_layout()
    return fig


def plot_win_loss_streaks(trades_df: pd.DataFrame) -> plt.Figure:
    """Win/loss streak distribution."""
    fig, ax = plt.subplots(figsize=(8, 4))
    fig.patch.set_facecolor('#0e1117')

    wins = (trades_df['pnl'] > 0).astype(int)
    streaks = []
    current = 0
    current_type = None
    for w in wins:
        if w == current_type:
            current += 1
        else:
            if current_type is not None:
                streaks.append((current_type, current))
            current_type = w
            current = 1
    if current_type is not None:
        streaks.append((current_type, current))

    win_streaks = [s for t, s in streaks if t == 1]
    loss_streaks = [s for t, s in streaks if t == 0]

    max_streak = max(max(win_streaks, default=0), max(loss_streaks, default=0))
    bins = range(1, min(max_streak + 2, 20))

    ax.hist(win_streaks, bins=bins, color='#00d4aa', alpha=0.7, label='Win streaks')
    ax.hist(loss_streaks, bins=bins, color='#ff4444', alpha=0.7, label='Loss streaks')

    if win_streaks:
        ax.axvline(x=max(win_streaks), color='#00ff88', linestyle='--', alpha=0.6,
                   label=f'Max win: {max(win_streaks)}')
    if loss_streaks:
        ax.axvline(x=max(loss_streaks), color='#ff6666', linestyle='--', alpha=0.6,
                   label=f'Max loss: {max(loss_streaks)}')

    ax.set_xlabel('Streak Length', color='#ccc')
    ax.set_ylabel('Count', color='#ccc')
    ax.set_title('Win/Loss Streaks', color='white', fontsize=14, fontweight='bold')
    ax.legend(facecolor='#1a1a2e', edgecolor='#333', labelcolor='white', fontsize=8)
    _style_axis(ax)
    plt.tight_layout()
    return fig


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard layout
# ══════════════════════════════════════════════════════════════════════════════

def main():
    st.title("Backtest Results Dashboard")

    # ── Sidebar: run selector ────────────────────────────────────────────
    runs = get_available_runs()
    if not runs:
        st.warning(
            f"No backtest results found in `{RESULTS_DIR}/`.\n\n"
            "Run a backtest first:\n```\n"
            "python backtests/run_backtest.py --engine pro --tickers all "
            "--data tradier --save-to-db --csv\n```"
        )
        return

    with st.sidebar:
        st.header("Backtest Run")
        selected_run = st.selectbox(
            "Select run",
            runs,
            format_func=lambda r: f"{r[:4]}-{r[4:6]}-{r[6:8]} {r[9:11]}:{r[11:13]}:{r[13:]}",
        )

    data = load_backtest_run(selected_run)
    trades = data['trades']
    equity = data['equity']
    summary = data['summary']

    if trades.empty:
        st.error("No trade data found for this run.")
        return

    # ── Sidebar: filters ─────────────────────────────────────────────────
    with st.sidebar:
        st.header("Filters")

        strategies = ['All'] + sorted(trades['strategy_name'].unique().tolist())
        selected_strategy = st.selectbox("Strategy", strategies)

        if 'layer' in trades.columns:
            layers = ['All'] + sorted(trades['layer'].unique().tolist())
            selected_layer = st.selectbox("Layer", layers)
        else:
            selected_layer = 'All'

        tickers_list = ['All'] + sorted(trades['ticker'].unique().tolist())
        selected_ticker = st.selectbox("Ticker", tickers_list)

        sides = ['All'] + sorted(trades['side'].dropna().unique().tolist())
        selected_side = st.selectbox("Side", sides)

        exit_reasons = ['All'] + sorted(trades['exit_reason'].dropna().unique().tolist())
        selected_exit = st.selectbox("Exit Reason", exit_reasons)

    # Apply filters
    filtered = trades.copy()
    if selected_strategy != 'All':
        filtered = filtered[filtered['strategy_name'] == selected_strategy]
    if selected_layer != 'All' and 'layer' in filtered.columns:
        filtered = filtered[filtered['layer'] == selected_layer]
    if selected_ticker != 'All':
        filtered = filtered[filtered['ticker'] == selected_ticker]
    if selected_side != 'All':
        filtered = filtered[filtered['side'] == selected_side]
    if selected_exit != 'All':
        filtered = filtered[filtered['exit_reason'] == selected_exit]

    if filtered.empty:
        st.warning("No trades match the selected filters.")
        return

    # ── Summary metrics row ──────────────────────────────────────────────
    total_pnl = filtered['pnl'].sum()
    num_trades = len(filtered)
    wins = (filtered['pnl'] > 0).sum()
    losses = (filtered['pnl'] <= 0).sum()
    win_rate = wins / num_trades if num_trades > 0 else 0
    avg_win = filtered.loc[filtered['pnl'] > 0, 'pnl'].mean() if wins > 0 else 0
    avg_loss = filtered.loc[filtered['pnl'] <= 0, 'pnl'].mean() if losses > 0 else 0
    gross_wins = filtered.loc[filtered['pnl'] > 0, 'pnl'].sum()
    gross_losses = abs(filtered.loc[filtered['pnl'] <= 0, 'pnl'].sum())
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else 0

    sharpe = float(summary['sharpe_ratio'].iloc[0]) if not summary.empty and 'sharpe_ratio' in summary else 0
    max_dd = float(summary['max_drawdown'].iloc[0]) if not summary.empty and 'max_drawdown' in summary else 0

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Total P&L", f"${total_pnl:,.2f}",
              delta_color="normal" if total_pnl >= 0 else "inverse")
    c2.metric("Trades", f"{num_trades:,}", delta=f"{wins}W / {losses}L")
    c3.metric("Win Rate", f"{win_rate:.1%}")
    c4.metric("Profit Factor", f"{profit_factor:.2f}")
    c5.metric("Avg Win / Loss", f"${avg_win:.2f} / ${avg_loss:.2f}")
    c6.metric("Sharpe", f"{sharpe:.2f}")

    st.divider()

    # ── Equity curve ─────────────────────────────────────────────────────
    if not equity.empty and len(equity) > 1:
        st.subheader("Equity Curve")
        fig = plot_equity_curve(equity)
        st.pyplot(fig)
        plt.close(fig)

    # ── Cumulative P&L by trade sequence ─────────────────────────────────
    st.subheader("Cumulative P&L")
    fig = plot_cumulative_pnl(filtered)
    st.pyplot(fig)
    plt.close(fig)

    # ── Strategy performance ─────────────────────────────────────────────
    st.subheader("Strategy Performance")
    fig = plot_strategy_performance(filtered)
    st.pyplot(fig)
    plt.close(fig)

    # ── Two-column charts ────────────────────────────────────────────────
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader("P&L Distribution")
        fig = plot_pnl_distribution(filtered)
        st.pyplot(fig)
        plt.close(fig)

    with col_right:
        st.subheader("Strategy: Win Rate vs Avg P&L")
        fig = plot_strategy_scatter(filtered)
        st.pyplot(fig)
        plt.close(fig)

    # ── Exit reasons ─────────────────────────────────────────────────────
    if 'exit_reason' in filtered.columns and filtered['exit_reason'].notna().any():
        st.subheader("Exit Analysis")
        fig = plot_exit_reasons(filtered)
        st.pyplot(fig)
        plt.close(fig)

    # ── Two more charts ──────────────────────────────────────────────────
    col_left2, col_right2 = st.columns(2)

    with col_left2:
        st.subheader("Ticker Performance")
        fig = plot_ticker_performance(filtered)
        st.pyplot(fig)
        plt.close(fig)

    with col_right2:
        st.subheader("Win/Loss Streaks")
        fig = plot_win_loss_streaks(filtered)
        st.pyplot(fig)
        plt.close(fig)

    # ── Strategy breakdown table ─────────────────────────────────────────
    st.subheader("Strategy Breakdown")
    strat_table = filtered.groupby('strategy_name').agg(
        Trades=('pnl', 'count'),
        Wins=('pnl', lambda x: int((x > 0).sum())),
        Losses=('pnl', lambda x: int((x <= 0).sum())),
        Win_Rate=('pnl', lambda x: f"{(x > 0).mean():.1%}"),
        Total_PnL=('pnl', lambda x: f"${x.sum():,.2f}"),
        Avg_PnL=('pnl', lambda x: f"${x.mean():.2f}"),
        Avg_Win=('pnl', lambda x: f"${x[x > 0].mean():,.2f}" if (x > 0).any() else "$0"),
        Avg_Loss=('pnl', lambda x: f"${x[x <= 0].mean():,.2f}" if (x <= 0).any() else "$0"),
        Best=('pnl', lambda x: f"${x.max():,.2f}"),
        Worst=('pnl', lambda x: f"${x.min():,.2f}"),
        PF=('pnl', lambda x: f"{x[x > 0].sum() / abs(x[x <= 0].sum()):.2f}"
            if x[x <= 0].sum() != 0 else "inf"),
    ).sort_values('Trades', ascending=False)
    st.dataframe(strat_table, use_container_width=True)

    # ── Ticker breakdown table ───────────────────────────────────────────
    st.subheader("Ticker Breakdown")
    ticker_table = filtered.groupby('ticker').agg(
        Trades=('pnl', 'count'),
        Win_Rate=('pnl', lambda x: f"{(x > 0).mean():.1%}"),
        Total_PnL=('pnl', lambda x: f"${x.sum():,.2f}"),
        Avg_PnL=('pnl', lambda x: f"${x.mean():.3f}"),
        Best=('pnl', 'max'),
        Worst=('pnl', 'min'),
    ).sort_values('Trades', ascending=False)
    st.dataframe(ticker_table, use_container_width=True, height=400)

    # ── Trade log ────────────────────────────────────────────────────────
    st.subheader("Trade Log (last 200)")
    display_cols = [c for c in ['ticker', 'strategy_name', 'side', 'entry_price',
                                'exit_price', 'qty', 'exit_reason', 'pnl', 'pnl_pct']
                    if c in filtered.columns]
    st.dataframe(
        filtered[display_cols].tail(200),
        use_container_width=True,
        height=400,
    )

    # ── Footer ───────────────────────────────────────────────────────────
    st.caption(
        f"Run: {selected_run} | "
        f"All trades: {len(trades)} | "
        f"Filtered: {len(filtered)} | "
        f"Strategies: {filtered['strategy_name'].nunique()}"
    )


if __name__ == '__main__':
    main()
