# Trading Bot Backtesting

This is a backtesting trading bot for stocks using a strategy based on EMA crossover and RSI indicators. The bot aims to achieve consistent returns, but please note that past performance does not guarantee future results. Trading involves risk, and this is for educational purposes only.

## Strategy Description

The strategy uses:
- Fast EMA (9-period)
- Slow EMA (21-period)
- RSI (14-period)

Buy signal: When fast EMA crosses above slow EMA and RSI < 70
Sell signal: When fast EMA crosses below slow EMA or RSI > 70

## Initial Investment

$1000

## Backtesting Period

From 2020-01-01 to present, using AAPL stock data.

## Disclaimer

This bot is a simulation and does not execute real trades. No strategy can guarantee 3% monthly returns, as markets are unpredictable. Use at your own risk. Consult a financial advisor before investing real money.

## Requirements

- Python 3.x
- Dependencies listed in requirements.txt

## Installation

1. Install Python dependencies:
   ```
   pip install -r requirements.txt
   ```

## Running the Backtest

### Command Line
Run the main script:
```
python main.py
```

### GUI (Web-based)
Run the GUI app:
```
streamlit run gui.py
```

This opens a web browser with a GUI to enter a ticker symbol, set open/close cost percentages, and run the backtest. The app now also includes:
- selectable strategy modes: EMA + RSI Crossover, Trend Following ATR, Momentum Breakout, and Mean Reversion
- custom strategy parameter inputs for each mode
- an optimization mode to sweep valid parameter combinations
- yearly returns, trade summary, and exact trade timestamps for intraday data

The script will output the backtesting results, including total return, average monthly return, annual return, max drawdown, and Sharpe ratio.

## Real-Time Trading

Run the real-time GUI:
```
streamlit run realtime_gui.py
```

The GUI includes a real-time monitoring section to analyze stocks every second and generate alerts.

### Setup
- Select a strategy and set parameters.
- Enter a comma-separated list of tickers to monitor.
- Optionally provide an email address for alerts (requires SMTP configuration in code).
- Enter your Alpaca API key and secret key (get from [alpaca.markets](https://alpaca.markets)).
- Choose paper trading for safe testing.
- Click "Start Real-Time Monitor" to begin.

### Features
- Analyzes each stock in the watchlist every second using 1-minute data.
- Generates BUY alerts when strategy conditions are met.
- Tracks positions and generates SELL alerts when exit conditions are met.
- Displays current positions in the GUI.
- Executes real trades via Alpaca API if keys are provided.

### Important Notes
- Real-time trading involves significant risk and requires a brokerage account with API access for actual order execution.
- This implementation uses Yahoo Finance for data, which has rate limits and may not be suitable for high-frequency trading.
- For production use, integrate with a professional trading API (e.g., Alpaca, Interactive Brokers).
- Alerts are currently printed to console or sent via email; customize as needed.
- This is for educational purposes only; consult a financial advisor before trading with real money.
- Start with paper trading to test without risking real money.