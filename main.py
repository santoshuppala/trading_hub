import backtrader as bt
import yfinance as yf
import pandas as pd
from datetime import datetime
from itertools import product
import sys
import time
import threading
import smtplib
from email.mime.text import MIMEText
from concurrent.futures import ThreadPoolExecutor, as_completed

POPULAR_TICKERS = [
    'AAPL','MSFT','GOOGL','AMZN','NVDA','TSLA','META','AVGO','ASML','COST',
    'CSCO','INTC','AMD','NFLX','ADBE','CRM','INTU','SNPS','CDNS','MU',
    'QCOM','AMAT','LRCX','TXN','STM','ADI','OKE','SLB','CVX','EOG',
    'XOM','KMI','ENPH','RUN','NEE','DUK','SO','EXC','AEP','XEL',
    'PEG','BAC','WFC','JPM','GS','PNC','BLK','AXP','USB','FITB',
    'HBAN','TFC','KEY','ALLY','RF','KKR','APO','SCHW','ICE','CME',
    'NDAQ','EPAM','RHI','MCK','CAH','AMP','LUV','DAL','JBLU','TPR',
    'NKE','PVH','DXCM','VEEV','UPST','PLTR','HOOD','COIN','RIOT','CLSK',
    'SPY','QQQ','IWM','GLD','SLV','TLT','HYG','EEM','VNQ','XLF',
    'XLK','XLE','XLV','XLI','XLY','XLP','XLU','XLB','XLRE','XLC',
]

class BaseTradeStrategy(bt.Strategy):
    params = (
        ('open_cost', 0.001),
        ('close_cost', 0.001),
        ('interval', '1d'),
        ('trade_log', None),
    )

    def __init__(self):
        self._entry_info = None

    def notify_order(self, order):
        if order.status != order.Completed:
            return

        if order.isbuy():
            size = abs(order.executed.size)
            open_cost = order.executed.price * size * self.params.open_cost
            dtopen = bt.num2date(order.executed.dt)
            dtopen_str = dtopen.strftime('%Y-%m-%d %H:%M:%S') if self.params.interval != '1d' else dtopen.strftime('%Y-%m-%d')
            self._entry_info = {
                'dtopen': dtopen_str,
                'entry_price': order.executed.price,
                'price': order.executed.price,
                'size': size,
                'open_cost': open_cost,
            }
        elif order.issell() and self._entry_info is not None:
            size = abs(order.executed.size)
            close_cost = order.executed.price * size * self.params.close_cost
            open_cost = self._entry_info.get('open_cost', 0.0)
            gross_pnl = (order.executed.price - self._entry_info['price']) * self._entry_info['size']
            net_pnl = gross_pnl - open_cost - close_cost
            total_cost = open_cost + close_cost
            dtclose = bt.num2date(order.executed.dt)
            dtclose_str = dtclose.strftime('%Y-%m-%d %H:%M:%S') if self.params.interval != '1d' else dtclose.strftime('%Y-%m-%d')
            self.params.trade_log.append({
                'dtopen': self._entry_info['dtopen'],
                'dtclose': dtclose_str,
                'entry_price': self._entry_info['entry_price'],
                'exit_price': order.executed.price,
                'gross_pnl': gross_pnl,
                'net_pnl': net_pnl,
                'open_cost': open_cost,
                'close_cost': close_cost,
                'total_cost': total_cost,
                'is_win': net_pnl >= 0,
            })
            self._entry_info = None

    def notify_trade(self, trade):
        pass


class EMARSICrossoverStrategy(BaseTradeStrategy):
    params = (
        ('fast_period', 9),
        ('slow_period', 21),
        ('rsi_period', 14),
        ('rsi_overbought', 70),
        ('stop_loss', 0.05),
        ('atr_period', 14),
        ('atr_multiplier', 2.0),
        ('breakout_period', 20),
        ('boll_period', 20),
        ('boll_stddev', 2.0),
        ('rsi_oversold', 30),
        ('exit_rsi', 50),
        ('open_cost', 0.001),
        ('close_cost', 0.001),
        ('interval', '1d'),
        ('trade_log', None),
    )

    def __init__(self):
        self.fast_ema = bt.indicators.EMA(self.data.close, period=self.params.fast_period)
        self.slow_ema = bt.indicators.EMA(self.data.close, period=self.params.slow_period)
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)
        self.crossover = bt.indicators.CrossOver(self.fast_ema, self.slow_ema)
        self.stop_price = None

    def next(self):
        if not self.position:
            if self.crossover > 0 and self.rsi < self.params.rsi_overbought:
                self.buy()
                self.stop_price = self.data.close[0] * (1 - self.params.stop_loss)
        else:
            if self.data.close[0] < self.stop_price or self.crossover < 0 or self.rsi > self.params.rsi_overbought:
                self.sell()
                self.stop_price = None


