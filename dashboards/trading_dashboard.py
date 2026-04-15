"""
Trading Hub — Streamlit Dashboard

Launch: streamlit run dashboards/trading_dashboard.py
Auto-refreshes every 60 seconds during market hours.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import psycopg2
from datetime import date, timedelta

# ── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Trading Hub",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://trading:trading_secret@localhost:5432/tradinghub",
)


@st.cache_resource
def get_engine():
    from sqlalchemy import create_engine
    return create_engine(DATABASE_URL)


@st.cache_data(ttl=60)
def run_query(query):
    engine = get_engine()
    with engine.connect() as conn:
        return pd.read_sql_query(query, conn)


# ── Sidebar ──────────────────────────────────────────────────────────────────
st.sidebar.title("Trading Hub")
st.sidebar.markdown("---")

# Date filter
available_dates = run_query("""
    SELECT DISTINCT event_time::DATE as d FROM event_store
    WHERE event_type = 'PositionClosed'
    ORDER BY d DESC LIMIT 30
""")
if not available_dates.empty:
    dates = available_dates['d'].tolist()
    selected_date = st.sidebar.selectbox("Trading Date", dates, index=0)
else:
    selected_date = date.today()

# Ticker filter
tickers_df = run_query(f"""
    SELECT DISTINCT event_payload->>'ticker' as ticker
    FROM event_store
    WHERE event_type = 'PositionClosed'
      AND event_time::DATE = '{selected_date}'
    ORDER BY 1
""")
all_tickers = tickers_df['ticker'].tolist() if not tickers_df.empty else []
selected_tickers = st.sidebar.multiselect("Tickers", all_tickers, default=all_tickers)

ticker_filter = "AND 1=1"
if selected_tickers and len(selected_tickers) < len(all_tickers):
    tlist = ",".join(f"'{t}'" for t in selected_tickers)
    ticker_filter = f"AND event_payload->>'ticker' IN ({tlist})"

st.sidebar.markdown("---")
auto_refresh = st.sidebar.checkbox("Auto-refresh (60s)", value=False)
if auto_refresh:
    st.rerun()

# ── Load Data ────────────────────────────────────────────────────────────────

trades_df = run_query(f"""
    SELECT
        c.event_id as trade_id,
        o.event_payload->>'ticker' as ticker,
        o.event_time as entry_ts,
        c.event_time as exit_ts,
        EXTRACT(EPOCH FROM (c.event_time - o.event_time))::INT as duration_sec,
        CASE
            WHEN EXTRACT(HOUR FROM o.event_time) * 60 + EXTRACT(MINUTE FROM o.event_time) < 585 THEN 'First 15min'
            WHEN EXTRACT(HOUR FROM o.event_time) * 60 + EXTRACT(MINUTE FROM o.event_time) < 690 THEN 'Morning'
            WHEN EXTRACT(HOUR FROM o.event_time) * 60 + EXTRACT(MINUTE FROM o.event_time) < 810 THEN 'Midday'
            WHEN EXTRACT(HOUR FROM o.event_time) * 60 + EXTRACT(MINUTE FROM o.event_time) < 930 THEN 'Afternoon'
            ELSE 'Power Hour'
        END as session_phase,
        (o.event_payload->>'entry_price')::FLOAT as entry_price,
        (c.event_payload->>'current_price')::FLOAT as exit_price,
        (o.event_payload->>'qty')::INT as qty,
        COALESCE((c.event_payload->>'pnl')::FLOAT, 0) as pnl,
        COALESCE(c.event_payload->>'exit_reason', c.event_payload->>'action', 'unknown') as exit_reason,
        EXTRACT(HOUR FROM o.event_time)::INT as entry_hour
    FROM event_store c
    JOIN event_store o ON c.aggregate_id = o.aggregate_id
        AND o.event_type = 'PositionOpened'
    WHERE c.event_type = 'PositionClosed'
      AND c.event_time::DATE = '{selected_date}'
      {ticker_filter}
    ORDER BY c.event_time
