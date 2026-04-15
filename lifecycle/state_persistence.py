"""
AtomicStateFile — thread-safe, crash-safe state persistence.

Replicates the pattern from monitor/state.py (save_state/load_state)
but generalized for any engine. Uses tmp file + os.replace for atomicity.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo('America/New_York')


class AtomicStateFile:
    """Persist and restore engine state with atomic writes."""

    def __init__(self, engine_name: str, state_dir: str = 'data'):
        self._engine_name = engine_name
        self._path = os.path.join(state_dir, f'{engine_name}_state.json')
        self._lock = threading.Lock()
        self._last_hash: str = ''
        os.makedirs(state_dir, exist_ok=True)

    def save(self, state: dict) -> bool:
        """Save state to disk atomically. Only writes if state changed.

        Returns True if written, False if unchanged or failed.
        """
        try:
            # Inject date for staleness check on reload
            state['date'] = datetime.now(ET).strftime('%Y-%m-%d')
            state['engine'] = self._engine_name

            serialized = json.dumps(state, default=str, sort_keys=True)

            # Skip if unchanged
            state_hash = hashlib.md5(serialized.encode()).hexdigest()
            if state_hash == self._last_hash:
                return False

            with self._lock:
                tmp = self._path + '.tmp'
                with open(tmp, 'w') as f:
                    f.write(serialized)
                os.replace(tmp, self._path)
                self._last_hash = state_hash

            return True
        except Exception as exc:
            log.error("[%s] State save FAILED: %s — next crash will lose state",
                      self._engine_name, exc)
            try:
                from monitor.alerts import send_alert
                send_alert(None, f"[{self._engine_name}] STATE SAVE FAILED: {exc}",
                           severity='CRITICAL')
            except Exception:
                pass
            return False

    def load(self) -> dict | None:
        """Load today's state from disk. Returns None if stale/missing/corrupt.

        Backs up corrupt files for post-mortem analysis.
        """
        if not os.path.exists(self._path):
            log.info("[%s] No state file found — starting fresh", self._engine_name)
            return None

        try:
            with open(self._path) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("[%s] Corrupt state file: %s — backing up and starting fresh",
                        self._engine_name, exc)
            self._backup_corrupt()
            return None

        # Validate date — discard stale state from previous day
        today = datetime.now(ET).strftime('%Y-%m-%d')
        file_date = state.get('date', '')
        if file_date != today:
            log.info("[%s] State file from %s (today=%s) — discarding",
                     self._engine_name, file_date, today)
            return None

        # Cache hash so first save after load doesn't re-write identical data
        serialized = json.dumps(state, default=str, sort_keys=True)
        self._last_hash = hashlib.md5(serialized.encode()).hexdigest()

        log.info("[%s] Restored state from %s", self._engine_name, self._path)
        return state

    def _backup_corrupt(self) -> None:
        """Move corrupt state file to .corrupt.{timestamp} for investigation."""
        try:
            ts = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup = f"{self._path}.corrupt.{ts}"
            shutil.move(self._path, backup)
            log.warning("[%s] Corrupt state backed up to %s", self._engine_name, backup)
        except Exception:
            pass
