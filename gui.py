import streamlit as st
import pandas as pd
from main import optimize_strategy, run_backtest, run_batch_backtest, run_compounding_backtest, run_batch_compounding_backtest, optimize_then_compound, RealTimeMonitor, POPULAR_TICKERS
import threading

st.title("Trading Bot Backtest & Real-Time Monitor")

# Backtesting section
st.header("Backtesting")

ticker = st.text_input("Enter Ticker Symbol", value="QQQ")
interval = st.selectbox("Time interval", ['1d', '5m', '30m', '1h'])
period = st.selectbox("Data period for intraday", ['30d', '60d', '90d'])
strategy = st.selectbox(
    "Strategy",
    [
        'EMA + RSI Crossover',
        'Trend Following ATR',
        'Momentum Breakout',
        'Mean Reversion',
        'Confirmed Crossover',
    ],
)
strategy_name_map = {
    'EMA + RSI Crossover': 'ema_rsi',
    'Trend Following ATR': 'trend_atr',
    'Momentum Breakout': 'momentum_breakout',
    'Mean Reversion': 'mean_reversion',
    'Confirmed Crossover': 'confirmed_crossover',
}
strategy_name = strategy_name_map[strategy]

strategy_params = {}
with st.expander("Strategy parameters", expanded=True):
    open_cost = st.number_input("Open Cost (% of trade)", min_value=0.0, max_value=1.0, value=0.1, step=0.01)
    close_cost = st.number_input("Close Cost (% of trade)", min_value=0.0, max_value=1.0, value=0.1, step=0.01)

    if strategy_name == 'ema_rsi':
        fast_period = st.number_input("Fast EMA period", value=9, min_value=1, max_value=100, step=1)
        slow_period = st.number_input("Slow EMA period", value=21, min_value=2, max_value=200, step=1)
        rsi_overbought = st.number_input("RSI overbought threshold", value=70, min_value=1, max_value=99, step=1)
        stop_loss_pct = st.number_input("Stop loss (%)", value=5.0, min_value=0.0, max_value=20.0, step=0.5)
        strategy_params = {
            'fast_period': fast_period,
            'slow_period': slow_period,
            'rsi_overbought': rsi_overbought,
            'stop_loss': stop_loss_pct / 100.0,
        }
    elif strategy_name == 'trend_atr':
        fast_period = st.number_input("Fast EMA period", value=9, min_value=1, max_value=100, step=1)
        slow_period = st.number_input("Slow EMA period", value=21, min_value=2, max_value=200, step=1)
        atr_period = st.number_input("ATR period", value=14, min_value=1, max_value=50, step=1)
        atr_multiplier = st.number_input("ATR multiplier", value=2.0, min_value=0.5, max_value=5.0, step=0.1)
        rsi_overbought = st.number_input("RSI overbought threshold", value=70, min_value=1, max_value=99, step=1)
        stop_loss_pct = st.number_input("Stop loss (%)", value=5.0, min_value=0.0, max_value=20.0, step=0.5)
        strategy_params = {
            'fast_period': fast_period,
            'slow_period': slow_period,
            'atr_period': atr_period,
            'atr_multiplier': atr_multiplier,
            'rsi_overbought': rsi_overbought,
            'stop_loss': stop_loss_pct / 100.0,
        }
    elif strategy_name == 'momentum_breakout':
        breakout_period = st.number_input("Breakout lookback (bars)", value=20, min_value=5, max_value=100, step=1)
        atr_period = st.number_input("ATR period", value=14, min_value=1, max_value=50, step=1)
        atr_multiplier = st.number_input("ATR multiplier", value=2.0, min_value=0.5, max_value=5.0, step=0.1)
        rsi_overbought = st.number_input("RSI overbought threshold", value=70, min_value=1, max_value=99, step=1)
        strategy_params = {
            'breakout_period': breakout_period,
            'atr_period': atr_period,
            'atr_multiplier': atr_multiplier,
            'rsi_overbought': rsi_overbought,
        }
    elif strategy_name == 'confirmed_crossover':
        fast_period = st.number_input("Fast EMA period", value=9, min_value=1, max_value=100, step=1)
        slow_period = st.number_input("Slow EMA period", value=21, min_value=2, max_value=200, step=1)
        rsi_momentum_low = st.number_input("RSI momentum low", value=45, min_value=1, max_value=60, step=1)
        rsi_momentum_high = st.number_input("RSI momentum high", value=65, min_value=50, max_value=90, step=1)
        atr_period = st.number_input("ATR period", value=14, min_value=1, max_value=50, step=1)
        atr_multiplier = st.number_input("ATR multiplier (reward)", value=2.0, min_value=0.5, max_value=5.0, step=0.1)
        volume_factor = st.number_input("Volume factor (× avg)", value=1.2, min_value=1.0, max_value=3.0, step=0.1)
        rsi_overbought = st.number_input("RSI overbought exit", value=70, min_value=1, max_value=99, step=1)
        strategy_params = {
            'fast_period': fast_period,
            'slow_period': slow_period,
            'rsi_momentum_low': rsi_momentum_low,
            'rsi_momentum_high': rsi_momentum_high,
            'atr_period': atr_period,
            'atr_multiplier': atr_multiplier,
            'volume_factor': volume_factor,
            'rsi_overbought': rsi_overbought,
        }
    else:
        boll_period = st.number_input("Bollinger period", value=20, min_value=5, max_value=100, step=1)
        boll_stddev = st.number_input("Bollinger stddev", value=2.0, min_value=1.0, max_value=4.0, step=0.1)
        rsi_oversold = st.number_input("RSI oversold threshold", value=30, min_value=1, max_value=50, step=1)
        exit_rsi = st.number_input("Exit RSI threshold", value=50, min_value=1, max_value=99, step=1)
        stop_loss_pct = st.number_input("Stop loss (%)", value=5.0, min_value=0.0, max_value=20.0, step=0.5)
        strategy_params = {
            'boll_period': boll_period,
            'boll_stddev': boll_stddev,
            'rsi_oversold': rsi_oversold,
            'exit_rsi': exit_rsi,
            'stop_loss': stop_loss_pct / 100.0,
        }

