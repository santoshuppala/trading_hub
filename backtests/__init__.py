"""
Backtesting framework for Pop/Pro/Options trading strategies.

Event-replay harness that runs live engine code (ProSetupEngine, PopStrategyEngine,
OptionsEngine) against historical bar data with SYNC event dispatch (no async workers).

No modifications to live code. All I/O boundaries replaced:
  - EventBus: real bus in SYNC mode
  - Broker calls: intercepted by SignalCapture before execution
  - External APIs: mocked (options chain, news/social sources)
"""
