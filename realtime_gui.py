import streamlit as st
import os
from realtime_main import RealTimeMonitor, get_strategy_class
from main import optimize_strategy, get_default_grid
from concurrent.futures import ThreadPoolExecutor, as_completed

st.title("Real-Time Trading Monitor")

# Strategy selection
selected_strategies = st.multiselect(
    "Strategies to track in parallel",
    [
        'Confirmed Crossover (Optimized)',
        'EMA + RSI Crossover',
        'Trend Following ATR',
        'Momentum Breakout',
        'Mean Reversion',
    ],
    default=['Confirmed Crossover (Optimized)'],
)
strategy_name_map = {
    'Confirmed Crossover (Optimized)': 'confirmed_crossover',
    'EMA + RSI Crossover': 'ema_rsi',
    'Trend Following ATR': 'trend_atr',
    'Momentum Breakout': 'momentum_breakout',
    'Mean Reversion': 'mean_reversion',
}
strategy_params_map = {}

with st.expander("Common trade settings", expanded=True):
    open_cost = st.number_input("Open Cost (% of trade)", min_value=0.0, max_value=1.0, value=0.1, step=0.01)
    close_cost = st.number_input("Close Cost (% of trade)", min_value=0.0, max_value=1.0, value=0.1, step=0.01)

for strategy in selected_strategies:
    strategy_name = strategy_name_map[strategy]
    with st.expander(f"{strategy} parameters", expanded=True):
        if strategy_name == 'ema_rsi':
            fast_period = st.number_input(f"{strategy} - Fast EMA period", value=9, min_value=1, max_value=100, step=1)
            slow_period = st.number_input(f"{strategy} - Slow EMA period", value=21, min_value=2, max_value=200, step=1)
            rsi_overbought = st.number_input(f"{strategy} - RSI overbought threshold", value=70, min_value=1, max_value=99, step=1)
            stop_loss_pct = st.number_input(f"{strategy} - Stop loss (%)", value=5.0, min_value=0.0, max_value=20.0, step=0.5)
            strategy_params_map[strategy_name] = {
                'fast_period': fast_period,
                'slow_period': slow_period,
                'rsi_overbought': rsi_overbought,
                'stop_loss': stop_loss_pct / 100.0,
            }
        elif strategy_name == 'trend_atr':
            fast_period = st.number_input(f"{strategy} - Fast EMA period", value=9, min_value=1, max_value=100, step=1)
            slow_period = st.number_input(f"{strategy} - Slow EMA period", value=21, min_value=2, max_value=200, step=1)
            atr_period = st.number_input(f"{strategy} - ATR period", value=14, min_value=1, max_value=50, step=1)
            atr_multiplier = st.number_input(f"{strategy} - ATR multiplier", value=2.0, min_value=0.5, max_value=5.0, step=0.1)
            rsi_overbought = st.number_input(f"{strategy} - RSI overbought threshold", value=70, min_value=1, max_value=99, step=1)
            strategy_params_map[strategy_name] = {
                'fast_period': fast_period,
                'slow_period': slow_period,
                'atr_period': atr_period,
                'atr_multiplier': atr_multiplier,
                'rsi_overbought': rsi_overbought,
            }
        elif strategy_name == 'momentum_breakout':
            breakout_period = st.number_input(f"{strategy} - Breakout lookback (bars)", value=20, min_value=5, max_value=100, step=1)
            atr_period = st.number_input(f"{strategy} - ATR period", value=14, min_value=1, max_value=50, step=1)
            atr_multiplier = st.number_input(f"{strategy} - ATR multiplier", value=2.0, min_value=0.5, max_value=5.0, step=0.1)
            rsi_overbought = st.number_input(f"{strategy} - RSI overbought threshold", value=70, min_value=1, max_value=99, step=1)
            strategy_params_map[strategy_name] = {
                'breakout_period': breakout_period,
                'atr_period': atr_period,
                'atr_multiplier': atr_multiplier,
                'rsi_overbought': rsi_overbought,
            }
        else:
            boll_period = st.number_input(f"{strategy} - Bollinger period", value=20, min_value=5, max_value=100, step=1)
            boll_stddev = st.number_input(f"{strategy} - Bollinger stddev", value=2.0, min_value=1.0, max_value=4.0, step=0.1)
            rsi_oversold = st.number_input(f"{strategy} - RSI oversold threshold", value=30, min_value=1, max_value=50, step=1)
            exit_rsi = st.number_input(f"{strategy} - Exit RSI threshold", value=50, min_value=1, max_value=99, step=1)
            stop_loss_pct = st.number_input(f"{strategy} - Stop loss (%)", value=5.0, min_value=0.0, max_value=20.0, step=0.5)
            strategy_params_map[strategy_name] = {
                'boll_period': boll_period,
                'boll_stddev': boll_stddev,
                'rsi_oversold': rsi_oversold,
                'exit_rsi': exit_rsi,
                'stop_loss': stop_loss_pct / 100.0,
            }

