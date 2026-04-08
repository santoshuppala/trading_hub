import backtrader as bt
import yfinance as yf
import pandas as pd
import requests as http
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from itertools import product
import sys
import time
import threading
import smtplib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.text import MIMEText
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame

ET = ZoneInfo('America/New_York')
ALPACA_DATA_URL = 'https://data.alpaca.markets'

class BaseTradeStrategy(bt.Strategy):
    params = (
        ('open_cost', 0.001),
        ('close_cost', 0.001),
        ('interval', '1m'),
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
            dtopen_str = dtopen.strftime('%Y-%m-%d %H:%M:%S')
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
            dtclose_str = dtclose.strftime('%Y-%m-%d %H:%M:%S')
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
        ('interval', '1m'),
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
        ('interval', '1m'),
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
        ('interval', '1m'),
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
        ('interval', '1m'),
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


def get_strategy_class(name):
    strategies = {
        'ema_rsi': EMARSICrossoverStrategy,
        'trend_atr': TrendFollowingATRStrategy,
        'momentum_breakout': MomentumBreakoutStrategy,
        'mean_reversion': MeanReversionStrategy,
    }
    return strategies.get(name)


class RealTimeMonitor:
    # Alpaca is commission-free; model friction as one-way slippage only
    SLIPPAGE_PCT  = 0.0001   # 0.01% per side
    MAX_SPREAD_PCT = 0.002   # skip entry if bid/ask spread > 0.2%

    def __init__(self, tickers, strategy_name, strategy_params, open_cost=0.0, close_cost=0.0, alert_email=None, alpaca_api_key=None, alpaca_secret_key=None, paper=True, max_positions=5, order_cooldown=300, per_ticker_params=None):
        self.base_tickers = list(tickers)       # fixed watchlist
        self.tickers = list(tickers)            # active list (base + dynamic momentum)
        self.strategy_name = strategy_name
        self.strategy_params = strategy_params
        # per_ticker_params: {ticker: {param_dict}} — overrides strategy_params per ticker
        self.per_ticker_params = per_ticker_params or {}
        self.open_cost = open_cost    # kept for backtest compatibility
        self.close_cost = close_cost
        self.alert_email = alert_email
        self.positions = {}       # ticker -> position dict
        self.trade_log = []       # completed trades for today's summary
        self.reclaimed_today = set()  # tickers that already had a reclaim today (no double-dip)
        self._bars_cache = {}     # today's bars: {ticker: DataFrame}
        self._rvol_cache = {}     # historical bars for RVOL: {ticker: DataFrame}
        self._last_momentum_refresh = None   # datetime of last screener refresh
        self.running = False
        self.thread = None
        self.trading_client = None
        self.max_positions = max_positions
        self.order_cooldown = order_cooldown
        self.last_order_time = {}
        self._last_reset_date = datetime.now().date()  # for daily state reset
        
        # Use environment variables as fallback if keys not provided
        if not alpaca_api_key:
            alpaca_api_key = os.getenv("APCA_API_KEY_ID")
        if not alpaca_secret_key:
            alpaca_secret_key = os.getenv("APCA_API_SECRET_KEY")
        
        # Store keys for screener HTTP calls
        self._api_key    = alpaca_api_key    or os.getenv('APCA_API_KEY_ID', '')
        self._api_secret = alpaca_secret_key or os.getenv('APCA_API_SECRET_KEY', '')

        if self._api_key and self._api_secret:
            self.trading_client = TradingClient(self._api_key, self._api_secret, paper=paper)
            self.data_client = StockHistoricalDataClient(self._api_key, self._api_secret)
            print("Alpaca trading client initialized.")
        else:
            self.trading_client = None
            self.data_client = StockHistoricalDataClient()  # unauthenticated (free IEX data)
            print("Warning: Alpaca keys not provided. Orders will be skipped.")

    def send_alert(self, message):
        if self.alert_email:
            email_user = os.getenv('ALERT_EMAIL_USER')
            email_pass = os.getenv('ALERT_EMAIL_PASS')
            email_from = os.getenv('ALERT_EMAIL_FROM') or email_user
            smtp_server = os.getenv('ALERT_EMAIL_SERVER') or 'smtp.mail.yahoo.com'
            smtp_port = int(os.getenv('ALERT_EMAIL_PORT') or 587)

            if not email_user or not email_pass or not email_from:
                print('Alert skipped: missing SMTP settings. Set ALERT_EMAIL_USER, ALERT_EMAIL_PASS, and ALERT_EMAIL_FROM.')
                print(f'ALERT: {message}')
                return

            try:
                msg = MIMEText(message)
                msg['Subject'] = 'Trading Alert'
                msg['From'] = email_from
                msg['To'] = self.alert_email

                server = smtplib.SMTP(smtp_server, smtp_port)
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(email_user, email_pass)
                server.sendmail(email_from, self.alert_email, msg.as_string())
                server.quit()
                print(f"Alert sent: {message}")
            except smtplib.SMTPAuthenticationError as e:
                print(f"Failed to send alert: SMTP authentication failed: {e}")
                print('Check ALERT_EMAIL_USER and ALERT_EMAIL_PASS, and if using Gmail enable an App Password.')
            except Exception as e:
                print(f"Failed to send alert: {e}")
        else:
            print(f"ALERT: {message}")

    # Marketable limit: how far above ask we're willing to pay (0.05%)
    LIMIT_BUFFER_PCT = 0.0005

    def place_order(self, ticker, side, qty, ref_price=None):
        """
        Places a marketable limit order when ref_price is provided (buys),
        or a plain market order for sells (speed > precision on exits).
        Marketable limit = limit set slightly above ask so it fills immediately
        but rejects if price spikes beyond our tolerance before fill.
        """
        if not self.trading_client:
            self.send_alert(f"Order skipped (no Alpaca client): {side} {qty} {ticker}")
            return None
        try:
            if side == OrderSide.BUY and ref_price:
                limit_price = round(ref_price * (1 + self.LIMIT_BUFFER_PCT), 2)
                order_request = LimitOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=side,
                    limit_price=limit_price,
                    time_in_force=TimeInForce.DAY,
                )
                order = self.trading_client.submit_order(order_request)
                self.send_alert(f"Order placed: BUY {qty} {ticker} limit ${limit_price:.2f} (ask ~${ref_price:.2f})")
            else:
                # Sells use market orders — exit speed is critical
                order_request = MarketOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                )
                order = self.trading_client.submit_order(order_request)
                self.send_alert(f"Order placed: SELL {qty} {ticker} at market")
            return order.id
        except Exception as e:
            self.send_alert(f"Order failed: {side} {qty} {ticker} - {e}")
            return None

    def _refresh_momentum_tickers(self):
        """
        Fetch high-momentum stocks from Alpaca screener API and merge into self.tickers.
        Runs once at market open, then refreshes every 30 minutes.
        Sources:
          - Top 50 most active stocks by volume
          - Top 20 gainers (movers)
        Filters: symbol is alpha-only, length ≤ 5 chars (no warrants/preferred),
                 not already in base list.
        """
        now = datetime.now(ET)
        # Only refresh during market hours
        if not ((now.hour == 9 and now.minute >= 30) or (10 <= now.hour <= 14)):
            return
        # Refresh at open or every 30 minutes
        if self._last_momentum_refresh is not None:
            elapsed = (now - self._last_momentum_refresh).total_seconds()
            if elapsed < 1800:
                return

        headers = {
            'APCA-API-KEY-ID':     self._api_key,
            'APCA-API-SECRET-KEY': self._api_secret,
        }
        candidates = set()

        # Most active by volume
        try:
            r = http.get(
                f'{ALPACA_DATA_URL}/v1beta1/screener/stocks/most-actives',
                params={'by': 'volume', 'top': 50},
                headers=headers,
                timeout=10,
            )
            if r.ok:
                for stock in r.json().get('most_actives', []):
                    candidates.add(stock['symbol'])
        except Exception as e:
            print(f"Screener most-actives error: {e}")

        # Top gainers
        try:
            r = http.get(
                f'{ALPACA_DATA_URL}/v1beta1/screener/stocks/movers',
                params={'market_type': 'stocks', 'top': 20},
                headers=headers,
                timeout=10,
            )
            if r.ok:
                for stock in r.json().get('gainers', []):
                    candidates.add(stock['symbol'])
        except Exception as e:
            print(f"Screener movers error: {e}")

        # Filter: valid US equity symbols only
        candidates = [
            s for s in candidates
            if s.isalpha() and len(s) <= 5 and s not in self.base_tickers
        ]

        # Relative Strength filter: keep only stocks outperforming SPY over last 5 trading days
        rs_passed = self._filter_by_relative_strength(candidates)

        if rs_passed:
            added = [s for s in rs_passed if s not in self.tickers]
            self.tickers = list(self.base_tickers) + rs_passed
            if added:
                print(f"[Momentum] Added {len(added)} RS-filtered tickers: {', '.join(added)}")
                print(f"[Momentum] Total scan list: {len(self.tickers)} tickers")
        else:
            self.tickers = list(self.base_tickers)

        self._last_momentum_refresh = now

    def _filter_by_relative_strength(self, candidates):
        """
        Keep only candidates whose 5-day return > SPY's 5-day return.
        Uses daily bars to avoid noise from intraday data.
        Returns filtered list; returns all candidates if data unavailable.
        """
        if not candidates:
            return candidates
        try:
            symbols = list(set(candidates + ['SPY']))
            start = datetime.now(ET) - timedelta(days=8)  # 8 calendar days → ~5 trading days
            req = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=start,
                feed='iex',
            )
            bars = self.data_client.get_stock_bars(req).df
            if bars.empty or not isinstance(bars.index, pd.MultiIndex):
                return candidates

            def five_day_return(sym):
                try:
                    df = bars.xs(sym, level='symbol')
                    if len(df) < 2:
                        return None
                    return (df['close'].iloc[-1] / df['close'].iloc[0]) - 1
                except Exception:
                    return None

            spy_ret = five_day_return('SPY')
            if spy_ret is None:
                return candidates  # can't compare, let all through

            passed = []
            for sym in candidates:
                ret = five_day_return(sym)
                if ret is None or ret > spy_ret:  # None = no data, let through
                    passed.append(sym)

            print(f"[RS Filter] SPY 5d: {spy_ret:+.2%} | {len(passed)}/{len(candidates)} passed")
            return passed
        except Exception as e:
            print(f"RS filter error: {e}")
            return candidates  # fail open

    def _fetch_batch_bars(self):
        """
        Fetch today's 1-minute bars for ALL tickers in a single Alpaca API call.
        Stores results in self._bars_cache: {ticker: DataFrame}.
        Also fetches 10 calendar days of data for RVOL in self._rvol_cache.
        Skips if market has not opened yet (today_open is still in the future).
        """
        now = datetime.now(ET)
        today_open = now.replace(hour=9, minute=30, second=0, microsecond=0)

        # Pre-market: no bars exist yet — skip and return empty caches
        if now < today_open:
            print(f"[{now.strftime('%H:%M:%S')} ET] Pre-market — bars not yet available, waiting for 9:30 AM ET.")
            self._bars_cache = {}
            self._rvol_cache = {}
            return

        rvol_start  = (now - timedelta(days=10)).replace(hour=9, minute=30, second=0, microsecond=0)

        def _parse_multi(raw_df, tickers):
            result = {}
            if raw_df.empty:
                return result
            for tkr in tickers:
                try:
                    df = raw_df.xs(tkr, level='symbol').copy() if isinstance(raw_df.index, pd.MultiIndex) else raw_df.copy()
                    df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
                    result[tkr] = df
                except Exception:
                    pass
            return result

        # Today's bars (for analysis)
        try:
            req = StockBarsRequest(symbol_or_symbols=self.tickers, timeframe=TimeFrame.Minute, start=today_open, feed='iex')
            self._bars_cache = _parse_multi(self.data_client.get_stock_bars(req).df, self.tickers)
        except Exception as e:
            print(f"Batch bars error: {e}")
            self._bars_cache = {}

        # Historical bars for RVOL (last 10 calendar days)
        try:
            req = StockBarsRequest(symbol_or_symbols=self.tickers, timeframe=TimeFrame.Minute, start=rvol_start, feed='iex')
            self._rvol_cache = _parse_multi(self.data_client.get_stock_bars(req).df, self.tickers)
        except Exception as e:
            print(f"RVOL batch bars error: {e}")
            self._rvol_cache = {}

    def _get_bars(self, ticker, calendar_days=2):
        """Return cached bars for ticker, falling back to individual fetch if not cached."""
        # Use today cache for short windows, rvol cache for long windows
        cache = self._rvol_cache if calendar_days > 2 else self._bars_cache
        cached = cache.get(ticker) if hasattr(self, '_bars_cache') else None
        if cached is not None and not cached.empty:
            return cached
        # Fallback: individual fetch
        try:
            now = datetime.now(ET)
            start = (now - timedelta(days=calendar_days)).replace(hour=9, minute=30, second=0, microsecond=0)
            req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute, start=start, feed='iex')
            bars = self.data_client.get_stock_bars(req)
            df = bars.df
            if df.empty:
                return pd.DataFrame()
            if isinstance(df.index, pd.MultiIndex):
                df = df.xs(ticker, level='symbol') if ticker in df.index.get_level_values('symbol') else pd.DataFrame()
            df.index = pd.to_datetime(df.index, utc=True).tz_convert(ET)
            return df
        except Exception as e:
            print(f"Alpaca data error for {ticker}: {e}")
            return pd.DataFrame()

    def _check_spread(self, ticker):
        """
        Fetch Level 1 quote and return (spread_pct, ask_price).
        Returns (None, None) on error.
        Skips entry if spread > MAX_SPREAD_PCT (0.2%).
        """
        try:
            req = StockLatestQuoteRequest(symbol_or_symbols=ticker)
            quote = self.data_client.get_stock_latest_quote(req)[ticker]
            bid = float(quote.bid_price)
            ask = float(quote.ask_price)
            if bid <= 0 or ask <= 0:
                return None, None
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid
            return spread_pct, ask
        except Exception as e:
            print(f"Quote error for {ticker}: {e}")
            return None, None

    def _reset_daily_state(self):
        """Reset per-day tracking at the start of each new trading day."""
        today = datetime.now().date()
        if today != self._last_reset_date:
            self.reclaimed_today.clear()
            self._last_reset_date = today

    def _get_rvol(self, ticker, current_volume_sum, current_bar_index):
        """
        Relative Volume: compare cumulative volume up to this bar vs
        same bar index averaged over the last 5 trading days.
        Returns RVOL ratio (2.0 = twice the usual volume at this time of day).
        """
        try:
            hist = self._get_bars(ticker, calendar_days=10)
            if hist.empty:
                return 1.0
            today = datetime.now(ET).date()
            past_days = sorted({idx.date() for idx in hist.index if idx.date() < today})[-5:]
            avg_cum_vol = []
            for d in past_days:
                day_vol = hist.loc[hist.index.date == d, 'volume']
                if len(day_vol) >= current_bar_index:
                    avg_cum_vol.append(day_vol.iloc[:current_bar_index].sum())
            if not avg_cum_vol:
                return 1.0
            avg = sum(avg_cum_vol) / len(avg_cum_vol)
            return current_volume_sum / avg if avg > 0 else 1.0
        except Exception:
            return 1.0

    def _get_spy_vwap_bias(self):
        """Returns True if SPY is above its cumulative VWAP (market bullish)."""
        try:
            spy = self._get_bars('SPY', calendar_days=2)
            if spy.empty:
                return True
            today = datetime.now(ET).date()
            spy = spy[spy.index.date == today]
            if spy.empty:
                return True
            typical = (spy['high'] + spy['low'] + spy['close']) / 3
            vwap = (typical * spy['volume']).cumsum() / spy['volume'].cumsum()
            return float(spy['close'].iloc[-1]) > float(vwap.iloc[-1])
        except Exception:
            return True

    def analyze_ticker(self, ticker):
        try:
            self._reset_daily_state()

            now = datetime.now()
            hour, minute = now.hour, now.minute

            # Enhancement 5: Force-close all positions by 3:00 PM ET
            force_close = (hour == 15 and minute >= 0)
            if force_close and ticker in self.positions:
                data = self._get_bars(ticker, calendar_days=2)
                today = now.date()
                data = data[data.index.date == today] if not data.empty else data
                if not data.empty:
                    current_price = float(data['close'].iloc[-1])
                    pos = self.positions[ticker]
                    qty = pos['quantity'] + pos.get('partial_qty', 0)
                    if qty > 0:
                        order_id = self.place_order(ticker, OrderSide.SELL, qty)
                        if order_id:
                            pnl = (current_price - pos['entry_price']) * qty
                            self.send_alert(f"SELL (EOD close): {ticker} @ ${current_price:.2f} | PnL: ${pnl:.2f}")
                            self.trade_log.append({
                                'ticker': ticker, 'entry_price': pos['entry_price'],
                                'entry_time': pos.get('entry_time', ''),
                                'exit_price': current_price, 'qty': qty,
                                'pnl': round(pnl, 2), 'reason': 'EOD force close',
                                'time': now.strftime('%H:%M:%S'), 'is_win': pnl >= 0,
                            })
                            del self.positions[ticker]
                return

            # Enhancement 1 (trading hours): only trade 9:45 AM – 3:00 PM ET
            market_open = (hour == 9 and minute >= 45) or (10 <= hour <= 14)
            if not market_open:
                return

            data = self._get_bars(ticker, calendar_days=2)
            if data.empty:
                return
            today = now.date()
            data = data[data.index.date == today]
            if len(data) < 30:
                return

            close  = data['close']
            high   = data['high']
            low    = data['low']
            volume = data['volume']

            params         = {**self.strategy_params, **self.per_ticker_params.get(ticker, {})}
            rsi_period     = params.get('rsi_period', 14)
            atr_period     = params.get('atr_period', 14)
            rsi_overbought = params.get('rsi_overbought', 70)
            atr_mult       = params.get('atr_multiplier', 2.0)
            min_stop_pct   = params.get('min_stop_pct', 0.05)

            def to_scalar(val):
                try:
                    if hasattr(val, 'item'):
                        return float(val.item())
                    elif hasattr(val, 'iloc'):
                        v = val.iloc[0] if len(val) > 0 else None
                        return None if v is None else (float(v.item()) if hasattr(v, 'item') else float(v))
                    return float(val)
                except (ValueError, TypeError, AttributeError):
                    return None

            # VWAP
            typical = (high + low + close) / 3
            vwap          = (typical * volume).cumsum() / volume.cumsum()
            current_vwap  = to_scalar(vwap.iloc[-1])
            prev_vwap     = to_scalar(vwap.iloc[-2])
            current_price = to_scalar(close.iloc[-1])
            prev_price    = to_scalar(close.iloc[-2])
            price_3       = to_scalar(close.iloc[-3])
            vwap_3        = to_scalar(vwap.iloc[-3])
            price_4       = to_scalar(close.iloc[-4])  # for 2-bar confirmation
            vwap_4        = to_scalar(vwap.iloc[-4])

            # RSI
            delta     = close.diff()
            gain      = delta.where(delta > 0, 0).rolling(window=rsi_period).mean()
            loss      = (-delta.where(delta < 0, 0)).rolling(window=rsi_period).mean()
            rsi_value = to_scalar((100 - (100 / (1 + gain / loss))).iloc[-1])

            # ATR
            tr = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low  - close.shift()).abs(),
            ], axis=1).max(axis=1)
            atr_value = to_scalar(tr.rolling(window=atr_period).mean().iloc[-1])

            # Enhancement 3: Opening range bias — first bar must have opened above VWAP
            open_price  = to_scalar(data['open'].iloc[0])
            open_vwap   = to_scalar(vwap.iloc[0])
            opened_above_vwap = open_price is not None and open_vwap is not None and open_price > open_vwap

            if any(v is None or (isinstance(v, float) and v != v) for v in [
                current_price, current_vwap, prev_price, prev_vwap,
                price_3, vwap_3, price_4, vwap_4,
                rsi_value, atr_value
            ]):
                return

            # Enhancement 7: 2-bar reclaim confirmation
            # bar[-4] above VWAP, bar[-3] or bar[-2] dipped below, bar[-2] AND bar[-1] both above VWAP
            vwap_reclaim = (
                price_4 > vwap_4 and        # uptrend context
                price_3 < vwap_3 and        # dipped below VWAP
                prev_price > prev_vwap and  # first reclaim bar
                current_price > current_vwap  # second confirmation bar
            )
            vwap_breakdown = prev_price >= prev_vwap and current_price < current_vwap

            reclaim_candle_low = to_scalar(low.iloc[-2])  # low of first reclaim bar

            if ticker not in self.positions:
                if len(self.positions) >= self.max_positions:
                    return
                if time.time() - self.last_order_time.get(ticker, 0) < self.order_cooldown:
                    return

                # Enhancement 6: Skip if this ticker already had a reclaim today
                if ticker in self.reclaimed_today:
                    return

                # Enhancement 2: Relative Volume — must be 2× the usual volume at this time of day
                cum_volume = float(volume.sum())
                rvol = self._get_rvol(ticker, cum_volume, len(volume))
                rvol_confirmed = rvol >= 2.0

                # Buy conditions — ALL must be true:
                # 1. 2-bar VWAP reclaim (confirmation)
                # 2. Stock opened above VWAP (bullish day bias)
                # 3. RSI 50–70
                # 4. RVOL ≥ 2× (institutional participation)
                # 5. SPY above its VWAP (market tailwind)
                rsi_bullish = 50 <= rsi_value <= 70
                spy_bullish = self._get_spy_vwap_bias()

                if vwap_reclaim and opened_above_vwap and rsi_bullish and rvol_confirmed and spy_bullish:
                    # Level 1 quote: check bid/ask spread before entering
                    spread_pct, ask_price = self._check_spread(ticker)
                    if spread_pct is None:
                        ask_price = current_price  # fallback to last close
                        spread_pct = 0.0
                    if spread_pct > self.MAX_SPREAD_PCT:
                        print(f"Skipping {ticker}: spread too wide ({spread_pct:.3%})")
                        return

                    # Effective entry = ask + slippage (what we actually pay)
                    effective_entry = ask_price * (1 + self.SLIPPAGE_PCT)

                    order_id = self.place_order(ticker, OrderSide.BUY, 1, ref_price=ask_price)
                    if order_id:
                        candle_stop  = reclaim_candle_low - (atr_value * 0.5)
                        pct_stop     = effective_entry * (1 - min_stop_pct)
                        stop_price   = min(candle_stop, pct_stop)
                        target_price = effective_entry + (atr_value * atr_mult)
                        half_target  = effective_entry + (atr_value * 1.0)
                        self.positions[ticker] = {
                            'entry_price':   effective_entry,
                            'entry_time':    now.strftime('%H:%M:%S'),
                            'quantity':      1,
                            'partial_qty':   0,
                            'partial_done':  False,
                            'order_id':      order_id,
                            'stop_price':    stop_price,
                            'target_price':  target_price,
                            'half_target':   half_target,
                        }
                        self.reclaimed_today.add(ticker)
                        self.last_order_time[ticker] = time.time()
                        self.send_alert(
                            f"BUY (VWAP Reclaim): {ticker} @ ${effective_entry:.2f} (ask ${ask_price:.2f} + slip) | "
                            f"Spread: {spread_pct:.3%} | VWAP: ${current_vwap:.2f} | "
                            f"Stop: ${stop_price:.2f} | Target: ${target_price:.2f} | "
                            f"Partial: ${half_target:.2f} | RSI: {rsi_value:.1f} | RVOL: {rvol:.1f}x"
                        )
            else:
                pos          = self.positions[ticker]
                stop_price   = pos['stop_price']
                target_price = pos['target_price']
                half_target  = pos['half_target']
                partial_done = pos['partial_done']
                qty          = pos['quantity']

                # Enhancement 4: Partial exit — sell 50% at 1×ATR, move stop to breakeven
                if not partial_done and current_price >= half_target and qty >= 2:
                    partial_qty = qty // 2
                    order_id = self.place_order(ticker, OrderSide.SELL, partial_qty)
                    if order_id:
                        pos['quantity']     -= partial_qty
                        pos['partial_done']  = True
                        pos['stop_price']    = pos['entry_price']  # move stop to breakeven
                        self.send_alert(
                            f"PARTIAL SELL: {ticker} {partial_qty} shares @ ${current_price:.2f} | "
                            f"Stop moved to breakeven ${pos['entry_price']:.2f}"
                        )
                        return

                # Enhancement 1 (trailing stop): trail stop up as price moves in our favour
                trail_stop = current_price - (atr_value * 1.0)
                if trail_stop > pos['stop_price']:
                    pos['stop_price'] = trail_stop

                hit_stop   = current_price <= pos['stop_price']
                hit_target = current_price >= target_price
                rsi_exit   = rsi_value > rsi_overbought
                vwap_exit  = vwap_breakdown

                if hit_stop or hit_target or rsi_exit or vwap_exit:
                    remaining_qty = pos['quantity']
                    order_id = self.place_order(ticker, OrderSide.SELL, remaining_qty)
                    if order_id:
                        pnl    = (current_price - pos['entry_price']) * remaining_qty
                        reason = (
                            "target reached" if hit_target else
                            "trailing stop"  if hit_stop   else
                            "RSI overbought" if rsi_exit   else
                            "VWAP breakdown"
                        )
                        self.send_alert(
                            f"SELL ({reason}): {ticker} @ ${current_price:.2f} | PnL: ${pnl:.2f}"
                        )
                        self.trade_log.append({
                            'ticker':      ticker,
                            'entry_price': pos['entry_price'],
                            'entry_time':  pos.get('entry_time', ''),
                            'exit_price':  current_price,
                            'qty':         remaining_qty,
                            'pnl':         round(pnl, 2),
                            'reason':      reason,
                            'time':        now.strftime('%H:%M:%S'),
                            'is_win':      pnl >= 0,
                        })
                        del self.positions[ticker]
                        self.last_order_time[ticker] = time.time()

        except Exception as e:
            print(f"Error analyzing {ticker}: {e}")

    def run(self):
        self.running = True
        while self.running:
            # Refresh momentum stocks from Alpaca screener (every 30 min)
            self._refresh_momentum_tickers()

            # Single batch API call — fetches all tickers at once
            print(f"[{datetime.now(ET).strftime('%H:%M:%S')}] Fetching bars for {len(self.tickers)} tickers...")
            self._fetch_batch_bars()

            # Analyze all tickers in parallel using cached data
            with ThreadPoolExecutor(max_workers=min(len(self.tickers), 50)) as ex:
                futures = {ex.submit(self.analyze_ticker, t): t for t in self.tickers}
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        print(f"Error in thread for {futures[f]}: {e}")

            now = datetime.now(ET)
            market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
            if now < market_open:
                # Sleep until 30 seconds before open instead of spinning every minute
                wait = max(10, (market_open - now).total_seconds() - 30)
                print(f"Pre-market: sleeping {wait/60:.1f} min until near open.")
                time.sleep(wait)
            else:
                time.sleep(60)  # During market: re-fetch every minute

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