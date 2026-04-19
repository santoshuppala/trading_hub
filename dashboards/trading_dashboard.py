"""
Trading Hub V8 — Streamlit Dashboard

Launch: streamlit run dashboards/trading_dashboard.py
DB-driven (reads from TimescaleDB event_store). No in-memory monitor.
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Load .env
_env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.split('#')[0].strip()
            if '=' in _line:
                _k, _v = _line.split('=', 1)
                _v = _v.strip().strip('"').strip("'")
                if _k.strip() and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v

import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from datetime import date, datetime
import json

# ── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Trading Hub V8",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://trading:trading@localhost:5432/tradinghub",
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
st.sidebar.title("Trading Hub V8")
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

# Engine filter (V8: VWAP, Pro, Options)
engine_filter_options = ['All', 'VWAP', 'Pro', 'Options']
selected_engine = st.sidebar.selectbox("Engine", engine_filter_options, index=0)

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

ticker_filter = ""
if selected_tickers and len(selected_tickers) < len(all_tickers):
    tlist = ",".join(f"'{t}'" for t in selected_tickers)
    ticker_filter = f"AND c.event_payload->>'ticker' IN ({tlist})"

st.sidebar.markdown("---")
auto_refresh = st.sidebar.checkbox("Auto-refresh (60s)", value=False)
if auto_refresh:
    import time
    time.sleep(60)
    st.rerun()

# ── Live Status (from bot_state.json + supervisor_status.json) ───────────────
st.sidebar.markdown("---")
st.sidebar.subheader("Live Status")
try:
    project_root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(project_root, 'data', 'supervisor_status.json')) as f:
        sup = json.load(f)
    for name, proc in sup.get('processes', {}).items():
        status = proc.get('status', '?')
        icon = {'running': '🟢', 'disabled': '🔴', 'crashed': '🔴', 'stopped': '🟡'}.get(status, '⚪')
        st.sidebar.text(f"{icon} {name}: {status}")
except Exception:
    st.sidebar.text("Supervisor status unavailable")

try:
    with open(os.path.join(project_root, 'bot_state.json')) as f:
        bot = json.load(f)
    open_pos = list(bot.get('positions', {}).keys())
    st.sidebar.text(f"Open positions: {len(open_pos)}")
    if open_pos:
        st.sidebar.text(f"  {', '.join(open_pos[:10])}")
except Exception:
    pass

# ── Load Trade Data ──────────────────────────────────────────────────────────
# V8: Extract engine (vwap/pro/options) from strategy field
trades_df = run_query(f"""
    SELECT
        c.event_id as trade_id,
        COALESCE(o.event_payload->>'ticker', c.aggregate_id) as ticker,
        o.event_time as entry_ts,
        c.event_time as exit_ts,
        EXTRACT(EPOCH FROM (c.event_time - o.event_time))::INT as duration_sec,
        CASE
            WHEN EXTRACT(HOUR FROM o.event_time AT TIME ZONE 'America/New_York') * 60
                 + EXTRACT(MINUTE FROM o.event_time AT TIME ZONE 'America/New_York') < 585 THEN 'First 15min'
            WHEN EXTRACT(HOUR FROM o.event_time AT TIME ZONE 'America/New_York') * 60
                 + EXTRACT(MINUTE FROM o.event_time AT TIME ZONE 'America/New_York') < 690 THEN 'Morning'
            WHEN EXTRACT(HOUR FROM o.event_time AT TIME ZONE 'America/New_York') * 60
                 + EXTRACT(MINUTE FROM o.event_time AT TIME ZONE 'America/New_York') < 810 THEN 'Midday'
            WHEN EXTRACT(HOUR FROM o.event_time AT TIME ZONE 'America/New_York') * 60
                 + EXTRACT(MINUTE FROM o.event_time AT TIME ZONE 'America/New_York') < 930 THEN 'Afternoon'
            ELSE 'Power Hour'
        END as session_phase,
        (o.event_payload->>'entry_price')::FLOAT as entry_price,
        COALESCE((c.event_payload->>'current_price')::FLOAT,
                 (c.event_payload->>'exit_price')::FLOAT, 0) as exit_price,
        COALESCE((o.event_payload->>'qty')::INT, 1) as qty,
        COALESCE((c.event_payload->>'pnl')::FLOAT, 0) as pnl,
        COALESCE(c.event_payload->>'exit_reason', c.event_payload->>'action', 'unknown') as exit_reason,
        COALESCE(o.event_payload->>'strategy', 'unknown') as strategy,
        COALESCE(o.event_payload->>'broker', 'unknown') as broker,
        EXTRACT(HOUR FROM o.event_time AT TIME ZONE 'America/New_York')::INT as entry_hour
    FROM event_store c
    JOIN LATERAL (
        SELECT event_time, event_payload
        FROM event_store
        WHERE aggregate_id = c.aggregate_id
          AND event_type = 'PositionOpened'
        ORDER BY event_time DESC
        LIMIT 1
    ) o ON true
    WHERE c.event_type = 'PositionClosed'
      AND c.event_time::DATE = '{selected_date}'
      {ticker_filter}
    ORDER BY c.event_time
