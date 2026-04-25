"""
Live Trading Dashboard — Real-time session monitoring.

Reads from JSON state files (bot_state.json, fill_ledger.json, heartbeat.json,
supervisor_status.json) — NO database queries, ZERO impact on trading.

Uses Streamlit fragments for partial 2-second refresh without full page reload.

Launch: streamlit run dashboards/live_dashboard.py --server.port 8504
"""
import os
import sys
import json
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

ET = ZoneInfo('America/New_York')
PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOT_STATE = PROJECT_ROOT / 'bot_state.json'
FILL_LEDGER = PROJECT_ROOT / 'data' / 'fill_ledger.json'
HEARTBEAT = PROJECT_ROOT / 'data' / 'heartbeat.json'
SUPERVISOR = PROJECT_ROOT / 'data' / 'supervisor_status.json'

# ── Page Config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Live Trading",
    page_icon="",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Dark Theme ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .stApp { background-color: #0e1117; }
    .metric-box {
        background: linear-gradient(145deg, #1a1a2e, #16213e);
        border: 1px solid #2a2a4a;
        border-radius: 10px;
        padding: 14px 18px;
        text-align: center;
    }
    .metric-label {
        color: #8892b0;
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 1.5px;
        font-family: 'SF Mono', monospace;
    }
    .metric-value {
        font-size: 26px;
        font-weight: 700;
        font-family: 'SF Mono', monospace;
        margin: 2px 0;
    }
    .green { color: #00e676; }
    .red { color: #ff5252; }
    .blue { color: #536dfe; }
    .yellow { color: #ffd740; }
    .gray { color: #8892b0; }
    .header-bar {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
        padding: 10px 20px;
        border-radius: 8px;
        margin-bottom: 12px;
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .header-title {
        color: #e0e0e0;
        font-size: 22px;
        font-weight: 700;
        font-family: 'SF Mono', monospace;
        letter-spacing: 2px;
    }
    .header-status {
        color: #00e676;
        font-size: 12px;
        font-family: 'SF Mono', monospace;
    }
    .section-header {
        color: #536dfe;
        font-size: 13px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 2px;
        font-family: 'SF Mono', monospace;
        border-bottom: 1px solid #2a2a4a;
        padding-bottom: 6px;
        margin: 16px 0 8px 0;
    }
    .pos-row {
        font-family: 'SF Mono', monospace;
        font-size: 12px;
        padding: 4px 0;
        border-bottom: 1px solid #1a1a2e;
    }
    .trade-row {
        font-family: 'SF Mono', monospace;
        font-size: 11px;
        padding: 3px 0;
    }
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
</style>
""", unsafe_allow_html=True)

DARK_LAYOUT = dict(
    paper_bgcolor='rgba(0,0,0,0)',
    plot_bgcolor='rgba(26,26,46,0.8)',
    font=dict(family='SF Mono, Fira Code, monospace', color='#8892b0', size=11),
    xaxis=dict(gridcolor='#2a2a4a', zerolinecolor='#2a2a4a'),
    yaxis=dict(gridcolor='#2a2a4a', zerolinecolor='#2a2a4a'),
    margin=dict(l=40, r=20, t=30, b=30),
    hoverlabel=dict(bgcolor='#1a1a2e', font_color='#e0e0e0'),
)


# ── Helper: read JSON safely ────────────────────────────────────────────────
def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _metric(label, value, color='blue'):
    st.markdown(f"""
    <div class="metric-box">
        <div class="metric-label">{label}</div>
        <div class="metric-value {color}">{value}</div>
    </div>
    """, unsafe_allow_html=True)


# ── Header (static) ─────────────────────────────────────────────────────────
now_et = datetime.now(ET)
market_open = now_et.weekday() < 5 and 9 <= now_et.hour < 16
status_text = "MARKET OPEN" if market_open else "MARKET CLOSED"
status_color = "#00e676" if market_open else "#ff5252"

st.markdown(f"""
<div class="header-bar">
    <div class="header-title">LIVE TRADING</div>
    <div class="header-status" style="color: {status_color};">
        {status_text} | {now_et.strftime('%A %b %d, %Y')}
    </div>
</div>
""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════════════
# LIVE METRICS — refreshes every 2 seconds via @st.fragment
# Reads from JSON files only — zero DB load, zero trading impact.
# ══════════════════════════════════════════════════════════════════════════════

@st.fragment(run_every=timedelta(seconds=2))
def live_metrics():
    """Real-time KPIs and positions — partial rerun every 2s."""
    bs = _read_json(BOT_STATE)
    fl = _read_json(FILL_LEDGER)
    hb = _read_json(HEARTBEAT)
    sup = _read_json(SUPERVISOR)

    positions = bs.get('positions', {})
    trade_log = bs.get('trade_log', [])
    fl_pnl = fl.get('daily_pnl', 0)
    now = datetime.now(ET)

    # Compute metrics
    total_trades = len(trade_log)
    wins = sum(1 for t in trade_log if t.get('pnl', 0) > 0.01)
    losses = sum(1 for t in trade_log if t.get('pnl', 0) < -0.01)
    breakeven = total_trades - wins - losses
    win_rate = (wins / total_trades * 100) if total_trades else 0
    total_pnl = sum(t.get('pnl', 0) for t in trade_log)

    avg_win = 0
    avg_loss = 0
    if wins:
        avg_win = sum(t['pnl'] for t in trade_log if t.get('pnl', 0) > 0.01) / wins
    if losses:
        avg_loss = sum(t['pnl'] for t in trade_log if t.get('pnl', 0) < -0.01) / losses

    gross_win = sum(t['pnl'] for t in trade_log if t.get('pnl', 0) > 0)
    gross_loss = abs(sum(t['pnl'] for t in trade_log if t.get('pnl', 0) < 0))
    pf = gross_win / gross_loss if gross_loss > 0 else 0

    # Process status
    procs = sup.get('processes', {})
    running = sum(1 for p in procs.values() if isinstance(p, dict) and p.get('status') == 'running')
    total_procs = len(procs)

    # ── Time + Status bar ────────────────────────────────────────────
    pnl_color = 'green' if total_pnl >= 0 else 'red'
    st.markdown(f"""
    <div style="display: flex; justify-content: space-between; align-items: center;
                padding: 6px 16px; background: #1a1a2e; border-radius: 6px;
                margin-bottom: 10px; font-family: 'SF Mono', monospace; font-size: 12px;">
        <span class="gray">{now.strftime('%H:%M:%S ET')}</span>
        <span class="{pnl_color}" style="font-size: 18px; font-weight: 700;">
            P&L ${total_pnl:+,.2f}
        </span>
        <span class="gray">{total_trades} trades | {len(positions)} open |
              Engines {running}/{total_procs}</span>
    </div>
    """, unsafe_allow_html=True)

    # ── KPI Row ──────────────────────────────────────────────────────
    c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)
    with c1: _metric("REALIZED P&L", f"${total_pnl:+,.2f}", pnl_color)
    with c2: _metric("LEDGER P&L", f"${fl_pnl:+,.2f}", 'green' if fl_pnl >= 0 else 'red')
    with c3: _metric("TRADES", f"{total_trades}", 'blue')
    with c4: _metric("WINS", f"{wins}", 'green')
    with c5: _metric("LOSSES", f"{losses}", 'red')
    with c6: _metric("WIN RATE", f"{win_rate:.0f}%", 'green' if win_rate >= 50 else 'red')
    with c7: _metric("PROFIT FACTOR", f"{pf:.2f}x", 'green' if pf > 1 else 'red')
    with c8: _metric("AVG W/L", f"${avg_win:.2f}/${avg_loss:.2f}", 'blue')

    # ── Open Positions + Equity Curve ────────────────────────────────
    col_pos, col_chart = st.columns([2, 3])

    with col_pos:
        st.markdown('<div class="section-header">OPEN POSITIONS</div>',
                    unsafe_allow_html=True)
        if positions:
            for ticker, pos in positions.items():
                entry = pos.get('entry_price', 0)
                stop = pos.get('stop_price', 0)
                qty = pos.get('quantity', pos.get('qty', 0))
                strategy = pos.get('strategy', '?')
                clean_strat = strategy.replace('pro:', '').split(':')[0] if strategy else '?'
                lcs = pos.get('_lifecycle_state', {})
                phase = lcs.get('phase', 0)
                partial = lcs.get('partial_done', False)
                bars = lcs.get('bars_held', 0)

                phase_names = {0: 'VALIDATE', 1: 'PROTECT', 2: 'BREAKEVEN',
                               3: 'HARVEST', 4: 'RUNNER'}
                phase_color = {0: 'red', 1: 'yellow', 2: 'blue', 3: 'green', 4: 'green'
                               }.get(phase, 'gray')
                partial_tag = ' <span class="green">PARTIAL</span>' if partial else ''

                st.markdown(
                    f'<div class="pos-row">'
                    f'<span class="blue" style="font-weight:700;">{ticker}</span> '
                    f'<span class="gray">{qty} @ ${entry:.2f}</span> '
                    f'<span class="yellow">stop ${stop:.2f}</span> '
                    f'<span class="{phase_color}">P{phase} {phase_names.get(phase, "")}</span>'
                    f'{partial_tag} '
                    f'<span class="gray">| {clean_strat} | {bars}bars</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )
        else:
            st.markdown('<span class="gray" style="font-family: SF Mono, monospace; '
                        'font-size: 12px;">No open positions</span>',
                        unsafe_allow_html=True)

    with col_chart:
        st.markdown('<div class="section-header">EQUITY CURVE</div>',
                    unsafe_allow_html=True)
        if trade_log:
            cum_pnl = []
            running_pnl = 0
            for t in trade_log:
                running_pnl += t.get('pnl', 0)
                cum_pnl.append(running_pnl)

            colors = ['#00e676' if t.get('pnl', 0) > 0 else '#ff5252'
                       for t in trade_log]

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=list(range(1, len(cum_pnl) + 1)),
                y=cum_pnl,
                mode='lines+markers',
                line=dict(width=2, color='#536dfe'),
                marker=dict(size=4, color=colors),
                fill='tozeroy',
                fillcolor='rgba(83, 109, 254, 0.06)',
                hovertemplate='#%{x} | $%{y:+,.2f}<br>%{text}',
                text=[f"{t.get('ticker','?')} ${t.get('pnl',0):+.2f}" for t in trade_log],
            ))
            fig.add_hline(y=0, line_dash='dash', line_color='#2a2a4a')
            fig.update_layout(**DARK_LAYOUT, height=250, showlegend=False)
            st.plotly_chart(fig, use_container_width=True, key=f"equity_{time.time()}")

    # ── Recent Trades + Strategy Breakdown ───────────────────────────
    col_trades, col_strat = st.columns([3, 2])

    with col_trades:
        st.markdown('<div class="section-header">RECENT TRADES</div>',
                    unsafe_allow_html=True)
        if trade_log:
            recent = trade_log[-15:][::-1]
            for t in recent:
                pnl = t.get('pnl', 0)
                color = 'green' if pnl > 0.01 else 'red' if pnl < -0.01 else 'gray'
                ticker = t.get('ticker', '?')
                reason = t.get('reason', '?')
                qty = t.get('qty', 0)
                st.markdown(f"""
                <div class="trade-row">
                    <span class="{color}" style="font-weight:600;">${pnl:+.2f}</span>
                    <span class="blue">{ticker}</span>
                    <span class="gray">{qty}sh {reason}</span>
                </div>
                """, unsafe_allow_html=True)

    with col_strat:
        st.markdown('<div class="section-header">BY STRATEGY</div>',
                    unsafe_allow_html=True)
        if trade_log:
            strat_map = {}
            for t in trade_log:
                s = t.get('strategy', 'unknown')
                s = s.replace('pro:', '').split(':')[0] if s else 'unknown'
                if s not in strat_map:
                    strat_map[s] = {'trades': 0, 'pnl': 0, 'wins': 0}
                strat_map[s]['trades'] += 1
                strat_map[s]['pnl'] += t.get('pnl', 0)
                if t.get('pnl', 0) > 0.01:
                    strat_map[s]['wins'] += 1

            sorted_strats = sorted(strat_map.items(), key=lambda x: -x[1]['pnl'])
            for name, stats in sorted_strats:
                pnl = stats['pnl']
                color = 'green' if pnl > 0 else 'red'
                wr = (stats['wins'] / stats['trades'] * 100) if stats['trades'] else 0
                st.markdown(f"""
                <div class="trade-row">
                    <span class="{color}" style="font-weight:600;">${pnl:+.2f}</span>
                    <span class="blue">{name}</span>
                    <span class="gray">{stats['trades']}t {wr:.0f}%</span>
                </div>
                """, unsafe_allow_html=True)

    # ── Trade Flow (bar chart) ───────────────────────────────────────
    if trade_log:
        st.markdown('<div class="section-header">TRADE FLOW</div>',
                    unsafe_allow_html=True)
        pnls = [t.get('pnl', 0) for t in trade_log]
        colors = ['#00e676' if p > 0 else '#ff5252' for p in pnls]
        fig2 = go.Figure(go.Bar(
            x=list(range(1, len(pnls) + 1)),
            y=pnls,
            marker_color=colors,
            hovertemplate='#%{x} %{text}<br>$%{y:+,.2f}<extra></extra>',
            text=[t.get('ticker', '?') for t in trade_log],
        ))
        fig2.add_hline(y=0, line_color='#2a2a4a')
        fig2.update_layout(**DARK_LAYOUT, height=180, showlegend=False)
        st.plotly_chart(fig2, use_container_width=True, key=f"flow_{time.time()}")


# ── Render the live fragment ─────────────────────────────────────────────────
live_metrics()