class TrendFollowingATRStrategy(BaseTradeStrategy):
    params = (
        ('fast_period', 9),
        ('slow_period', 21),
        ('atr_period', 14),
        ('atr_multiplier', 2.0),
        ('rsi_period', 14),
        ('rsi_overbought', 70),
        ('stop_loss', 0.05),
        ('breakout_period', 20),
        ('boll_period', 20),
        ('boll_stddev', 2.0),
        ('rsi_oversold', 30),
        ('exit_rsi', 50),
        ('open_cost', 0.001),
        ('close_cost', 0.001),
        ('interval', '1d'),
        ('trade_log', None),
    )

    def __init__(self):
        self.fast_ema = bt.indicators.EMA(self.data.close, period=self.params.fast_period)
        self.slow_ema = bt.indicators.EMA(self.data.close, period=self.params.slow_period)
        self.atr = bt.indicators.ATR(self.data, period=self.params.atr_period)
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)
        self.crossover = bt.indicators.CrossOver(self.fast_ema, self.slow_ema)
        self.stop_price = None

    def next(self):
        if not self.position:
            if self.crossover > 0 and self.rsi < self.params.rsi_overbought:
                self.buy()
                self.stop_price = self.data.close[0] - (self.params.atr_multiplier * self.atr[0])
        else:
            trailing_stop = self.data.close[0] - (self.params.atr_multiplier * self.atr[0])
            self.stop_price = max(self.stop_price, trailing_stop) if self.stop_price is not None else trailing_stop
            if self.data.close[0] < self.stop_price or self.crossover < 0 or self.rsi > self.params.rsi_overbought:
                self.sell()
                self.stop_price = None


class MomentumBreakoutStrategy(BaseTradeStrategy):
    params = (
        ('breakout_period', 20),
        ('atr_period', 14),
        ('atr_multiplier', 2.0),
        ('rsi_period', 14),
        ('rsi_overbought', 70),
        ('stop_loss', 0.05),
        ('fast_period', 9),
        ('slow_period', 21),
        ('boll_period', 20),
        ('boll_stddev', 2.0),
        ('rsi_oversold', 30),
        ('exit_rsi', 50),
        ('open_cost', 0.001),
        ('close_cost', 0.001),
        ('interval', '1d'),
        ('trade_log', None),
    )

    def __init__(self):
        self.highest = bt.indicators.Highest(self.data.high(-1), period=self.params.breakout_period)
        self.atr = bt.indicators.ATR(self.data, period=self.params.atr_period)
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)
        self.stop_price = None

    def next(self):
        if not self.position:
            if self.data.close[0] > self.highest[0] and self.rsi < self.params.rsi_overbought:
                self.buy()
                self.stop_price = self.data.close[0] - (self.params.atr_multiplier * self.atr[0])
        else:
            trailing_stop = self.data.close[0] - (self.params.atr_multiplier * self.atr[0])
            self.stop_price = max(self.stop_price, trailing_stop) if self.stop_price is not None else trailing_stop
            if self.data.close[0] < self.stop_price or self.rsi > self.params.rsi_overbought:
                self.sell()
                self.stop_price = None


