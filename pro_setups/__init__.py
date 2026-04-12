"""
pro_setups — Additive multi-setup module for Trading Hub.
==========================================================
11 new trading setups across 3 tiers, wired additively onto the EventBus.
No existing modules are modified beyond:
  - monitor/event_bus.py  : PRO_STRATEGY_SIGNAL EventType added
  - monitor/events.py     : ProStrategySignalPayload added
  - run_monitor.py        : ProSetupEngine instantiation
  - config.py             : PRO_* config constants

Architecture
------------
BAR → ProSetupEngine._on_bar()
  └─ 11 detectors in sequence
  └─ StrategyClassifier.classify() → (name, tier, direction, confidence) | None
  └─ strategy.detect_signal() final confirmation
  └─ strategy.generate_entry/stop/exit()
  └─ emit PRO_STRATEGY_SIGNAL (non-durable, for audit)
  └─ ProStrategyRouter._on_pro_signal()
       └─ RiskAdapter.validate_and_emit()
            └─ emit ORDER_REQ (durable) → AlpacaBroker → FILL → PositionManager

Tier stop/exit rules
--------------------
Tier 1 (high win-rate): SL=0.4 ATR, partial=1R, full=2R
Tier 2 (moderate):      SL=1.0 ATR, partial=1.5R, full=3R
Tier 3 (high expectancy): SL=2.0 ATR, partial=3R, full=6R
"""
