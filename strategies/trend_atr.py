import backtrader as bt
from .base import BaseTradeStrategy


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