class MeanReversionStrategy(BaseTradeStrategy):
    params = (
        ('boll_period', 20),
        ('boll_stddev', 2.0),
        ('rsi_period', 14),
        ('rsi_oversold', 30),
        ('exit_rsi', 50),
        ('stop_loss', 0.05),
        ('fast_period', 9),
        ('slow_period', 21),
        ('atr_period', 14),
        ('atr_multiplier', 2.0),
        ('breakout_period', 20),
        ('rsi_overbought', 70),
        ('open_cost', 0.001),
        ('close_cost', 0.001),
        ('interval', '1d'),
        ('trade_log', None),
    )

    def __init__(self):
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)
        self.boll = bt.indicators.BollingerBands(self.data.close, period=self.params.boll_period, devfactor=self.params.boll_stddev)
        self.stop_price = None

    def next(self):
        if not self.position:
            if self.data.close[0] < self.boll.bot[0] and self.rsi < self.params.rsi_oversold:
                self.buy()
                self.stop_price = self.data.close[0] * (1 - self.params.stop_loss)
        else:
            if self.data.close[0] > self.boll.mid[0] or self.rsi > self.params.exit_rsi or self.data.close[0] < self.stop_price:
                self.sell()
                self.stop_price = None


class ConfirmedCrossoverStrategy(BaseTradeStrategy):
    """Mirrors the realtime logic: fresh EMA crossover + RSI momentum zone + volume confirmation + ATR stop/target."""
    params = (
        ('fast_period', 9),
        ('slow_period', 21),
        ('rsi_period', 14),
        ('atr_period', 14),
        ('atr_multiplier', 2.0),
        ('rsi_overbought', 70),
        ('rsi_momentum_low', 45),
        ('rsi_momentum_high', 65),
        ('volume_factor', 1.2),
        # unused — kept for param compatibility with the GUI
        ('stop_loss', 0.05),
        ('breakout_period', 20),
        ('boll_period', 20),
        ('boll_stddev', 2.0),
        ('rsi_oversold', 30),
        ('exit_rsi', 50),
        ('open_cost', 0.001),
        ('close_cost', 0.001),
        ('interval', '1d'),
        ('trade_log', None),
    )

    def __init__(self):
        self.fast_ema = bt.indicators.EMA(self.data.close, period=self.params.fast_period)
        self.slow_ema = bt.indicators.EMA(self.data.close, period=self.params.slow_period)
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)
        self.atr = bt.indicators.ATR(self.data, period=self.params.atr_period)
        self.crossover = bt.indicators.CrossOver(self.fast_ema, self.slow_ema)
        self.avg_volume = bt.indicators.SMA(self.data.volume, period=20)
        self.stop_price = None
        self.target_price = None

    def next(self):
        if not self.position:
            rsi_in_zone = self.params.rsi_momentum_low <= self.rsi[0] <= self.params.rsi_momentum_high
            volume_confirmed = self.data.volume[0] > self.avg_volume[0] * self.params.volume_factor
            price_in_trend = self.data.close[0] > self.slow_ema[0]

            if self.crossover > 0 and rsi_in_zone and volume_confirmed and price_in_trend:
                self.buy()
                self.stop_price = self.data.close[0] - self.atr[0]
                self.target_price = self.data.close[0] + (self.atr[0] * self.params.atr_multiplier)
        else:
            hit_stop = self.data.close[0] <= self.stop_price
            hit_target = self.data.close[0] >= self.target_price
            rsi_exit = self.rsi[0] > self.params.rsi_overbought
            bearish_cross = self.crossover < 0

            if bearish_cross or rsi_exit or hit_stop or hit_target:
                self.sell()
                self.stop_price = None
                self.target_price = None


def get_strategy_class(name):
    # Import from strategies package to avoid duplication
    from strategies import get_strategy_class as _pkg_get
    from strategies import VWAPReclaimStrategy  # noqa: F401
    cls = _pkg_get(name)
    if cls is not None:
        return cls
    # Fallback to locally-defined strategies (confirmed_crossover etc.)
    strategies = {
        'ema_rsi': EMARSICrossoverStrategy,
        'trend_atr': TrendFollowingATRStrategy,
        'momentum_breakout': MomentumBreakoutStrategy,
        'mean_reversion': MeanReversionStrategy,
        'confirmed_crossover': ConfirmedCrossoverStrategy,
    }
    return strategies.get(name)


