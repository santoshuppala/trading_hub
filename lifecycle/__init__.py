"""
lifecycle — Production lifecycle management for satellite trading engines.

Brings Pro, Pop, and Options engines to parity with Core's lifecycle features:
state persistence, kill switch, heartbeat, EOD reports, position reconciliation.

Usage in any satellite run script:
    from lifecycle import EngineLifecycle
    from lifecycle.adapters.options_adapter import OptionsLifecycleAdapter

    lifecycle = EngineLifecycle(
        engine_name='options',
        adapter=OptionsLifecycleAdapter(options_engine),
        bus=bus,
        alert_email=ALERT_EMAIL,
        max_daily_loss=OPTIONS_MAX_DAILY_LOSS,
    )
    lifecycle.startup()

    while running:
        # ... process bars ...
        if not lifecycle.tick():
            break  # kill switch triggered

    lifecycle.shutdown()
"""
from .core import EngineLifecycle

__all__ = ['EngineLifecycle']