""")

if trades_df.empty:
    st.warning(f"No trades found for {selected_date}")
    st.stop()

trades_df['outcome'] = trades_df['pnl'].apply(lambda x: 'Win' if x > 0 else 'Loss')
trades_df['cumulative_pnl'] = trades_df['pnl'].cumsum()
trades_df['hold_min'] = (trades_df['duration_sec'].fillna(0) / 60).round(1)

# ── KPI Cards ────────────────────────────────────────────────────────────────
total_pnl = trades_df['pnl'].sum()
total_trades = len(trades_df)
wins = (trades_df['pnl'] > 0).sum()
losses = total_trades - wins
win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
avg_win = trades_df.loc[trades_df['pnl'] > 0, 'pnl'].mean() if wins > 0 else 0
avg_loss = trades_df.loc[trades_df['pnl'] <= 0, 'pnl'].mean() if losses > 0 else 0
profit_factor = abs(trades_df.loc[trades_df['pnl'] > 0, 'pnl'].sum() / trades_df.loc[trades_df['pnl'] <= 0, 'pnl'].sum()) if losses > 0 and trades_df.loc[trades_df['pnl'] <= 0, 'pnl'].sum() != 0 else 0
max_dd = (trades_df['cumulative_pnl'] - trades_df['cumulative_pnl'].cummax()).min()

st.title(f"Trading Dashboard — {selected_date}")

k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Total P&L", f"${total_pnl:+,.2f}", delta_color="normal")
k2.metric("Trades", f"{total_trades}", f"{wins}W / {losses}L")
k3.metric("Win Rate", f"{win_rate:.1f}%")
k4.metric("Avg Win", f"${avg_win:+,.2f}")
k5.metric("Avg Loss", f"${avg_loss:,.2f}")
k6.metric("Max Drawdown", f"${max_dd:,.2f}")

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 1: Equity Curve + P&L by Ticker
# ═══════════════════════════════════════════════════════════════════════════════
col1, col2 = st.columns([3, 2])

with col1:
    st.subheader("Equity Curve")
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=list(range(1, len(trades_df) + 1)),
        y=trades_df['cumulative_pnl'],
        mode='lines+markers',
        line=dict(width=2, color='#00CC96'),
        marker=dict(
            size=6,
            color=trades_df['pnl'].apply(lambda x: '#00CC96' if x > 0 else '#EF553B'),
        ),
        hovertemplate='Trade #%{x}<br>Cum P&L: $%{y:.2f}<br>%{text}',
        text=trades_df.apply(lambda r: f"{r['ticker']} ${r['pnl']:+.2f}", axis=1),
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
    fig.update_layout(
        height=350, margin=dict(l=20, r=20, t=10, b=20),
        xaxis_title="Trade #", yaxis_title="Cumulative P&L ($)",
        showlegend=False,
    )
    st.plotly_chart(fig, width='stretch')

with col2:
    st.subheader("P&L by Ticker")
    ticker_pnl = trades_df.groupby('ticker')['pnl'].sum().sort_values()
    fig = go.Figure(go.Bar(
        y=ticker_pnl.index,
        x=ticker_pnl.values,
        orientation='h',
        marker_color=ticker_pnl.values > 0,
        marker=dict(
            color=['#00CC96' if v > 0 else '#EF553B' for v in ticker_pnl.values],
        ),
        hovertemplate='%{y}: $%{x:.2f}<extra></extra>',
    ))
    fig.update_layout(
        height=350, margin=dict(l=20, r=20, t=10, b=20),
        xaxis_title="P&L ($)",
    )
    st.plotly_chart(fig, width='stretch')

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 2: Exit Reasons + Session Phase + Distribution
# ═══════════════════════════════════════════════════════════════════════════════
col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("Exit Reasons")
    exit_stats = trades_df.groupby('exit_reason').agg(
        count=('pnl', 'count'),
        total_pnl=('pnl', 'sum'),
        win_rate=('outcome', lambda x: (x == 'Win').mean() * 100),
    ).sort_values('count', ascending=False)

    fig = px.bar(
        exit_stats.reset_index(),
        x='exit_reason', y='count',
        color='total_pnl',
        color_continuous_scale=['#EF553B', '#636EFA', '#00CC96'],
        hover_data={'total_pnl': ':.2f', 'win_rate': ':.1f'},
    )
    fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20), showlegend=False)
    st.plotly_chart(fig, width='stretch')

with col2:
    st.subheader("Session Phase")
    phase_order = ['First 15min', 'Morning', 'Midday', 'Afternoon', 'Power Hour']
    phase_stats = trades_df.groupby('session_phase').agg(
        trades=('pnl', 'count'),
        avg_pnl=('pnl', 'mean'),
        total_pnl=('pnl', 'sum'),
    ).reindex(phase_order).dropna()

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=phase_stats.index,
        y=phase_stats['avg_pnl'],
        marker_color=['#00CC96' if v > 0 else '#EF553B' for v in phase_stats['avg_pnl']],
        text=[f"n={int(t)}" for t in phase_stats['trades']],
        textposition='outside',
        hovertemplate='%{x}<br>Avg P&L: $%{y:.2f}<br>%{text}<extra></extra>',
    ))
    fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20), yaxis_title="Avg P&L ($)")
    st.plotly_chart(fig, width='stretch')

with col3:
    st.subheader("P&L Distribution")
    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=trades_df.loc[trades_df['pnl'] > 0, 'pnl'],
        name='Wins', marker_color='#00CC96', opacity=0.7,
        nbinsx=20,
    ))
    fig.add_trace(go.Histogram(
        x=trades_df.loc[trades_df['pnl'] <= 0, 'pnl'],
        name='Losses', marker_color='#EF553B', opacity=0.7,
        nbinsx=20,
    ))
    fig.update_layout(
        height=300, margin=dict(l=20, r=20, t=10, b=20),
        barmode='overlay', xaxis_title="P&L ($)", yaxis_title="Count",
    )
    st.plotly_chart(fig, width='stretch')

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 3: Trades by Hour + Hold Time vs P&L
# ═══════════════════════════════════════════════════════════════════════════════
col1, col2 = st.columns(2)

with col1:
    st.subheader("Trades by Hour")
    hourly = trades_df.groupby(['entry_hour', 'outcome']).size().unstack(fill_value=0)
    fig = go.Figure()
    if 'Win' in hourly.columns:
        fig.add_trace(go.Bar(x=hourly.index, y=hourly['Win'], name='Wins', marker_color='#00CC96'))
    if 'Loss' in hourly.columns:
        fig.add_trace(go.Bar(x=hourly.index, y=hourly['Loss'], name='Losses', marker_color='#EF553B'))
    fig.update_layout(
        height=300, margin=dict(l=20, r=20, t=10, b=20),
        barmode='stack', xaxis_title="Hour (ET)", yaxis_title="Trades",
    )
    st.plotly_chart(fig, width='stretch')

with col2:
    st.subheader("Hold Time vs P&L")
    fig = px.scatter(
        trades_df, x='hold_min', y='pnl',
        color='outcome',
        color_discrete_map={'Win': '#00CC96', 'Loss': '#EF553B'},
        hover_data=['ticker', 'exit_reason'],
        opacity=0.7,
    )
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
    fig.update_layout(
        height=300, margin=dict(l=20, r=20, t=10, b=20),
        xaxis_title="Hold Time (min)", yaxis_title="P&L ($)",
    )
    st.plotly_chart(fig, width='stretch')

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 4: MFE/MAE Scatter + Strategy Breakdown (from ML tables)
# ═══════════════════════════════════════════════════════════════════════════════
ml_df = run_query(f"""
    SELECT ticker, strategy_name, exit_reason,
           realized_pnl, mfe_pct, mae_pct,
           ROUND(time_in_position_sec / 60.0, 1) as hold_min,
           entry_rsi, entry_rvol, entry_confidence,
           CASE WHEN realized_pnl > 0 THEN 'Win' ELSE 'Loss' END as outcome
    FROM ml_trade_outcomes
    WHERE entry_ts::DATE = '{selected_date}'
      AND exit_reason IS NOT NULL
