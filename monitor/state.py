"""
Lightweight JSON persistence for intraday bot state.

Saves: positions, reclaimed_today, trade_log.
Does NOT save: last_order_time (cooldowns reset cleanly on restart).

On startup, state is restored only if the file is from today.
On a new trading day, the stale file is silently discarded.

Atomic writes (tmp → replace) prevent corruption on mid-write crashes.
A threading.Lock serialises concurrent saves from the ThreadPoolExecutor.
"""
import json
import logging
import os
import shutil
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)

_STATE_FILE = os.path.join(os.path.dirname(__file__), '..', 'bot_state.json')
_lock = threading.Lock()


def save_state(positions, reclaimed_today, trade_log):
    """
    Persist current intraday state to disk.

    Parameters
    ----------
    positions      : dict  {ticker -> position dict}
    reclaimed_today: set   tickers that already had a VWAP reclaim today
    trade_log      : list  completed trade dicts
    """
    state = {
        'date':           datetime.now(ET).strftime('%Y-%m-%d'),
        'positions':      positions,
        'reclaimed_today': list(reclaimed_today),
        'trade_log':      trade_log,
    }
    with _lock:
        try:
            tmp = _STATE_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp, _STATE_FILE)   # atomic on POSIX
            log.debug(
                f"State saved: {len(positions)} open position(s), "
                f"{len(trade_log)} trade(s)."
            )
        except Exception as e:
            log.error(f"State save failed: {e}")


def load_state():
    """
    Load persisted state if it is from today.

    Returns
    -------
    (positions, reclaimed_today, trade_log)
    Falls back to ({}, set(), []) if no valid state exists.
    """
    try:
        if not os.path.exists(_STATE_FILE):
            return {}, set(), []

        with open(_STATE_FILE) as f:
            state = json.load(f)

        today = datetime.now(ET).strftime('%Y-%m-%d')
        saved_date = state.get('date', '')

        if saved_date != today:
            log.info(
                f"State file is from {saved_date or 'unknown date'} "
                f"— discarding (today is {today})."
            )
            return {}, set(), []

        positions       = state.get('positions', {})
        reclaimed_today = set(state.get('reclaimed_today', []))
        trade_log       = state.get('trade_log', [])

        if positions:
            log.warning(
                f"Restored {len(positions)} open position(s) from previous session: "
                f"{list(positions.keys())}. "
                f"Verify these are still open in Alpaca before trading!"
            )
        if trade_log:
            log.info(f"Restored {len(trade_log)} completed trade(s) from earlier today.")

        return positions, reclaimed_today, trade_log

    except Exception as e:
        log.error(f"State load failed: {e}")
        # Preserve the corrupt file for post-mortem inspection.
        if os.path.exists(_STATE_FILE):
            backup = _STATE_FILE + '.corrupt'
            try:
                shutil.copy2(_STATE_FILE, backup)
                log.warning(f"Corrupt state file backed up to {backup}")
            except Exception as be:
                log.warning(f"Could not back up corrupt state file: {be}")
        return {}, set(), []
