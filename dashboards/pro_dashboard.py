"""
Trading Hub — Pro Dashboard (Dark Terminal View)

A modern, Bloomberg-terminal-inspired dashboard with dark theme,
real-time metrics, and dense information display.

Launch: streamlit run dashboards/pro_dashboard.py
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
                if _k.strip():
                    os.environ[_k.strip()] = _v

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import psycopg2
from datetime import date, datetime
from zoneinfo import ZoneInfo
import time

ET = ZoneInfo('America/New_York')

# ── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Trading Hub Pro",
    page_icon="",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Dark Theme CSS ───────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Dark background */
    .stApp {
        background-color: #0e1117;
    }

    /* Header bar */
    .header-bar {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
        padding: 12px 24px;
        border-radius: 8px;
        margin-bottom: 16px;
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .header-title {
        color: #e0e0e0;
        font-size: 24px;
        font-weight: 700;
        font-family: 'SF Mono', 'Fira Code', monospace;
        letter-spacing: 2px;
    }
    .header-status {
        color: #00e676;
        font-size: 13px;
        font-family: 'SF Mono', monospace;
    }

    /* Metric cards */
    .metric-card {
        background: linear-gradient(145deg, #1a1a2e, #16213e);
        border: 1px solid #2a2a4a;
        border-radius: 10px;
        padding: 16px 20px;
        text-align: center;
        transition: all 0.3s ease;
    }
    .metric-card:hover {
        border-color: #536dfe;
        box-shadow: 0 0 20px rgba(83, 109, 254, 0.15);
    }
    .metric-label {
        color: #8892b0;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 1.5px;
        font-family: 'SF Mono', monospace;
        margin-bottom: 4px;
    }
    .metric-value {
        font-size: 28px;
        font-weight: 700;
        font-family: 'SF Mono', monospace;
        margin: 4px 0;
    }
    .metric-delta {
        font-size: 12px;
        font-family: 'SF Mono', monospace;
    }
    .green { color: #00e676; }
    .red { color: #ff5252; }
    .blue { color: #536dfe; }
    .yellow { color: #ffd740; }
    .gray { color: #8892b0; }

    /* Section headers */
    .section-header {
        color: #536dfe;
        font-size: 14px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 2px;
        font-family: 'SF Mono', monospace;
        border-bottom: 1px solid #2a2a4a;
        padding-bottom: 8px;
        margin: 20px 0 12px 0;
    }

    /* Status pill */
    .pill {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 12px;
        font-size: 11px;
        font-weight: 600;
        font-family: 'SF Mono', monospace;
        letter-spacing: 1px;
    }
    .pill-green { background: rgba(0, 230, 118, 0.15); color: #00e676; border: 1px solid #00e676; }
    .pill-red { background: rgba(255, 82, 82, 0.15); color: #ff5252; border: 1px solid #ff5252; }
    .pill-blue { background: rgba(83, 109, 254, 0.15); color: #536dfe; border: 1px solid #536dfe; }
    .pill-yellow { background: rgba(255, 215, 64, 0.15); color: #ffd740; border: 1px solid #ffd740; }

    /* Tables */
    .dataframe { font-family: 'SF Mono', monospace !important; font-size: 12px !important; }

    /* Hide Streamlit branding */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}

    /* Tabs styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0px;
        background: #1a1a2e;
        border-radius: 8px;
        padding: 4px;
    }
    .stTabs [data-baseweb="tab"] {
        color: #8892b0;
        font-family: 'SF Mono', monospace;
        font-size: 12px;
        letter-spacing: 1px;
    }
    .stTabs [aria-selected="true"] {
        color: #536dfe;
    }
</style>
""", unsafe_allow_html=True)

# ── Dark plotly theme ────────────────────────────────────────────────────────
DARK_LAYOUT = dict(
    paper_bgcolor='rgba(0,0,0,0)',
    plot_bgcolor='rgba(26,26,46,0.8)',
    font=dict(family='SF Mono, Fira Code, monospace', color='#8892b0', size=11),
    xaxis=dict(gridcolor='#2a2a4a', zerolinecolor='#2a2a4a'),
    yaxis=dict(gridcolor='#2a2a4a', zerolinecolor='#2a2a4a'),
    margin=dict(l=40, r=20, t=30, b=30),
    hoverlabel=dict(bgcolor='#1a1a2e', font_color='#e0e0e0'),
)
ACCENT_GREEN = '#00e676'
ACCENT_RED = '#ff5252'
ACCENT_BLUE = '#536dfe'

