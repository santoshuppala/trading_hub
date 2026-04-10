# Trading Hub

A real-time stock trading system with backtesting, live monitoring, and automated scheduling. Uses Alpaca for order execution and market data, and implements a VWAP Reclaim strategy with institutional-grade filters.

> **Disclaimer:** This is for educational purposes only. Trading involves significant risk. Always start with paper trading. Consult a financial advisor before trading with real money.

---

## Project Structure

```
trading_hub/
├── monitor/                  # Real-time monitor modules
│   ├── alerts.py             # Email alert logic (Yahoo SMTP)
│   ├── data_client.py        # Alpaca data fetching (bars, quotes, SPY bias)
│   ├── screener.py           # Momentum screener + Relative Strength filter
│   ├── signals.py            # VWAP Reclaim signal analysis (entry/exit)
│   ├── orders.py             # Order placement (marketable limit / market)
│   └── monitor.py            # RealTimeMonitor orchestrator + lock file guard
├── strategies/               # Backtrader strategy classes (for backtesting)
│   ├── base.py               # BaseTradeStrategy
│   ├── ema_rsi.py            # EMA + RSI Crossover
│   ├── trend_atr.py          # Trend Following ATR
│   ├── momentum_breakout.py  # Momentum Breakout
│   └── mean_reversion.py     # Mean Reversion (Bollinger + RSI)
├── app.py                    # Unified Streamlit UI (live monitor + backtest)
├── main.py                   # Backtesting engine
├── run_monitor.py            # Headless daily launcher (no UI required)
├── start_monitor.sh          # Shell launcher for cron scheduling
├── config.py                 # Shared configuration (tickers, strategy, credentials)
├── logs/                     # Daily log files (monitor_YYYY-MM-DD.log)
├── .env                      # API keys and SMTP credentials (never commit)
└── requirements.txt
```

---

## Installation

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Configuration

Create a `.env` file in the project root. No quotes around values, no spaces around `=`:

```
APCA_API_KEY_ID=PKxxxxxxxxxxxxxxxx
APCA_API_SECRET_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
PAPER_TRADING=true

ALERT_EMAIL_USER=you@yahoo.com
ALERT_EMAIL_PASS=your16charapppassword
ALERT_EMAIL_FROM=you@yahoo.com
ALERT_EMAIL_TO=you@yahoo.com
```

Get Alpaca API keys at [alpaca.markets](https://alpaca.markets). Use paper trading keys for safe testing.

All shared settings (tickers, strategy params, max positions, etc.) live in `config.py`. Both the UI and headless launcher read from there — change it once and it applies everywhere.

---

## Running

### UI (live monitor + backtest in one place)

```bash
streamlit run app.py
```

Opens a browser with two tabs:
- **Live Monitor** — start/stop the monitor, view open positions, today's trades, live log with auto-refresh
- **Backtest** — run single-ticker or year-by-year compounding backtests

All UI activity is logged to `logs/monitor_YYYY-MM-DD.log` — same file and format as the headless run.

### Headless (command line / cron)

```bash
bash start_monitor.sh
```

Or directly:

```bash
source venv/bin/activate
python run_monitor.py
```

Runs without any browser. Logs to `logs/monitor_YYYY-MM-DD.log`. Stops automatically at 3:15 PM ET with a full EOD summary.

### Scheduled Daily (cron)

Runs automatically at 6:00 AM PST (9:00 AM ET) on weekdays:

```
0 6 * * 1-5 /path/to/trading_hub/start_monitor.sh >> /path/to/logs/cron.log 2>&1
```

### Single instance enforcement

Only one monitor can run at a time. If you try to start a second instance (from either the UI or command line), it will refuse and print the PID of the running process. The lock file is at `.monitor.lock` in the project root and is cleaned up automatically on stop.

---

## Live Trading Strategy: VWAP Reclaim

All 9 conditions must be true simultaneously for a buy order to be placed:

| # | Filter | Detail |
|---|---|---|
| 1 | **2-bar VWAP reclaim** | Was above VWAP → dipped below → reclaimed for 2 consecutive bars |
| 2 | **Opened above VWAP** | Day bias is bullish |
| 3 | **RSI 50–70** | Momentum without being overbought |
| 4 | **RVOL ≥ 2×** | Twice the usual volume at this time of day |
| 5 | **SPY above VWAP** | Market tailwind |
| 6 | **Bid/ask spread ≤ 0.2%** | Spread not wider than profit margin |
| 7 | **Not already traded today** | No double-dip on same ticker |
| 8 | **Trading hours 9:45–3:00 PM ET** | Avoid noisy open and illiquid close |
| 9 | **Max 5 concurrent positions** | Risk management |

**Exit conditions:** trailing stop (1×ATR), target (2×ATR), RSI overbought, VWAP breakdown, or EOD force-close at 3:00 PM ET.

**Order types:** Buys use marketable limit orders (ask + 0.05% buffer) — fills immediately like a market order but rejects if price spikes before fill. Sells use market orders for guaranteed exit speed.

**Friction model:** Alpaca is commission-free. A 0.01% slippage buffer is applied to the entry price. Entries are skipped if the bid/ask spread exceeds 0.2%.

---

## Stock Scanning

**Base watchlist:** 164 pre-selected liquid stocks across mega-cap tech, semiconductors, software, fintech, financials, energy, healthcare, consumer, and sector ETFs — defined in `config.py`.

**Dynamic momentum tickers:** Refreshed every 30 minutes during market hours via the Alpaca screener API:
- Top 50 most active stocks by volume
- Top 20 gainers

Filtered by **Relative Strength**: only stocks outperforming SPY over the last 5 trading days are added.

**Data fetching:** All tickers fetched in a single Alpaca batch API call (`feed='iex'`), then analyzed across 50 parallel threads — one full scan cycle per minute.

---

## Logging

Both run modes write to `logs/monitor_YYYY-MM-DD.log`:

| Event | Logged |
|---|---|
| Monitor start/stop | Strategy, ticker count, paper mode, PID |
| Heartbeat (every 60s) | Ticker count, open positions, trade count, running PnL |
| Every completed trade | Entry/exit price, time, quantity, PnL, exit reason |
| Batch fetch errors | Data API errors with details |
| Email alerts | Sent/failed status |

The UI log viewer reads this file live with optional 5-second auto-refresh.

---

## EOD Summary

When the monitor stops (3:15 PM ET or manual stop), a detailed summary is logged:

- Total trades, wins/losses, win rate
- Average win / average loss
- Profit factor
- Best and worst trade
- Exit reason breakdown
- Per-ticker breakdown
- Full trade-by-trade log with entry time, exit time, prices, PnL

---

## Backtesting Strategies

| Strategy | Description |
|---|---|
| EMA + RSI Crossover | Fast/slow EMA crossover with RSI filter |
| Trend Following ATR | EMA crossover with ATR-based trailing stop |
| Momentum Breakout | N-bar high breakout with ATR stop |
| Mean Reversion | Bollinger Band lower touch + RSI oversold |

Supports single-ticker backtests and year-by-year compounding (reinvest returns each year).

---

## Requirements

- Python 3.9+
- Alpaca account (free paper trading available at [alpaca.markets](https://alpaca.markets))
- Yahoo Mail app password for email alerts (16-character, no spaces)
- macOS/Linux for cron scheduling
