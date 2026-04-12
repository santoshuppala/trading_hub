"""
pop_screener — Multi-strategy pop-stock subsystem
==================================================
Screens for 'pop' events across news, social, and market-behaviour dimensions,
classifies each candidate into a deterministic strategy type, routes it to the
correct engine, and emits POP_SIGNAL + SIGNAL events into the existing EventBus
pipeline.

Integration with the existing 9-layer pipeline
-----------------------------------------------
                    ┌─────────────────────────────────────────┐
  BAR events ──────►│  PopStrategyEngine  (T3.5)              │
                    │   ingestion → features → screener        │
                    │   → classifier → strategy_router         │
                    │   → emit POP_SIGNAL (durable)            │
                    │   → translate to SIGNAL → RiskEngine     │
                    └─────────────────────────────────────────┘

How to swap mock ingestion for real APIs
----------------------------------------
Each source adapter in ingestion.py implements a protocol (get_news /
get_social / get_market_slice / get_momentum_universe).  Replace the mock
implementation class with a real one and inject it into PopStrategyEngine —
no other code changes required.
"""
from pop_screener.models import (
    NewsData, SocialData, OHLCVBar, MarketDataSlice,
    EngineeredFeatures, PopCandidate, StrategyAssignment,
    EntrySignal, ExitSignal, TradeCandidate,
    StrategyType, PopReason, FloatCategory, ExitReason,
)

__all__ = [
    'NewsData', 'SocialData', 'OHLCVBar', 'MarketDataSlice',
    'EngineeredFeatures', 'PopCandidate', 'StrategyAssignment',
    'EntrySignal', 'ExitSignal', 'TradeCandidate',
    'StrategyType', 'PopReason', 'FloatCategory', 'ExitReason',
]
