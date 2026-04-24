"""
Shared market data cache for process-isolated engines.

V7 hardening:
  - fcntl.flock: exclusive on write, shared on read
  - SHA256 checksum sidecar prevents corrupt reads
  - Version number for change detection (skip if unchanged)
  - Staleness enforcement: get_bars() returns empty if cache too old

V9: Replaced pickle with JSON.
  - JSON is human-readable, schema-stable, and debuggable
  - No class dependency — survives code changes without deserialization errors
  - Atomic write (tmp → fsync → replace) prevents corruption on crash
  - Backward compat: if legacy .pkl exists, reads it once then writes .json

Core process writes bars + rvol data to a JSON file.
Satellite processes read from it with freshness validation.

File: data/live_cache.json (was data/live_cache.pkl)
"""
import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from typing import Dict, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)

from config import LIVE_CACHE_PATH
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = LIVE_CACHE_PATH
CACHE_DIR = os.path.dirname(CACHE_PATH)
CHECKSUM_PATH = CACHE_PATH + '.sha256'
LOCK_PATH = CACHE_PATH + '.flock'

# Legacy pickle path — used for one-time migration only
_LEGACY_PKL_PATH = os.path.join(PROJECT_ROOT, 'data', 'live_cache.pkl')

_MAX_CACHE_BYTES = 50 * 1024 * 1024  # 50MB — cap file read


class CacheWriter:
    """Write market data to shared cache (used by core process only).

    V7: exclusive fcntl lock, version number, SHA256 checksum.
    V9: JSON format replaces pickle — human-readable, schema-stable.
    """

    def __init__(self, cache_path: str = None):
        self._path = cache_path or CACHE_PATH
        self._checksum_path = self._path + '.sha256'
        self._lock_path = self._path + '.flock'
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._write_count = 0
        self._version = 0

    def write(self, bars_cache: dict, rvol_cache: dict) -> None:
        """Atomically write bars and rvol data as JSON with exclusive lock + checksum."""
        # Snapshot dict keys first to avoid RuntimeError: dictionary changed
        # size during iteration. Bar-fetching threads may add/remove tickers
        # concurrently while we iterate.
        bars_data = {}
        for ticker in list(bars_cache):
            try:
                df = bars_cache.get(ticker)
                if df is not None and not df.empty:
                    bars_data[ticker] = df.to_dict('list')
            except (RuntimeError, KeyError):
                continue  # ticker removed mid-iteration

        rvol_data = {}
        for ticker in list(rvol_cache):
            try:
                df = rvol_cache.get(ticker)
                if df is not None and not df.empty:
                    rvol_data[ticker] = df.to_dict('list')
            except (RuntimeError, KeyError):
                continue

        self._version += 1
        cache = {
            '_version': self._version,
            '_format': 'json_v1',
            'timestamp': time.monotonic(),
            'updated_at': pd.Timestamp.now('UTC').isoformat(),
            'wall_clock': time.time(),
            'bars': bars_data,
            'rvol': rvol_data,
        }

        tmp_path = None
        try:
            # Exclusive lock for write
            lock_fd = open(self._lock_path, 'w')
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                # Serialize to JSON (default=str handles Timestamp, numpy types)
                serialized = json.dumps(cache, default=str)

                # Atomic write: tmp → fsync → replace
                fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR,
                                                suffix='.json.tmp')
                with os.fdopen(fd, 'w') as f:
                    f.write(serialized)
                    f.flush()
                    os.fsync(f.fileno())

                # Compute checksum
                file_hash = hashlib.sha256(serialized.encode()).hexdigest()

                # Atomic replace
                os.replace(tmp_path, self._path)
                tmp_path = None

                # Write checksum sidecar
                with open(self._checksum_path, 'w') as f:
                    f.write(file_hash)

                self._write_count += 1
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception as exc:
            log.warning("[SharedCache] Write failed: %s", exc)
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass


class CacheReader:
    """Read market data from shared cache (used by satellite processes).

    V7: shared fcntl lock, checksum validation, staleness enforcement.
    V9: Reads JSON format. Falls back to legacy pickle for one-time migration.
    """

    def __init__(self, cache_path: str = None, max_age_seconds: float = 15.0):
        self._path = cache_path or CACHE_PATH
        self._checksum_path = self._path + '.sha256'
        self._lock_path = self._path + '.flock'
        self._max_age = max_age_seconds
        self._last_mtime = 0.0
        self._last_version = 0
        self._cached_data = None

    def read(self) -> Optional[dict]:
        """Read cache with shared lock and checksum validation.

        Returns None if file missing or corrupt.
        Returns cached_data if file unchanged since last read.
        Falls back to legacy .pkl if .json doesn't exist yet.
        """
        # Determine which file to read
        read_path = self._path
        is_legacy = False
        if not os.path.exists(self._path) and os.path.exists(_LEGACY_PKL_PATH):
            read_path = _LEGACY_PKL_PATH
            is_legacy = True

        try:
            mtime = os.path.getmtime(read_path)
            if mtime <= self._last_mtime and not is_legacy:
                return self._cached_data

            # Shared lock for read
            lock_fd = open(self._lock_path, 'a+')
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
            try:
                with open(read_path, 'rb' if is_legacy else 'r') as f:
                    raw = f.read(_MAX_CACHE_BYTES if is_legacy else None)

                # Checksum validation (for JSON path)
                if not is_legacy and os.path.exists(self._checksum_path):
                    with open(self._checksum_path) as f:
                        expected = f.read().strip()
                    raw_bytes = raw.encode() if isinstance(raw, str) else raw
                    actual = hashlib.sha256(raw_bytes).hexdigest()
                    if actual != expected:
                        log.warning("[SharedCache] Checksum mismatch — "
                                    "skipping corrupt cache")
                        return self._cached_data

                # Deserialize
                if is_legacy:
                    import pickle
                    data = pickle.loads(raw)
                    log.info("[SharedCache] Read legacy .pkl — "
                             "will be replaced by .json on next Core write")
                else:
                    data = json.loads(raw)

                self._last_mtime = mtime
                self._cached_data = data
                self._last_version = data.get('_version', 0)
                return data
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()

        except FileNotFoundError:
            return None
        except Exception as exc:
            log.warning("[SharedCache] Read failed: %s", exc)
            return self._cached_data

    def get_bars(self) -> Tuple[Dict[str, pd.DataFrame],
                                Dict[str, pd.DataFrame]]:
        """Read cache and return (bars_cache, rvol_cache) as DataFrames.

        V7: Returns empty dicts if cache is stale (older than max_age).
        This prevents trading on outdated data.
        """
        if not self.is_fresh():
            log.warning("[SharedCache] Cache is STALE (>%.0fs old) — "
                        "returning empty bars to prevent stale-data trading",
                        self._max_age)
            return {}, {}

        data = self.read()
        if data is None:
            return {}, {}

        bars_cache = {}
        for ticker, d in data.get('bars', {}).items():
            try:
                bars_cache[ticker] = pd.DataFrame(d)
            except Exception:
                pass

        rvol_cache = {}
        for ticker, d in data.get('rvol', {}).items():
            try:
                rvol_cache[ticker] = pd.DataFrame(d)
            except Exception:
                pass

        return bars_cache, rvol_cache

    def is_fresh(self) -> bool:
        """Check if cache has been updated within max_age_seconds."""
        try:
            mtime = os.path.getmtime(self._path)
            return (time.time() - mtime) < self._max_age
        except Exception:
            return False

    @property
    def version(self) -> int:
        """Return last-read cache version number."""
        return self._last_version