run_optimization = st.checkbox("Optimize strategy parameters")
strategy_grid = {}
max_results = 5
if run_optimization:
    with st.expander("Optimization ranges", expanded=True):
        max_results = st.number_input("Top results to display", value=5, min_value=1, max_value=10, step=1)
        if strategy_name == 'ema_rsi':
            fast_period_range = st.slider("Fast EMA range", 2, 50, (5, 15), step=1)
            slow_period_range = st.slider("Slow EMA range", 10, 100, (16, 35), step=1)
            rsi_range = st.slider("RSI threshold range", 50, 90, (60, 80), step=1)
            stop_loss_range = st.slider("Stop loss range (%)", 0.5, 10.0, (2.0, 6.0), step=0.5)
            strategy_grid = {
                'fast_period': range(fast_period_range[0], fast_period_range[1] + 1),
                'slow_period': range(slow_period_range[0], slow_period_range[1] + 1),
                'rsi_overbought': range(rsi_range[0], rsi_range[1] + 1),
                'stop_loss': [x / 100.0 for x in range(int(stop_loss_range[0] * 2), int(stop_loss_range[1] * 2) + 1)],
            }
        elif strategy_name == 'trend_atr':
            fast_period_range = st.slider("Fast EMA range", 2, 50, (5, 15), step=1)
            slow_period_range = st.slider("Slow EMA range", 10, 100, (16, 35), step=1)
            atr_mult_range = st.slider("ATR multiplier range", 1.0, 4.0, (1.5, 3.0), step=0.1)
            rsi_range = st.slider("RSI threshold range", 50, 90, (60, 80), step=1)
            stop_loss_range = st.slider("Stop loss range (%)", 0.5, 10.0, (2.0, 6.0), step=0.5)
            strategy_grid = {
                'fast_period': range(fast_period_range[0], fast_period_range[1] + 1),
                'slow_period': range(slow_period_range[0], slow_period_range[1] + 1),
                'atr_multiplier': [x / 10.0 for x in range(int(atr_mult_range[0] * 10), int(atr_mult_range[1] * 10) + 1)],
                'rsi_overbought': range(rsi_range[0], rsi_range[1] + 1),
                'stop_loss': [x / 100.0 for x in range(int(stop_loss_range[0] * 2), int(stop_loss_range[1] * 2) + 1)],
            }
        elif strategy_name == 'momentum_breakout':
            breakout_range = st.slider("Breakout lookback range", 5, 50, (10, 30), step=1)
            atr_mult_range = st.slider("ATR multiplier range", 1.0, 4.0, (1.5, 3.0), step=0.1)
            rsi_range = st.slider("RSI threshold range", 50, 90, (60, 80), step=1)
            strategy_grid = {
                'breakout_period': range(breakout_range[0], breakout_range[1] + 1),
                'atr_multiplier': [x / 10.0 for x in range(int(atr_mult_range[0] * 10), int(atr_mult_range[1] * 10) + 1)],
                'rsi_overbought': range(rsi_range[0], rsi_range[1] + 1),
            }
        elif strategy_name == 'confirmed_crossover':
            fast_period_range = st.slider("Fast EMA range", 2, 50, (5, 15), step=1)
            slow_period_range = st.slider("Slow EMA range", 10, 100, (16, 35), step=1)
            rsi_low_range = st.slider("RSI momentum low range", 30, 55, (40, 50), step=1)
            rsi_high_range = st.slider("RSI momentum high range", 55, 80, (60, 70), step=1)
            atr_mult_range = st.slider("ATR multiplier range", 1.0, 4.0, (1.5, 3.0), step=0.5)
            vol_factor_range = st.slider("Volume factor range", 1.0, 2.0, (1.1, 1.5), step=0.1)
            strategy_grid = {
                'fast_period': range(fast_period_range[0], fast_period_range[1] + 1),
                'slow_period': range(slow_period_range[0], slow_period_range[1] + 1),
                'rsi_momentum_low': range(rsi_low_range[0], rsi_low_range[1] + 1),
                'rsi_momentum_high': range(rsi_high_range[0], rsi_high_range[1] + 1),
                'atr_multiplier': [x / 10.0 for x in range(int(atr_mult_range[0] * 10), int(atr_mult_range[1] * 10) + 1, 5)],
                'volume_factor': [x / 10.0 for x in range(int(vol_factor_range[0] * 10), int(vol_factor_range[1] * 10) + 1)],
            }
        else:
            boll_range = st.slider("Bollinger period range", 10, 50, (15, 25), step=1)
            stddev_range = st.slider("Bollinger stddev range", 1.0, 3.0, (1.5, 2.5), step=0.1)
            rsi_oversold_range = st.slider("RSI oversold range", 10, 40, (20, 35), step=1)
            exit_rsi_range = st.slider("Exit RSI range", 40, 70, (45, 55), step=1)
            strategy_grid = {
                'boll_period': range(boll_range[0], boll_range[1] + 1),
                'boll_stddev': [x / 10.0 for x in range(int(stddev_range[0] * 10), int(stddev_range[1] * 10) + 1)],
                'rsi_oversold': range(rsi_oversold_range[0], rsi_oversold_range[1] + 1),
                'exit_rsi': range(exit_rsi_range[0], exit_rsi_range[1] + 1),
            }

