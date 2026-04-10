import streamlit as st
import os
import time
import logging
import threading
import pandas as pd
from datetime import datetime

from monitor import RealTimeMonitor
from strategies import get_strategy_class
from main import run_backtest, run_compounding_backtest
from config import (
    TICKERS, STRATEGY, STRATEGY_PARAMS,
    MAX_POSITIONS, ORDER_COOLDOWN, TRADE_BUDGET,
    ALERT_EMAIL, PAPER_TRADING, DATA_SOURCE,
)

# ── File logger (shared across reruns via module-level state) ──────────────────
_log_handler_installed = False

def _setup_file_logger():
    """Configure root logger to write to today's log file. Safe to call multiple times."""
    global _log_handler_installed
    if _log_handler_installed:
        return
    log_dir = os.path.join(os.path.dirname(__file__), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"monitor_{datetime.now().strftime('%Y-%m-%d')}.log")
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    _log_handler_installed = True

def _start_log_writer(monitor: RealTimeMonitor):
    """
    Background thread that writes heartbeat + trade events to the log file,
    identical to what run_monitor.py does. Stops when monitor stops.
    """
    log = logging.getLogger('gui_log_writer')

    def _run():
        log.info(f"Monitor started from UI | Strategy: {monitor.strategy_name} | "
                 f"Tickers: {len(monitor.tickers)} | Paper: {monitor.trading_client is not None}")
        while monitor.running:
            trades    = monitor.trade_log
            positions = monitor.positions
            wins      = sum(1 for t in trades if t['is_win'])
            total_pnl = sum(t['pnl'] for t in trades)
            log.info(
                f"[heartbeat] scanning {len(monitor.tickers)} tickers | "
                f"open positions: {len(positions)} {list(positions.keys()) or 'none'} | "
                f"trades today: {len(trades)} ({wins} wins) | "
                f"PnL: ${total_pnl:+.2f}"
            )
            # Log each new completed trade
            if hasattr(monitor, '_logged_trades'):
                new = trades[len(monitor._logged_trades):]
            else:
                new = []
                monitor._logged_trades = []
            for t in new:
                flag = "WIN" if t['is_win'] else "LOSS"
                log.info(
                    f"TRADE {flag}: {t['ticker']} | entry ${t['entry_price']:.2f} @ {t.get('entry_time','?')} | "
                    f"exit ${t['exit_price']:.2f} @ {t['time']} | PnL ${t['pnl']:+.2f} | {t['reason']}"
                )
            monitor._logged_trades = list(trades)
            time.sleep(60)
        log.info("Monitor stopped.")

    t = threading.Thread(target=_run, daemon=True)
    t.start()

st.set_page_config(page_title="Trading Hub", layout="wide")
st.title("Trading Hub")

