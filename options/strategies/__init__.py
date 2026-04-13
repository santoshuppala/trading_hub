"""
All 13 options strategy implementations and registry.
"""
from .directional import LongCall, LongPut
from .vertical import BullCallSpread, BearPutSpread, BullPutSpread, BearCallSpread
from .volatility import LongStraddle, LongStrangle
from .neutral import IronCondor, IronButterfly
from .time_based import CalendarSpread, DiagonalSpread
from .complex import ButterflySpread

STRATEGY_REGISTRY = {
    'long_call':        LongCall,
    'long_put':         LongPut,
    'bull_call_spread': BullCallSpread,
    'bear_put_spread':  BearPutSpread,
    'bull_put_spread':  BullPutSpread,
    'bear_call_spread': BearCallSpread,
    'long_straddle':    LongStraddle,
    'long_strangle':    LongStrangle,
    'iron_condor':      IronCondor,
    'iron_butterfly':   IronButterfly,
    'calendar_spread':  CalendarSpread,
    'diagonal_spread':  DiagonalSpread,
    'butterfly_spread': ButterflySpread,
}

__all__ = [
    'STRATEGY_REGISTRY',
    'LongCall', 'LongPut',
    'BullCallSpread', 'BearPutSpread', 'BullPutSpread', 'BearCallSpread',
    'LongStraddle', 'LongStrangle',
    'IronCondor', 'IronButterfly',
    'CalendarSpread', 'DiagonalSpread',
    'ButterflySpread',
]