class RealTimeMonitor:
    def __init__(self, tickers, strategy_name, strategy_params, open_cost=0.001, close_cost=0.001, alert_email=None):
        self.tickers = tickers
        self.strategy_name = strategy_name
        self.strategy_params = strategy_params
        self.open_cost = open_cost
        self.close_cost = close_cost
        self.alert_email = alert_email
        self.positions = {}  # ticker: {'entry_price': float, 'quantity': int}
        self.running = False
        self.thread = None

    def send_alert(self, message):
        if self.alert_email:
            try:
                msg = MIMEText(message)
                msg['Subject'] = 'Trading Alert'
                msg['From'] = 'your_email@example.com'  # Replace with your email
                msg['To'] = self.alert_email

                server = smtplib.SMTP('smtp.gmail.com', 587)  # Example for Gmail
                server.starttls()
                server.login('your_email@example.com', 'your_password')  # Replace with credentials
                server.sendmail('your_email@example.com', self.alert_email, msg.as_string())
                server.quit()
                print(f"Alert sent: {message}")
            except Exception as e:
                print(f"Failed to send alert: {e}")
        else:
            print(f"ALERT: {message}")

    def analyze_ticker(self, ticker):
        try:
            # Fetch latest data (last 1 day, 1m interval for real-time)
            data = yf.download(ticker, period='1d', interval='1m')
            if data.empty:
                return

            # Create a simple cerebro instance for analysis
            cerebro = bt.Cerebro()
            strategy_cls = get_strategy_class(self.strategy_name)
            cerebro.addstrategy(
                strategy_cls,
                open_cost=self.open_cost,
                close_cost=self.close_cost,
                interval='1m',
                trade_log=[],
                **self.strategy_params,
            )

            data_feed = bt.feeds.PandasData(dataname=data)
            cerebro.adddata(data_feed)
            cerebro.run()

            # Simple buy/sell logic based on strategy signals
            # For demo, check if fast EMA > slow EMA and RSI < threshold for buy
            # This is simplified; in real implementation, use strategy's next() logic
            fast_ema = data['Close'].ewm(span=self.strategy_params.get('fast_period', 9)).mean().iloc[-1]
            slow_ema = data['Close'].ewm(span=self.strategy_params.get('slow_period', 21)).mean().iloc[-1]
            rsi = bt.indicators.RSI(data['Close'], period=self.strategy_params.get('rsi_period', 14)).rsi.iloc[-1]
            current_price = data['Close'].iloc[-1]

            if ticker not in self.positions:
                # Check buy condition
                if fast_ema > slow_ema and rsi < self.strategy_params.get('rsi_overbought', 70):
                    self.send_alert(f"BUY ALERT: {ticker} at ${current_price:.2f}")
                    # In real trading, place order here
                    self.positions[ticker] = {'entry_price': current_price, 'quantity': 1}  # Assume 1 share for demo
            else:
                # Check sell condition
                if fast_ema < slow_ema or rsi > self.strategy_params.get('rsi_overbought', 70):
                    entry_price = self.positions[ticker]['entry_price']
                    pnl = (current_price - entry_price) * self.positions[ticker]['quantity']
                    self.send_alert(f"SELL ALERT: {ticker} at ${current_price:.2f}, PnL: ${pnl:.2f}")
                    # In real trading, place sell order here
                    del self.positions[ticker]

        except Exception as e:
            print(f"Error analyzing {ticker}: {e}")

    def run(self):
        self.running = True
        while self.running:
            for ticker in self.tickers:
                self.analyze_ticker(ticker)
            time.sleep(1)  # Analyze every second

    def start(self):
        if not self.running:
            self.thread = threading.Thread(target=self.run)
            self.thread.start()
            print("Real-time monitoring started.")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
        print("Real-time monitoring stopped.")


def download_price_data(ticker, interval='1d', period='60d', start_date=None, end_date=None):
    # Use Ticker.history() instead of yf.download() — thread-safe for parallel backtests
    t = yf.Ticker(ticker)
    if start_date and end_date:
        data = t.history(start=start_date, end=end_date, interval=interval)
        source_note = f'Data from {start_date} to {end_date}'
    elif interval == '1d':
        start_date = '2020-01-01'
        end_date = datetime.now().strftime('%Y-%m-%d')
        data = t.history(start=start_date, end=end_date, interval=interval)
        source_note = f'Daily data from {start_date} to {end_date}'
    else:
        data = t.history(period=period, interval=interval)
        source_note = f'Intraday {interval} data for last {period}'

    if data.empty:
        return None, source_note

    data = data[['Open', 'High', 'Low', 'Close', 'Volume']]
    data.columns = ['open', 'high', 'low', 'close', 'volume']
    data.index.name = 'datetime'
    return data, source_note