# ── Database ─────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL",
    "postgresql://trading:trading_secret@localhost:5432/tradinghub")


@st.cache_resource
def get_engine():
    from sqlalchemy import create_engine
    return create_engine(DATABASE_URL)


@st.cache_data(ttl=30)
def run_query(query):
    engine = get_engine()
    with engine.connect() as conn:
        return pd.read_sql_query(query, conn)


def metric_card(label, value, delta=None, delta_color='green'):
    """Render a styled metric card."""
    color_class = delta_color if delta_color in ('green', 'red', 'blue', 'yellow') else 'gray'
    delta_html = f'<div class="metric-delta {color_class}">{delta}</div>' if delta else ''
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">{label}</div>
        <div class="metric-value {color_class}">{value}</div>
        {delta_html}
    </div>
    """, unsafe_allow_html=True)


def pill(text, color='blue'):
    return f'<span class="pill pill-{color}">{text}</span>'


# ── Header ───────────────────────────────────────────────────────────────────
now_et = datetime.now(ET)
market_open = 9 <= now_et.hour < 16 and now_et.weekday() < 5
status_text = "MARKET OPEN" if market_open else "MARKET CLOSED"
status_color = "#00e676" if market_open else "#ff5252"

st.markdown(f"""
<div class="header-bar">
    <div class="header-title">TRADING HUB</div>
    <div class="header-status" style="color: {status_color};">
        {status_text} | {now_et.strftime('%H:%M:%S ET')} | {now_et.strftime('%A %b %d, %Y')}
    </div>
</div>
""", unsafe_allow_html=True)

# ── Date selector (minimal) ─────────────────────────────────────────────────
available_dates = run_query("""
    SELECT DISTINCT event_time::DATE as d FROM event_store
    WHERE event_type = 'PositionClosed' ORDER BY d DESC LIMIT 14
""")
dates = available_dates['d'].tolist() if not available_dates.empty else [date.today()]
selected_date = st.selectbox("", dates, index=0, label_visibility="collapsed")

# ── Load trades ──────────────────────────────────────────────────────────────
trades_df = run_query(f"""
    SELECT
        event_time as ts,
        event_payload->>'ticker' as ticker,
        COALESCE((event_payload->>'pnl')::FLOAT, 0) as pnl,
        COALESCE(event_payload->>'reason', event_payload->>'exit_reason',
                 event_payload->>'action', 'unknown') as reason,
        COALESCE(event_payload->>'strategy', '') as strategy,
        COALESCE(event_payload->>'broker', '') as broker,
        COALESCE((event_payload->>'entry_price')::FLOAT, 0) as entry_price,
        COALESCE((event_payload->>'exit_price')::FLOAT,
                 (event_payload->>'current_price')::FLOAT, 0) as exit_price,
        COALESCE((event_payload->>'qty')::INT, 0) as qty
    FROM event_store
    WHERE event_type = 'PositionClosed'
      AND event_time::DATE = '{selected_date}'
    ORDER BY event_time