tab_monitor, tab_backtest = st.tabs(["Live Monitor", "Backtest"])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — LIVE MONITOR
# ══════════════════════════════════════════════════════════════════════════════
with tab_monitor:

    # ── Alpaca credentials ────────────────────────────────────────────────────
    api_key    = os.getenv("APCA_API_KEY_ID", "")
    api_secret = os.getenv("APCA_API_SECRET_KEY", "")

    if api_key and api_secret:
        st.success(f"API keys loaded from environment ({api_key[:4]}…{api_key[-4:]})")
    else:
        st.warning("No API keys found in environment. Enter them below.")
        api_key    = st.text_input("Alpaca API Key",    type="password")
        api_secret = st.text_input("Alpaca Secret Key", type="password")

    # ── Settings ──────────────────────────────────────────────────────────────
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        paper      = st.checkbox("Paper Trading", value=PAPER_TRADING)
    with col2:
        max_pos    = st.number_input("Max Positions", min_value=1, max_value=20, value=MAX_POSITIONS)
    with col3:
        cooldown   = st.number_input("Order Cooldown (s)", min_value=60, max_value=3600, value=ORDER_COOLDOWN, step=30)
    with col4:
        budget     = st.number_input("Trade Budget ($)", min_value=100, max_value=100000, value=TRADE_BUDGET, step=100)

    alert_email = st.text_input("Alert Email (optional)", value=ALERT_EMAIL)

    # ── Data source ───────────────────────────────────────────────────────────
    data_source = st.radio(
        "Market Data Source",
        options=["tradier", "alpaca"],
        index=0 if DATA_SOURCE == "tradier" else 1,
        horizontal=True,
        help="Tradier: dedicated data API. Alpaca: uses your trading keys for data too.",
    )
    if data_source == "tradier":
        tradier_token = os.getenv("TRADIER_TOKEN", "")
        if not tradier_token:
            tradier_token = st.text_input("Tradier Token", type="password")
    else:
        tradier_token = ""

    st.caption(f"Scanning {len(TICKERS)} base tickers + dynamic momentum stocks")

    # ── Start / Stop ──────────────────────────────────────────────────────────
    if "monitor" not in st.session_state:
        st.session_state.monitor = None

    col_start, col_stop = st.columns(2)
    with col_start:
        if st.button("▶ Start Monitor", use_container_width=True):
            if st.session_state.monitor and st.session_state.monitor.running:
                st.warning("Already running.")
            elif not api_key or not api_secret:
                st.error("Alpaca API keys required.")
            else:
                m = RealTimeMonitor(
                    tickers=TICKERS,
                    strategy_name=STRATEGY,
                    strategy_params=STRATEGY_PARAMS,
                    alert_email=alert_email or None,
                    alpaca_api_key=api_key,
                    alpaca_secret_key=api_secret,
                    tradier_token=tradier_token,
                    paper=paper,
                    max_positions=max_pos,
                    order_cooldown=cooldown,
                    trade_budget=budget,
                    data_source=data_source,
                )
                try:
                    _setup_file_logger()   # set up file handler before init logs fire
                    m.start()
                    st.session_state.monitor = m
                    _start_log_writer(m)
                    st.success(f"Monitor started (PID {os.getpid()}).")
                except RuntimeError as e:
                    st.error(str(e))

    with col_stop:
        if st.button("⏹ Stop Monitor", use_container_width=True):
            m = st.session_state.monitor
            if m and m.running:
                m.stop()
                st.success("Monitor stopped.")
            else:
                st.warning("Not running.")

    # ── Live status ───────────────────────────────────────────────────────────
    m = st.session_state.monitor
    if m and m.running:
        st.divider()

        # Positions
        st.subheader("Open Positions")
        if m.positions:
            rows = []
            for ticker, pos in m.positions.items():
                rows.append({
                    "Ticker": ticker,
                    "Entry $": f"${pos['entry_price']:.2f}",
                    "Stop $":  f"${pos['stop_price']:.2f}",
                    "Target $":f"${pos['target_price']:.2f}",
                    "Qty":     pos['quantity'],
                    "Since":   pos.get('entry_time', '—'),
                })
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("No open positions.")

        # Trade summary
        st.subheader("Today's Trades")
        if m.trade_log:
            trades    = m.trade_log
            total_pnl = sum(t['pnl'] for t in trades)
            wins      = sum(1 for t in trades if t['is_win'])
            win_rate  = wins / len(trades) * 100

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Trades",    len(trades))
            c2.metric("Win Rate",  f"{win_rate:.1f}%")
            c3.metric("Total PnL", f"${total_pnl:+.2f}")
            c4.metric("Scanning",  f"{len(m.tickers)} tickers")

            df = pd.DataFrame(trades)[['time', 'ticker', 'entry_price', 'exit_price', 'qty', 'pnl', 'reason', 'is_win']]
            df.columns = ['Exit', 'Ticker', 'Entry $', 'Exit $', 'Qty', 'PnL $', 'Reason', 'Win']
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No completed trades yet today.")

    # ── Log viewer ────────────────────────────────────────────────────────────
    st.divider()
    st.subheader("Log")

    log_path = os.path.join(os.path.dirname(__file__), 'logs', f"monitor_{datetime.now().strftime('%Y-%m-%d')}.log")

    col_a, col_b = st.columns([1, 1])
    with col_a:
        n_lines = st.slider("Lines", min_value=10, max_value=200, value=50, step=10)
    with col_b:
        auto_refresh = st.checkbox("Auto-refresh (5s)")

    if os.path.exists(log_path):
        with open(log_path) as f:
            lines = f.readlines()
        st.code("".join(lines[-n_lines:]), language=None)
        st.caption(f"{log_path} — {len(lines)} lines total")
    else:
        st.info("No log file for today yet.")

    if auto_refresh:
        time.sleep(5)
        st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — BACKTEST