""")

if not ml_df.empty:
    col1, col2 = st.columns(2)

    with col1:
        st.subheader("MFE vs MAE (Trade Quality)")
        fig = px.scatter(
            ml_df, x='mae_pct', y='mfe_pct',
            color='outcome',
            color_discrete_map={'Win': '#00CC96', 'Loss': '#EF553B'},
            size=ml_df['realized_pnl'].abs().clip(lower=0.1),
            hover_data=['ticker', 'strategy_name', 'realized_pnl'],
            opacity=0.7,
        )
        # Diagonal = perfect exit
        max_val = max(ml_df['mae_pct'].max(), ml_df['mfe_pct'].max(), 0.01)
        fig.add_trace(go.Scatter(
            x=[0, max_val], y=[0, max_val],
            mode='lines', line=dict(dash='dash', color='gray'),
            showlegend=False, hoverinfo='skip',
        ))
        fig.update_layout(
            height=350, margin=dict(l=20, r=20, t=10, b=20),
            xaxis_title="Max Adverse Excursion %",
            yaxis_title="Max Favorable Excursion %",
        )
        st.plotly_chart(fig, width='stretch')

    with col2:
        st.subheader("Strategy Breakdown")
        strat_stats = ml_df.groupby('exit_reason').agg(
            trades=('realized_pnl', 'count'),
            total_pnl=('realized_pnl', 'sum'),
            avg_pnl=('realized_pnl', 'mean'),
            win_rate=('outcome', lambda x: (x == 'Win').mean() * 100),
        ).sort_values('total_pnl', ascending=False)

        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=strat_stats.index,
            y=strat_stats['total_pnl'],
            marker_color=['#00CC96' if v > 0 else '#EF553B' for v in strat_stats['total_pnl']],
            text=[f"{wr:.0f}% WR" for wr in strat_stats['win_rate']],
            textposition='outside',
        ))
        fig.update_layout(
            height=350, margin=dict(l=20, r=20, t=10, b=20),
            xaxis_title="Exit Strategy", yaxis_title="Total P&L ($)",
        )
        st.plotly_chart(fig, width='stretch')

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 5: Trade Log Table
# ═══════════════════════════════════════════════════════════════════════════════
st.subheader("Trade Log")
display_df = trades_df[['ticker', 'entry_ts', 'exit_ts', 'entry_price', 'exit_price',
                         'qty', 'pnl', 'exit_reason', 'session_phase', 'hold_min', 'outcome']].copy()
display_df['entry_ts'] = pd.to_datetime(display_df['entry_ts']).dt.strftime('%H:%M:%S')
display_df['exit_ts'] = pd.to_datetime(display_df['exit_ts']).dt.strftime('%H:%M:%S')
display_df = display_df.rename(columns={
    'entry_ts': 'Entry', 'exit_ts': 'Exit', 'entry_price': 'Entry $',
    'exit_price': 'Exit $', 'qty': 'Qty', 'pnl': 'P&L',
    'exit_reason': 'Exit Reason', 'session_phase': 'Session',
    'hold_min': 'Hold (min)', 'outcome': 'Result',
})

st.dataframe(
    display_df.style.map(
        lambda v: 'color: #00CC96' if v == 'Win' else 'color: #EF553B' if v == 'Loss' else '',
        subset=['Result']
    ),
    width='stretch',
    height=400,
)

# ═══════════════════════════════════════════════════════════════════════════════
# ADVANCED ANALYTICS TABS
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
tab_perf, tab_risk, tab_strat, tab_attr, tab_market, tab_compliance = st.tabs([
    "Performance Metrics", "Risk Analytics", "Strategy Attribution",
    "P&L Attribution", "Market Regime", "Compliance & Audit",
])

# ── TAB 1: Performance Metrics ───────────────────────────────────────────────
with tab_perf:
    st.subheader("Performance Metrics")

    if total_trades >= 2:
        returns = trades_df['pnl'].values
        import numpy as np

        # Sharpe (annualized)
        rf_daily = 0.045 / 252
        mean_ret = np.mean(returns)
        std_ret = np.std(returns, ddof=1) if len(returns) > 1 else 1
        sharpe = (mean_ret - rf_daily) / std_ret * np.sqrt(252) if std_ret > 0 else 0

        # Sortino (downside only)
        downside = returns[returns < 0]
        down_std = np.std(downside, ddof=1) if len(downside) > 1 else 1
        sortino = (mean_ret - rf_daily) / down_std * np.sqrt(252) if down_std > 0 else 0

        # Calmar
        cum = np.cumsum(returns)
        running_max = np.maximum.accumulate(cum)
        drawdowns = cum - running_max
        max_dd = abs(drawdowns.min()) if len(drawdowns) > 0 else 1
        annual_ret = mean_ret * 252
        calmar = annual_ret / max_dd if max_dd > 0 else 0

        # Recovery factor
        recovery = total_pnl / max_dd if max_dd > 0 else 0

        # Expected value per trade
        ev = mean_ret

        # Display metrics
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Sharpe Ratio", f"{sharpe:.2f}")
        m2.metric("Sortino Ratio", f"{sortino:.2f}")
        m3.metric("Calmar Ratio", f"{calmar:.2f}")
        m4.metric("Recovery Factor", f"{recovery:.2f}")

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Profit Factor", f"{profit_factor:.2f}")
        m6.metric("Expected Value", f"${ev:.2f}/trade")
        m7.metric("Max Drawdown", f"${max_dd:.2f}")
        m8.metric("Avg Hold Time", f"{trades_df['hold_min'].mean():.1f} min")

        # Drawdown chart
        st.subheader("Drawdown Curve")
        dd_df = pd.DataFrame({
            'Trade #': range(1, len(drawdowns) + 1),
            'Drawdown': drawdowns,
        })
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=dd_df['Trade #'], y=dd_df['Drawdown'],
            fill='tozeroy', fillcolor='rgba(239,85,59,0.2)',
            line=dict(color='#EF553B', width=1),
            hovertemplate='Trade #%{x}<br>Drawdown: $%{y:.2f}<extra></extra>',
        ))
        fig.update_layout(height=250, margin=dict(l=20, r=20, t=10, b=20),
                          yaxis_title="Drawdown ($)")
        st.plotly_chart(fig, width='stretch')

        # Rolling Sharpe (5-trade window)
        if len(returns) >= 10:
            st.subheader("Rolling Sharpe (10-trade window)")
            rolling_mean = pd.Series(returns).rolling(10).mean()
            rolling_std = pd.Series(returns).rolling(10).std()
            rolling_sharpe = (rolling_mean / rolling_std * np.sqrt(252)).fillna(0)
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=list(range(1, len(rolling_sharpe) + 1)),
                y=rolling_sharpe,
                mode='lines', line=dict(color='#636EFA', width=2),
            ))
            fig.add_hline(y=0, line_dash="dash", line_color="gray")
            fig.add_hline(y=1.0, line_dash="dot", line_color="green", annotation_text="Sharpe=1")
            fig.update_layout(height=250, margin=dict(l=20, r=20, t=10, b=20),
                              yaxis_title="Rolling Sharpe")
            st.plotly_chart(fig, width='stretch')

        # Win rate by exit reason
        st.subheader("Win Rate by Exit Strategy")
        exit_perf = trades_df.groupby('exit_reason').agg(
            trades=('pnl', 'count'),
            wins=('outcome', lambda x: (x == 'Win').sum()),
            total_pnl=('pnl', 'sum'),
            avg_pnl=('pnl', 'mean'),
        )
        exit_perf['win_rate'] = (exit_perf['wins'] / exit_perf['trades'] * 100).round(1)
        exit_perf['profit_factor'] = exit_perf.apply(
            lambda r: abs(trades_df.loc[
                (trades_df['exit_reason'] == r.name) & (trades_df['pnl'] > 0), 'pnl'
            ].sum() / trades_df.loc[
                (trades_df['exit_reason'] == r.name) & (trades_df['pnl'] <= 0), 'pnl'
            ].sum()) if trades_df.loc[
                (trades_df['exit_reason'] == r.name) & (trades_df['pnl'] <= 0), 'pnl'
            ].sum() != 0 else 0, axis=1
        ).round(2)
        st.dataframe(
            exit_perf[['trades', 'win_rate', 'total_pnl', 'avg_pnl', 'profit_factor']].sort_values('total_pnl', ascending=False),
            width='stretch',
        )
    else:
        st.info("Need at least 2 trades for performance metrics")

# ── TAB 2: Risk Analytics ────────────────────────────────────────────────────
with tab_risk:
    st.subheader("Risk Analytics")

    try:
        from analytics.risk_metrics import RiskMetricsEngine
        risk_engine = RiskMetricsEngine()
        trade_records = trades_df.to_dict('records')
        risk_m = risk_engine.compute(trade_records)

        if risk_m['trade_count'] >= 2:
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Sharpe Ratio", f"{risk_m['sharpe_ratio']:.2f}")
            r2.metric("Sortino Ratio", f"{risk_m['sortino_ratio']:.2f}")
            r3.metric("Daily VaR (95%)", f"${risk_m['daily_var_95']:.2f}")
            r4.metric("Calmar Ratio", f"{risk_m['calmar_ratio']:.2f}")

            r5, r6, r7, r8 = st.columns(4)
            r5.metric("Profit Factor", f"{risk_m['profit_factor']:.2f}")
            r6.metric("Kelly Fraction", f"{risk_m['kelly_fraction']:.2f}")
            r7.metric("Expectancy", f"${risk_m['expectancy']:.3f}")
            r8.metric("Max Consecutive Losses", risk_m['consecutive_losses_max'])

            r9, r10, r11, r12 = st.columns(4)
            r9.metric("Recovery Factor", f"{risk_m['recovery_factor']:.2f}")
            r10.metric("Avg R:R", f"{risk_m['avg_rr']:.2f}")
            r11.metric("Max Drawdown %", f"{risk_m['max_drawdown_pct']:.1f}%")
            r12.metric("Max Consecutive Wins", risk_m['consecutive_wins_max'])

            # Rolling Sharpe chart
            rolling_sharpe = risk_engine.rolling_sharpe(trade_records, window=20)
            if rolling_sharpe:
                st.subheader("Rolling Sharpe Ratio (20-trade window)")
                sharpe_df = pd.DataFrame({
                    'Trade #': list(range(20, 20 + len(rolling_sharpe))),
                    'Sharpe': rolling_sharpe,
                })
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=sharpe_df['Trade #'], y=sharpe_df['Sharpe'],
                    mode='lines', line=dict(width=2, color='#636EFA'),
                ))
                fig.add_hline(y=0, line_dash='dash', line_color='gray', opacity=0.5)
                fig.add_hline(y=1, line_dash='dot', line_color='green', opacity=0.3,
                              annotation_text='Good (1.0)')
                fig.add_hline(y=2, line_dash='dot', line_color='#00CC96', opacity=0.3,
                              annotation_text='Excellent (2.0)')
                fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                                  yaxis_title='Sharpe Ratio')
                st.plotly_chart(fig, use_container_width=True)

            # Rolling VaR chart
            rolling_var = risk_engine.rolling_var(trade_records, window=20)
            if rolling_var:
                st.subheader("Rolling Value at Risk (95%, 20-trade)")
                var_df = pd.DataFrame({
                    'Trade #': list(range(20, 20 + len(rolling_var))),
                    'VaR 95%': rolling_var,
                })
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=var_df['Trade #'], y=var_df['VaR 95%'],
                    mode='lines', fill='tozeroy',
                    line=dict(width=2, color='#EF553B'),
                    fillcolor='rgba(239, 85, 59, 0.1)',
                ))
                fig.update_layout(height=250, margin=dict(l=20, r=20, t=10, b=20),
                                  yaxis_title='VaR ($)')
                st.plotly_chart(fig, use_container_width=True)

            # Sector exposure
            try:
                from monitor.state_engine import StateEngine
                positions_data = {}
                for _, row in trades_df.iterrows():
                    t = row['ticker']
                    if t not in positions_data:
                        positions_data[t] = {
                            'quantity': row.get('qty', 1),
                            'entry_price': row.get('entry_price', 0),
                        }
                sector_exp = risk_engine.sector_exposure(positions_data)
                if sector_exp:
                    st.subheader("Sector Exposure (traded tickers)")
                    sec_df = pd.DataFrame([
                        {'Sector': s, 'Count': d['count'], 'Notional': f"${d['notional']:,.0f}",
                         'Weight': f"{d['pct']:.1f}%"}
                        for s, d in sorted(sector_exp.items(), key=lambda x: -x[1]['count'])
                    ])
                    st.dataframe(sec_df, use_container_width=True, hide_index=True)
            except Exception:
                pass
        else:
            st.info("Need at least 2 trades for risk analytics")
    except ImportError:
        st.warning("Risk analytics module not available (analytics/risk_metrics.py)")

# ── TAB 3: Strategy Attribution ──────────────────────────────────────────────
with tab_strat:
    st.subheader("Strategy Attribution")

    try:
        from analytics.attribution import StrategyAttributionEngine
        attr_engine = StrategyAttributionEngine()
        attr_result = attr_engine.from_trade_log(trades_df.to_dict('records'))

        if attr_result['strategies']:
            # KPI: best and worst strategy
            bc1, bc2 = st.columns(2)
            if attr_result.get('best_strategy'):
                best = attr_result['strategies'][attr_result['best_strategy']]
                bc1.metric(f"Best: {attr_result['best_strategy']}",
                           f"${best['pnl']:+.2f}",
                           f"{best['win_rate']:.0f}% win rate")
            if attr_result.get('worst_strategy'):
                worst = attr_result['strategies'][attr_result['worst_strategy']]
                bc2.metric(f"Worst: {attr_result['worst_strategy']}",
                           f"${worst['pnl']:+.2f}",
                           f"{worst['win_rate']:.0f}% win rate")

            # Strategy table
            strat_rows = []
            for name, stats in sorted(attr_result['strategies'].items(), key=lambda x: -x[1]['pnl']):
                strat_rows.append({
                    'Strategy': name,
                    'Trades': stats['trades'],
                    'P&L': f"${stats['pnl']:+.2f}",
                    'Win Rate': f"{stats['win_rate']:.0f}%",
                    'Avg Win': f"${stats.get('avg_win', 0):+.2f}",
                    'Avg Loss': f"${stats.get('avg_loss', 0):.2f}",
                })
            st.dataframe(pd.DataFrame(strat_rows), use_container_width=True, hide_index=True)

            # Strategy P&L bar chart
            strat_pnl = {name: stats['pnl'] for name, stats in attr_result['strategies'].items()}
            fig = go.Figure(go.Bar(
                x=list(strat_pnl.keys()),
                y=list(strat_pnl.values()),
                marker_color=['#00CC96' if v > 0 else '#EF553B' for v in strat_pnl.values()],
            ))
            fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                              yaxis_title='P&L ($)', xaxis_title='Strategy')
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No strategy attribution data available")
    except ImportError:
        st.warning("Attribution module not available (analytics/attribution.py)")

# ── TAB 4: P&L Attribution ───────────────────────────────────────────────────
with tab_attr:
    st.subheader("P&L Attribution")

    # Load SPY data for attribution
    spy_df = run_query(f"""
        SELECT event_time as ts,
               (event_payload->>'close')::FLOAT as close
        FROM event_store
        WHERE event_type = 'BarReceived'
          AND event_payload->>'ticker' = 'SPY'
          AND event_time::DATE = '{selected_date}'
        ORDER BY event_time
    """)

    if not spy_df.empty and total_trades > 0:
        import numpy as np

        attr_rows = []
        for _, trade in trades_df.iterrows():
            trade_ret = trade['pnl'] / (trade['entry_price'] * (trade['qty'] or 1)) if trade['entry_price'] and trade['qty'] else 0

            # Find SPY return over same period
            mask = (spy_df['ts'] >= trade['entry_ts']) & (spy_df['ts'] <= trade['exit_ts'])
            spy_slice = spy_df[mask]
            if len(spy_slice) >= 2:
                spy_ret = (float(spy_slice['close'].iloc[-1]) - float(spy_slice['close'].iloc[0])) / float(spy_slice['close'].iloc[0])
            else:
                spy_ret = 0

            beta_contribution = spy_ret  # beta=1.0 assumption
            alpha = trade_ret - beta_contribution

            # Factor decomposition
            exit_r = str(trade.get('exit_reason', ''))
            is_momentum = exit_r in ('SELL_TARGET', 'SELL_RSI', 'profit_target')
            is_mean_rev = exit_r in ('SELL_VWAP', 'SELL_STOP')

            factor_momentum = alpha * 0.4 if is_momentum else 0
            factor_mean_rev = alpha * 0.3 if is_mean_rev else 0
            residual = alpha - factor_momentum - factor_mean_rev

            attr_rows.append({
                'ticker': trade['ticker'],
                'pnl': trade['pnl'],
                'trade_return': round(trade_ret * 100, 3),
                'spy_return': round(spy_ret * 100, 3),
                'beta_pnl': round(beta_contribution * trade['entry_price'] * (trade['qty'] or 1), 2),
                'alpha_pnl': round(alpha * trade['entry_price'] * (trade['qty'] or 1), 2),
                'momentum_pnl': round(factor_momentum * trade['entry_price'] * (trade['qty'] or 1), 2),
                'mean_rev_pnl': round(factor_mean_rev * trade['entry_price'] * (trade['qty'] or 1), 2),
                'residual_pnl': round(residual * trade['entry_price'] * (trade['qty'] or 1), 2),
            })

        attr_df = pd.DataFrame(attr_rows)

        # Summary
        a1, a2, a3, a4 = st.columns(4)
        a1.metric("Alpha P&L", f"${attr_df['alpha_pnl'].sum():+.2f}")
        a2.metric("Beta P&L", f"${attr_df['beta_pnl'].sum():+.2f}")
        a3.metric("Momentum Factor", f"${attr_df['momentum_pnl'].sum():+.2f}")
        a4.metric("Mean Rev Factor", f"${attr_df['mean_rev_pnl'].sum():+.2f}")

        # Attribution waterfall
        st.subheader("Return Decomposition")
        components = {
            'Beta (Market)': attr_df['beta_pnl'].sum(),
            'Momentum': attr_df['momentum_pnl'].sum(),
            'Mean Reversion': attr_df['mean_rev_pnl'].sum(),
            'Residual Alpha': attr_df['residual_pnl'].sum(),
        }
        fig = go.Figure(go.Waterfall(
            x=list(components.keys()) + ['Total'],
            y=list(components.values()) + [sum(components.values())],
            measure=['relative'] * len(components) + ['total'],
            connector=dict(line=dict(color='gray', dash='dot')),
            increasing=dict(marker_color='#00CC96'),
            decreasing=dict(marker_color='#EF553B'),
            totals=dict(marker_color='#636EFA'),
        ))
        fig.update_layout(height=350, margin=dict(l=20, r=20, t=10, b=20),
                          yaxis_title="P&L ($)")
        st.plotly_chart(fig, width='stretch')

        # Alpha by ticker
        st.subheader("Alpha by Ticker")
        ticker_alpha = attr_df.groupby('ticker')['alpha_pnl'].sum().sort_values()
        fig = go.Figure(go.Bar(
            y=ticker_alpha.index, x=ticker_alpha.values, orientation='h',
            marker_color=['#00CC96' if v > 0 else '#EF553B' for v in ticker_alpha.values],
        ))
        fig.update_layout(height=max(250, len(ticker_alpha) * 20),
                          margin=dict(l=20, r=20, t=10, b=20), xaxis_title="Alpha P&L ($)")
        st.plotly_chart(fig, width='stretch')

        # Full table
        st.subheader("Trade-Level Attribution")
        st.dataframe(attr_df.style.format({
            'pnl': '${:.2f}', 'trade_return': '{:.3f}%', 'spy_return': '{:.3f}%',
            'beta_pnl': '${:.2f}', 'alpha_pnl': '${:.2f}',
            'momentum_pnl': '${:.2f}', 'mean_rev_pnl': '${:.2f}', 'residual_pnl': '${:.2f}',
        }), width='stretch', height=300)
    else:
        st.info("Need SPY bar data in event_store for P&L attribution")

# ── TAB 5: Market Regime & Data Sources ──────────────────────────────────────
with tab_market:
    st.subheader("Market Regime & Alternative Data")

    # Fear & Greed
    try:
        from data_sources.fear_greed import FearGreedSource
        fg = FearGreedSource()
        fg_data = fg.get_current()
        if fg_data:
            fg1, fg2, fg3, fg4 = st.columns(4)
            fg_val = fg_data['value']
            fg_color = ('inverse' if fg_val <= 25 else 'normal' if fg_val <= 55 else 'off')
            fg1.metric("Fear & Greed Index", f"{fg_val:.0f}", fg_data['label'],
                       delta_color=fg_color)
            fg2.metric("Previous Close", f"{fg_data['previous_close']:.0f}")
            fg3.metric("1 Week Ago", f"{fg_data['one_week_ago']:.0f}")
            fg4.metric("1 Month Ago", f"{fg_data['one_month_ago']:.0f}")

            # Gauge chart
            fig = go.Figure(go.Indicator(
                mode="gauge+number",
                value=fg_val,
                title={'text': 'Fear & Greed'},
                gauge={
                    'axis': {'range': [0, 100]},
                    'bar': {'color': '#636EFA'},
                    'steps': [
                        {'range': [0, 25], 'color': '#EF553B'},
                        {'range': [25, 45], 'color': '#FFA15A'},
                        {'range': [45, 55], 'color': '#FECB52'},
                        {'range': [55, 75], 'color': '#00CC96'},
                        {'range': [75, 100], 'color': '#00CC96'},
                    ],
                },
            ))
            fig.update_layout(height=250, margin=dict(l=30, r=30, t=50, b=10))
            st.plotly_chart(fig, use_container_width=True)
    except Exception as exc:
        st.info(f"Fear & Greed unavailable: {exc}")

    st.markdown("---")

    # FRED Macro Regime
    try:
        from data_sources.fred_macro import FREDMacroSource
        fred = FREDMacroSource()
        if fred._api_key:
            regime = fred.macro_regime()
            st.subheader("Macro Regime")
            m1, m2, m3, m4 = st.columns(4)

            regime_colors = {'risk_on': 'normal', 'neutral': 'off',
                             'risk_off': 'inverse', 'crisis': 'inverse'}
            m1.metric("Regime", regime.regime.upper(),
                      delta_color=regime_colors.get(regime.regime, 'off'))
            m2.metric("Fed Rate", f"{regime.fed_rate:.2f}%", regime.fed_trend)
            m3.metric("Yield Curve", f"{regime.yield_curve:+.2f}%", regime.yield_status)
            m4.metric("VIX", f"{regime.vix:.1f}", regime.vix_regime)
        else:
            st.info("FRED API key not set — macro regime unavailable")
    except Exception as exc:
        st.info(f"FRED unavailable: {exc}")

    st.markdown("---")

    # Data source snapshots from DB
    st.subheader("Data Source Activity (Today)")
    try:
        source_counts = run_query(f"""
            SELECT event_type, COUNT(*) as cnt,
                   MIN(event_time) as first_seen,
                   MAX(event_time) as last_seen
            FROM event_store
            WHERE event_type LIKE 'DataSource_%%'
              AND event_time::DATE = '{selected_date}'
            GROUP BY event_type
            ORDER BY cnt DESC
        """)
        if not source_counts.empty:
            st.dataframe(source_counts, use_container_width=True, hide_index=True)
        else:
            st.info("No data source snapshots yet (collector runs during trading session)")
    except Exception:
        pass

    # News/Social snapshots
    try:
        news_social = run_query(f"""
            SELECT event_type, COUNT(*) as snapshots,
                   COUNT(DISTINCT event_payload->>'ticker') as tickers
            FROM event_store
            WHERE event_type IN ('NewsDataSnapshot', 'SocialDataSnapshot')
              AND event_time::DATE = '{selected_date}'
            GROUP BY event_type
        """)
        if not news_social.empty:
            st.subheader("Benzinga / StockTwits Snapshots")
            st.dataframe(news_social, use_container_width=True, hide_index=True)
    except Exception:
        pass

    # Polygon previous close for top tickers
    try:
        from data_sources.polygon_data import PolygonSource
        from config import POLYGON_API_KEY
        if POLYGON_API_KEY:
            poly = PolygonSource()
            st.subheader("Polygon Previous Close (Top Traded)")
            top_tickers = trades_df['ticker'].value_counts().head(5).index.tolist()
            poly_rows = []
            for t in top_tickers:
                prev = poly.previous_close(t)
                if prev:
                    poly_rows.append({
                        'Ticker': t,
                        'Close': f"${prev.close:.2f}",
                        'Volume': f"{prev.volume:,}",
                        'VWAP': f"${prev.vwap:.2f}",
                        'Change': f"{prev.change_pct:+.2f}%",
                    })
            if poly_rows:
                st.dataframe(pd.DataFrame(poly_rows), use_container_width=True, hide_index=True)
    except Exception:
        pass

# ── TAB 6: Compliance & Audit ────────────────────────────────────────────────
with tab_compliance:
    st.subheader("Compliance & Audit Trail")

    # Trade audit: reconstruct signal → fill → close chain
    st.subheader("Event Chain Audit")
    audit_df = run_query(f"""
        SELECT
            event_time as ts,
            event_type,
            event_payload ->> 'ticker' as ticker,
            CASE event_type
                WHEN 'StrategySignal' THEN (event_payload ->> 'action')
                WHEN 'OrderRequested' THEN (event_payload ->> 'side') || ' ' || (event_payload ->> 'qty')
                WHEN 'FillExecuted' THEN (event_payload ->> 'side') || ' @ $' || (event_payload ->> 'fill_price')
                WHEN 'PositionOpened' THEN 'OPENED'
                WHEN 'PositionClosed' THEN 'CLOSED pnl=$' || (event_payload ->> 'pnl')
                WHEN 'RiskBlocked' THEN 'BLOCKED: ' || (event_payload ->> 'reason')
                ELSE ''
            END as detail,
            correlation_id::TEXT
        FROM event_store
        WHERE event_time::DATE = '{selected_date}'
        ORDER BY event_time DESC
        LIMIT 500
    """)

    if not audit_df.empty:
        audit_df['ts'] = pd.to_datetime(audit_df['ts']).dt.strftime('%H:%M:%S')
        st.dataframe(audit_df, width='stretch', height=400)
    else:
        st.info("No events in event_store for this date")

    # Daily compliance summary
    st.subheader("Daily Compliance Summary")
    c1, c2, c3 = st.columns(3)

    risk_blocks = run_query(f"""
        SELECT COUNT(*) as cnt FROM event_store
        WHERE event_type = 'RiskBlocked' AND event_time::DATE = '{selected_date}'
    """)
    c1.metric("Risk Blocks", int(risk_blocks['cnt'].iloc[0]) if not risk_blocks.empty else 0)

    events_count = run_query(f"""
        SELECT COUNT(*) as cnt FROM event_store
        WHERE event_time::DATE = '{selected_date}'
    """)
    c2.metric("Total Events", int(events_count['cnt'].iloc[0]) if not events_count.empty else 0)

    c3.metric("Tickers Traded", len(trades_df['ticker'].unique()))

    # Event type distribution
    st.subheader("Event Distribution")
    event_dist = run_query(f"""
        SELECT event_type, COUNT(*) as count
        FROM event_store
        WHERE event_time::DATE = '{selected_date}'
        GROUP BY event_type
        ORDER BY count DESC
    """)
    if not event_dist.empty:
        fig = px.bar(event_dist, x='event_type', y='count',
                     color='count', color_continuous_scale='Blues')
        fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20))
        st.plotly_chart(fig, width='stretch')

# ═══════════════════════════════════════════════════════════════════════════════
# LIVE SESSION MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

st.markdown("---")
st.subheader("Live Session Monitor")


@st.cache_data(ttl=10)
def fetch_live_trades(_conn_str, date_str):
    """Fetch today's closed trades from event_store."""
    try:
        conn = psycopg2.connect(_conn_str, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("""
            SELECT event_time,
                   event_payload->>'ticker' as ticker,
                   (event_payload->>'pnl')::float as pnl,
                   event_payload->>'reason' as reason
            FROM event_store
            WHERE event_type = 'PositionClosed'
              AND event_time >= %s::date
              AND event_time < %s::date + interval '1 day'
            ORDER BY event_time
        """, (date_str, date_str))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception:
        return []


live_auto_refresh = st.checkbox("Auto-refresh (10s)", value=False, key="live_auto_refresh")
if live_auto_refresh:
    import time as _time
    _time.sleep(10)
    st.rerun()

# Get today's date in ET and fetch live trades
from datetime import datetime as _datetime
from zoneinfo import ZoneInfo
_today = _datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')

try:
    live_rows = fetch_live_trades(DATABASE_URL, _today)
except Exception:
    live_rows = []

if live_rows:
    live_df = pd.DataFrame(live_rows, columns=['time', 'ticker', 'pnl', 'reason'])
    live_df['cumulative_pnl'] = live_df['pnl'].cumsum()

    live_total_pnl = live_df['pnl'].sum()
    live_n_trades = len(live_df)
    live_n_wins = (live_df['pnl'] >= 0).sum()
    live_win_rate = live_n_wins / live_n_trades * 100 if live_n_trades > 0 else 0

    # KPI cards
    lc1, lc2, lc3, lc4 = st.columns(4)
    lc1.metric("Session P&L", f"${live_total_pnl:+.2f}")
    lc2.metric("Trades", live_n_trades)
    lc3.metric("Wins", int(live_n_wins))
    lc4.metric("Win Rate", f"{live_win_rate:.0f}%")

    # Equity curve
    st.line_chart(live_df.set_index('time')['cumulative_pnl'])

    # Last 10 trades
    st.dataframe(
        live_df[['time', 'ticker', 'pnl', 'reason']].tail(10).iloc[::-1],
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("No trades yet today. Start the monitor to see live data.")

# ── Footer ───────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(f"Data: {total_trades} trades | {len(all_tickers)} tickers | {selected_date}")