combination_count = None
if run_optimization and strategy_grid:
    combination_count = 1
    for values in strategy_grid.values():
        combination_count *= len(values)
    if combination_count > 100:
        st.warning(f"This optimization will test {combination_count} parameter combinations. It may take a while.")

st.markdown("_Exact trade times are available only when using intraday data (5m, 30m, 1h)._ ")

button_label = "Run Optimization" if run_optimization else "Run Backtest"
if st.button(button_label):
    if run_optimization:
        with st.spinner("Running strategy optimization..."):
            result = optimize_strategy(
                ticker=ticker.upper(),
                strategy_name=strategy_name,
                strategy_grid=strategy_grid,
                open_cost=open_cost / 100.0,
                close_cost=close_cost / 100.0,
                interval=interval,
                period=period,
                top_n=max_results,
            )
    else:
        with st.spinner("Running backtest..."):
            result = run_backtest(
                ticker=ticker.upper(),
                strategy_name=strategy_name,
                strategy_params=strategy_params,
                open_cost=open_cost / 100.0,
                close_cost=close_cost / 100.0,
                interval=interval,
                period=period,
            )

    if 'error' in result:
        st.error(result['error'])
    elif run_optimization:
        st.subheader("Optimization Results")
        st.markdown(f"- Data source: {result['source_note']}")
        st.markdown(f"- Tested parameter combinations: {result['total_combinations']}")

        if result['top_results']:
            for idx, row in enumerate(result['top_results'], start=1):
                st.markdown(f"**Rank {idx}**: Total Return {row['total_return']:.2f}%, Sharpe {row['sharpe']:.2f}, Max Drawdown {row['max_drawdown']:.2f}%")
                st.markdown(f"- Params: {row['strategy_params']}")
                st.markdown(f"- Trades: {row['trade_summary']['total']}")
        else:
            st.info("No optimization results were found. Try expanding the ranges or using a different ticker.")
    else:
        st.subheader("Summary")
        for line in result['summary']:
            st.markdown(f"- {line}")

        st.markdown("**Trading hours:** 9:30 AM EST to 4:00 PM EST (intraday data only)")
        if interval == '1d':
            st.markdown("_Using daily bars: exact intraday time is not available, only trade dates are shown._")
        else:
            st.markdown("_Using intraday bars: trade timestamps include exact executed times._")

        if result['yearly_returns']:
            st.subheader("Yearly Returns")
            for row in result['yearly_returns']:
                st.markdown(f"- **{row['year']}**: {row['return']:.2f}%")

        st.subheader("Trade Summary")
        st.markdown(f"- Total Trades: {result['trade_summary']['total']}")
        st.markdown(f"- Winning Trades: {result['trade_summary']['won']}")
        st.markdown(f"- Losing Trades: {result['trade_summary']['lost']}")

        if result['trades']:
            st.subheader("Trades")
            for trade in result['trades']:
                color = "green" if trade['is_win'] else "red"
                st.markdown(
                    f"<div style='color: {color};'>"
                    f"Trade: Opened {trade['dtopen']} at ${trade['entry_price']:.2f} | "
                    f"Closed {trade['dtclose']} at ${trade['exit_price']:.2f} | "
                    f"Gross PnL: ${trade['gross_pnl']:.2f} | Net PnL: ${trade['net_pnl']:.2f} | "
                    f"Open Cost: ${trade['open_cost']:.2f} | Close Cost: ${trade['close_cost']:.2f} | "
                    f"Total Cost: ${trade['total_cost']:.2f}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
        else:
            st.info("No trades executed.")

# Real-time monitoring section
st.header("Real-Time Monitoring")

watchlist = st.text_area("Enter tickers to monitor (comma-separated)", value="AAPL,GOOGL,MSFT")
alert_email = st.text_input("Alert email (optional)", value="")
start_monitor = st.button("Start Real-Time Monitor")
stop_monitor = st.button("Stop Real-Time Monitor")

if 'monitor' not in st.session_state:
    st.session_state.monitor = None

if start_monitor:
    if st.session_state.monitor and st.session_state.monitor.running:
        st.warning("Monitor is already running.")
    else:
        tickers = [t.strip().upper() for t in watchlist.split(',')]
        st.session_state.monitor = RealTimeMonitor(
            tickers=tickers,
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            open_cost=open_cost / 100.0,
            close_cost=close_cost / 100.0,
            alert_email=alert_email if alert_email else None,
        )
        st.session_state.monitor.start()
        st.success("Real-time monitoring started.")

if stop_monitor:
    if st.session_state.monitor and st.session_state.monitor.running:
        st.session_state.monitor.stop()
        st.success("Real-time monitoring stopped.")
    else:
        st.warning("Monitor is not running.")

if st.session_state.monitor:
    st.subheader("Current Positions")
    positions = st.session_state.monitor.positions
    if positions:
        for ticker, pos in positions.items():
            st.markdown(f"- {ticker}: Entry ${pos['entry_price']:.2f}, Qty {pos['quantity']}")
    else:
        st.info("No positions held.")

# ── Batch Backtest ────────────────────────────────────────────────────────────
st.header("Batch Backtest (Top 100 Stocks)")

with st.expander("Batch settings", expanded=True):
    batch_strategy = st.selectbox(
        "Strategy",
        ['confirmed_crossover', 'ema_rsi', 'trend_atr', 'momentum_breakout', 'mean_reversion'],
        key='batch_strategy',
    )
    batch_interval = st.selectbox("Interval", ['1d', '5m', '30m', '1h'], key='batch_interval')
    batch_period = st.selectbox("Period", ['30d', '60d', '90d'], key='batch_period')
    batch_open_cost = st.number_input("Open Cost (%)", min_value=0.0, max_value=1.0, value=0.1, step=0.01, key='batch_open_cost')
    batch_close_cost = st.number_input("Close Cost (%)", min_value=0.0, max_value=1.0, value=0.1, step=0.01, key='batch_close_cost')
    custom_tickers = st.text_area(
        "Tickers (comma-separated, leave blank for default 100)",
        value='',
        height=3,
        key='batch_tickers',
    )

if st.button("Run Batch Backtest"):
    tickers = (
        [t.strip().upper() for t in custom_tickers.split(',') if t.strip()]
        if custom_tickers.strip()
        else POPULAR_TICKERS
    )
    progress_bar = st.progress(0)
    status_text = st.empty()

    def on_progress(completed, total, ticker):
        progress_bar.progress(completed / total)
        status_text.text(f"Processed {completed}/{total}: {ticker}")

    with st.spinner(f"Running backtest for {len(tickers)} tickers..."):
        batch_result = run_batch_backtest(
            tickers=tickers,
            strategy_name=batch_strategy,
            open_cost=batch_open_cost / 100.0,
            close_cost=batch_close_cost / 100.0,
            interval=batch_interval,
            period=batch_period,
            max_workers=10,
            progress_callback=on_progress,
        )

    progress_bar.progress(1.0)
    status_text.text(f"Done. {len(batch_result['results'])} tickers backtested.")

    if batch_result['results']:
        st.subheader("Results (sorted by Total Return)")
        df = pd.DataFrame(batch_result['results'])
        df.columns = ['Ticker', 'Total Return %', 'Annual Return %', 'Sharpe', 'Max Drawdown %', 'Trades', 'Won', 'Lost', 'Win Rate %']

        def color_return(val):
            try:
                f = float(val)
                if f != f:
                    return ''
                return 'color: green' if f > 0 else 'color: red'
            except (TypeError, ValueError):
                return ''

        st.dataframe(
            df.style.map(color_return, subset=['Total Return %', 'Annual Return %'])
                    .format({
                        'Total Return %': '{:.2f}',
                        'Annual Return %': '{:.2f}',
                        'Sharpe': '{:.2f}',
                        'Max Drawdown %': '{:.2f}',
                        'Win Rate %': '{:.1f}',
                    }),
            use_container_width=True,
            height=600,
        )

        st.subheader("Top 10 by Total Return")
        top10 = df.head(10)
        st.bar_chart(top10.set_index('Ticker')['Total Return %'])

    if batch_result['errors']:
        with st.expander(f"{len(batch_result['errors'])} tickers failed"):
            for e in batch_result['errors']:
                st.markdown(f"- **{e['ticker']}**: {e['error']}")

# ── Compounding Backtest ──────────────────────────────────────────────────────
st.header("Compounding Backtest (Year-by-Year since 2023)")
st.markdown("Invests a fixed amount in 2023 and reinvests the full portfolio value each year for all tickers.")

with st.expander("Settings", expanded=True):
    comp_strategy = st.selectbox(
        "Strategy",
        ['confirmed_crossover', 'ema_rsi', 'trend_atr', 'momentum_breakout', 'mean_reversion'],
        key='comp_strategy',
    )
    comp_initial = st.number_input("Initial Investment ($)", min_value=100.0, value=1000.0, step=100.0)
    comp_open_cost = st.number_input("Open Cost (%)", min_value=0.0, max_value=1.0, value=0.1, step=0.01, key='comp_open')
    comp_close_cost = st.number_input("Close Cost (%)", min_value=0.0, max_value=1.0, value=0.1, step=0.01, key='comp_close')
    comp_auto_optimize = st.checkbox(
        "Auto-Optimize (train on 2020–2022, apply best params to 2023+)",
        value=True,
        help="Finds the best strategy parameters using 2020–2022 data, then runs the compounding backtest from 2023 with those params.",
    )
    comp_custom_tickers = st.text_area(
        "Tickers (comma-separated, leave blank for all 100)",
        value='',
        height=3,
        key='comp_tickers',
    )

def color_ret(val):
    try:
        f = float(val)
        if f != f:  # NaN check
            return ''
        return 'color: green' if f > 0 else 'color: red'
    except (TypeError, ValueError):
        return ''

if st.button("Run Compounding Backtest"):
    tickers = (
        [t.strip().upper() for t in comp_custom_tickers.split(',') if t.strip()]
        if comp_custom_tickers.strip()
        else POPULAR_TICKERS
    )

    comp_progress = st.progress(0)
    comp_status = st.empty()

    if comp_auto_optimize:
        # Per-ticker: optimize on 2020-2022, then compound from 2023
        results_opt = []
        errors_opt = []
        for i, tkr in enumerate(tickers):
            comp_status.text(f"Optimizing + compounding {i+1}/{len(tickers)}: {tkr}")
            comp_progress.progress((i + 1) / len(tickers))
            try:
                r = optimize_then_compound(
                    ticker=tkr,
                    strategy_name=comp_strategy,
                    open_cost=comp_open_cost / 100.0,
                    close_cost=comp_close_cost / 100.0,
                    initial_cash=comp_initial,
                )
                results_opt.append({
                    'ticker': r['ticker'],
                    'initial': r['initial_cash'],
                    'final_value': r['final_value'],
                    'total_gain': r['total_gain'],
                    'total_return_pct': r['total_return_pct'],
                    'yearly': r['yearly'],
                    'optimized_params': r.get('optimized_params', {}),
                })
            except Exception as e:
                errors_opt.append({'ticker': tkr, 'error': str(e)})
        results_opt.sort(key=lambda x: x['final_value'], reverse=True)
        batch_comp = {'results': results_opt, 'errors': errors_opt, 'total': len(tickers)}
    else:
        def on_comp_progress(completed, total, ticker):
            comp_progress.progress(completed / total)
            comp_status.text(f"Processed {completed}/{total}: {ticker}")

        with st.spinner(f"Running compounding backtest for {len(tickers)} tickers..."):
            batch_comp = run_batch_compounding_backtest(
                tickers=tickers,
                strategy_name=comp_strategy,
                open_cost=comp_open_cost / 100.0,
                close_cost=comp_close_cost / 100.0,
                initial_cash=comp_initial,
                max_workers=10,
                progress_callback=on_comp_progress,
            )

    comp_progress.progress(1.0)
    comp_status.text(f"Done. {len(batch_comp['results'])} tickers completed.")

    if batch_comp['results']:
        st.subheader("All Tickers — Sorted by Final Value")

        summary_rows = []
        for r in batch_comp['results']:
            row = {'Ticker': r['ticker'], 'Initial ($)': r['initial'], 'Final Value ($)': r['final_value'],
                   'Total Gain ($)': r['total_gain'], 'Total Return %': r['total_return_pct']}
            for y in r['yearly']:
                row[f"{y['year']} Return %"] = y['return_pct']
                row[f"{y['year']} End ($)"] = y['end_value']
            summary_rows.append(row)

        df_summary = pd.DataFrame(summary_rows)
        st.dataframe(df_summary, use_container_width=True, height=600)

        st.subheader("Top 10 Final Values")
        top10 = df_summary.head(10)
        st.bar_chart(top10.set_index('Ticker')['Final Value ($)'])

        st.subheader("Drill Down — Year-by-Year for a Single Ticker")
        selected = st.selectbox("Select ticker", [r['ticker'] for r in batch_comp['results']], key='comp_drill')
        detail = next(r for r in batch_comp['results'] if r['ticker'] == selected)

        col1, col2, col3 = st.columns(3)
        col1.metric("Initial", f"${detail['initial']:,.2f}")
        col2.metric("Final Value", f"${detail['final_value']:,.2f}", delta=f"${detail['total_gain']:+,.2f}")
        col3.metric("Total Return", f"{detail['total_return_pct']:+.2f}%")

        if detail.get('optimized_params'):
            st.info(f"Optimized params (trained 2020–2022): {detail['optimized_params']}")

        df_drill = pd.DataFrame(detail['yearly'])
        df_drill.columns = ['Year', 'Start ($)', 'End ($)', 'Return %', 'Profit ($)', 'Trades', 'Won', 'Lost', 'Win Rate %']
        st.dataframe(df_drill, use_container_width=True)

        chart_data = pd.DataFrame({
            'Year': [y['year'] for y in detail['yearly']],
            'Portfolio Value ($)': [y['end_value'] for y in detail['yearly']],
        }).set_index('Year')
        st.line_chart(chart_data)

    if batch_comp['errors']:
        with st.expander(f"{len(batch_comp['errors'])} tickers failed"):
            for e in batch_comp['errors']:
                st.markdown(f"- **{e['ticker']}**: {e['error']}")
