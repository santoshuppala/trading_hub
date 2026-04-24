"""
Trade Analysis Dashboard — Post-session analysis for strategy improvement.

Reads from reports/daily_analysis/trade_analysis_YYYYMMDD.csv
Launch: streamlit run dashboards/trade_analysis_dashboard.py
"""
import os
import sys
import glob

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

st.set_page_config(page_title="Trade Analysis", layout="wide", page_icon="📊")

# ── Load Data ────────────────────────────────────────────────────────────────

REPORTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           'reports', 'daily_analysis')

csv_files = sorted(glob.glob(os.path.join(REPORTS_DIR, 'trade_analysis_*.csv')), reverse=True)

if not csv_files:
    st.error("No trade analysis reports found in reports/daily_analysis/")
    st.stop()

# Date selector
dates = [os.path.basename(f).replace('trade_analysis_', '').replace('.csv', '') for f in csv_files]
display_dates = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates]
selected = st.sidebar.selectbox("Trading Date", display_dates, index=0)
selected_file = csv_files[display_dates.index(selected)]

df = pd.read_csv(selected_file)
df['pnl'] = pd.to_numeric(df['pnl'], errors='coerce').fillna(0)
df['optimal_pnl'] = pd.to_numeric(df['optimal_pnl'], errors='coerce').fillna(0)
df['left_on_table'] = pd.to_numeric(df['left_on_table'], errors='coerce').fillna(0)
df['trail_exit_pnl'] = pd.to_numeric(df['trail_exit_pnl'], errors='coerce').fillna(0)
df['qty'] = pd.to_numeric(df['qty'], errors='coerce').fillna(0)
df['entry_price'] = pd.to_numeric(df['entry_price'], errors='coerce').fillna(0)
df['exit_price'] = pd.to_numeric(df['exit_price'], errors='coerce').fillna(0)
df['optimal_exit_price'] = pd.to_numeric(df['optimal_exit_price'], errors='coerce').fillna(0)

for col in ['if_held_5m', 'if_held_10m', 'if_held_15m', 'if_held_30m']:
    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)

# Parse hold time for filtering
def parse_hold(h):
    h = str(h)
    if h == 'prior_day' or h == '': return -1
    if h.endswith('s'): return float(h[:-1])
    if h.endswith('m'): return float(h[:-1]) * 60
    return 0

df['hold_secs'] = df['hold_time'].apply(parse_hold)

# ── Sidebar Filters ─────────────────────────────────────────────────────────

st.sidebar.markdown("---")
st.sidebar.subheader("Filters")

# Strategy filter
strategies = ['All'] + sorted(df['entry_reason'].unique().tolist())
selected_strategy = st.sidebar.selectbox("Strategy", strategies)

# Exit reason filter
exit_reasons = ['All'] + sorted(df['exit_reason'].unique().tolist())
selected_exit = st.sidebar.selectbox("Exit Reason", exit_reasons)

# Ticker filter
tickers = ['All'] + sorted(df['ticker'].unique().tolist())
selected_ticker = st.sidebar.selectbox("Ticker", tickers)

# P&L filter
pnl_filter = st.sidebar.radio("P&L", ['All', 'Winners', 'Losers', 'Breakeven'], horizontal=True)

# Hold time filter
hold_filter = st.sidebar.radio("Hold Time", ['All', '<10s', '<1m', '1-5m', '5-30m', '>30m'], horizontal=True)

# Prior day filter
prior_filter = st.sidebar.radio("Entry Type", ['All', 'Same Day', 'Prior Day'], horizontal=True)

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
st.title(f"📊 Trade Analysis — {selected}")
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
        'Scenario': ['Current Exits', '1-ATR Trailing Stop', 'Perfect Exit (Hindsight)'],
        'P&L': [total_pnl, total_trail, total_optimal],
    })
    fig_scenarios = px.bar(scenarios, x='Scenario', y='P&L', color='Scenario',
                           color_discrete_sequence=['#ff4444', '#ffaa00', '#00cc66'],
                           text_auto='$.2f')
    fig_scenarios.update_layout(title="Exit Strategy Comparison", yaxis_title="P&L ($)",
                                 showlegend=False, height=400)
    st.plotly_chart(fig_scenarios, use_container_width=True)

