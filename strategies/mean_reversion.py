import backtrader as bt
from .base import BaseTradeStrategy


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