def run_backtest(
    ticker='QQQ',
    strategy_name='ema_rsi',
    strategy_params=None,
    open_cost=0.001,
    close_cost=0.001,
    interval='1d',
    period='60d',
    data=None,
    start_cash=1000.0,
    start_date=None,
    end_date=None,
):
    strategy_params = strategy_params or {}
    trade_log = []
    cerebro = bt.Cerebro()
    strategy_cls = get_strategy_class(strategy_name)
    if strategy_cls is None:
        return {'error': f'Unknown strategy: {strategy_name}'}

    cerebro.addstrategy(
        strategy_cls,
        open_cost=open_cost,
        close_cost=close_cost,
        interval=interval,
        trade_log=trade_log,
        **strategy_params,
    )

    if data is None:
        data, source_note = download_price_data(ticker, interval=interval, period=period, start_date=start_date, end_date=end_date)
        if data is None:
            return {
                'error': f'No data available for {ticker} with interval={interval}. Try daily or a different ticker.',
            }
    else:
        source_note = f'Provided data for {ticker} with interval={interval} ({len(data)} bars)'

    data_feed = bt.feeds.PandasData(dataname=data)
    cerebro.adddata(data_feed)

    cerebro.broker.setcash(start_cash)
    cerebro.addsizer(bt.sizers.PercentSizer, percents=100)

    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='time_return', timeframe=bt.TimeFrame.Months)
    cerebro.addanalyzer(bt.analyzers.TimeReturn, _name='yearly_return', timeframe=bt.TimeFrame.Years)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trade')

    results = cerebro.run()

    summary = []
    summary.append(f'Starting Portfolio Value: ${cerebro.broker.startingcash:.2f}')
    summary.append(f'Final Portfolio Value: ${cerebro.broker.getvalue():.2f}')
    total_return = ((cerebro.broker.getvalue() / cerebro.broker.startingcash) - 1) * 100
    summary.append(f'Total Return: {total_return:.2f}%')
    summary.append(source_note)
    summary.append(f"Strategy: {strategy_name.replace('_', ' ').title()}")

    if strategy_params:
        summary.append('Strategy parameters: ' + ', '.join([f'{k}={v}' for k, v in strategy_params.items()]))

    time_returns = results[0].analyzers.time_return.get_analysis()
    monthly_returns = list(time_returns.values())
    avg_monthly_return = None
    if monthly_returns:
        avg_monthly_return = (sum(monthly_returns) / len(monthly_returns)) * 100
        summary.append(f'Average Monthly Return: {avg_monthly_return:.2f}%')
    else:
        summary.append('No monthly returns data available.')

    yearly_returns = results[0].analyzers.yearly_return.get_analysis()
    yearly_results = []
    if yearly_returns:
        for year, ret in yearly_returns.items():
            yearly_results.append({'year': year, 'return': ret * 100})

    returns_analyzer = results[0].analyzers.returns.get_analysis()
    annual_return = returns_analyzer.get('rnorm100', 0)
    summary.append(f'Annual Return: {annual_return:.2f}%')

    drawdown_analyzer = results[0].analyzers.drawdown.get_analysis()
    max_drawdown = drawdown_analyzer.max.drawdown
    summary.append(f'Max Drawdown: {max_drawdown:.2f}%')

    sharpe_analyzer = results[0].analyzers.sharpe.get_analysis()
    sharperatio = sharpe_analyzer.get('sharperatio')
    summary.append(f'Sharpe Ratio: {sharperatio if sharperatio is not None else 0:.2f}')

    trade_analyzer = results[0].analyzers.trade.get_analysis()
    total_trades = 0
    winning_trades = 0
    losing_trades = 0
    try:
        total_trades = trade_analyzer.total.total
    except Exception:
        total_trades = 0
    try:
        winning_trades = trade_analyzer.won.total
    except Exception:
        winning_trades = 0
    try:
        losing_trades = trade_analyzer.lost.total
    except Exception:
        losing_trades = 0

    summary.append(f'Total Trades: {total_trades}')
    summary.append(f'Winning Trades: {winning_trades}')
    summary.append(f'Losing Trades: {losing_trades}')

    return {
        'ticker': ticker,
        'trading_hours': '9:30 AM EST to 3:55 PM EST',
        'summary': summary,
        'average_monthly_return': avg_monthly_return,
        'yearly_returns': yearly_results,
        'annual_return': annual_return,
        'max_drawdown': max_drawdown,
        'sharpe': sharperatio if sharperatio is not None else 0,
        'total_return': total_return,
        'strategy_name': strategy_name,
        'strategy_params': strategy_params,
        'trade_summary': {
            'total': total_trades,
            'won': winning_trades,
            'lost': losing_trades,
        },
        'trades': trade_log,
    }