st.markdown(f"""
> **Bottom line:** The system's entries generated **${total_optimal:+,.2f}** of potential profit today.
> Current exits captured **${total_pnl:+,.2f}** — a **{capture:.0f}% capture rate**.
> A simple 1-ATR trailing stop would have captured **${total_trail:+,.2f}** —
> a **${total_trail - total_pnl:+,.2f} improvement** with no change to entry logic.
""")

st.markdown("---")

# ── Problem 1: Rapid Exits ──────────────────────────────────────────────────

st.header("🚨 Problem 1: Rapid Exits (<10 seconds)")

rapid = df[df['hold_secs'].between(0, 10)]
non_rapid = df[df['hold_secs'] > 10]

col1, col2 = st.columns(2)

with col1:
    st.metric("Rapid Trades (<10s)", f"{len(rapid)}", f"of {n_trades} total ({len(rapid)/n_trades*100:.0f}%)")
    st.metric("Rapid P&L", f"${rapid['pnl'].sum():+,.2f}", delta_color="normal")
    st.metric("Non-Rapid P&L", f"${non_rapid['pnl'].sum():+,.2f}", delta_color="normal")

with col2:
    hold_bins = pd.cut(df[df['hold_secs'] >= 0]['hold_secs'],
                       bins=[0, 10, 30, 60, 300, 600, 99999],
                       labels=['<10s', '10-30s', '30s-1m', '1-5m', '5-10m', '>10m'])
    hold_pnl = df[df['hold_secs'] >= 0].groupby(hold_bins, observed=True)['pnl'].agg(['sum', 'count'])
    hold_pnl.columns = ['P&L', 'Trades']

    fig_hold = make_subplots(specs=[[{"secondary_y": True}]])
    fig_hold.add_trace(go.Bar(x=hold_pnl.index.astype(str), y=hold_pnl['P&L'], name='P&L',
                              marker_color=['#ff4444' if x < 0 else '#00cc66' for x in hold_pnl['P&L']]))
    fig_hold.add_trace(go.Scatter(x=hold_pnl.index.astype(str), y=hold_pnl['Trades'],
                                   name='Trade Count', mode='markers+text',
                                   text=hold_pnl['Trades'].values, textposition='top center',
                                   marker=dict(size=12, color='#3366ff')), secondary_y=True)
    fig_hold.update_layout(title="P&L by Hold Time", height=400)
    fig_hold.update_yaxes(title_text="P&L ($)", secondary_y=False)
    fig_hold.update_yaxes(title_text="Trades", secondary_y=True)
    st.plotly_chart(fig_hold, use_container_width=True)

# Show rapid trades table
if len(rapid) > 0:
    st.subheader("Rapid Exit Trades")
    rapid_display = rapid[['ticker', 'entry_time', 'entry_reason', 'entry_price',
                            'exit_time', 'exit_reason', 'exit_price', 'pnl',
                            'optimal_exit_time', 'optimal_exit_price', 'optimal_pnl',
                            'if_held_5m', 'if_held_15m']].copy()
    rapid_display = rapid_display.sort_values('pnl')
    st.dataframe(rapid_display, use_container_width=True, hide_index=True)

st.markdown("---")

# ── Problem 2: Exit Reason Breakdown ────────────────────────────────────────

st.header("📉 Problem 2: Exit Triggers Destroying Value")

col1, col2 = st.columns(2)

with col1:
    reason_stats = df.groupby('exit_reason').agg(
        trades=('pnl', 'count'),
        total_pnl=('pnl', 'sum'),
        avg_pnl=('pnl', 'mean'),
        optimal=('optimal_pnl', 'sum'),
    ).sort_values('total_pnl')

    fig_reason = px.bar(reason_stats.reset_index(), x='exit_reason', y='total_pnl',
                        color='total_pnl',
                        color_continuous_scale=['#ff4444', '#ffaa00', '#00cc66'],
                        text='trades', labels={'total_pnl': 'P&L', 'exit_reason': 'Exit Reason'})
    fig_reason.update_layout(title="P&L by Exit Reason", height=400, showlegend=False)
    fig_reason.update_traces(texttemplate='%{text} trades', textposition='outside')
    st.plotly_chart(fig_reason, use_container_width=True)

