"""
Trading Dashboard — Live session monitoring + Post-session analysis.

Tab 1: Live Session — real-time P&L, positions, trades (reads JSON files, 2s refresh)
Tab 2: Post-Session Analysis — detailed trade analysis from CSV reports

Launch: streamlit run dashboards/trade_analysis_dashboard.py
"""
import os
import re
import sys
import glob
import json
import time as _time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ET = ZoneInfo('America/New_York')
PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOT_STATE = PROJECT_ROOT / 'bot_state.json'
FILL_LEDGER = PROJECT_ROOT / 'data' / 'fill_ledger.json'
SUPERVISOR = PROJECT_ROOT / 'data' / 'supervisor_status.json'

st.set_page_config(page_title="Trading Hub", layout="wide", page_icon="📊")


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


DARK_LAYOUT = dict(
    paper_bgcolor='rgba(0,0,0,0)',
    plot_bgcolor='rgba(0,0,0,0)',
    font=dict(size=11),
    xaxis=dict(gridcolor='#eee'),
    yaxis=dict(gridcolor='#eee'),
    margin=dict(l=40, r=20, t=30, b=30),
)

# ══════════════════════════════════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════════════════════════════════

tab_live, tab_post = st.tabs(["Live Session", "Post-Session Analysis"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1: LIVE SESSION — real-time from JSON files, 2s fragment refresh
# ══════════════════════════════════════════════════════════════════════════════

with tab_live:

    @st.fragment(run_every=timedelta(seconds=2))
    def live_panel():
        bs = _read_json(BOT_STATE)
        fl = _read_json(FILL_LEDGER)
        sup = _read_json(SUPERVISOR)
        now = datetime.now(ET)

        positions = bs.get('positions', {})
        trade_log = bs.get('trade_log', [])
        fl_pnl = fl.get('daily_pnl', 0)

        total_trades = len(trade_log)
        wins = sum(1 for t in trade_log if t.get('pnl', 0) > 0.01)
        losses = sum(1 for t in trade_log if t.get('pnl', 0) < -0.01)
        win_rate = (wins / total_trades * 100) if total_trades else 0
        total_pnl = sum(t.get('pnl', 0) for t in trade_log)

        avg_win = (sum(t['pnl'] for t in trade_log if t.get('pnl', 0) > 0.01) / wins) if wins else 0
        avg_loss = (sum(t['pnl'] for t in trade_log if t.get('pnl', 0) < -0.01) / losses) if losses else 0
        gross_win = sum(t['pnl'] for t in trade_log if t.get('pnl', 0) > 0)
        gross_loss = abs(sum(t['pnl'] for t in trade_log if t.get('pnl', 0) < 0))
        pf = gross_win / gross_loss if gross_loss > 0 else 0

        procs = sup.get('processes', {})
        running = sum(1 for p in procs.values() if isinstance(p, dict) and p.get('status') == 'running')

        market_open = now.weekday() < 5 and 9 <= now.hour < 16
        pnl_color = "green" if total_pnl >= 0 else "red"

        # Status bar
        st.markdown(
            f"**{now.strftime('%H:%M:%S ET')}** | "
            f"**P&L: :{'green' if total_pnl >= 0 else 'red'}[${total_pnl:+,.2f}]** | "
            f"{total_trades} trades | {len(positions)} open | "
            f"{'🟢 MARKET OPEN' if market_open else '🔴 MARKET CLOSED'} | "
            f"Engines {running}/{len(procs)}"
        )

        # KPIs
        c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
        c1.metric("Realized P&L", f"${total_pnl:+,.2f}")
        c2.metric("Ledger P&L", f"${fl_pnl:+,.2f}")
        c3.metric("Trades", f"{total_trades}", f"{wins}W / {losses}L")
        c4.metric("Win Rate", f"{win_rate:.0f}%")
        c5.metric("Profit Factor", f"{pf:.2f}x")
        c6.metric("Avg Win", f"${avg_win:+,.2f}")
        c7.metric("Avg Loss", f"${avg_loss:,.2f}")

        # Positions + Equity
        col_pos, col_eq = st.columns([2, 3])

        with col_pos:
            st.subheader("Open Positions")
            if positions:
                pos_rows = []
                for ticker, pos in positions.items():
                    lcs = pos.get('_lifecycle_state', {})
                    phase_names = {0: 'VALIDATE', 1: 'PROTECT', 2: 'BREAKEVEN',
                                   3: 'HARVEST', 4: 'RUNNER'}
                    phase = lcs.get('phase', 0)
                    pos_rows.append({
                        'Ticker': ticker,
                        'Qty': pos.get('quantity', pos.get('qty', 0)),
                        'Entry': f"${pos.get('entry_price', 0):.2f}",
                        'Stop': f"${pos.get('stop_price', 0):.2f}",
                        'Phase': f"P{phase} {phase_names.get(phase, '')}",
                        'Partial': '✓' if lcs.get('partial_done') else '',
                        'Bars': lcs.get('bars_held', 0),
                        'Strategy': (pos.get('strategy', '?') or '?').replace('pro:', '').split(':')[0],
                    })
                st.dataframe(pd.DataFrame(pos_rows), use_container_width=True,
                             hide_index=True)
            else:
                st.info("No open positions")

        with col_eq:
            st.subheader("Equity Curve")
            if trade_log:
                cum = []
                r = 0
                for t in trade_log:
                    r += t.get('pnl', 0)
                    cum.append(r)
                colors = ['#00cc66' if t.get('pnl', 0) > 0 else '#ff4444' for t in trade_log]
                fig = go.Figure()
                fig.add_trace(go.Scatter(
                    x=list(range(1, len(cum) + 1)), y=cum,
                    mode='lines+markers',
                    line=dict(width=2, color='#4488ff'),
                    marker=dict(size=4, color=colors),
                    fill='tozeroy', fillcolor='rgba(68, 136, 255, 0.06)',
                    hovertemplate='#%{x} $%{y:+,.2f}<br>%{text}',
                    text=[f"{t.get('ticker','?')} ${t.get('pnl',0):+.2f}" for t in trade_log],
                ))
                fig.add_hline(y=0, line_dash='dash', line_color='#ccc')
                fig.update_layout(**DARK_LAYOUT, height=280, showlegend=False)
                st.plotly_chart(fig, use_container_width=True, key=f"eq_{_time.time()}")

        # Recent Trades + Strategy
        col_trades, col_strat = st.columns([3, 2])

        with col_trades:
            st.subheader("Recent Trades")
            if trade_log:
                recent = trade_log[-20:][::-1]
                rows = []
                for t in recent:
                    pnl = t.get('pnl', 0)
                    rows.append({
                        'P&L': f"${pnl:+.2f}",
                        'Ticker': t.get('ticker', '?'),
                        'Qty': t.get('qty', 0),
                        'Reason': t.get('reason', '?'),
                        'Strategy': (t.get('strategy', '?') or '?').replace('pro:', '').split(':')[0],
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True,
                             hide_index=True, height=350)

        with col_strat:
            st.subheader("By Strategy")
            if trade_log:
                strat_map = {}
                for t in trade_log:
                    s = (t.get('strategy', 'unknown') or 'unknown').replace('pro:', '').split(':')[0]
                    if s not in strat_map:
                        strat_map[s] = {'Trades': 0, 'P&L': 0.0, 'Wins': 0}
                    strat_map[s]['Trades'] += 1
                    strat_map[s]['P&L'] += t.get('pnl', 0)
                    if t.get('pnl', 0) > 0.01:
                        strat_map[s]['Wins'] += 1
                sdf = pd.DataFrame([
                    {'Strategy': k, 'Trades': v['Trades'],
                     'P&L': f"${v['P&L']:+.2f}",
                     'Win%': f"{v['Wins']/v['Trades']*100:.0f}%" if v['Trades'] else '0%'}
                    for k, v in sorted(strat_map.items(), key=lambda x: -x[1]['P&L'])
                ])
                st.dataframe(sdf, use_container_width=True, hide_index=True, height=350)

        # Trade flow bar chart
        if trade_log:
            st.subheader("Trade Flow")
            pnls = [t.get('pnl', 0) for t in trade_log]
            colors = ['#00cc66' if p > 0 else '#ff4444' for p in pnls]
            fig2 = go.Figure(go.Bar(
                x=list(range(1, len(pnls) + 1)), y=pnls,
                marker_color=colors,
                hovertemplate='#%{x} %{text}<br>$%{y:+,.2f}<extra></extra>',
                text=[t.get('ticker', '?') for t in trade_log],
            ))
            fig2.add_hline(y=0, line_color='#ccc')
            fig2.update_layout(**DARK_LAYOUT, height=200, showlegend=False)
            st.plotly_chart(fig2, use_container_width=True, key=f"flow_{_time.time()}")

    live_panel()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2: POST-SESSION ANALYSIS — CSV-based detailed analysis
# ══════════════════════════════════════════════════════════════════════════════

with tab_post:

    REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'reports', 'daily_analysis')

    csv_files = sorted(glob.glob(os.path.join(REPORTS_DIR, 'trade_analysis_*.csv')), reverse=True)

    if not csv_files:
        st.error("No trade analysis reports found in reports/daily_analysis/")
        st.stop()

    # Date selector — inside the post-session tab, not sidebar
    dates = [os.path.basename(f).replace('trade_analysis_', '').replace('.csv', '') for f in csv_files]
    display_dates = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]
    selected = st.selectbox("Select report date", display_dates, index=0)
    selected_file = csv_files[display_dates.index(selected)]

    df = pd.read_csv(selected_file)

    # Coerce numeric columns — handle both V9 (Apr 23) and V10 (Apr 24+) CSV formats
    for col in ['pnl', 'optimal_pnl', 'left_on_table', 'trail_exit_pnl', 'qty',
                'entry_price', 'exit_price', 'optimal_exit_price',
                'if_held_5m', 'if_held_10m', 'if_held_15m', 'if_held_30m',
                'exit_phase', 'max_phase_reached', 'r_multiple_at_exit',
                'trail_stop', 'bars_held']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
        else:
            df[col] = 0

    # Parse hold time for filtering
    def parse_hold(h):
        h = str(h)
        if h == 'prior_day' or h == '': return -1
        if h.endswith('s'): return float(h[:-1])
        if h.endswith('m'): return float(h[:-1]) * 60
        return 0

    if 'hold_time' in df.columns:
        df['hold_secs'] = df['hold_time'].apply(parse_hold)
    else:
        df['hold_secs'] = 0

    # Column aliases: V10 CSV uses 'strategy' where V9 used 'entry_reason'
    if 'entry_reason' not in df.columns and 'strategy' in df.columns:
        df['entry_reason'] = df['strategy']
    if 'exit_reason' not in df.columns and 'exit_category' in df.columns:
        df['exit_reason'] = df['exit_category']
    if 'is_prior_day_entry' not in df.columns:
        df['is_prior_day_entry'] = False

    # ── V10 Phase Parsing ──────────────────────────────────────────────────────
    # Extract exit phase from exit_reason (e.g. STOP_LOSS_phase0 -> 0, TRAIL_STOP_phase3 -> 3)
    def extract_phase(reason):
        """Parse phase number from V10 exit_reason strings."""
        r = str(reason)
        m = re.search(r'phase(\d)', r)
        if m:
            return int(m.group(1))
        # Map non-phase exit reasons to approximate phases
        if r in ('DEAD_TRADE', 'MAX_HOLD', 'MAX_HOLD_BARS'):
            return -1  # terminal / timeout
        return -1  # unknown

    def exit_category(reason):
        """Map raw V10 exit_reason to dashboard category."""
        r = str(reason)
        if 'STOP_LOSS' in r or 'TRAIL_STOP' in r:
            return 'SELL_STOP'
        if 'VWAP' in r:
            return 'SELL_VWAP'
        if 'RSI' in r:
            return 'SELL_RSI'
        if r in ('DEAD_TRADE', 'MAX_HOLD', 'MAX_HOLD_BARS'):
            return 'SELL_TARGET'
        if 'PARTIAL' in r:
            return 'PARTIAL_SELL'
        return r  # pass-through for legacy / unmapped

    # Only parse exit_phase from exit_reason if CSV doesn't already have it
    # V10 CSVs (Apr 24+) have exit_phase pre-computed; V9 CSVs need parsing
    if 'exit_phase' not in df.columns or df['exit_phase'].max() <= 0:
        df['exit_phase'] = df['exit_reason'].apply(extract_phase)
    if 'exit_cat' not in df.columns and 'exit_category' in df.columns:
        df['exit_cat'] = df['exit_category']
    elif 'exit_cat' not in df.columns:
        df['exit_cat'] = df['exit_reason'].apply(exit_category)

    PHASE_LABELS = {
        -1: 'Unknown/Timeout',
        0: 'Phase 0 — Entry Validation',
        1: 'Phase 1 — Protection',
        2: 'Phase 2 — Breakeven',
        3: 'Phase 3 — Harvest',
        4: 'Phase 4 — Runner',
    }
    df['phase_label'] = df['exit_phase'].map(PHASE_LABELS).fillna('Unknown')

    # Compute R-multiple where possible (pnl / risk per share approximation)
    if 'risk_per_share' in df.columns:
        df['risk_per_share'] = pd.to_numeric(df['risk_per_share'], errors='coerce')
        df['r_multiple'] = df.apply(
            lambda row: row['pnl'] / (row['risk_per_share'] * row['qty'])
            if row.get('risk_per_share', 0) and row.get('qty', 0) else 0, axis=1)
    else:
        # Fallback: estimate R from entry_price * 0.5% as 1R
        df['r_multiple'] = df.apply(
            lambda row: row['pnl'] / (row['entry_price'] * 0.005 * row['qty'])
            if row['entry_price'] > 0 and row['qty'] > 0 else 0, axis=1)

    # ── Filters (compact row) ──────────────────────────────────────────────────

    st.markdown("---")
    fc1, fc2, fc3, fc4, fc5, fc6 = st.columns(6)
    strategies = ['All'] + sorted(df['entry_reason'].unique().tolist())
    selected_strategy = fc1.selectbox("Strategy", strategies)
    exit_reasons = ['All'] + sorted(df['exit_reason'].unique().tolist())
    selected_exit = fc2.selectbox("Exit Reason", exit_reasons)
    tickers = ['All'] + sorted(df['ticker'].unique().tolist())
    selected_ticker = fc3.selectbox("Ticker", tickers)
    pnl_filter = fc4.radio("P&L", ['All', 'Winners', 'Losers', 'Breakeven'])
    hold_filter = fc5.radio("Hold Time", ['All', '<10s', '<1m', '1-5m', '5-30m', '>30m'])
    prior_filter = fc6.radio("Entry Type", ['All', 'Same Day', 'Prior Day'])

    # Apply filters
    mask = pd.Series(True, index=df.index)
    if selected_strategy != 'All':
        mask &= df['entry_reason'] == selected_strategy
    if selected_exit != 'All':
        mask &= df['exit_reason'] == selected_exit
    if selected_ticker != 'All':
        mask &= df['ticker'] == selected_ticker
    if pnl_filter == 'Winners':
        mask &= df['pnl'] > 0
    elif pnl_filter == 'Losers':
        mask &= df['pnl'] < 0
    elif pnl_filter == 'Breakeven':
        mask &= df['pnl'] == 0
    if hold_filter == '<10s':
        mask &= df['hold_secs'].between(0, 10)
    elif hold_filter == '<1m':
        mask &= df['hold_secs'].between(0, 60)
    elif hold_filter == '1-5m':
        mask &= df['hold_secs'].between(60, 300)
    elif hold_filter == '5-30m':
        mask &= df['hold_secs'].between(300, 1800)
    elif hold_filter == '>30m':
        mask &= df['hold_secs'] > 1800
    if prior_filter == 'Same Day':
        mask &= df['is_prior_day_entry'] != True
    elif prior_filter == 'Prior Day':
        mask &= df['is_prior_day_entry'] == True

    df = df[mask].copy()

    if len(df) == 0:
        st.warning("No trades match the selected filters")
        st.stop()

    active_filters = []
    if selected_strategy != 'All': active_filters.append(f"strategy={selected_strategy}")
    if selected_exit != 'All': active_filters.append(f"exit={selected_exit}")
    if selected_ticker != 'All': active_filters.append(f"ticker={selected_ticker}")
    if pnl_filter != 'All': active_filters.append(pnl_filter)
    if hold_filter != 'All': active_filters.append(f"hold {hold_filter}")
    if prior_filter != 'All': active_filters.append(prior_filter)

    # ── Header ───────────────────────────────────────────────────────────────────

    filter_text = f" | Filters: {', '.join(active_filters)}" if active_filters else ""
    st.title(f"📊 Post-Session Analysis — {selected}")
    if active_filters:
        st.caption(f"Filtered: {', '.join(active_filters)} ({len(df)} trades)")

    total_pnl = df['pnl'].sum()
    total_optimal = df['optimal_pnl'].sum()
    total_left = df['left_on_table'].sum()
    total_trail = df['trail_exit_pnl'].sum()
    n_trades = len(df)
    winners = len(df[df['pnl'] > 0])
    losers = len(df[df['pnl'] < 0])
    win_rate = winners / n_trades * 100 if n_trades > 0 else 0
    gross_win = df[df['pnl'] > 0]['pnl'].sum()
    gross_loss = df[df['pnl'] < 0]['pnl'].sum()
    pf = abs(gross_win / gross_loss) if gross_loss != 0 else 0
    capture = (total_pnl / total_optimal * 100) if total_optimal > 0 else 0

    # ── KPI Row ──────────────────────────────────────────────────────────────────

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Actual P&L", f"${total_pnl:+,.2f}", delta_color="normal")
    c2.metric("Optimal P&L", f"${total_optimal:+,.2f}")
    c3.metric("Left on Table", f"${total_left:,.2f}")
    c4.metric("Trades", f"{n_trades}", f"{winners}W / {losers}L")
    c5.metric("Win Rate", f"{win_rate:.0f}%")
    c6.metric("Profit Factor", f"{pf:.2f}")

    st.markdown("---")

    # ── The Case for Change ─────────────────────────────────────────────────────

    st.header("💰 The Opportunity: Exit Strategy Improvement")

    col1, col2 = st.columns(2)

    with col1:
        # Waterfall: Actual vs Optimal vs Trail
        fig_waterfall = go.Figure(go.Waterfall(
            x=["Actual P&L", "Left on Table", "Optimal P&L"],
            y=[total_pnl, total_left, 0],
            measure=["absolute", "relative", "total"],
            text=[f"${total_pnl:+,.2f}", f"${total_left:+,.2f}", f"${total_optimal:+,.2f}"],
            textposition="outside",
            connector={"line": {"color": "rgb(63, 63, 63)"}},
            decreasing={"marker": {"color": "#ff4444"}},
            increasing={"marker": {"color": "#00cc66"}},
            totals={"marker": {"color": "#3366ff"}},
        ))
        fig_waterfall.update_layout(
            title="Actual vs Optimal P&L",
            yaxis_title="P&L ($)",
            showlegend=False, height=400,
        )
        st.plotly_chart(fig_waterfall, use_container_width=True)

    with col2:
        # Comparison: Actual vs Trail Stop vs Optimal
        scenarios = pd.DataFrame({
            'Scenario': ['Lifecycle Exits', 'Trail Stop Baseline', 'Perfect Exit (Hindsight)'],
            'P&L': [total_pnl, total_trail, total_optimal],
        })
        fig_scenarios = px.bar(scenarios, x='Scenario', y='P&L', color='Scenario',
                               color_discrete_sequence=['#3366ff', '#ffaa00', '#00cc66'],
                               text_auto='$.2f')
        fig_scenarios.update_layout(title="Exit Strategy Comparison", yaxis_title="P&L ($)",
                                     showlegend=False, height=400)
        st.plotly_chart(fig_scenarios, use_container_width=True)

    trail_vs_lifecycle = total_pnl - total_trail
    lifecycle_better = trail_vs_lifecycle > 0
    st.markdown(f"""
    > **Bottom line:** The system's entries generated **${total_optimal:+,.2f}** of potential profit today.
    > Lifecycle exits captured **${total_pnl:+,.2f}** — a **{capture:.0f}% capture rate**.
    > Compared to a simple trailing stop baseline (${total_trail:+,.2f}), the lifecycle engine
    > {"**outperformed** by" if lifecycle_better else "**underperformed** by"}
    > **${abs(trail_vs_lifecycle):,.2f}**.
    """)

    st.markdown("---")

    # ── Phase 0 Rejections ─────────────────────────────────────────────────────

    st.header("Phase 0 Rejections — Thesis Filtering")

    phase0 = df[df['exit_phase'] == 0]
    passed_phase0 = df[df['exit_phase'] > 0]

    col1, col2 = st.columns(2)

    with col1:
        phase0_count = len(phase0)
        phase0_pct = phase0_count / n_trades * 100 if n_trades > 0 else 0
        st.metric("Phase 0 Rejections", f"{phase0_count}",
                  f"{phase0_pct:.0f}% of {n_trades} trades")
        st.metric("Phase 0 Avg Loss", f"${phase0['pnl'].mean():+,.2f}" if phase0_count > 0 else "$0.00",
                  delta_color="normal")
        st.metric("Passed Validation P&L",
                  f"${passed_phase0['pnl'].sum():+,.2f}" if len(passed_phase0) > 0 else "$0.00",
                  delta_color="normal")

        if phase0_count > 0:
            avg_hold_p0 = phase0['hold_secs'].mean()
            st.metric("Avg Phase 0 Hold Time", f"{avg_hold_p0:.0f}s")

        st.markdown("""
        Phase 0 rejections are **intentional** fast exits. The entry thesis was invalidated
        quickly, and the exit engine correctly cut the position before further damage.
        Small, controlled losses here indicate proper risk management.
        """)

    with col2:
        # Phase 0 vs Passed: P&L comparison
        phase_summary = pd.DataFrame({
            'Group': ['Phase 0 Rejected', 'Passed Validation'],
            'Trades': [phase0_count, len(passed_phase0)],
            'Total P&L': [phase0['pnl'].sum(), passed_phase0['pnl'].sum()],
            'Avg P&L': [phase0['pnl'].mean() if phase0_count > 0 else 0,
                         passed_phase0['pnl'].mean() if len(passed_phase0) > 0 else 0],
        })
        fig_p0 = make_subplots(specs=[[{"secondary_y": True}]])
        fig_p0.add_trace(go.Bar(x=phase_summary['Group'], y=phase_summary['Total P&L'],
                                 name='Total P&L',
                                 marker_color=['#ff8800', '#00cc66'],
                                 text=[f"${v:+,.2f}" for v in phase_summary['Total P&L']],
                                 textposition='outside'))
        fig_p0.add_trace(go.Scatter(x=phase_summary['Group'], y=phase_summary['Trades'],
                                     name='Trade Count', mode='markers+text',
                                     text=phase_summary['Trades'].values,
                                     textposition='top center',
                                     marker=dict(size=14, color='#3366ff')),
                         secondary_y=True)
        fig_p0.update_layout(title="Phase 0 Filtering Effectiveness", height=400)
        fig_p0.update_yaxes(title_text="P&L ($)", secondary_y=False)
        fig_p0.update_yaxes(title_text="Trades", secondary_y=True)
        st.plotly_chart(fig_p0, use_container_width=True)

    # Show Phase 0 trades table
    if phase0_count > 0:
        st.subheader("Phase 0 Rejected Trades")
        p0_cols = [c for c in ['ticker', 'entry_time', 'entry_reason', 'entry_price',
                                'exit_time', 'exit_reason', 'exit_price', 'pnl',
                                'hold_time', 'if_held_5m', 'if_held_15m'] if c in df.columns]
        p0_display = phase0[p0_cols].copy().sort_values('pnl')
        st.dataframe(p0_display, use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Exit Phase Analysis ─────────────────────────────────────────────────────

    st.header("Exit Phase Analysis — Lifecycle Breakdown")

    col1, col2 = st.columns(2)

    # Phase-level aggregation (only known phases)
    phase_order = [0, 1, 2, 3, 4]
    phase_data = df[df['exit_phase'].isin(phase_order)].copy()

    with col1:
        if len(phase_data) > 0:
            phase_stats = phase_data.groupby('exit_phase').agg(
                trades=('pnl', 'count'),
                total_pnl=('pnl', 'sum'),
                avg_pnl=('pnl', 'mean'),
                optimal=('optimal_pnl', 'sum'),
            ).reindex(phase_order).fillna(0)
            phase_stats['label'] = phase_stats.index.map(
                {0: 'P0 Validation', 1: 'P1 Protection', 2: 'P2 Breakeven',
                 3: 'P3 Harvest', 4: 'P4 Runner'})

            fig_phase = px.bar(phase_stats.reset_index(), x='label', y='total_pnl',
                                color='total_pnl',
                                color_continuous_scale=['#ff4444', '#ffaa00', '#00cc66'],
                                text='trades',
                                labels={'total_pnl': 'P&L', 'label': 'Exit Phase'})
            fig_phase.update_layout(title="P&L by Exit Phase", height=400, showlegend=False)
            fig_phase.update_traces(texttemplate='%{text} trades', textposition='outside')
            st.plotly_chart(fig_phase, use_container_width=True)

            st.markdown("""
            **Expected pattern:** Phase 0/1 exits should show small controlled losses
            (proper risk management). Phase 3/4 exits should be positive
            (profit harvesting and runners).
            """)
        else:
            st.info("No phase-tagged exits found in this session.")

    with col2:
        if len(phase_data) > 0:
            # P&L distribution per phase — box plot
            fig_box = px.box(phase_data, x='phase_label', y='pnl',
                              color='phase_label',
                              color_discrete_sequence=['#ff4444', '#ff8800', '#ffaa00',
                                                        '#00cc66', '#3366ff'],
                              labels={'pnl': 'P&L ($)', 'phase_label': 'Exit Phase'})
            fig_box.update_layout(title="P&L Distribution by Phase", height=400,
                                   showlegend=False)
            st.plotly_chart(fig_box, use_container_width=True)

        # Exit category breakdown (SELL_STOP, SELL_VWAP, etc.)
        cat_stats = df.groupby('exit_cat').agg(
            trades=('pnl', 'count'),
            total_pnl=('pnl', 'sum'),
        ).sort_values('total_pnl')

        fig_cat = px.bar(cat_stats.reset_index(), x='exit_cat', y='total_pnl',
                          color='total_pnl',
                          color_continuous_scale=['#ff4444', '#ffaa00', '#00cc66'],
                          text='trades',
                          labels={'total_pnl': 'P&L', 'exit_cat': 'Exit Category'})
        fig_cat.update_layout(title="P&L by Exit Category", height=350, showlegend=False)
        fig_cat.update_traces(texttemplate='%{text} trades', textposition='outside')
        st.plotly_chart(fig_cat, use_container_width=True)

    st.markdown("---")

    # ── Problem 3: Strategy Breakdown ───────────────────────────────────────────

    st.header("📋 Problem 3: Strategy Performance")

    strat_stats = df.groupby('entry_reason').agg(
        trades=('pnl', 'count'),
        total_pnl=('pnl', 'sum'),
        win_rate=('pnl', lambda x: (x > 0).mean() * 100),
        avg_pnl=('pnl', 'mean'),
        optimal=('optimal_pnl', 'sum'),
        left=('left_on_table', 'sum'),
        trail_pnl=('trail_exit_pnl', 'sum'),
    ).sort_values('total_pnl')

    col1, col2 = st.columns(2)

    with col1:
        fig_strat = px.bar(strat_stats.reset_index(), x='entry_reason', y='total_pnl',
                            color='total_pnl',
                            color_continuous_scale=['#ff4444', '#ffaa00', '#00cc66'],
                            text='trades')
        fig_strat.update_layout(title="P&L by Strategy", height=400, showlegend=False)
        fig_strat.update_traces(texttemplate='%{text} trades', textposition='outside')
        st.plotly_chart(fig_strat, use_container_width=True)

    with col2:
        strat_display = strat_stats[['trades', 'total_pnl', 'win_rate', 'avg_pnl', 'optimal', 'trail_pnl']].copy()
        strat_display.columns = ['Trades', 'Actual P&L', 'Win %', 'Avg P&L', 'Optimal P&L', 'Trail Stop P&L']
        strat_display = strat_display.round(2)
        st.dataframe(strat_display, use_container_width=True)

    st.markdown("---")

    # ── Lifecycle Exit Value Capture ─────────────────────────────────────────────

    st.header("Lifecycle Exit Value Capture")

    col1, col2 = st.columns(2)

    with col1:
        # Scatter: Actual P&L colored by exit phase
        phase_colors = {0: '#ff4444', 1: '#ff8800', 2: '#ffaa00', 3: '#00cc66', 4: '#3366ff'}
        scatter_df = df[df['exit_phase'].isin(phase_order)].copy()
        scatter_df['phase_name'] = scatter_df['exit_phase'].map(
            {0: 'P0', 1: 'P1', 2: 'P2', 3: 'P3', 4: 'P4'})

        if len(scatter_df) > 0:
            fig_scatter = px.scatter(scatter_df, x='r_multiple', y='pnl',
                                      color='phase_name',
                                      color_discrete_map={'P0': '#ff4444', 'P1': '#ff8800',
                                                           'P2': '#ffaa00', 'P3': '#00cc66',
                                                           'P4': '#3366ff'},
                                      hover_data=['ticker', 'entry_reason', 'exit_reason'],
                                      labels={'r_multiple': 'R-Multiple', 'pnl': 'P&L ($)',
                                              'phase_name': 'Phase'})
            fig_scatter.add_hline(y=0, line_dash='dash', line_color='gray', opacity=0.5)
            fig_scatter.add_vline(x=0, line_dash='dash', line_color='gray', opacity=0.5)
            fig_scatter.update_layout(title="P&L vs R-Multiple by Exit Phase", height=500)
            st.plotly_chart(fig_scatter, use_container_width=True)

            st.markdown("""
            **Phase 3/4 trades** (green/blue) should cluster in the upper-right quadrant
            (positive R, positive P&L). **Phase 0/1** (red/orange) should be tightly clustered
            near zero with small losses.
            """)
        else:
            st.info("No phase-tagged trades to display.")

    with col2:
        # Value captured per exit category
        cat_compare = df.groupby('exit_cat').agg(
            trades=('pnl', 'count'),
            actual=('pnl', 'sum'),
            optimal=('optimal_pnl', 'sum'),
        ).sort_values('actual', ascending=False)
        cat_compare['capture_pct'] = (cat_compare['actual'] / cat_compare['optimal'] * 100).clip(-999, 999)
        cat_compare.loc[cat_compare['optimal'] == 0, 'capture_pct'] = 0

        fig_cap = go.Figure()
        fig_cap.add_trace(go.Bar(x=cat_compare.index, y=cat_compare['actual'],
                                  name='Actual P&L', marker_color='#ff8800'))
        fig_cap.add_trace(go.Bar(x=cat_compare.index, y=cat_compare['optimal'],
                                  name='Optimal P&L', marker_color='#00cc66'))
        fig_cap.update_layout(title="Value Capture by Exit Category",
                               barmode='group', height=350)
        st.plotly_chart(fig_cap, use_container_width=True)

        # Phase distribution donut
        if len(phase_data) > 0:
            phase_counts = phase_data['exit_phase'].value_counts().sort_index()
            phase_names = [PHASE_LABELS.get(p, f'Phase {p}') for p in phase_counts.index]
            fig_donut = px.pie(values=phase_counts.values, names=phase_names,
                                color_discrete_sequence=['#ff4444', '#ff8800', '#ffaa00',
                                                          '#00cc66', '#3366ff'],
                                hole=0.4)
            fig_donut.update_layout(
                title=f"Exit Phase Distribution ({len(phase_data)} trades)", height=350)
            st.plotly_chart(fig_donut, use_container_width=True)

    st.markdown("---")

    # ── What-If: Timed Exits ────────────────────────────────────────────────────

    st.header("⏱️ What If: Timed Exit Analysis")

    timed_data = {
        'Hold Duration': ['Actual', '5 min', '10 min', '15 min', '30 min', 'Trailing Stop', 'Optimal'],
        'P&L': [
            total_pnl,
            df['if_held_5m'].sum(),
            df['if_held_10m'].sum(),
            df['if_held_15m'].sum(),
            df['if_held_30m'].sum(),
            total_trail,
            total_optimal,
        ]
    }
    timed_df = pd.DataFrame(timed_data)

    fig_timed = px.bar(timed_df, x='Hold Duration', y='P&L',
                        color='P&L', color_continuous_scale=['#ff4444', '#ffaa00', '#00cc66'],
                        text_auto='$.2f')
    fig_timed.update_layout(title="P&L by Exit Strategy", height=400, showlegend=False)
    st.plotly_chart(fig_timed, use_container_width=True)

    st.markdown("---")

    # ── Detectable Signals ───────────────────────────────────────────────────────

    st.header("🔍 Can Our System Detect the Optimal Exit?")

    if 'detectable_by' not in df.columns or df['detectable_by'].isna().all():
        st.info("No detectable_by data available for this date.")
        st.stop()
    detect_counts = df['detectable_by'].dropna().str.split(' \\+ ', expand=True).stack().value_counts()
    detect_df = detect_counts.reset_index()
    detect_df.columns = ['Signal', 'Trades']
    fig_detect = px.bar(detect_df, x='Signal', y='Trades',
                         color='Trades', color_continuous_scale=['#ffaa00', '#00cc66'])
    fig_detect.update_layout(title="Detectable Exit Signals at Optimal Price", height=400, showlegend=False)
    st.plotly_chart(fig_detect, use_container_width=True)

    st.markdown("""
    The majority of optimal exits are detectable by **trailing stops** and **RSI crossovers** —
    signals the lifecycle exit engine uses for phase transitions and trail adjustments.
    """)

    st.markdown("---")

    # ── Lifecycle Efficiency ────────────────────────────────────────────────────

    st.header("Lifecycle Efficiency by Strategy")

    strat_lifecycle = df.groupby('entry_reason').agg(
        trades=('pnl', 'count'),
        avg_phase=('exit_phase', lambda x: x[x >= 0].mean() if (x >= 0).any() else 0),
        avg_r=('r_multiple', 'mean'),
        total_pnl=('pnl', 'sum'),
    ).copy()

    # Percent that passed Phase 0 (exit_phase > 0)
    strat_passed_p0 = df[df['exit_phase'] > 0].groupby('entry_reason').size()
    strat_lifecycle['passed_p0_pct'] = (strat_passed_p0 / strat_lifecycle['trades'] * 100).fillna(0)

    # Percent that reached Phase 3+
    strat_reached_p3 = df[df['exit_phase'] >= 3].groupby('entry_reason').size()
    strat_lifecycle['reached_p3_pct'] = (strat_reached_p3 / strat_lifecycle['trades'] * 100).fillna(0)

    strat_lifecycle = strat_lifecycle.sort_values('total_pnl', ascending=False)

    col1, col2 = st.columns(2)

    with col1:
        # Heatmap-style bar chart: avg phase reached per strategy
        fig_eff = go.Figure()
        fig_eff.add_trace(go.Bar(
            x=strat_lifecycle.index,
            y=strat_lifecycle['avg_phase'],
            marker_color=[f'rgb({max(0, int(255 - v * 60))}, {min(255, int(v * 60))}, 100)'
                          for v in strat_lifecycle['avg_phase']],
            text=[f"{v:.1f}" for v in strat_lifecycle['avg_phase']],
            textposition='outside',
        ))
        fig_eff.update_layout(title="Avg Exit Phase by Strategy",
                               yaxis_title="Avg Phase Reached",
                               yaxis=dict(range=[0, 5]),
                               height=400, showlegend=False)
        st.plotly_chart(fig_eff, use_container_width=True)

    with col2:
        # Stacked percent bar: Phase 0 pass rate and Phase 3+ reach rate
        fig_rates = go.Figure()
        fig_rates.add_trace(go.Bar(
            x=strat_lifecycle.index,
            y=strat_lifecycle['passed_p0_pct'],
            name='Passed Phase 0 %',
            marker_color='#ffaa00',
            text=[f"{v:.0f}%" for v in strat_lifecycle['passed_p0_pct']],
            textposition='outside',
        ))
        fig_rates.add_trace(go.Bar(
            x=strat_lifecycle.index,
            y=strat_lifecycle['reached_p3_pct'],
            name='Reached Phase 3+ %',
            marker_color='#00cc66',
            text=[f"{v:.0f}%" for v in strat_lifecycle['reached_p3_pct']],
            textposition='outside',
        ))
        fig_rates.update_layout(title="Phase Progression Rates by Strategy",
                                 yaxis_title="% of Trades",
                                 barmode='group', height=400)
        st.plotly_chart(fig_rates, use_container_width=True)

    # Summary table
    lifecycle_display = strat_lifecycle[['trades', 'avg_phase', 'avg_r', 'passed_p0_pct',
                                          'reached_p3_pct', 'total_pnl']].copy()
    lifecycle_display.columns = ['Trades', 'Avg Phase', 'Avg R-Multiple', 'Passed P0 %',
                                  'Reached P3+ %', 'Total P&L']
    lifecycle_display = lifecycle_display.round(2)
    st.dataframe(lifecycle_display, use_container_width=True)

    st.markdown("""
    **Interpretation:** Strategies with high Phase 0 pass rates and high Phase 3+ reach rates
    have strong entry quality. Strategies stuck in Phase 1/2 may need thesis refinement
    or adjusted validation criteria.
    """)

    st.markdown("---")

    # ── Full Trade Table ─────────────────────────────────────────────────────────

    st.header("📋 All Trades")

    all_display_cols = ['ticker', 'qty', 'entry_time', 'entry_reason', 'entry_price',
                        'exit_time', 'exit_reason', 'exit_cat', 'phase_label', 'r_multiple',
                        'exit_price', 'hold_time', 'pnl',
                        'optimal_exit_time', 'optimal_exit_price', 'optimal_pnl',
                        'left_on_table', 'trail_exit_pnl', 'detectable_by']
    display_cols = [c for c in all_display_cols if c in df.columns]

    sort_col = st.selectbox("Sort by", ['pnl', 'left_on_table', 'optimal_pnl', 'ticker', 'entry_time'],
                             index=0)
    ascending = st.checkbox("Ascending", value=True)

    display_df = df[display_cols].sort_values(sort_col, ascending=ascending)

    st.dataframe(display_df, use_container_width=True, hide_index=True, height=600)

    # ── Footer ───────────────────────────────────────────────────────────────────

    st.markdown("---")
    st.markdown(f"""
    **Summary:** {n_trades} trades | {winners}W/{losers}L ({win_rate:.0f}%) |
    PF {pf:.2f} | Actual ${total_pnl:+,.2f} | Optimal ${total_optimal:+,.2f} |
    Trail Stop ${total_trail:+,.2f} | Left ${total_left:+,.2f}

    *Report: {os.path.basename(selected_file)}*
    """)
