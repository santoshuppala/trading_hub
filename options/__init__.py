"""
T3.7 — Options Engine

Self-contained options trading subsystem with 13 strategy types, independent risk gate,
and dedicated Alpaca account (APCA_OPTIONS_KEY / APCA_OPTIONS_SECRET).

Budget: $10,000 total, $500 per trade, 5 max concurrent positions.

Excluded strategies: Covered Calls, Cash-Secured Puts.

Implemented strategies:
  - Directional: LongCall, LongPut
  - Vertical Debit: BullCallSpread, BearPutSpread
  - Vertical Credit: BullPutSpread, BearCallSpread
  - Volatility: LongStraddle, LongStrangle
  - Neutral: IronCondor, IronButterfly
  - Time-Based: CalendarSpread, DiagonalSpread
  - Complex: ButterflySpread

Architecture:
  - OptionsEngine: BAR/SIGNAL subscriber → orchestrator
  - OptionStrategySelector: signal/bar → strategy type mapper
  - AlpacaOptionChainClient: Alpaca options data API wrapper
  - OptionStrategySelector: Base + 13 concrete strategy builders
  - OptionsRiskGate: Independent budget + position tracking
  - AlpacaOptionsBroker: Multi-leg order executor
"""