# Popular stocks to monitor - S&P 500 + Tech leaders (cleaned, no delisted)
POPULAR_TICKERS = "AAPL,MSFT,GOOGL,AMZN,NVDA,TSLA,META,AVGO,ASML,COST,CSCO,INTC,AMD,NFLX,ADBE,CRM,INTU,SNPS,CDNS,MU,QCOM,AMAT,LRCX,TXN,STM,ADI,OKE,SLB,CVX,EOG,XOM,KMI,ENPH,RUN,NEE,DUK,SO,EXC,AEP,XEL,PEG,BAC,WFC,JPM,GS,PNC,BLK,AXP,USB,FITB,HBAN,TFC,KEY,ALLY,RF,KKR,BDN,APO,SCHW,ICE,CME,NDAQ,EPAM,RHI,MCK,CAH,AMP,LUV,DAL,JBLU,TPR,NKE,PVH,DXCM,VEEV,UPST,PLTR,HOOD,COIN,RIOT,CLSK"

# Watchlist and alerts
default_watchlist = st.text_area(
    "Enter tickers to monitor (comma-separated)", 
    value=POPULAR_TICKERS,
    height=3,
    help="Click 'clear popular list' button to start with empty list and add your own tickers"
)

# Option to use custom watchlist
col1, col2 = st.columns(2)
with col1:
    use_popular = st.checkbox("Use popular stocks list", value=True)
with col2:
    if st.button("Clear & Start Fresh"):
        default_watchlist = ""

watchlist = POPULAR_TICKERS if use_popular else default_watchlist
alert_email = st.text_input("Alert email (optional)", value="usantoshayyappa@yahoo.com")
position_limit = st.number_input("Max concurrent positions", min_value=1, max_value=50, value=5, step=1)
order_cooldown = st.number_input("Order cooldown (seconds)", min_value=60, max_value=3600, value=300, step=30)
# Alpaca API settings
st.subheader("Alpaca API Settings")
# Load from environment variables if available
default_api_key = os.getenv("APCA_API_KEY_ID", "")
default_secret_key = os.getenv("APCA_API_SECRET_KEY", "")

if default_api_key and default_secret_key:
    st.info("✓ API keys loaded from environment variables (APCA_API_KEY_ID, APCA_API_SECRET_KEY)")
    alpaca_api_key = default_api_key
    alpaca_secret_key = default_secret_key
else:
    st.warning("No API keys in environment. Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in your shell.")
    alpaca_api_key = st.text_input("Alpaca API Key", value=default_api_key, type="password")
    alpaca_secret_key = st.text_input("Alpaca Secret Key", value=default_secret_key, type="password")

paper_trading = st.checkbox("Paper Trading (recommended for testing)", value=True)
auto_optimize = st.checkbox(
    "Auto-Optimize per ticker before starting (trains on 2020–2022 daily data)",
    value=True,
    help="Finds the best strategy parameters for each ticker using historical data, then applies them in real-time.",
)

st.markdown("**How to set keys (recommended):**")
st.code("""
export APCA_API_KEY_ID="your_key_here"
export APCA_API_SECRET_KEY="your_secret_here"
export APCA_API_BASE_URL="https://paper-api.alpaca.markets"
streamlit run realtime_gui.py
""", language="bash")

st.markdown("**Note:** Real-time trading involves risk. This is for educational purposes only. Start with paper trading!")

start_monitor = st.button("Start Real-Time Monitor")
stop_monitor = st.button("Stop Real-Time Monitor")

if 'monitors' not in st.session_state:
    st.session_state.monitors = []

