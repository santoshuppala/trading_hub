from .base import BaseTradeStrategy
from .ema_rsi import EMARSICrossoverStrategy
from .trend_atr import TrendFollowingATRStrategy
from .momentum_breakout import MomentumBreakoutStrategy
from .mean_reversion import MeanReversionStrategy
from .vwap_reclaim import VWAPReclaimStrategy


def get_strategy_class(name):
    strategies = {
        'ema_rsi': EMARSICrossoverStrategy,
        'trend_atr': TrendFollowingATRStrategy,
        'momentum_breakout': MomentumBreakoutStrategy,
        'mean_reversion': MeanReversionStrategy,
        'vwap_reclaim': VWAPReclaimStrategy,
    }
    return strategies.get(name)


__all__ = [
    'BaseTradeStrategy',
    'EMARSICrossoverStrategy',
    'TrendFollowingATRStrategy',
    'MomentumBreakoutStrategy',
    'MeanReversionStrategy',
    'VWAPReclaimStrategy',
    'get_strategy_class',
]
