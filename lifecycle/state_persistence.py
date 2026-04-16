"""
AtomicStateFile — V7 wrapper around SafeStateFile for engine lifecycle.

Provides the same API as V6 (save/load) but delegates to SafeStateFile
for fcntl locking, versioning, checksums, rolling backups, and staleness.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from lifecycle.safe_state import SafeStateFile

log = logging.getLogger(__name__)
ET = ZoneInfo('America/New_York')


class AtomicStateFile:
    """Persist and restore engine state with full V7 hardening."""

    def __init__(self, engine_name: str, state_dir: str = 'data'):
        self._engine_name = engine_name
        self._sf = SafeStateFile(
            f'{state_dir}/{engine_name}_state.json',
            max_age_seconds=120.0,  # 2 minutes — lifecycle tick is 10s
        )

    def save(self, state: dict) -> bool:
        """Save state to disk. Returns True if written, False if unchanged."""
        state['engine'] = self._engine_name
        written = self._sf.write(state)
        if written:
            log.debug("[%s] State saved (v%d)", self._engine_name,
                      self._sf.version())
        return written

    def load(self) -> dict | None:
        """Load today's state from disk. Returns None if stale/missing/corrupt.

        V7: shared fcntl lock, checksum validation, .prev/.prev2 fallback.
        """
        data, _fresh = self._sf.read()
        if data is None:
            log.info("[%s] No state file found — starting fresh",
                     self._engine_name)
            return None

        # Validate date — discard stale state from previous day
        if not self._sf.is_today(data):
            file_date = data.get('_date', 'unknown')
            log.info("[%s] State file from %s (not today) — discarding",
                     self._engine_name, file_date)
            return None

        log.info("[%s] Restored state v%d from disk",
                 self._engine_name, data.get('_version', 0))
        return data