def get_default_grid(strategy_name):
    """Return a compact but meaningful optimization grid for each strategy."""
    grids = {
        'confirmed_crossover': {
            'fast_period': [7, 9, 12],
            'slow_period': [18, 21, 26],
            'rsi_momentum_low': [35, 42, 48],
            'rsi_momentum_high': [60, 65, 70],
            'atr_multiplier': [1.5, 2.0, 2.5],
            'volume_factor': [1.0, 1.1, 1.2],
        },
        'ema_rsi': {
            'fast_period': [7, 9, 12],
            'slow_period': [18, 21, 26],
            'rsi_overbought': [65, 70, 75],
            'stop_loss': [0.03, 0.05, 0.07],
        },
        'trend_atr': {
            'fast_period': [7, 9, 12],
            'slow_period': [18, 21, 26],
            'atr_multiplier': [1.5, 2.0, 2.5],
            'rsi_overbought': [65, 70, 75],
        },
        'momentum_breakout': {
            'breakout_period': [15, 20, 25],
            'atr_multiplier': [1.5, 2.0, 2.5],
            'rsi_overbought': [65, 70, 75],
        },
        'mean_reversion': {
            'boll_period': [15, 20, 25],
            'boll_stddev': [1.5, 2.0, 2.5],
            'rsi_oversold': [25, 30, 35],
            'exit_rsi': [48, 52, 55],
        },
    }
    return grids.get(strategy_name, {})


def optimize_strategy(
    ticker='QQQ',
    strategy_name='ema_rsi',
    strategy_grid=None,
    open_cost=0.001,
    close_cost=0.001,
    interval='1d',
    period='60d',
    top_n=5,
    start_date=None,
    end_date=None,
    min_trades=3,
):
    strategy_grid = strategy_grid or get_default_grid(strategy_name) or {
        'fast_period': [9],
        'slow_period': [21],
        'rsi_overbought': [70],
        'stop_loss': [0.05],
    }

    data, source_note = download_price_data(ticker, interval=interval, period=period,
                                             start_date=start_date, end_date=end_date)
    if data is None:
        return {
            'error': f'No data available for {ticker} with interval={interval}. Try daily or a different ticker.',
        }

    results = []
    total_combinations = 0
    grid_keys = list(strategy_grid.keys())
    grid_values = [strategy_grid[key] for key in grid_keys]

    for combo in product(*grid_values):
        params = dict(zip(grid_keys, combo))
        if 'fast_period' in params and 'slow_period' in params and params['slow_period'] <= params['fast_period']:
            continue

        total_combinations += 1
        backtest_result = run_backtest(
            ticker=ticker,
            strategy_name=strategy_name,
            strategy_params=params,
            open_cost=open_cost,
            close_cost=close_cost,
            interval=interval,
            period=period,
            data=data,
        )
        if 'error' in backtest_result:
            continue

        trades = backtest_result['trade_summary']['total']
        if trades < min_trades:
            continue  # skip param combos that barely trade — likely overfitting

        sharpe = backtest_result['sharpe'] or 0
        total_ret = backtest_result['total_return']
        # Score: reward return but penalise drawdown and require positive sharpe
        score = total_ret * max(sharpe, 0.1)

        results.append({
            'strategy_params': backtest_result['strategy_params'],
            'total_return': total_ret,
            'sharpe': sharpe,
            'max_drawdown': backtest_result['max_drawdown'],
            'yearly_returns': backtest_result['yearly_returns'],
            'trade_summary': backtest_result['trade_summary'],
            'trade_count': trades,
            'score': score,
        })

    results.sort(key=lambda row: row['score'], reverse=True)
    top_results = results[:top_n]

    return {
        'ticker': ticker,
        'strategy_name': strategy_name,
        'source_note': source_note,
        'total_combinations': total_combinations,
        'top_results': top_results,
    }


