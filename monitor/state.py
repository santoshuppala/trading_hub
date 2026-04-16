"""
Lightweight JSON persistence for intraday bot state.

V7: Uses SafeStateFile for hardened I/O:
  - fcntl.flock on reads (shared) and writes (exclusive)
  - Monotonic version numbers
  - SHA256 checksums
  - Rolling backups (current → .prev → .prev2)
  - Staleness detection

Saves: positions, reclaimed_today, trade_log.
Does NOT save: last_order_time (cooldowns reset cleanly on restart).
"""
import logging
import os

from lifecycle.safe_state import SafeStateFile

log = logging.getLogger(__name__)

_STATE_FILE = os.path.join(os.path.dirname(__file__), '..', 'bot_state.json')
_safe = SafeStateFile(_STATE_FILE, max_age_seconds=120.0)


def save_state(positions, reclaimed_today, trade_log):
    """
    Persist current intraday state to disk.

    Uses SafeStateFile: exclusive fcntl lock, version bump, SHA256 checksum,
    rolling backup (current → .prev → .prev2), skip-if-unchanged.
    """
    state = {
        'positions':      positions,
        'reclaimed_today': list(reclaimed_today),
        'trade_log':      trade_log,
    }
    written = _safe.write(state)
    if written:
        log.debug(
            f"State saved: {len(positions)} open position(s), "
            f"{len(trade_log)} trade(s)."
        )


def load_state():
    """
    Load persisted state if it is from today.

    Uses SafeStateFile: shared fcntl lock, checksum validation,
    fallback to .prev/.prev2 on corruption.

    Returns
    -------
    (positions, reclaimed_today, trade_log)
    Falls back to ({}, set(), []) if no valid state exists.
    """
    data, _fresh = _safe.read()

    if data is None:
        return {}, set(), []

    # Date validation — discard stale state from previous day
    if not _safe.is_today(data):
        saved_date = data.get('_date', 'unknown')
        log.info(f"State file is from {saved_date} — discarding.")
        return {}, set(), []

    positions       = data.get('positions', {})
    reclaimed_today = set(data.get('reclaimed_today', []))
    trade_log       = data.get('trade_log', [])

    if positions:
        log.warning(
            f"Restored {len(positions)} open position(s) from previous session: "
            f"{list(positions.keys())}. "
            f"Verify these are still open in Alpaca before trading!"
        )
    if trade_log:
        log.info(f"Restored {len(trade_log)} completed trade(s) from earlier today.")

    return positions, reclaimed_today, trade_log
