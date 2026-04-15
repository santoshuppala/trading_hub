"""
Distributed Position Registry — file-locked cross-process position tracking.

Replaces the in-memory GlobalPositionRegistry for process isolation.
Uses file-based locking (fcntl.flock) to ensure atomic read-modify-write
across multiple processes.

State file: data/position_registry.json
Format: {"positions": {ticker: layer_name}, "updated_at": ISO-string}
"""
import fcntl
import json
import logging
import os
import time
from typing import Dict, Optional, Set

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_PATH = os.path.join(PROJECT_ROOT, 'data', 'position_registry.json')
LOCK_PATH = REGISTRY_PATH + '.lock'


class DistributedPositionRegistry:
    """File-locked position registry for cross-process deduplication."""

    def __init__(self, global_max: int = 75, registry_path: str = None):
        self._path = registry_path or REGISTRY_PATH
        self._lock_path = LOCK_PATH
        self._global_max = global_max
        os.makedirs(os.path.dirname(self._path), exist_ok=True)

        # Initialize file if it doesn't exist
        if not os.path.exists(self._path):
            self._write({'positions': {}, 'updated_at': ''})

    def try_acquire(self, ticker: str, layer: str) -> bool:
        """Atomically try to acquire a ticker for a layer. Returns True on success."""
        with self._file_lock():
            state = self._read()
            positions = state.get('positions', {})

            # Already held by this layer
            if positions.get(ticker) == layer:
                return True

            # Held by another layer
            if ticker in positions:
                return False

            # Global max check
            if len(positions) >= self._global_max:
                return False

            # Acquire
            positions[ticker] = layer
            state['positions'] = positions
            state['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
            self._write(state)
            return True

    def release(self, ticker: str) -> None:
        """Release a ticker."""
        with self._file_lock():
            state = self._read()
            positions = state.get('positions', {})
            if ticker in positions:
                del positions[ticker]
                state['positions'] = positions
                state['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
                self._write(state)

    def is_held(self, ticker: str) -> bool:
        state = self._read()
        return ticker in state.get('positions', {})

    def held_by(self, ticker: str) -> Optional[str]:
        state = self._read()
        return state.get('positions', {}).get(ticker)

    def count(self) -> int:
        state = self._read()
        return len(state.get('positions', {}))

    def all_positions(self) -> Dict[str, str]:
        state = self._read()
        return dict(state.get('positions', {}))

    def tickers_for_layer(self, layer: str) -> Set[str]:
        state = self._read()
        return {t for t, l in state.get('positions', {}).items() if l == layer}

    def reset(self) -> None:
        """Clear all positions (called at session start)."""
        with self._file_lock():
            self._write({'positions': {}, 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S')})

    def _read(self) -> dict:
        try:
            with open(self._path, 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {'positions': {}, 'updated_at': ''}

    def _write(self, state: dict) -> None:
        try:
            with open(self._path, 'w') as f:
                json.dump(state, f)
        except Exception as exc:
            log.error("[DistributedRegistry] Write failed: %s", exc)

    class _file_lock:
        """Context manager for file-based locking."""
        def __enter__(self):
            os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
            self._fd = open(LOCK_PATH, 'w')
            fcntl.flock(self._fd, fcntl.LOCK_EX)
            return self

        def __exit__(self, *args):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._fd.close()
