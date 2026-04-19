"""
Backtest Results Dashboard — Streamlit UI

Loads CSV results from backtest_results/ directory and visualizes:
  - Summary metrics (P&L, Sharpe, drawdown, win rate)
  - Equity curve
  - Per-strategy breakdown
  - Trade list with filters
  - P&L distribution
  - Daily P&L heatmap
  - Win rate by hour / exit reason

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
import matplotlib.dates as mdates

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
        if 'entry_ts' in df.columns:
            df['entry_ts'] = pd.to_datetime(df['entry_ts'], errors='coerce')
        if 'exit_ts' in df.columns:
            df['exit_ts'] = pd.to_datetime(df['exit_ts'], errors='coerce')
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
        # Extract run_id from trades_YYYYMMDD_HHMMSS.csv
        run_id = basename.replace('trades_', '').replace('.csv', '')
        runs.append(run_id)
    return sorted(runs, reverse=True)


# ══════════════════════════════════════════════════════════════════════════════
# Chart helpers
# ══════════════════════════════════════════════════════════════════════════════

def plot_equity_curve(equity_df: pd.DataFrame) -> plt.Figure:
    """Plot equity curve with drawdown shading."""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 6), height_ratios=[3, 1],
                                    sharex=True)
    fig.patch.set_facecolor('#0e1117')

    ts = equity_df['timestamp']
    eq = equity_df['equity_value']

    # Equity curve
    ax1.plot(ts, eq, color='#00d4aa', linewidth=1.5, label='Equity')
    ax1.fill_between(ts, eq.iloc[0], eq, alpha=0.15, color='#00d4aa')
    ax1.axhline(y=eq.iloc[0], color='#555', linestyle='--', alpha=0.5, label='Starting')
    ax1.set_ylabel('Equity ($)', color='white')
    ax1.set_title('Equity Curve', color='white', fontsize=14, fontweight='bold')
    ax1.legend(loc='upper left', facecolor='#1a1a2e', edgecolor='#333',
               labelcolor='white')
    _style_axis(ax1)

    # Drawdown
    running_max = eq.cummax()
    drawdown = (eq - running_max) / running_max * 100
    ax2.fill_between(ts, 0, drawdown, color='#ff4444', alpha=0.6)
    ax2.set_ylabel('Drawdown %', color='white')
    ax2.set_xlabel('Time', color='white')
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

    bins = min(50, max(10, len(pnl) // 5))
    ax.hist(wins, bins=bins, color='#00d4aa', alpha=0.7, label=f'Wins ({len(wins)})')
    ax.hist(losses, bins=bins, color='#ff4444', alpha=0.7, label=f'Losses ({len(losses)})')
    ax.axvline(x=0, color='white', linestyle='--', alpha=0.5)
    ax.axvline(x=pnl.mean(), color='#ffd700', linestyle='-', alpha=0.8,
               label=f'Mean: ${pnl.mean():.2f}')

    ax.set_xlabel('P&L ($)', color='white')
    ax.set_ylabel('Count', color='white')
    ax.set_title('P&L Distribution', color='white', fontsize=14, fontweight='bold')
    ax.legend(facecolor='#1a1a2e', edgecolor='#333', labelcolor='white')
    _style_axis(ax)

    plt.tight_layout()
    return fig


def plot_daily_pnl(trades_df: pd.DataFrame) -> plt.Figure:
    """Daily P&L bar chart."""
    fig, ax = plt.subplots(figsize=(12, 4))
    fig.patch.set_facecolor('#0e1117')

    if 'exit_ts' not in trades_df.columns or trades_df['exit_ts'].isna().all():
        ax.text(0.5, 0.5, 'No exit timestamps available', ha='center', va='center',
                color='white', transform=ax.transAxes)
        _style_axis(ax)
        return fig

    df = trades_df.dropna(subset=['exit_ts']).copy()
    df['date'] = df['exit_ts'].dt.date
    daily = df.groupby('date')['pnl'].sum().reset_index()

    colors = ['#00d4aa' if p >= 0 else '#ff4444' for p in daily['pnl']]
    ax.bar(range(len(daily)), daily['pnl'], color=colors, alpha=0.8)
    ax.set_xticks(range(len(daily)))
    ax.set_xticklabels([str(d) for d in daily['date']], rotation=45, ha='right',
                        fontsize=8, color='white')
    ax.axhline(y=0, color='#555', linestyle='-', alpha=0.5)
    ax.set_ylabel('P&L ($)', color='white')
    ax.set_title('Daily P&L', color='white', fontsize=14, fontweight='bold')
    _style_axis(ax)

    plt.tight_layout()
    return fig


def plot_strategy_performance(trades_df: pd.DataFrame) -> plt.Figure:
    """Horizontal bar chart of P&L by strategy."""
    fig, ax = plt.subplots(figsize=(10, max(4, len(trades_df['strategy_name'].unique()) * 0.5)))
    fig.patch.set_facecolor('#0e1117')

    strat_pnl = trades_df.groupby('strategy_name').agg(
        total_pnl=('pnl', 'sum'),
        num_trades=('pnl', 'count'),
        win_rate=('pnl', lambda x: (x > 0).mean()),
    ).sort_values('total_pnl')

    colors = ['#00d4aa' if p >= 0 else '#ff4444' for p in strat_pnl['total_pnl']]
    bars = ax.barh(range(len(strat_pnl)), strat_pnl['total_pnl'], color=colors, alpha=0.8)

    labels = [f"{name}  ({row.num_trades}t, {row.win_rate:.0%})"
              for name, row in strat_pnl.iterrows()]
    ax.set_yticks(range(len(strat_pnl)))
    ax.set_yticklabels(labels, fontsize=9, color='white')
    ax.axvline(x=0, color='#555', linestyle='-', alpha=0.5)
    ax.set_xlabel('Total P&L ($)', color='white')
    ax.set_title('Strategy Performance', color='white', fontsize=14, fontweight='bold')
    _style_axis(ax)

    plt.tight_layout()
    return fig


def plot_cumulative_pnl_by_strategy(trades_df: pd.DataFrame) -> plt.Figure:
    """Cumulative P&L line chart per strategy over time."""
    fig, ax = plt.subplots(figsize=(12, 5))
    fig.patch.set_facecolor('#0e1117')

    if 'exit_ts' not in trades_df.columns or trades_df['exit_ts'].isna().all():
        ax.text(0.5, 0.5, 'No exit timestamps available', ha='center', va='center',
                color='white', transform=ax.transAxes)
        _style_axis(ax)
        return fig

    df = trades_df.dropna(subset=['exit_ts']).sort_values('exit_ts')
    strategies = df['strategy_name'].unique()

    colors = plt.cm.Set2(np.linspace(0, 1, len(strategies)))
    for strat, color in zip(strategies, colors):
        mask = df['strategy_name'] == strat
        cum_pnl = df.loc[mask, 'pnl'].cumsum()
        ax.plot(df.loc[mask, 'exit_ts'], cum_pnl, label=strat, color=color, linewidth=1.2)

    ax.axhline(y=0, color='#555', linestyle='--', alpha=0.5)
    ax.set_ylabel('Cumulative P&L ($)', color='white')
    ax.set_title('Cumulative P&L by Strategy', color='white', fontsize=14, fontweight='bold')
    ax.legend(loc='upper left', facecolor='#1a1a2e', edgecolor='#333',
              labelcolor='white', fontsize=8, ncol=2)
    _style_axis(ax)

    plt.tight_layout()
    return fig


def plot_exit_reasons(trades_df: pd.DataFrame) -> plt.Figure:
    """Pie chart of exit reasons."""
    fig, ax = plt.subplots(figsize=(6, 4))
    fig.patch.set_facecolor('#0e1117')

    if 'exit_reason' not in trades_df.columns:
        ax.text(0.5, 0.5, 'No exit reason data', ha='center', va='center',
                color='white', transform=ax.transAxes)
        return fig

    counts = trades_df['exit_reason'].value_counts()
    color_map = {
        'stop': '#ff4444', 'target_2': '#00d4aa', 'target_1': '#00aa88',
        'eod': '#ffd700', 'profit_target': '#00d4aa', 'stop_loss': '#ff4444',
        'dte_close': '#ff8800',
    }
    colors = [color_map.get(r, '#888888') for r in counts.index]

    wedges, texts, autotexts = ax.pie(
        counts, labels=counts.index, autopct='%1.0f%%',
        colors=colors, textprops={'color': 'white', 'fontsize': 9},
    )
    ax.set_title('Exit Reasons', color='white', fontsize=14, fontweight='bold')

    plt.tight_layout()
    return fig


def plot_hourly_performance(trades_df: pd.DataFrame) -> plt.Figure:
    """Bar chart of P&L by entry hour."""
    fig, ax = plt.subplots(figsize=(8, 4))
    fig.patch.set_facecolor('#0e1117')

    if 'entry_ts' not in trades_df.columns or trades_df['entry_ts'].isna().all():
        ax.text(0.5, 0.5, 'No entry timestamps', ha='center', va='center',
                color='white', transform=ax.transAxes)
        _style_axis(ax)
        return fig

    df = trades_df.dropna(subset=['entry_ts']).copy()
    df['hour'] = df['entry_ts'].dt.hour
    hourly = df.groupby('hour').agg(
        total_pnl=('pnl', 'sum'),
        count=('pnl', 'count'),
    ).reindex(range(9, 16), fill_value=0)

    colors = ['#00d4aa' if p >= 0 else '#ff4444' for p in hourly['total_pnl']]
    ax.bar(hourly.index, hourly['total_pnl'], color=colors, alpha=0.8)

    for i, (h, row) in enumerate(hourly.iterrows()):
        if row['count'] > 0:
            ax.text(h, row['total_pnl'], f"{int(row['count'])}t",
                    ha='center', va='bottom' if row['total_pnl'] >= 0 else 'top',
                    color='white', fontsize=8)

    ax.set_xlabel('Hour (ET)', color='white')
    ax.set_ylabel('P&L ($)', color='white')
    ax.set_title('P&L by Entry Hour', color='white', fontsize=14, fontweight='bold')
    ax.axhline(y=0, color='#555', linestyle='-', alpha=0.5)
    _style_axis(ax)

    plt.tight_layout()
    return fig


def plot_ticker_heatmap(trades_df: pd.DataFrame, top_n: int = 20) -> plt.Figure:
    """Top/bottom tickers by P&L."""
    fig, ax = plt.subplots(figsize=(10, max(4, top_n * 0.3)))
    fig.patch.set_facecolor('#0e1117')

    ticker_pnl = trades_df.groupby('ticker').agg(
        total_pnl=('pnl', 'sum'),
        trades=('pnl', 'count'),
        win_rate=('pnl', lambda x: (x > 0).mean()),
    ).sort_values('total_pnl')

    # Show top and bottom tickers
    if len(ticker_pnl) > top_n:
        bottom = ticker_pnl.head(top_n // 2)
        top = ticker_pnl.tail(top_n // 2)
        ticker_pnl = pd.concat([bottom, top])

    colors = ['#00d4aa' if p >= 0 else '#ff4444' for p in ticker_pnl['total_pnl']]
    ax.barh(range(len(ticker_pnl)), ticker_pnl['total_pnl'], color=colors, alpha=0.8)

    labels = [f"{t}  ({int(r.trades)}t)" for t, r in ticker_pnl.iterrows()]
    ax.set_yticks(range(len(ticker_pnl)))
    ax.set_yticklabels(labels, fontsize=8, color='white')
    ax.axvline(x=0, color='#555', linestyle='-', alpha=0.5)
    ax.set_xlabel('Total P&L ($)', color='white')
    ax.set_title(f'Top/Bottom Tickers (by P&L)', color='white', fontsize=14, fontweight='bold')
    _style_axis(ax)

    plt.tight_layout()
    return fig


def _style_axis(ax):
    """Apply dark theme to matplotlib axis."""
    ax.set_facecolor('#0e1117')
    ax.tick_params(colors='white', labelsize=9)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['bottom'].set_color('#333')
    ax.spines['left'].set_color('#333')
    ax.grid(axis='y', alpha=0.15, color='white')


# ══════════════════════════════════════════════════════════════════════════════
# Dashboard layout
# ══════════════════════════════════════════════════════════════════════════════

def main():
    st.title("Backtest Results Dashboard")

    # ── Sidebar: run selector ────────────────────────────────────────────
    runs = get_available_runs()
    if not runs:
        st.warning(
            f"No backtest results found in `{RESULTS_DIR}/`. "
            "Run a backtest first:\n\n"
            "```\npython backtests/run_backtest.py --engine pro --tickers all "
            "--data tradier --save-to-db --csv\n```"
        )
        return

    with st.sidebar:
        st.header("Backtest Run")
        selected_run = st.selectbox(
            "Select run",
            runs,
            format_func=lambda r: f"{r[:8]} {r[9:11]}:{r[11:13]}:{r[13:]}",
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

        layers = ['All'] + sorted(trades['layer'].unique().tolist())
        selected_layer = st.selectbox("Layer", layers)

        tickers = ['All'] + sorted(trades['ticker'].unique().tolist())
        selected_ticker = st.selectbox("Ticker", tickers)

        sides = ['All'] + sorted(trades['side'].unique().tolist())
        selected_side = st.selectbox("Side", sides)

    # Apply filters
    filtered = trades.copy()
    if selected_strategy != 'All':
        filtered = filtered[filtered['strategy_name'] == selected_strategy]
    if selected_layer != 'All':
        filtered = filtered[filtered['layer'] == selected_layer]
    if selected_ticker != 'All':
        filtered = filtered[filtered['ticker'] == selected_ticker]
    if selected_side != 'All':
        filtered = filtered[filtered['side'] == selected_side]

    # ── Summary metrics ──────────────────────────────────────────────────
    total_pnl = filtered['pnl'].sum()
    num_trades = len(filtered)
    wins = (filtered['pnl'] > 0).sum()
    losses = (filtered['pnl'] <= 0).sum()
    win_rate = wins / num_trades if num_trades > 0 else 0
    avg_win = filtered.loc[filtered['pnl'] > 0, 'pnl'].mean() if wins > 0 else 0
    avg_loss = filtered.loc[filtered['pnl'] <= 0, 'pnl'].mean() if losses > 0 else 0
    profit_factor = abs(filtered.loc[filtered['pnl'] > 0, 'pnl'].sum() /
                        filtered.loc[filtered['pnl'] <= 0, 'pnl'].sum()) \
        if losses > 0 and filtered.loc[filtered['pnl'] <= 0, 'pnl'].sum() != 0 else 0

    # Sharpe and drawdown from summary if available
    sharpe = float(summary['sharpe_ratio'].iloc[0]) if not summary.empty and 'sharpe_ratio' in summary else 0
    max_dd = float(summary['max_drawdown'].iloc[0]) if not summary.empty and 'max_drawdown' in summary else 0

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    with col1:
        color = "normal" if total_pnl >= 0 else "inverse"
        st.metric("Total P&L", f"${total_pnl:,.2f}", delta=f"{total_pnl:+,.2f}",
                  delta_color=color)
    with col2:
        st.metric("Trades", f"{num_trades}", delta=f"{wins}W / {losses}L")
    with col3:
        st.metric("Win Rate", f"{win_rate:.1%}")
    with col4:
        st.metric("Profit Factor", f"{profit_factor:.2f}")
    with col5:
        st.metric("Sharpe Ratio", f"{sharpe:.2f}")
    with col6:
        st.metric("Max Drawdown", f"${max_dd:,.2f}")

    st.divider()

    # ── Equity curve ─────────────────────────────────────────────────────
    if not equity.empty:
        st.subheader("Equity Curve")
        fig = plot_equity_curve(equity)
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
        st.subheader("Exit Reasons")
        fig = plot_exit_reasons(filtered)
        st.pyplot(fig)
        plt.close(fig)

    # ── Daily P&L ────────────────────────────────────────────────────────
    st.subheader("Daily P&L")
    fig = plot_daily_pnl(filtered)
    st.pyplot(fig)
    plt.close(fig)

    # ── Strategy Performance ─────────────────────────────────────────────
    col_left2, col_right2 = st.columns(2)

    with col_left2:
        st.subheader("Strategy Performance")
        fig = plot_strategy_performance(filtered)
        st.pyplot(fig)
        plt.close(fig)

    with col_right2:
        st.subheader("P&L by Entry Hour")
        fig = plot_hourly_performance(filtered)
        st.pyplot(fig)
        plt.close(fig)

    # ── Cumulative P&L by Strategy ───────────────────────────────────────
    st.subheader("Cumulative P&L by Strategy")
    fig = plot_cumulative_pnl_by_strategy(filtered)
    st.pyplot(fig)
    plt.close(fig)

    # ── Top/Bottom Tickers ───────────────────────────────────────────────
    st.subheader("Ticker Performance")
    fig = plot_ticker_heatmap(filtered)
    st.pyplot(fig)
    plt.close(fig)

    # ── Strategy breakdown table ─────────────────────────────────────────
    st.subheader("Strategy Breakdown")
    strat_table = filtered.groupby('strategy_name').agg(
        Trades=('pnl', 'count'),
        Wins=('pnl', lambda x: (x > 0).sum()),
        Losses=('pnl', lambda x: (x <= 0).sum()),
        Win_Rate=('pnl', lambda x: f"{(x > 0).mean():.1%}"),
        Total_PnL=('pnl', lambda x: f"${x.sum():,.2f}"),
        Avg_Win=('pnl', lambda x: f"${x[x > 0].mean():,.2f}" if (x > 0).any() else "$0"),
        Avg_Loss=('pnl', lambda x: f"${x[x <= 0].mean():,.2f}" if (x <= 0).any() else "$0"),
        Best_Trade=('pnl', lambda x: f"${x.max():,.2f}"),
        Worst_Trade=('pnl', lambda x: f"${x.min():,.2f}"),
    ).sort_values('Trades', ascending=False)
    st.dataframe(strat_table, use_container_width=True)

    # ── Trade log ────────────────────────────────────────────────────────
    st.subheader("Trade Log")
    display_cols = ['ticker', 'layer', 'strategy_name', 'side', 'entry_price',
                    'exit_price', 'qty', 'exit_reason', 'pnl', 'pnl_pct']
    available_cols = [c for c in display_cols if c in filtered.columns]

    # Color P&L column
    def color_pnl(val):
        try:
            v = float(val)
            if v > 0:
                return 'color: #00d4aa'
            elif v < 0:
                return 'color: #ff4444'
        except (ValueError, TypeError):
            pass
        return ''

    styled = filtered[available_cols].style.applymap(
        color_pnl, subset=['pnl'] if 'pnl' in available_cols else [],
    )
    st.dataframe(styled, use_container_width=True, height=400)

    # ── Footer ───────────────────────────────────────────────────────────
    st.caption(
        f"Run: {selected_run} | "
        f"Total trades: {num_trades} | "
        f"Filtered: {len(filtered)} | "
        f"Data: {RESULTS_DIR}"
    )


if __name__ == '__main__':
    main()