# ══════════════════════════════════════════════════════════════════════════════
with tab_backtest:

    col1, col2, col3 = st.columns(3)
    with col1:
        ticker   = st.text_input("Ticker", value="QQQ")
    with col2:
        start_dt = st.text_input("Start date", value="2023-01-01")
    with col3:
        end_dt   = st.text_input("End date",   value=datetime.now().strftime("%Y-%m-%d"))

    strategy_label = st.selectbox("Strategy", [
        'VWAP Reclaim',
        'EMA + RSI Crossover',
        'Trend Following ATR',
        'Momentum Breakout',
        'Mean Reversion',
    ])
    strategy_map = {
        'VWAP Reclaim':         'vwap_reclaim',
        'EMA + RSI Crossover':  'ema_rsi',
        'Trend Following ATR':  'trend_atr',
        'Momentum Breakout':    'momentum_breakout',
        'Mean Reversion':       'mean_reversion',
    }
    strategy_name = strategy_map[strategy_label]

    if strategy_label == 'VWAP Reclaim':
        interval = st.selectbox(
            "Data interval",
            ['1d', '1h', '5m', '1m'],
            index=0,
            help="1d = multi-day VWAP swing trades. 1m/5m = intraday (mirrors live strategy, limited to 60 days of history).",
        )
    else:
        interval = '1d'

    start_cash = st.number_input("Starting capital ($)", min_value=1000, value=10000, step=1000)

    compound = st.checkbox("Year-by-year compounding", value=False,
                           help="Reinvest returns each year starting from the start date")

    if st.button("Run Backtest", use_container_width=True):
        with st.spinner("Running..."):
            try:
                if compound:
                    start_year = int(start_dt[:4])
                    result = run_compounding_backtest(
                        ticker=ticker,
                        strategy_name=strategy_name,
                        strategy_params={},
                        start_year=start_year,
                        initial_cash=start_cash,
                    )
                    if 'error' in result:
                        st.error(result['error'])
                    else:
                        st.subheader("Compounding Results")
                        years = result.get('yearly', [])
                        if years:
                            df = pd.DataFrame(years)
                            if 'return' in df.columns:
                                df['return'] = df['return'].map(lambda x: f"{x:+.1f}%")
                            if 'end_value' in df.columns:
                                df['end_value'] = df['end_value'].map(lambda x: f"${x:,.0f}")
                            st.dataframe(df, use_container_width=True, hide_index=True)
                        c1, c2 = st.columns(2)
                        c1.metric("Final Value",   f"${result.get('final_value', 0):,.0f}")
                        c2.metric("Total Return",  f"{result.get('total_return_pct', 0):+.1f}%")
                else:
                    result = run_backtest(
                        ticker=ticker,
                        strategy_name=strategy_name,
                        strategy_params={},
                        start_cash=start_cash,
                        start_date=start_dt,
                        end_date=end_dt,
                        interval=interval,
                    )
                    if 'error' in result:
                        st.error(result['error'])
                    else:
                        st.subheader("Results")
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("Total Return",  f"{result.get('total_return', 0):+.2f}%")
                        c2.metric("Sharpe Ratio",  f"{result.get('sharpe', 0):.2f}")
                        c3.metric("Max Drawdown",  f"{result.get('max_drawdown', 0):.2f}%")
                        c4.metric("Total Trades",  result.get('trade_summary', {}).get('total', 0))

                        trades = result.get('trades', [])
                        if trades:
                            df = pd.DataFrame(trades)
                            st.dataframe(df, use_container_width=True, hide_index=True)
            except Exception as e:
                st.error(f"Backtest error: {e}")