def format_backtest_text(result):
    lines = []
    lines.extend(result['summary'])
    if result['yearly_returns']:
        lines.append('\nYearly Returns:')
        for row in result['yearly_returns']:
            lines.append(f"{row['year']}: {row['return']:.2f}%")
    if result['trades']:
        lines.append('\nIndividual Trades:')
        for trade in result['trades']:
            lines.append(
                f"Trade: Opened {trade['dtopen']} at ${trade['entry_price']:.2f}, Closed {trade['dtclose']} at ${trade['exit_price']:.2f}, "
                f"Gross PnL: ${trade['gross_pnl']:.2f}, Net PnL: ${trade['net_pnl']:.2f}, "
                f"Open Cost: ${trade['open_cost']:.2f}, Close Cost: ${trade['close_cost']:.2f}, Total Cost: ${trade['total_cost']:.2f}"
            )
    return '\n'.join(lines)

def optimize_then_compound(
    ticker='QQQ',
    strategy_name='confirmed_crossover',
    open_cost=0.001,
    close_cost=0.001,
    interval='1d',
    initial_cash=1000.0,
    start_year=2023,
    train_start='2020-01-01',
    train_end='2022-12-31',
):
    """
    Step 1 — Optimize params on training data (train_start to train_end, no lookahead).
    Step 2 — Run compounding backtest from start_year using those best params.
    Returns both the best params found and the compounding result.
    """
    opt = optimize_strategy(
        ticker=ticker,
        strategy_name=strategy_name,
        open_cost=open_cost,
        close_cost=close_cost,
        interval=interval,
        start_date=train_start,
        end_date=train_end,
        top_n=1,
        min_trades=3,
    )

    if 'error' in opt or not opt['top_results']:
        # Fall back to default params if optimization finds nothing
        best_params = {}
    else:
        best_params = opt['top_results'][0]['strategy_params']

    compound_result = run_compounding_backtest(
        ticker=ticker,
        strategy_name=strategy_name,
        strategy_params=best_params,
        open_cost=open_cost,
        close_cost=close_cost,
        interval=interval,
        start_year=start_year,
        initial_cash=initial_cash,
    )
    compound_result['optimized_params'] = best_params
    compound_result['train_period'] = f'{train_start} to {train_end}'
    return compound_result


def run_compounding_backtest(
    ticker='QQQ',
    strategy_name='confirmed_crossover',
    strategy_params=None,
    open_cost=0.001,
    close_cost=0.001,
    interval='1d',
    start_year=2023,
    initial_cash=1000.0,
):
    """
    Run year-by-year compounding backtest from start_year to current year.
    Each year's ending value becomes the next year's starting capital.
    Returns a list of per-year results plus a final summary.
    """
    today = datetime.now()
    current_year = today.year
    years = list(range(start_year, current_year + 1))

    cash = initial_cash
    yearly_results = []

    for year in years:
        year_start = f'{year}-01-01'
        year_end = f'{year}-12-31' if year < current_year else today.strftime('%Y-%m-%d')

        result = run_backtest(
            ticker=ticker,
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            open_cost=open_cost,
            close_cost=close_cost,
            interval=interval,
            start_cash=cash,
            start_date=year_start,
            end_date=year_end,
        )

        if 'error' in result:
            yearly_results.append({
                'year': year,
                'start_value': round(cash, 2),
                'end_value': round(cash, 2),
                'return_pct': 0.0,
                'profit': 0.0,
                'trades': 0,
                'won': 0,
                'lost': 0,
                'win_rate': 0.0,
                'error': result['error'],
            })
            continue

        end_value = cash * (1 + result['total_return'] / 100)
        profit = end_value - cash
        total_trades = result['trade_summary']['total']
        won = result['trade_summary']['won']

        yearly_results.append({
            'year': f"{year} (YTD)" if year == current_year else str(year),
            'start_value': round(cash, 2),
            'end_value': round(end_value, 2),
            'return_pct': round(result['total_return'], 2),
            'profit': round(profit, 2),
            'trades': total_trades,
            'won': won,
            'lost': result['trade_summary']['lost'],
            'win_rate': round(won / total_trades * 100, 1) if total_trades > 0 else 0.0,
        })

        cash = end_value  # compound into next year

    total_gain = cash - initial_cash
    total_return_pct = (cash / initial_cash - 1) * 100

    return {
        'ticker': ticker,
        'strategy': strategy_name,
        'initial_cash': initial_cash,
        'final_value': round(cash, 2),
        'total_gain': round(total_gain, 2),
        'total_return_pct': round(total_return_pct, 2),
        'yearly': yearly_results,
    }


