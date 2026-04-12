from .base import BaseProStrategy
from .tier1.trend_pullback import TrendPullback
from .tier1.vwap_reclaim import VWAPReclaim
from .tier1.sr_flip import SRFlip
from .tier2.orb import ORB
from .tier2.inside_bar import InsideBar
from .tier2.gap_and_go import GapAndGo
from .tier2.flag_pennant import FlagPennant
from .tier3.liquidity_sweep import LiquiditySweep
from .tier3.bollinger_squeeze import BollingerSqueeze
from .tier3.fib_confluence import FibConfluence
from .tier3.momentum_ignition import MomentumIgnition

STRATEGY_REGISTRY = {
    'trend_pullback':    TrendPullback,
    'vwap_reclaim':      VWAPReclaim,
    'sr_flip':           SRFlip,
    'orb':               ORB,
    'inside_bar':        InsideBar,
    'gap_and_go':        GapAndGo,
    'flag_pennant':      FlagPennant,
    'liquidity_sweep':   LiquiditySweep,
    'bollinger_squeeze': BollingerSqueeze,
    'fib_confluence':    FibConfluence,
    'momentum_ignition': MomentumIgnition,
}

__all__ = [
    'BaseProStrategy', 'STRATEGY_REGISTRY',
    'TrendPullback', 'VWAPReclaim', 'SRFlip',
    'ORB', 'InsideBar', 'GapAndGo', 'FlagPennant',
    'LiquiditySweep', 'BollingerSqueeze', 'FibConfluence', 'MomentumIgnition',
]