if start_monitor:
    if any(m.running for m in st.session_state.monitors):
        st.warning("Monitor is already running.")
    elif not selected_strategies:
        st.warning("Select at least one strategy before starting.")
    else:
        watchlist = POPULAR_TICKERS if use_popular else default_watchlist
        tickers = [t.strip().upper() for t in watchlist.split(',') if t.strip()]
        st.session_state.monitors = []

        for strategy in selected_strategies:
            strategy_name = strategy_name_map[strategy]
            base_params = strategy_params_map.get(strategy_name, {})
            per_ticker_params = {}

            if auto_optimize:
                opt_progress = st.progress(0)
                opt_status = st.empty()
                opt_status.text(f"Optimizing {strategy} params for {len(tickers)} tickers...")

                def optimize_one(tkr):
                    result = optimize_strategy(
                        ticker=tkr,
                        strategy_name=strategy_name,
                        open_cost=open_cost / 100.0,
                        close_cost=close_cost / 100.0,
                        interval='1d',
                        start_date='2020-01-01',
                        end_date='2022-12-31',
                        top_n=1,
                        min_trades=3,
                    )
                    if 'error' not in result and result['top_results']:
                        return tkr, result['top_results'][0]['strategy_params']
                    return tkr, {}

                completed = 0
                with ThreadPoolExecutor(max_workers=10) as ex:
                    futures = {ex.submit(optimize_one, t): t for t in tickers}
                    for future in as_completed(futures):
                        tkr, best_params = future.result()
                        per_ticker_params[tkr] = best_params
                        completed += 1
                        opt_progress.progress(completed / len(tickers))
                        opt_status.text(f"Optimized {completed}/{len(tickers)}: {tkr} → {best_params}")

                opt_status.text(f"Optimization complete for {strategy}.")

            monitor = RealTimeMonitor(
                tickers=tickers,
                strategy_name=strategy_name,
                strategy_params=base_params,
                open_cost=open_cost / 100.0,
                close_cost=close_cost / 100.0,
                alert_email=alert_email if alert_email else None,
                alpaca_api_key=alpaca_api_key if alpaca_api_key else None,
                alpaca_secret_key=alpaca_secret_key if alpaca_secret_key else None,
                paper=paper_trading,
                max_positions=position_limit,
                order_cooldown=order_cooldown,
                per_ticker_params=per_ticker_params,
            )
            monitor.start()
            st.session_state.monitors.append(monitor)
        st.success("Real-time monitoring started for selected strategies.")

if stop_monitor:
    if st.session_state.monitors and any(m.running for m in st.session_state.monitors):
        for monitor in st.session_state.monitors:
            monitor.stop()
        st.session_state.monitors = []
        st.success("Real-time monitoring stopped.")
    else:
        st.warning("Monitor is not running.")

if st.session_state.monitors:
    st.subheader("Current Positions")
    for monitor in st.session_state.monitors:
        st.markdown(f"### {monitor.strategy_name.replace('_', ' ').title()}")
        positions = monitor.positions
        if positions:
            for ticker, pos in positions.items():
                unrealised = ""
                st.markdown(f"- **{ticker}**: Entry ${pos['entry_price']:.2f} | Stop ${pos['stop_price']:.2f} | Target ${pos['target_price']:.2f} | Qty {pos['quantity']}")
        else:
            st.info(f"No positions held for {monitor.strategy_name.replace('_', ' ').title()}.")

    # ── Today's Trade Summary ─────────────────────────────────────────────────
    st.subheader("Today's Trade Summary")
    for monitor in st.session_state.monitors:
        st.markdown(f"### {monitor.strategy_name.replace('_', ' ').title()}")
        log = monitor.trade_log
        if not log:
            st.info("No completed trades yet today.")
            continue

        total = len(log)
        wins = sum(1 for t in log if t['is_win'])
        losses = total - wins
        total_pnl = sum(t['pnl'] for t in log)
        win_rate = wins / total * 100 if total else 0

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Trades", total)
        col2.metric("Wins / Losses", f"{wins} / {losses}")
        col3.metric("Win Rate", f"{win_rate:.1f}%")
        col4.metric("Total PnL", f"${total_pnl:+.2f}", delta=f"{'▲' if total_pnl >= 0 else '▼'}")

        import pandas as pd
        df = pd.DataFrame(log)
        df = df[['time', 'ticker', 'entry_price', 'exit_price', 'qty', 'pnl', 'reason', 'is_win']]
        df.columns = ['Time', 'Ticker', 'Entry $', 'Exit $', 'Qty', 'PnL $', 'Reason', 'Win']
        st.dataframe(df, use_container_width=True)