with col2:
    # Show what each exit reason COULD have made
    reason_compare = reason_stats[['total_pnl', 'optimal']].copy()
    reason_compare.columns = ['Actual', 'Optimal']
    fig_compare = go.Figure()
    fig_compare.add_trace(go.Bar(x=reason_compare.index, y=reason_compare['Actual'],
                                  name='Actual P&L', marker_color='#ff4444'))
    fig_compare.add_trace(go.Bar(x=reason_compare.index, y=reason_compare['Optimal'],
                                  name='Optimal P&L', marker_color='#00cc66'))
    fig_compare.update_layout(title="Actual vs Optimal by Exit Reason",
                               barmode='group', height=400)
    st.plotly_chart(fig_compare, use_container_width=True)

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

# ── The Solution: Trailing Stop Impact ───────────────────────────────────────

st.header("✅ Proposed Fix: 1-ATR Trailing Stop")

col1, col2 = st.columns(2)

with col1:
    # Scatter: Actual P&L vs Trail Stop P&L per trade
    df_with_trail = df[df['trail_exit_pnl'] != 0].copy()
    fig_scatter = px.scatter(df_with_trail, x='pnl', y='trail_exit_pnl',
                              color='exit_reason', hover_data=['ticker', 'entry_time', 'exit_time'],
                              labels={'pnl': 'Actual P&L', 'trail_exit_pnl': 'Trail Stop P&L'})
    # Add diagonal line (break-even)
    pnl_range = max(abs(df_with_trail['pnl'].max()), abs(df_with_trail['trail_exit_pnl'].max()), 10)
    fig_scatter.add_trace(go.Scatter(x=[-pnl_range, pnl_range], y=[-pnl_range, pnl_range],
                                      mode='lines', line=dict(dash='dash', color='gray'),
                                      name='Break-even', showlegend=False))
    fig_scatter.update_layout(title="Trade-by-Trade: Actual vs Trailing Stop",
                               height=500)
    st.plotly_chart(fig_scatter, use_container_width=True)

    st.markdown("""
    **Points above the diagonal** = trailing stop would have been better.
    Most trades cluster above the line — trailing stop improves the majority.
    """)

with col2:
    # How many trades improve vs worsen with trailing stop
    df_compare = df[df['trail_exit_pnl'] != 0].copy()
    df_compare['improvement'] = df_compare['trail_exit_pnl'] - df_compare['pnl']
    improved = len(df_compare[df_compare['improvement'] > 0])
    worsened = len(df_compare[df_compare['improvement'] < 0])
    same = len(df_compare[df_compare['improvement'] == 0])

    fig_donut = px.pie(values=[improved, worsened, same],
                        names=['Improved', 'Worsened', 'Same'],
                        color_discrete_sequence=['#00cc66', '#ff4444', '#cccccc'],
                        hole=0.4)
    fig_donut.update_layout(title=f"Trail Stop Impact: {improved} improved, {worsened} worsened",
                             height=350)
    st.plotly_chart(fig_donut, use_container_width=True)

    # Impact summary
    total_improvement = df_compare['improvement'].sum()
    st.metric("Net Improvement", f"${total_improvement:+,.2f}",
              f"{improved} of {len(df_compare)} trades improved")

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

detect_counts = df['detectable_by'].str.split(' \\+ ', expand=True).stack().value_counts()
detect_df = detect_counts.reset_index()
detect_df.columns = ['Signal', 'Trades']
fig_detect = px.bar(detect_df, x='Signal', y='Trades',
                     color='Trades', color_continuous_scale=['#ffaa00', '#00cc66'])
fig_detect.update_layout(title="Detectable Exit Signals at Optimal Price", height=400, showlegend=False)
st.plotly_chart(fig_detect, use_container_width=True)

st.markdown("""
The majority of optimal exits are detectable by **trailing stops** and **RSI crossovers** —
signals our system already has. The issue is these signals are currently overridden by
premature VWAP breakdown and same-bar RSI exits.
""")

st.markdown("---")

# ── Full Trade Table ─────────────────────────────────────────────────────────

st.header("📋 All Trades")

display_cols = ['ticker', 'qty', 'entry_time', 'entry_reason', 'entry_price',
                'exit_time', 'exit_reason', 'exit_price', 'hold_time', 'pnl',
                'optimal_exit_time', 'optimal_exit_price', 'optimal_pnl',
                'left_on_table', 'trail_exit_pnl', 'detectable_by']

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
