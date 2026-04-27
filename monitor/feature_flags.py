"""
Feature Flags — simple toggle system for gradual rollout.

Flags are read from environment variables at import time.
Each flag defaults to ON (True) — set to 'false'/'0'/'no' to disable.

Usage:
    from monitor.feature_flags import flags

    if flags.pending_order_dedup:
        # new behavior
    else:
        # old behavior

Toggle without restart: set env var + send SIGHUP (if wired in main loop).
"""
import logging
import os

log = logging.getLogger(__name__)


def _flag(env_key: str, default: bool = True) -> bool:
    """Read a boolean flag from env. Default is True (feature ON)."""
    val = os.getenv(env_key, str(default)).lower()
    return val not in ('false', '0', 'no', 'off', 'disabled')


class FeatureFlags:
    """All feature flags in one place. Read-only after init."""

    def __init__(self):
        self.reload()

    def reload(self):
        """Re-read all flags from env vars. Called on SIGHUP."""
        self.pending_order_dedup = _flag('FF_PENDING_ORDER_DEDUP', True)
        self.per_broker_kill = _flag('FF_PER_BROKER_KILL', True)
        self.options_lifecycle = _flag('FF_OPTIONS_LIFECYCLE', True)
        self.data_circuit_breaker = _flag('FF_DATA_CIRCUIT_BREAKER', True)
        self.token_bucket_rate_limit = _flag('FF_TOKEN_BUCKET', True)
        self.bar_builder_auto_restart = _flag('FF_BB_AUTO_RESTART', True)
        self.db_fallback_file = _flag('FF_DB_FALLBACK', True)
        self.read_only = _flag('READ_ONLY', False)

        log.info(
            "[FeatureFlags] loaded: pending_dedup=%s per_broker_kill=%s "
            "options_lifecycle=%s circuit_breaker=%s rate_limit=%s "
            "bb_restart=%s db_fallback=%s read_only=%s",
            self.pending_order_dedup, self.per_broker_kill,
            self.options_lifecycle, self.data_circuit_breaker,
            self.token_bucket_rate_limit, self.bar_builder_auto_restart,
            self.db_fallback_file, self.read_only,
        )

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}


# Singleton instance — import this
flags = FeatureFlags()
