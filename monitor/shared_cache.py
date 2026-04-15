"""
Shared market data cache for process-isolated engines.

Core process writes bars + rvol data to a pickle file.
Satellite processes read from it. Atomic writes prevent corruption.

File: data/live_cache.pkl
Format: {
    'timestamp': float (monotonic),
    'updated_at': str (ISO datetime),
    'bars': {ticker: DataFrame.to_dict('list')},
    'rvol': {ticker: DataFrame.to_dict('list')},
}
"""
import json
import logging
import os
import pickle
import tempfile
import time
from typing import Dict, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_PATH = os.path.join(PROJECT_ROOT, 'data', 'live_cache.pkl')
CACHE_DIR = os.path.dirname(CACHE_PATH)


class CacheWriter:
    """Write market data to shared cache (used by core process only)."""

    def __init__(self, cache_path: str = None):
        self._path = cache_path or CACHE_PATH
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._write_count = 0

    def write(self, bars_cache: dict, rvol_cache: dict) -> None:
        """Atomically write bars and rvol data to the cache file."""
        # Convert DataFrames to serializable dicts
        bars_data = {}
        for ticker, df in bars_cache.items():
            if df is not None and not df.empty:
                bars_data[ticker] = df.to_dict('list')

        rvol_data = {}
        for ticker, df in rvol_cache.items():
            if df is not None and not df.empty:
                rvol_data[ticker] = df.to_dict('list')

        cache = {
            'timestamp': time.monotonic(),
            'updated_at': pd.Timestamp.now('UTC').isoformat(),
            'bars': bars_data,
            'rvol': rvol_data,
        }

        # Atomic write: write to temp file, then rename
        try:
            fd, tmp_path = tempfile.mkstemp(dir=CACHE_DIR, suffix='.pkl.tmp')
            with os.fdopen(fd, 'wb') as f:
                pickle.dump(cache, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_path, self._path)
            self._write_count += 1
        except Exception as exc:
            log.warning("[SharedCache] Write failed: %s", exc)
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


class CacheReader:
    """Read market data from shared cache (used by satellite processes)."""

    def __init__(self, cache_path: str = None, max_age_seconds: float = 30.0):
        self._path = cache_path or CACHE_PATH
        self._max_age = max_age_seconds
        self._last_mtime = 0.0
        self._cached_data = None

    def read(self) -> Optional[dict]:
        """Read cache if it's been updated since last read. Returns None if stale."""
        try:
            mtime = os.path.getmtime(self._path)
            if mtime <= self._last_mtime:
                return self._cached_data  # No update since last read

            with open(self._path, 'rb') as f:
                data = pickle.load(f)

            self._last_mtime = mtime
            self._cached_data = data
            return data
        except FileNotFoundError:
            return None
        except Exception as exc:
            log.debug("[SharedCache] Read failed: %s", exc)
            return self._cached_data

    def get_bars(self) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
        """Read cache and return (bars_cache, rvol_cache) as DataFrames."""
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
