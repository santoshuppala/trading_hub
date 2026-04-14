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
    return pd.read_sql(query, engine)


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

# ── Footer ───────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(f"Data: {total_trades} trades | {len(all_tickers)} tickers | {selected_date}")