""")

if trades_df.empty:
    st.warning(f"No trades found for {selected_date}")
    st.stop()

# V8: Derive engine from strategy field
def _get_engine(strategy):
    s = str(strategy).lower()
    if s.startswith('pro:'):
        return 'Pro'
    elif 'option' in s or 'iron' in s or 'spread' in s or 'straddle' in s:
        return 'Options'
    else:
        return 'VWAP'

def _get_tier(strategy):
    s = str(strategy).lower()
    if not s.startswith('pro:'):
        return '-'
    # Map strategy name to tier
    t1 = ['sr_flip', 'trend_pullback']
    t2 = ['orb', 'gap_and_go', 'inside_bar', 'flag_pennant']
    t3 = ['momentum_ignition', 'fib_confluence', 'bollinger_squeeze', 'liquidity_sweep']
    name = s.replace('pro:', '').split(':')[0] if ':' in s else s
    if name in t1: return 'T1'
    if name in t2: return 'T2'
    if name in t3: return 'T3'
    return 'T?'

trades_df['engine'] = trades_df['strategy'].apply(_get_engine)
trades_df['tier'] = trades_df['strategy'].apply(_get_tier)
trades_df['outcome'] = trades_df['pnl'].apply(lambda x: 'Win' if x > 0 else 'Loss')
trades_df['cumulative_pnl'] = trades_df['pnl'].cumsum()
trades_df['hold_min'] = (trades_df['duration_sec'].fillna(0) / 60).round(1)

# V8: Apply engine filter
if selected_engine != 'All':
    trades_df = trades_df[trades_df['engine'] == selected_engine]
    if trades_df.empty:
        st.warning(f"No {selected_engine} trades found for {selected_date}")
        st.stop()
    trades_df['cumulative_pnl'] = trades_df['pnl'].cumsum()

# ── KPI Cards ────────────────────────────────────────────────────────────────
total_pnl = trades_df['pnl'].sum()
total_trades = len(trades_df)
wins = (trades_df['pnl'] > 0).sum()
losses = total_trades - wins
win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
avg_win = trades_df.loc[trades_df['pnl'] > 0, 'pnl'].mean() if wins > 0 else 0
avg_loss = trades_df.loc[trades_df['pnl'] <= 0, 'pnl'].mean() if losses > 0 else 0
profit_factor = abs(trades_df.loc[trades_df['pnl'] > 0, 'pnl'].sum() /
                    trades_df.loc[trades_df['pnl'] <= 0, 'pnl'].sum()) \
    if losses > 0 and trades_df.loc[trades_df['pnl'] <= 0, 'pnl'].sum() != 0 else 0
max_dd = (trades_df['cumulative_pnl'] - trades_df['cumulative_pnl'].cummax()).min()

title_suffix = f" ({selected_engine})" if selected_engine != 'All' else ""
st.title(f"Trading Dashboard — {selected_date}{title_suffix}")

k1, k2, k3, k4, k5, k6 = st.columns(6)
k1.metric("Total P&L", f"${total_pnl:+,.2f}", delta_color="normal")
k2.metric("Trades", f"{total_trades}", f"{wins}W / {losses}L")
k3.metric("Win Rate", f"{win_rate:.1f}%")
k4.metric("Avg Win", f"${avg_win:+,.2f}")
k5.metric("Avg Loss", f"${avg_loss:,.2f}")
k6.metric("Max Drawdown", f"${max_dd:,.2f}")

# V8: Per-engine P&L summary
engine_summary = trades_df.groupby('engine').agg(
    trades=('pnl', 'count'),
    pnl=('pnl', 'sum'),
    win_rate=('outcome', lambda x: (x == 'Win').mean() * 100),
).round(2)

e1, e2, e3 = st.columns(3)
for col, eng in zip([e1, e2, e3], ['VWAP', 'Pro', 'Options']):
    if eng in engine_summary.index:
        row = engine_summary.loc[eng]
        col.metric(f"{eng}", f"${row['pnl']:+,.2f}",
                   f"{int(row['trades'])} trades | {row['win_rate']:.0f}% WR")
    else:
        col.metric(f"{eng}", "$0.00", "0 trades")

st.markdown("---")

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 1: Equity Curve + P&L by Ticker
# ═══════════════════════════════════════════════════════════════════════════════
col1, col2 = st.columns([3, 2])

with col1:
    st.subheader("Equity Curve")
    fig = go.Figure()
    # V8: Color by engine
    engine_colors = {'VWAP': '#636EFA', 'Pro': '#00CC96', 'Options': '#FFA15A'}
    fig.add_trace(go.Scatter(
        x=list(range(1, len(trades_df) + 1)),
        y=trades_df['cumulative_pnl'],
        mode='lines+markers',
        line=dict(width=2, color='#00CC96'),
        marker=dict(
            size=6,
            color=[engine_colors.get(e, '#636EFA') for e in trades_df['engine']],
        ),
        hovertemplate='Trade #%{x}<br>Cum P&L: $%{y:.2f}<br>%{text}',
        text=trades_df.apply(lambda r: f"{r['ticker']} [{r['engine']}] ${r['pnl']:+.2f}", axis=1),
    ))
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
    fig.update_layout(
        height=350, margin=dict(l=20, r=20, t=10, b=20),
        xaxis_title="Trade #", yaxis_title="Cumulative P&L ($)",
        showlegend=False,
    )
    st.plotly_chart(fig, use_container_width=True)

with col2:
    st.subheader("P&L by Ticker")
    ticker_pnl = trades_df.groupby('ticker')['pnl'].sum().sort_values()
    fig = go.Figure(go.Bar(
        y=ticker_pnl.index,
        x=ticker_pnl.values,
        orientation='h',
        marker=dict(
            color=['#00CC96' if v > 0 else '#EF553B' for v in ticker_pnl.values],
        ),
        hovertemplate='%{y}: $%{x:.2f}<extra></extra>',
    ))
    fig.update_layout(
        height=350, margin=dict(l=20, r=20, t=10, b=20),
        xaxis_title="P&L ($)",
    )
    st.plotly_chart(fig, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 2: Engine Breakdown + Exit Reasons + Session Phase
# ═══════════════════════════════════════════════════════════════════════════════
col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("Engine Performance")
    if not engine_summary.empty:
        fig = go.Figure()
        fig.add_trace(go.Bar(
            x=engine_summary.index,
            y=engine_summary['pnl'],
            marker_color=[engine_colors.get(e, '#636EFA') for e in engine_summary.index],
            text=[f"{int(t)} trades<br>{wr:.0f}% WR" for t, wr in
                  zip(engine_summary['trades'], engine_summary['win_rate'])],
            textposition='outside',
        ))
        fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                          yaxis_title="P&L ($)")
        st.plotly_chart(fig, use_container_width=True)

with col2:
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
    st.plotly_chart(fig, use_container_width=True)

with col3:
    st.subheader("Session Phase")
    phase_order = ['First 15min', 'Morning', 'Midday', 'Afternoon', 'Power Hour']
    phase_stats = trades_df.groupby('session_phase').agg(
        trades=('pnl', 'count'),
        avg_pnl=('pnl', 'mean'),
    ).reindex(phase_order).dropna()
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=phase_stats.index,
        y=phase_stats['avg_pnl'],
        marker_color=['#00CC96' if v > 0 else '#EF553B' for v in phase_stats['avg_pnl']],
        text=[f"n={int(t)}" for t in phase_stats['trades']],
        textposition='outside',
    ))
    fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                      yaxis_title="Avg P&L ($)")
    st.plotly_chart(fig, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 3: Pro Strategy Tiers + P&L Distribution + Hold Time
# ═══════════════════════════════════════════════════════════════════════════════
col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("Pro Strategy Tiers")
    pro_df = trades_df[trades_df['engine'] == 'Pro']
    if not pro_df.empty:
        # Show by strategy name
        strat_name = pro_df['strategy'].apply(lambda s: s.replace('pro:', '').split(':')[0])
        strat_stats = pro_df.groupby(strat_name).agg(
            trades=('pnl', 'count'),
            pnl=('pnl', 'sum'),
            win_rate=('outcome', lambda x: (x == 'Win').mean() * 100),
        ).sort_values('pnl', ascending=False)
        fig = go.Figure(go.Bar(
            x=strat_stats.index,
            y=strat_stats['pnl'],
            marker_color=['#00CC96' if v > 0 else '#EF553B' for v in strat_stats['pnl']],
            text=[f"{int(t)} | {wr:.0f}%WR" for t, wr in
                  zip(strat_stats['trades'], strat_stats['win_rate'])],
            textposition='outside',
        ))
        fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                          yaxis_title="P&L ($)")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No Pro trades today")

with col2:
    st.subheader("P&L Distribution")
    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=trades_df.loc[trades_df['pnl'] > 0, 'pnl'],
        name='Wins', marker_color='#00CC96', opacity=0.7, nbinsx=20,
    ))
    fig.add_trace(go.Histogram(
        x=trades_df.loc[trades_df['pnl'] <= 0, 'pnl'],
        name='Losses', marker_color='#EF553B', opacity=0.7, nbinsx=20,
    ))
    fig.update_layout(
        height=300, margin=dict(l=20, r=20, t=10, b=20),
        barmode='overlay', xaxis_title="P&L ($)", yaxis_title="Count",
    )
    st.plotly_chart(fig, use_container_width=True)

with col3:
    st.subheader("Hold Time vs P&L")
    fig = px.scatter(
        trades_df, x='hold_min', y='pnl',
        color='engine',
        color_discrete_map=engine_colors,
        hover_data=['ticker', 'strategy', 'exit_reason'],
        opacity=0.7,
    )
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
    fig.update_layout(
        height=300, margin=dict(l=20, r=20, t=10, b=20),
        xaxis_title="Hold Time (min)", yaxis_title="P&L ($)",
    )
    st.plotly_chart(fig, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 4: Broker Split + Trades by Hour
# ═══════════════════════════════════════════════════════════════════════════════
col1, col2 = st.columns(2)

with col1:
    st.subheader("Broker Performance")
    broker_stats = trades_df.groupby('broker').agg(
        trades=('pnl', 'count'),
        pnl=('pnl', 'sum'),
        win_rate=('outcome', lambda x: (x == 'Win').mean() * 100),
    ).round(2)
    if not broker_stats.empty:
        fig = go.Figure(go.Bar(
            x=broker_stats.index,
            y=broker_stats['pnl'],
            marker_color=['#636EFA', '#FFA15A', '#00CC96'][:len(broker_stats)],
            text=[f"{int(t)} trades | {wr:.0f}%WR" for t, wr in
                  zip(broker_stats['trades'], broker_stats['win_rate'])],
            textposition='outside',
        ))
        fig.update_layout(height=300, margin=dict(l=20, r=20, t=10, b=20),
                          yaxis_title="P&L ($)")
        st.plotly_chart(fig, use_container_width=True)

with col2:
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
    st.plotly_chart(fig, use_container_width=True)

# ═══════════════════════════════════════════════════════════════════════════════
# ADVANCED ANALYTICS TABS
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
tab_perf, tab_risk, tab_engine, tab_log = st.tabs([
    "Performance Metrics", "Risk Analytics", "Engine Deep Dive", "Trade Log",
])

# ── TAB 1: Performance Metrics ──────────────────────────────────────────────
with tab_perf:
    if total_trades >= 2:
        returns = trades_df['pnl'].values
        rf_daily = 0.045 / 252
        mean_ret = np.mean(returns)
        std_ret = np.std(returns, ddof=1) if len(returns) > 1 else 1
        sharpe = (mean_ret - rf_daily) / std_ret * np.sqrt(252) if std_ret > 0 else 0
        downside = returns[returns < 0]
        down_std = np.std(downside, ddof=1) if len(downside) > 1 else 1
        sortino = (mean_ret - rf_daily) / down_std * np.sqrt(252) if down_std > 0 else 0
        cum = np.cumsum(returns)
        running_max = np.maximum.accumulate(cum)
        drawdowns = cum - running_max
        max_dd_val = abs(drawdowns.min()) if len(drawdowns) > 0 else 1
        calmar = (mean_ret * 252) / max_dd_val if max_dd_val > 0 else 0
        recovery = total_pnl / max_dd_val if max_dd_val > 0 else 0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Sharpe Ratio", f"{sharpe:.2f}")
        m2.metric("Sortino Ratio", f"{sortino:.2f}")
        m3.metric("Calmar Ratio", f"{calmar:.2f}")
        m4.metric("Recovery Factor", f"{recovery:.2f}")

        m5, m6, m7, m8 = st.columns(4)
        m5.metric("Profit Factor", f"{profit_factor:.2f}")
        m6.metric("Expected Value", f"${mean_ret:.2f}/trade")
        m7.metric("Max Drawdown", f"${max_dd_val:.2f}")
        m8.metric("Avg Hold Time", f"{trades_df['hold_min'].mean():.1f} min")

        # Drawdown chart
        st.subheader("Drawdown Curve")
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=list(range(1, len(drawdowns) + 1)), y=drawdowns,
            fill='tozeroy', fillcolor='rgba(239,85,59,0.2)',
            line=dict(color='#EF553B', width=1),
        ))
        fig.update_layout(height=250, margin=dict(l=20, r=20, t=10, b=20),
                          yaxis_title="Drawdown ($)")
        st.plotly_chart(fig, use_container_width=True)

        # Rolling Sharpe
        if len(returns) >= 10:
            st.subheader("Rolling Sharpe (10-trade window)")
            rolling_mean = pd.Series(returns).rolling(10).mean()
            rolling_std = pd.Series(returns).rolling(10).std()
            rolling_sharpe = (rolling_mean / rolling_std * np.sqrt(252)).fillna(0)
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=list(range(1, len(rolling_sharpe) + 1)),
                y=rolling_sharpe, mode='lines', line=dict(color='#636EFA', width=2),
            ))
            fig.add_hline(y=0, line_dash="dash", line_color="gray")
            fig.add_hline(y=1.0, line_dash="dot", line_color="green", annotation_text="Sharpe=1")
            fig.update_layout(height=250, margin=dict(l=20, r=20, t=10, b=20),
                              yaxis_title="Rolling Sharpe")
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Need at least 2 trades for performance metrics")

# ── TAB 2: Risk Analytics ───────────────────────────────────────────────────
with tab_risk:
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
            r8.metric("Max Consec Losses", risk_m['consecutive_losses_max'])
    except ImportError:
        st.info("Risk analytics module not available")
    except Exception as exc:
        st.warning(f"Risk analytics error: {exc}")

# ── TAB 3: Engine Deep Dive ─────────────────────────────────────────────────
with tab_engine:
    st.subheader("Per-Engine Equity Curves")

    fig = go.Figure()
    for eng, color in engine_colors.items():
        eng_df = trades_df[trades_df['engine'] == eng]
        if not eng_df.empty:
            eng_cum = eng_df['pnl'].cumsum()
            fig.add_trace(go.Scatter(
                x=list(range(1, len(eng_cum) + 1)),
                y=eng_cum, mode='lines',
                name=f"{eng} (${eng_df['pnl'].sum():+.2f})",
                line=dict(color=color, width=2),
            ))
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
    fig.update_layout(height=350, margin=dict(l=20, r=20, t=10, b=20),
                      yaxis_title="Cumulative P&L ($)")
    st.plotly_chart(fig, use_container_width=True)

    # Per-engine stats table
    st.subheader("Engine Comparison")
    engine_detail = trades_df.groupby('engine').agg(
        trades=('pnl', 'count'),
        total_pnl=('pnl', 'sum'),
        avg_pnl=('pnl', 'mean'),
        win_rate=('outcome', lambda x: (x == 'Win').mean() * 100),
        avg_hold=('hold_min', 'mean'),
        best_trade=('pnl', 'max'),
        worst_trade=('pnl', 'min'),
    ).round(2)
    st.dataframe(engine_detail, use_container_width=True)

    # Pro tier breakdown
    pro_trades = trades_df[trades_df['engine'] == 'Pro']
    if not pro_trades.empty:
        st.subheader("Pro Tier Breakdown")
        tier_stats = pro_trades.groupby('tier').agg(
            trades=('pnl', 'count'),
            total_pnl=('pnl', 'sum'),
            avg_pnl=('pnl', 'mean'),
            win_rate=('outcome', lambda x: (x == 'Win').mean() * 100),
        ).round(2)
        st.dataframe(tier_stats, use_container_width=True)

# ── TAB 4: Trade Log ────────────────────────────────────────────────────────
with tab_log:
    st.subheader("Trade Log")
    display_df = trades_df[['ticker', 'engine', 'strategy', 'entry_ts', 'exit_ts',
                             'entry_price', 'exit_price', 'qty', 'pnl',
                             'exit_reason', 'broker', 'hold_min', 'outcome']].copy()
    display_df['entry_ts'] = pd.to_datetime(display_df['entry_ts']).dt.strftime('%H:%M:%S')
    display_df['exit_ts'] = pd.to_datetime(display_df['exit_ts']).dt.strftime('%H:%M:%S')
    display_df = display_df.rename(columns={
        'entry_ts': 'Entry', 'exit_ts': 'Exit', 'entry_price': 'Entry $',
        'exit_price': 'Exit $', 'qty': 'Qty', 'pnl': 'P&L',
        'exit_reason': 'Reason', 'hold_min': 'Hold (min)',
        'outcome': 'Result', 'engine': 'Engine', 'strategy': 'Strategy',
        'broker': 'Broker',
    })

    st.dataframe(
        display_df.style.map(
            lambda v: 'color: #00CC96' if v == 'Win' else 'color: #EF553B' if v == 'Loss' else '',
            subset=['Result']
        ),
        use_container_width=True,
        height=500,
    )
