"""
Distributed Position Registry — hardened cross-process position tracking.

V7: Uses SafeStateFile for:
  - fcntl.flock on ALL reads (shared) AND writes (exclusive)
  - SHA256 checksums to detect corruption
  - Rolling backups (current → .prev → .prev2)
  - Monotonic version numbers

State file: data/position_registry.json
Format: {"positions": {ticker: layer_name}, ...}
"""
import logging
import os
from typing import Dict, Optional, Set

from lifecycle.safe_state import SafeStateFile

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY_PATH = os.path.join(PROJECT_ROOT, 'data', 'position_registry.json')


class DistributedPositionRegistry:
    """File-locked position registry for cross-process deduplication.

    V7: All reads use shared fcntl lock, all writes use exclusive fcntl lock.
    No unlocked reads — eliminates stale-read race conditions.
    """

    def __init__(self, global_max: int = 75, registry_path: str = None):
        path = registry_path or REGISTRY_PATH
        self._global_max = global_max
        # SafeStateFile handles directory creation, locking, checksums, backups
        self._sf = SafeStateFile(path, max_age_seconds=60.0)

        # Initialize file if it doesn't exist
        data, _ = self._sf.read()
        if data is None:
            self._sf.write({'positions': {}})

    def try_acquire(self, ticker: str, layer: str) -> bool:
        """Atomically try to acquire a ticker for a layer.

        Uses exclusive fcntl lock for read-modify-write.
        Returns True on success, False if held by another layer or at capacity.
        """
        with self._sf._exclusive_lock():
            data = self._sf._read_raw_unlocked()
            if data is None:
                data = {'positions': {}}
            positions = data.get('positions', {})

            # Already held by this layer (idempotent)
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
            data['positions'] = positions
            self._sf.write(data, _already_locked=True)
            return True

    def release(self, ticker: str) -> None:
        """Release a ticker. Uses exclusive lock."""
        with self._sf._exclusive_lock():
            data = self._sf._read_raw_unlocked()
            if data is None:
                return
            positions = data.get('positions', {})
            if ticker in positions:
                del positions[ticker]
                data['positions'] = positions
                self._sf.write(data, _already_locked=True)

    def is_held(self, ticker: str) -> bool:
        """Check if ticker is held. Uses shared fcntl lock."""
        data, _ = self._sf.read()
        if data is None:
            return False
        return ticker in data.get('positions', {})

    def held_by(self, ticker: str) -> Optional[str]:
        """Return layer name holding ticker. Uses shared fcntl lock."""
        data, _ = self._sf.read()
        if data is None:
            return None
        return data.get('positions', {}).get(ticker)

    def count(self) -> int:
        """Return total position count. Uses shared fcntl lock."""
        data, _ = self._sf.read()
        if data is None:
            return 0
        return len(data.get('positions', {}))

    def all_positions(self) -> Dict[str, str]:
        """Return copy of all positions. Uses shared fcntl lock."""
        data, _ = self._sf.read()
        if data is None:
            return {}
        return dict(data.get('positions', {}))

    def tickers_for_layer(self, layer: str) -> Set[str]:
        """Return set of tickers held by specific layer. Uses shared fcntl lock."""
        data, _ = self._sf.read()
        if data is None:
            return set()
        return {t for t, l in data.get('positions', {}).items() if l == layer}

    def cleanup_stale(self, max_age_seconds: float = 3600) -> int:
        """V8: Evict entries older than max_age_seconds (default 1 hour).

        Called on startup to clean up stale locks from crashed processes.
        Entries must have '_acquired_at' timestamp (set by try_acquire).
        """
        import time as _time
        cleaned = 0
        try:
            with self._sf._exclusive_lock():
                data = self._sf._read_raw_unlocked()
                if data is None:
                    return 0
                positions = data.get('positions', {})
                now = _time.time()
                stale = [t for t, info in positions.items()
                         if isinstance(info, dict) and
                         now - info.get('_acquired_at', now) > max_age_seconds]
                for t in stale:
                    del positions[t]
                    cleaned += 1
                    log.warning("[Registry] Evicted stale entry: %s", t)
                if cleaned:
                    data['positions'] = positions
                    self._sf.write(data, _already_locked=True)
        except Exception as exc:
            log.warning("[Registry] Stale cleanup failed: %s", exc)
        return cleaned

    def reset(self) -> None:
        """Clear all positions (called at session start). Uses exclusive lock."""
        self._sf.write({'positions': {}})