def run_batch_compounding_backtest(
    tickers=None,
    strategy_name='confirmed_crossover',
    strategy_params=None,
    open_cost=0.001,
    close_cost=0.001,
    interval='1d',
    start_year=2023,
    initial_cash=1000.0,
    max_workers=10,
    progress_callback=None,
):
    """Run compounding year-by-year backtest for all tickers in parallel."""
    tickers = tickers or POPULAR_TICKERS
    results = []
    errors = []
    completed = 0
    total = len(tickers)

    def compound_one(ticker):
        return run_compounding_backtest(
            ticker=ticker,
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            open_cost=open_cost,
            close_cost=close_cost,
            interval=interval,
            start_year=start_year,
            initial_cash=initial_cash,
        )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(compound_one, t): t for t in tickers}
        for future in as_completed(futures):
            ticker = futures[future]
            completed += 1
            if progress_callback:
                progress_callback(completed, total, ticker)
            try:
                result = future.result()
                results.append({
                    'ticker': result['ticker'],
                    'initial': result['initial_cash'],
                    'final_value': result['final_value'],
                    'total_gain': result['total_gain'],
                    'total_return_pct': result['total_return_pct'],
                    'yearly': result['yearly'],
                })
            except Exception as e:
                errors.append({'ticker': ticker, 'error': str(e)})

    results.sort(key=lambda x: x['final_value'], reverse=True)
    return {'results': results, 'errors': errors, 'total': total}


def run_batch_backtest(
    tickers=None,
    strategy_name='confirmed_crossover',
    strategy_params=None,
    open_cost=0.001,
    close_cost=0.001,
    interval='1d',
    period='60d',
    max_workers=10,
    progress_callback=None,
):
    """Run backtest for a list of tickers in parallel. Returns results sorted by total return."""
    tickers = tickers or POPULAR_TICKERS
    strategy_params = strategy_params or {}
    results = []
    errors = []
    completed = 0
    total = len(tickers)

    def backtest_one(ticker):
        result = run_backtest(
            ticker=ticker,
            strategy_name=strategy_name,
            strategy_params=strategy_params,
            open_cost=open_cost,
            close_cost=close_cost,
            interval=interval,
            period=period,
        )
        return ticker, result

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(backtest_one, t): t for t in tickers}
        for future in as_completed(futures):
            ticker, result = future.result()
            completed += 1
            if progress_callback:
                progress_callback(completed, total, ticker)
            if 'error' in result:
                errors.append({'ticker': ticker, 'error': result['error']})
                continue
            total_trades = result['trade_summary']['total']
            won = result['trade_summary']['won']
            win_rate = (won / total_trades * 100) if total_trades > 0 else 0.0
            results.append({
                'ticker': ticker,
                'total_return': round(result['total_return'], 2),
                'annual_return': round(result.get('annual_return', 0), 2),
                'sharpe': round(result['sharpe'], 2),
                'max_drawdown': round(result['max_drawdown'], 2),
                'total_trades': total_trades,
                'won': won,
                'lost': result['trade_summary']['lost'],
                'win_rate': round(win_rate, 1),
            })

    results.sort(key=lambda x: x['total_return'], reverse=True)
    return {'results': results, 'errors': errors, 'total': total}


if __name__ == '__main__':
    results = run_backtest()
    print(format_backtest_text(results))