""")

if trades_df.empty:
    st.markdown(f"""
    <div style="text-align: center; padding: 60px; color: #8892b0;">
        <div style="font-size: 48px; margin-bottom: 16px;">NO TRADES</div>
        <div style="font-size: 14px; font-family: 'SF Mono', monospace;">
            No closed positions for {selected_date}
        </div>
    </div>
    """, unsafe_allow_html=True)
    st.stop()

trades_df['cumulative_pnl'] = trades_df['pnl'].cumsum()

# ── Compute metrics ──────────────────────────────────────────────────────────
total_pnl = trades_df['pnl'].sum()
total_trades = len(trades_df)
wins = (trades_df['pnl'] > 0).sum()
losses = total_trades - wins
win_rate = (wins / total_trades * 100) if total_trades else 0
avg_win = trades_df.loc[trades_df['pnl'] > 0, 'pnl'].mean() if wins else 0
avg_loss = trades_df.loc[trades_df['pnl'] <= 0, 'pnl'].mean() if losses else 0
max_dd = (trades_df['cumulative_pnl'] - trades_df['cumulative_pnl'].cummax()).min()
gross_win = trades_df.loc[trades_df['pnl'] > 0, 'pnl'].sum() if wins else 0
gross_loss = abs(trades_df.loc[trades_df['pnl'] <= 0, 'pnl'].sum()) if losses else 1
pf = gross_win / gross_loss if gross_loss > 0 else 0

# ── Risk metrics ─────────────────────────────────────────────────────────────
risk_m = {}
try:
    from analytics.risk_metrics import RiskMetricsEngine
    risk_m = RiskMetricsEngine().compute(trades_df.to_dict('records'))
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 1: Primary KPIs
# ═══════════════════════════════════════════════════════════════════════════════
c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)

pnl_color = 'green' if total_pnl >= 0 else 'red'
with c1: metric_card("DAILY P&L", f"${total_pnl:+,.2f}", None, pnl_color)
with c2: metric_card("TRADES", str(total_trades), f"{wins}W / {losses}L", 'blue')
with c3: metric_card("WIN RATE", f"{win_rate:.0f}%", None, 'green' if win_rate >= 50 else 'red')
with c4: metric_card("PROFIT FACTOR", f"{pf:.2f}x", None, 'green' if pf > 1 else 'red')
with c5: metric_card("SHARPE", f"{risk_m.get('sharpe_ratio', 0):.2f}", None, 'blue')
with c6: metric_card("MAX DD", f"${max_dd:,.2f}", None, 'red')
with c7: metric_card("AVG WIN", f"${avg_win:+,.2f}", None, 'green')
with c8: metric_card("AVG LOSS", f"${avg_loss:,.2f}", None, 'red')

st.markdown("<div style='height: 8px'></div>", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 2: Equity Curve + Market Regime
# ═══════════════════════════════════════════════════════════════════════════════
col_chart, col_regime = st.columns([4, 1])

with col_chart:
    st.markdown('<div class="section-header">EQUITY CURVE</div>', unsafe_allow_html=True)
    fig = go.Figure()

    # Positive/negative fill
    fig.add_trace(go.Scatter(
        x=list(range(1, len(trades_df) + 1)),
        y=trades_df['cumulative_pnl'],
        mode='lines',
        line=dict(width=2, color=ACCENT_BLUE),
        fill='tozeroy',
        fillcolor='rgba(83, 109, 254, 0.08)',
        hovertemplate='#%{x} | $%{y:+,.2f}<br>%{text}',
        text=trades_df.apply(lambda r: f"{r['ticker']} ${r['pnl']:+.2f}", axis=1),
    ))

    # Win/loss markers
    fig.add_trace(go.Scatter(
        x=list(range(1, len(trades_df) + 1)),
        y=trades_df['cumulative_pnl'],
        mode='markers',
        marker=dict(
            size=5,
            color=[ACCENT_GREEN if p > 0 else ACCENT_RED for p in trades_df['pnl']],
        ),
        hoverinfo='skip',
    ))

    fig.add_hline(y=0, line_dash='dash', line_color='#2a2a4a')
    fig.update_layout(**DARK_LAYOUT, height=320, showlegend=False)
    st.plotly_chart(fig, use_container_width=True)

with col_regime:
    st.markdown('<div class="section-header">MARKET</div>', unsafe_allow_html=True)

    # Fear & Greed
    try:
        from data_sources.fear_greed import FearGreedSource
        fg = FearGreedSource().get_current()
        if fg:
            fg_val = fg['value']
            fg_color = 'red' if fg_val <= 25 else 'yellow' if fg_val <= 45 else 'blue' if fg_val <= 55 else 'green'
            metric_card("FEAR & GREED", f"{fg_val:.0f}", fg['label'], fg_color)
    except Exception:
        metric_card("FEAR & GREED", "N/A", None, 'gray')

    st.markdown("<div style='height: 8px'></div>", unsafe_allow_html=True)

    # FRED macro
    try:
        from data_sources.fred_macro import FREDMacroSource
        fred = FREDMacroSource()
        if fred._api_key:
            regime = fred.macro_regime()
            r_color = {'risk_on': 'green', 'neutral': 'blue', 'risk_off': 'yellow', 'crisis': 'red'}
            metric_card("REGIME", regime.regime.upper(), f"VIX {regime.vix:.0f}", r_color.get(regime.regime, 'gray'))
        else:
            metric_card("REGIME", "N/A", "No FRED key", 'gray')
    except Exception:
        metric_card("REGIME", "N/A", None, 'gray')

    st.markdown("<div style='height: 8px'></div>", unsafe_allow_html=True)

    # Process status
    try:
        import json
        status_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'supervisor_status.json')
        if os.path.exists(status_path):
            with open(status_path) as f:
                sup = json.load(f)
            procs = sup.get('processes', {})
            running = sum(1 for p in procs.values() if p.get('status') == 'running')
            total_p = len(procs)
            p_color = 'green' if running == total_p else 'yellow' if running > 0 else 'red'
            metric_card("ENGINES", f"{running}/{total_p}", "isolated mode", p_color)
        else:
            metric_card("ENGINES", "1/1", "monolith", 'blue')
    except Exception:
        metric_card("ENGINES", "?", None, 'gray')

    st.markdown("<div style='height: 8px'></div>", unsafe_allow_html=True)

    try:
        from monitor.portfolio_risk import PortfolioRiskGate, MAX_INTRADAY_DRAWDOWN, MAX_NOTIONAL_EXPOSURE
        # Show limits as reference (gate runs in monitor process, not dashboard)
        metric_card("DRAWDOWN LIMIT", f"${MAX_INTRADAY_DRAWDOWN:,.0f}", "intraday halt", 'red')
        st.markdown("<div style='height: 6px'></div>", unsafe_allow_html=True)
        metric_card("NOTIONAL CAP", f"${MAX_NOTIONAL_EXPOSURE:,.0f}", "max exposure", 'yellow')
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 3: Ticker Heatmap + Strategy Breakdown + Risk Metrics
# ═══════════════════════════════════════════════════════════════════════════════
col_heat, col_strat, col_risk = st.columns([2, 2, 1])

with col_heat:
    st.markdown('<div class="section-header">TICKER P&L</div>', unsafe_allow_html=True)
    ticker_pnl = trades_df.groupby('ticker')['pnl'].sum().sort_values(ascending=False)

    fig = go.Figure(go.Bar(
        x=ticker_pnl.values,
        y=ticker_pnl.index,
        orientation='h',
        marker_color=[ACCENT_GREEN if v > 0 else ACCENT_RED for v in ticker_pnl.values],
        hovertemplate='%{y}: $%{x:+,.2f}<extra></extra>',
    ))
    layout = {**DARK_LAYOUT, 'height': 300}
    layout['yaxis'] = {**DARK_LAYOUT.get('yaxis', {}), 'autorange': 'reversed'}
    fig.update_layout(**layout)
    st.plotly_chart(fig, use_container_width=True)

with col_strat:
    st.markdown('<div class="section-header">STRATEGY ATTRIBUTION</div>', unsafe_allow_html=True)
    try:
        from analytics.attribution import StrategyAttributionEngine
        attr = StrategyAttributionEngine().from_trade_log(trades_df.to_dict('records'))
        if attr['strategies']:
            strat_data = []
            for name, stats in sorted(attr['strategies'].items(), key=lambda x: -x[1]['pnl']):
                pnl_val = stats['pnl']
                color = 'green' if pnl_val > 0 else 'red'
                strat_data.append({
                    'Strategy': name,
                    'Trades': stats['trades'],
                    'P&L': f"${pnl_val:+.2f}",
                    'Win%': f"{stats['win_rate']:.0f}%",
                })
            st.dataframe(pd.DataFrame(strat_data), use_container_width=True, hide_index=True, height=280)
        else:
            st.info("No strategy data")
    except Exception:
        st.info("Attribution module unavailable")

with col_risk:
    st.markdown('<div class="section-header">RISK</div>', unsafe_allow_html=True)
    if risk_m:
        metric_card("SORTINO", f"{risk_m.get('sortino_ratio', 0):.2f}", None, 'blue')
        st.markdown("<div style='height: 6px'></div>", unsafe_allow_html=True)
        metric_card("VAR 95%", f"${risk_m.get('daily_var_95', 0):.2f}", None, 'yellow')
        st.markdown("<div style='height: 6px'></div>", unsafe_allow_html=True)
        metric_card("KELLY", f"{risk_m.get('kelly_fraction', 0):.2f}", None, 'blue')
        st.markdown("<div style='height: 6px'></div>", unsafe_allow_html=True)
        metric_card("EXPECTANCY", f"${risk_m.get('expectancy', 0):.3f}", None,
                     'green' if risk_m.get('expectancy', 0) > 0 else 'red')

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 4: Trade Flow + Exit Reasons + Strategy Breakdown
# ═══════════════════════════════════════════════════════════════════════════════
col_flow, col_exit, col_strat2 = st.columns([3, 2, 2])

with col_flow:
    st.markdown('<div class="section-header">TRADE FLOW</div>', unsafe_allow_html=True)

    # Per-trade waterfall
    colors = [ACCENT_GREEN if p > 0 else ACCENT_RED for p in trades_df['pnl']]
    fig = go.Figure(go.Bar(
        x=list(range(1, len(trades_df) + 1)),
        y=trades_df['pnl'],
        marker_color=colors,
        hovertemplate='#%{x} %{text}<br>$%{y:+,.2f}<extra></extra>',
        text=trades_df['ticker'],
    ))
    fig.add_hline(y=0, line_color='#2a2a4a')
    fig.update_layout(**DARK_LAYOUT, height=250)
    st.plotly_chart(fig, use_container_width=True)

with col_exit:
    st.markdown('<div class="section-header">EXIT REASONS</div>', unsafe_allow_html=True)
    reason_pnl = trades_df.groupby('reason').agg(
        count=('pnl', 'count'),
        total_pnl=('pnl', 'sum'),
    ).sort_values('total_pnl', ascending=False)

    fig = go.Figure(go.Bar(
        x=reason_pnl['total_pnl'],
        y=reason_pnl.index,
        orientation='h',
        marker_color=[ACCENT_GREEN if v > 0 else ACCENT_RED for v in reason_pnl['total_pnl']],
        text=[f"{c} trades" for c in reason_pnl['count']],
        textposition='auto',
        textfont=dict(color='#8892b0', size=10),
    ))
    fig.update_layout(**DARK_LAYOUT, height=250)
    st.plotly_chart(fig, use_container_width=True)

with col_strat2:
    st.markdown('<div class="section-header">BY STRATEGY</div>', unsafe_allow_html=True)
    if 'strategy' in trades_df.columns and trades_df['strategy'].any():
        # Clean strategy names (strip pro: prefix)
        strat_clean = trades_df['strategy'].str.replace('pro:', '', regex=False).str.split(':').str[0]
        strat_pnl = trades_df.groupby(strat_clean).agg(
            count=('pnl', 'count'),
            total_pnl=('pnl', 'sum'),
            win_rate=('pnl', lambda x: (x > 0).mean() * 100),
        ).sort_values('total_pnl', ascending=False)
        strat_data = []
        for name, row in strat_pnl.iterrows():
            strat_data.append({
                'Strategy': name or 'unknown',
                '#': int(row['count']),
                'P&L': f"${row['total_pnl']:+.2f}",
                'Win%': f"{row['win_rate']:.0f}%",
            })
        st.dataframe(pd.DataFrame(strat_data), use_container_width=True,
                      hide_index=True, height=250)

# ═══════════════════════════════════════════════════════════════════════════════
# ROW 5: Trade Log (compact terminal view)
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown('<div class="section-header">TRADE LOG</div>', unsafe_allow_html=True)

_log_cols = ['ts', 'ticker', 'qty', 'entry_price', 'exit_price', 'pnl', 'reason', 'strategy', 'broker']
_log_cols = [c for c in _log_cols if c in trades_df.columns]
display_df = trades_df[_log_cols].copy()
_col_map = {'ts': 'Time', 'ticker': 'Ticker', 'qty': 'Qty', 'entry_price': 'Entry',
            'exit_price': 'Exit', 'pnl': 'P&L', 'reason': 'Reason',
            'strategy': 'Strategy', 'broker': 'Broker'}
display_df.columns = [_col_map.get(c, c) for c in display_df.columns]
display_df['Time'] = pd.to_datetime(display_df['Time']).dt.strftime('%H:%M:%S')
display_df['Entry'] = display_df['Entry'].apply(lambda x: f"${x:.2f}" if x else "")
display_df['Exit'] = display_df['Exit'].apply(lambda x: f"${x:.2f}" if x else "")
display_df['P&L'] = display_df['P&L'].apply(lambda x: f"${x:+.2f}")

st.dataframe(
    display_df.iloc[::-1],
    use_container_width=True,
    hide_index=True,
    height=300,
)

# ── Footer ───────────────────────────────────────────────────────────────────
st.markdown(f"""
<div style="text-align: center; padding: 16px; color: #4a4a6a; font-family: 'SF Mono', monospace; font-size: 11px;">
    TRADING HUB V10 | {total_trades} trades | {len(trades_df['ticker'].unique())} tickers | {selected_date} |
    {'LIVE' if market_open else 'CLOSED'} | Phase-Based Lifecycle Exits
</div>
""", unsafe_allow_html=True)
