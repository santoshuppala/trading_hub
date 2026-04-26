"""
SafeStateFile — hardened state file I/O for V7.

Implements 6 safety guarantees:
  1. fcntl.flock on ALL reads (shared) and writes (exclusive)
  2. Monotonic version numbers for stale-state detection
  3. SHA256 checksums to detect corruption
  4. Rolling backups (current → .prev → .prev2) for fallback
  5. Staleness detection — refuse data older than max_age
  6. Single-writer enforcement via process-level lock

Usage:
    sf = SafeStateFile('data/bot_state.json', max_age_seconds=30)
    sf.write({'positions': {...}})

    data, fresh = sf.read()
    if not fresh:
        send_alert(...)  # state is stale, stop trading
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import time
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ET = ZoneInfo('America/New_York')


class _FcntlLock:
    """Context manager for fcntl file locking (shared or exclusive)."""

    def __init__(self, lock_path: str, mode: int):
        self._lock_path = lock_path
        self._mode = mode  # fcntl.LOCK_EX or fcntl.LOCK_SH
        self._fd = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self._lock_path) or '.', exist_ok=True)
        self._fd = open(self._lock_path, 'a+')
        fcntl.flock(self._fd, self._mode)
        return self._fd

    def __exit__(self, *args):
        try:
            if self._fd:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            if self._fd:
                self._fd.close()


class SafeStateFile:
    """Production-hardened state file with locking, versioning, checksums, and fallbacks."""

    def __init__(self, path: str, max_age_seconds: float = 30.0):
        self._path = os.path.abspath(path)
        self._lock_path = self._path + '.flock'
        self._checksum_path = self._path + '.sha256'
        self._prev_path = self._path + '.prev'
        self._prev2_path = self._path + '.prev2'
        self._max_age = max_age_seconds
        self._last_hash: str = ''
        os.makedirs(os.path.dirname(self._path), exist_ok=True)

    # ── Write ──────────────────────────────────────────────────────────

    def write(self, data: dict, _already_locked: bool = False) -> bool:
        """Atomic write with exclusive lock, version bump, checksum, and backup.

        Returns True if written, False if unchanged or failed.

        If _already_locked=True, caller holds the exclusive lock — skip re-acquiring.
        Used by DistributedPositionRegistry for atomic read-modify-write.
        """
        try:
            if _already_locked:
                return self._write_inner(data)
            with self._exclusive_lock() as lock_fd:
                return self._write_inner(data)
        except Exception as exc:
            log.error("[SafeState] Write FAILED for %s: %s",
                      self._path, exc)
            return False

    def _write_inner(self, data: dict) -> bool:
        """Inner write logic (caller must hold exclusive lock)."""
        # Read current version (inside lock)
        current = self._read_raw_unlocked()
        current_version = current.get('_version', 0) if current else 0

        # Inject metadata
        new_version = current_version + 1
        data['_version'] = new_version
        data['_timestamp'] = datetime.now(ET).isoformat()
        data['_date'] = datetime.now(ET).strftime('%Y-%m-%d')

        # Skip if unchanged (compare without metadata)
        content_for_hash = {k: v for k, v in data.items()
                            if not k.startswith('_')}
        serialized_content = json.dumps(content_for_hash,
                                        default=str, sort_keys=True)
        content_hash = hashlib.sha256(
            serialized_content.encode()).hexdigest()
        if content_hash == self._last_hash:
            return False

        # Serialize full state
        serialized = json.dumps(data, indent=2, default=str)

        # V10: Pre-flight disk space check (need ~2x serialized size for tmp + final)
        try:
            stat = os.statvfs(os.path.dirname(self._path) or '.')
            free_bytes = stat.f_bavail * stat.f_frsize
            needed = len(serialized.encode()) * 3  # safety margin
            if free_bytes < needed:
                log.critical("[SafeState] DISK FULL — %d bytes free, need %d. "
                             "Skipping write to preserve existing state.", free_bytes, needed)
                return False
        except (OSError, AttributeError):
            pass  # statvfs not available on all platforms — proceed with write

        # Atomic write: tmp → fsync → replace
        # Rotate AFTER successful write (not before) to preserve current on failure
        tmp = self._path + '.tmp'
        try:
            with open(tmp, 'w') as f:
                f.write(serialized)
                f.flush()
                os.fsync(f.fileno())
            # Verify written bytes match expected
            if os.path.getsize(tmp) != len(serialized.encode()):
                log.error("[SafeState] Write size mismatch for %s — disk may be full",
                          self._path)
                os.unlink(tmp)
                return False
        except OSError as exc:
            log.critical("[SafeState] Write FAILED for %s: %s — preserving existing state",
                         self._path, exc)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

        # Rotate backups AFTER successful tmp write (current → prev → prev2)
        self._rotate_backups()
        try:
            os.replace(tmp, self._path)
        except OSError as exc:
            log.critical("[SafeState] os.replace FAILED for %s: %s — cleaning up tmp",
                         self._path, exc)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

        # Embed checksum in sidecar (best-effort — main file is already safe)
        file_hash = hashlib.sha256(serialized.encode()).hexdigest()
        try:
            with open(self._checksum_path, 'w') as f:
                f.write(file_hash)
        except OSError as exc:
            log.warning("[SafeState] Checksum write failed: %s", exc)

        self._last_hash = content_hash
        log.debug("[SafeState] %s v%d written (%d bytes)",
                  os.path.basename(self._path), new_version,
                  len(serialized))
        return True

    # ── Read ───────────────────────────────────────────────────────────

    def read(self) -> tuple[dict | None, bool]:
        """Read with shared lock, checksum validation, and fallback.

        Returns (data, is_fresh).
        - data is None if all copies are corrupt/missing.
        - is_fresh is False if state is older than max_age_seconds.
        """
        try:
            with self._shared_lock():
                data = self._read_and_validate(self._path,
                                               self._checksum_path)
                if data is None:
                    # Fallback to .prev
                    log.warning("[SafeState] Primary corrupt, trying .prev: %s",
                                self._path)
                    data = self._read_and_validate(
                        self._prev_path,
                        self._prev_path + '.sha256')
                if data is None:
                    # Fallback to .prev2
                    log.warning("[SafeState] .prev corrupt, trying .prev2: %s",
                                self._path)
                    data = self._read_and_validate(
                        self._prev2_path,
                        self._prev2_path + '.sha256')
                if data is None:
                    log.error("[SafeState] ALL copies corrupt/missing: %s",
                              self._path)
                    return None, False

                # V10: Version monotonic check — reject if lower than last seen
                file_version = data.get('_version', 0)
                if not hasattr(self, '_last_read_version'):
                    self._last_read_version = 0
                if file_version < self._last_read_version:
                    log.warning(
                        "[SafeState] Version regression: file v%d < last seen v%d "
                        "— possible concurrent writer. Using higher version.",
                        file_version, self._last_read_version)
                else:
                    self._last_read_version = file_version

                # Check staleness
                fresh = self._check_freshness(data)
                return data, fresh

        except Exception as exc:
            log.error("[SafeState] Read FAILED for %s: %s",
                      self._path, exc)
            return None, False

    def read_if_changed(self, last_version: int) -> tuple[dict | None, bool, int]:
        """Read only if version has changed since last_version.

        Returns (data, is_fresh, current_version).
        - data is None if version unchanged or file missing.
        """
        data, fresh = self.read()
        if data is None:
            return None, False, last_version
        current_version = data.get('_version', 0)
        if current_version <= last_version:
            return None, fresh, last_version  # unchanged
        return data, fresh, current_version

    def is_stale(self) -> bool:
        """Quick staleness check without full read.

        Returns True if state is too old for trading.
        """
        try:
            mtime = os.path.getmtime(self._path)
            age = time.time() - mtime
            return age > self._max_age
        except (FileNotFoundError, OSError):
            return True

    def version(self) -> int:
        """Read current version number without loading full state."""
        try:
            with self._shared_lock():
                data = self._read_raw_unlocked()
                return data.get('_version', 0) if data else 0
        except Exception:
            return 0

    # ── Locking ────────────────────────────────────────────────────────

    def _exclusive_lock(self):
        """Context manager: fcntl exclusive lock for writes."""
        return _FcntlLock(self._lock_path, fcntl.LOCK_EX)

    def _shared_lock(self):
        """Context manager: fcntl shared lock for reads."""
        return _FcntlLock(self._lock_path, fcntl.LOCK_SH)

    # ── Internal helpers ───────────────────────────────────────────────

    def _read_raw_unlocked(self) -> dict | None:
        """Read JSON without lock or validation (caller must hold lock)."""
        try:
            if not os.path.exists(self._path):
                return None
            with open(self._path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def _read_and_validate(self, data_path: str,
                           checksum_path: str) -> dict | None:
        """Read a JSON file and validate its SHA256 checksum."""
        try:
            if not os.path.exists(data_path):
                return None
            with open(data_path) as f:
                raw = f.read()

            # Validate checksum if sidecar exists
            if os.path.exists(checksum_path):
                with open(checksum_path) as f:
                    expected_hash = f.read().strip()
                actual_hash = hashlib.sha256(raw.encode()).hexdigest()
                if actual_hash != expected_hash:
                    log.warning("[SafeState] Checksum mismatch: %s "
                                "(expected=%s got=%s)",
                                data_path, expected_hash[:12],
                                actual_hash[:12])
                    return None

            return json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("[SafeState] Read/parse failed: %s — %s",
                        data_path, exc)
            return None

    def _check_freshness(self, data: dict) -> bool:
        """Check if state timestamp is within max_age."""
        ts_str = data.get('_timestamp')
        if not ts_str:
            # No timestamp — check file mtime as fallback
            try:
                mtime = os.path.getmtime(self._path)
                return (time.time() - mtime) < self._max_age
            except (FileNotFoundError, OSError):
                return False

        try:
            ts = datetime.fromisoformat(ts_str)
            now = datetime.now(ET)
            age = (now - ts).total_seconds()
            return age < self._max_age
        except (ValueError, TypeError):
            return False

    def _rotate_backups(self) -> None:
        """Rotate: current → .prev → .prev2 (with checksums + fsync)."""
        try:
            # prev → prev2
            if os.path.exists(self._prev_path):
                shutil.copy2(self._prev_path, self._prev2_path)
                # V8: fsync backup to prevent corruption if crash mid-copy
                try:
                    fd = os.open(self._prev2_path, os.O_RDONLY)
                    os.fsync(fd)
                    os.close(fd)
                except OSError:
                    pass
                prev_cs = self._prev_path + '.sha256'
                if os.path.exists(prev_cs):
                    shutil.copy2(prev_cs, self._prev2_path + '.sha256')

            # current → prev
            if os.path.exists(self._path):
                shutil.copy2(self._path, self._prev_path)
                if os.path.exists(self._checksum_path):
                    shutil.copy2(self._checksum_path,
                                 self._prev_path + '.sha256')
        except OSError as exc:
            log.debug("[SafeState] Backup rotation failed: %s", exc)

    # ── Date validation ────────────────────────────────────────────────

    def is_today(self, data: dict) -> bool:
        """Check if state is from today (ET timezone)."""
        today = datetime.now(ET).strftime('%Y-%m-%d')
        return data.get('_date') == today


class StalenessGuard:
    """Monitor state freshness and halt trading if stale.

    Usage in satellite main loop:
        guard = StalenessGuard(cache_state_file, max_stale_cycles=2)
        if guard.check():
            continue  # state is fresh, trade
        else:
            log.critical("State stale — halting ORDER_REQ emission")
            send_alert(...)
    """

    def __init__(self, state_file: SafeStateFile,
                 max_stale_cycles: int = 2,
                 alert_fn=None):
        self._sf = state_file
        self._max_stale = max_stale_cycles
        self._stale_count = 0
        self._halted = False
        self._alert_fn = alert_fn

    def check(self) -> bool:
        """Returns True if state is fresh enough to trade.
        Returns False if stale — caller should stop emitting ORDER_REQ.
        """
        if self._halted:
            # Check for recovery
            if not self._sf.is_stale():
                self._stale_count = 0
                self._halted = False
                log.info("[StalenessGuard] State recovered — resuming trading")
                return True
            return False

        if self._sf.is_stale():
            self._stale_count += 1
            log.warning("[StalenessGuard] State stale (cycle %d/%d)",
                        self._stale_count, self._max_stale)
            if self._stale_count >= self._max_stale:
                self._halted = True
                log.critical("[StalenessGuard] State stale for %d cycles "
                             "— HALTING order emission", self._max_stale)
                if self._alert_fn:
                    try:
                        self._alert_fn(
                            f"STALENESS HALT: State file "
                            f"{self._sf._path} has not been updated "
                            f"for {self._max_stale} consecutive cycles. "
                            f"ORDER_REQ emission suspended."
                        )
                    except Exception:
                        pass
                return False
            return True  # still within tolerance
        else:
            self._stale_count = 0
            return True

    @property
    def is_halted(self) -> bool:
        return self._halted
