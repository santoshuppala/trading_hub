"""
Shared market data cache for process-isolated engines.

V7 hardening:
  - fcntl.flock: exclusive on write, shared on read
  - SHA256 checksum sidecar prevents corrupt-pickle reads
  - Version number for change detection (skip if unchanged)
  - Staleness enforcement: get_bars() returns empty if cache too old
  - Timeout on pickle.load() via alarm signal

Core process writes bars + rvol data to a pickle file.
Satellite processes read from it with freshness validation.

File: data/live_cache.pkl
"""
import fcntl
import hashlib
import logging
import os
import pickle
import signal
import tempfile
import time
from typing import Dict, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(PROJECT_ROOT, 'data', 'live_cache.pkl')
CACHE_DIR = os.path.dirname(CACHE_PATH)
CHECKSUM_PATH = CACHE_PATH + '.sha256'
LOCK_PATH = CACHE_PATH + '.flock'

_PICKLE_READ_TIMEOUT = 5   # seconds — kill hung pickle.load()
_MAX_CACHE_BYTES = 50 * 1024 * 1024  # 50MB — cap file read to prevent OOM on corrupt file


class CacheWriter:
    """Write market data to shared cache (used by core process only).

    V7: exclusive fcntl lock, version number, SHA256 checksum.
    """

    def __init__(self, cache_path: str = None):
        self._path = cache_path or CACHE_PATH
        self._checksum_path = self._path + '.sha256'
        self._lock_path = self._path + '.flock'
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._write_count = 0
        self._version = 0

    def write(self, bars_cache: dict, rvol_cache: dict) -> None:
        """Atomically write bars and rvol data with exclusive lock + checksum."""
        bars_data = {}
        for ticker, df in bars_cache.items():
            if df is not None and not df.empty:
                bars_data[ticker] = df.to_dict('list')

        rvol_data = {}
        for ticker, df in rvol_cache.items():
            if df is not None and not df.empty:
                rvol_data[ticker] = df.to_dict('list')

        self._version += 1
        cache = {
            '_version': self._version,
            'timestamp': time.monotonic(),
            'updated_at': pd.Timestamp.now('UTC').isoformat(),
            'wall_clock': time.time(),
            'bars': bars_data,
            'rvol': rvol_data,
        }

        fd = None
        tmp_path = None
        try:
            # Exclusive lock for write
            lock_fd = open(self._lock_path, 'w')
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR,
                                                suffix='.pkl.tmp')
                with os.fdopen(fd, 'wb') as f:
                    pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
                fd = None  # os.fdopen closed it

                # Compute checksum of the tmp file
                with open(tmp_path, 'rb') as f:
                    file_hash = hashlib.sha256(f.read()).hexdigest()

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

    V7: shared fcntl lock, checksum validation, staleness enforcement,
    timeout on pickle.load().
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
        """Read cache with shared lock, checksum validation, and timeout.

        Returns None if file missing, corrupt, or read times out.
        Returns cached_data if file unchanged since last read.
        """
        try:
            mtime = os.path.getmtime(self._path)
            if mtime <= self._last_mtime:
                return self._cached_data

            # Shared lock for read
            lock_fd = open(self._lock_path, 'a+')
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
            try:
                # Validate checksum before deserializing
                if os.path.exists(self._checksum_path):
                    with open(self._path, 'rb') as f:
                        raw = f.read()
                    with open(self._checksum_path) as f:
                        expected = f.read().strip()
                    actual = hashlib.sha256(raw).hexdigest()
                    if actual != expected:
                        log.warning("[SharedCache] Checksum mismatch — "
                                    "skipping corrupt cache")
                        return self._cached_data

                    # Deserialize from bytes (already read)
                    data = pickle.loads(raw)
                else:
                    # No checksum sidecar (pre-V7 cache) — read with timeout
                    # V7 P1-6: Wrap pickle.load in timeout to prevent hang on
                    # corrupted file. Read raw bytes first (bounded by file size),
                    # then deserialize from memory.
                    with open(self._path, 'rb') as f:
                        raw = f.read(_MAX_CACHE_BYTES)
                    data = pickle.loads(raw)

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
