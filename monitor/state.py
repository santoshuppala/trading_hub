"""
Lightweight JSON persistence for intraday bot state.

V7: Uses SafeStateFile for hardened I/O:
  - fcntl.flock on reads (shared) and writes (exclusive)
  - Monotonic version numbers
  - SHA256 checksums
  - Rolling backups (current → .prev → .prev2)
  - Staleness detection

V7.2: Three-tier position recovery on restart:
  Tier 1: bot_state.json (fast, local, has stop/target/strategy)
  Tier 2: DB event_store  (durable, survives any crash, has all fields)
  Tier 3: Broker APIs     (absolute truth for qty — handled by reconciliation)

  Merge priority: richest data source wins (local > DB > broker defaults).

Saves: positions, reclaimed_today, trade_log.
Does NOT save: last_order_time (cooldowns reset cleanly on restart).
"""
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from lifecycle.safe_state import SafeStateFile

log = logging.getLogger(__name__)
ET = ZoneInfo('America/New_York')

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
    Load persisted state with three-tier recovery.

    Tier 1: bot_state.json (+ .prev / .prev2 backups)
    Tier 2: DB event_store — query for today's open positions
    Tier 3: Empty — reconciliation will import from brokers with defaults

    Returns
    -------
    (positions, reclaimed_today, trade_log)
    """
    # ── Tier 1: JSON state file ──────────────────────────────────────────
    positions, reclaimed, trades = _load_from_file()

    if positions:
        log.info(
            "[state] Tier 1 (JSON): restored %d position(s): %s",
            len(positions), list(positions.keys()))
        return positions, reclaimed, trades

    # ── Tier 2: DB event_store ───────────────────────────────────────────
    db_positions = _load_open_positions_from_db()
    if db_positions:
        log.warning(
            "[state] Tier 2 (DB): JSON state empty/corrupt — recovered %d "
            "position(s) from event_store: %s",
            len(db_positions), list(db_positions.keys()))
        return db_positions, set(), trades or []

    # ── Tier 3: Empty — reconciliation will handle ───────────────────────
    if not positions and not db_positions:
        log.info("[state] Tiers 1+2 empty — reconciliation will import from brokers.")

    return positions or {}, reclaimed or set(), trades or []


def _load_from_file():
    """Tier 1: Load from bot_state.json with backup fallback."""
    data, _fresh = _safe.read()

    if data is not None:
        result = _extract_if_valid(data, 'primary')
        if result is not None:
            return result

    # Primary failed — try backups
    for suffix, label in [('.prev', 'backup'), ('.prev2', 'backup2')]:
        backup_data = _read_backup(_STATE_FILE + suffix)
        if backup_data is not None:
            result = _extract_if_valid(backup_data, label)
            if result is not None:
                return result

    return {}, set(), []


def _load_open_positions_from_db():
    """
    Tier 2: Query DB for today's open positions (opened but not closed).

    Uses synchronous psycopg2 (same as _fetch_last_atr) since this runs
    in __init__ before the async pool is ready.

    Returns dict of {ticker: position_dict} or empty dict on failure.
    """
    try:
        import psycopg2
        from config import DATABASE_URL
    except Exception:
        log.debug("[state] DB imports unavailable — skipping Tier 2")
        return {}

    today = datetime.now(ET).strftime('%Y-%m-%d')

    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        try:
            with conn.cursor() as cur:
                # Find positions opened today that have NOT been closed.
                # Joins with OrderRequested to recover strategy from the
                # reason field (e.g. "pro:sr_flip:T1:long" → "pro:sr_flip").
                # PositionOpened.strategy is often NULL for older events.
                cur.execute("""
                    WITH opened AS (
                        SELECT DISTINCT ON (event_payload->>'ticker')
                            event_payload->>'ticker'       AS ticker,
                            event_payload->>'entry_price'  AS entry_price,
                            event_payload->>'entry_time'   AS entry_time,
                            event_payload->>'stop_price'   AS stop_price,
                            event_payload->>'target_price' AS target_price,
                            event_payload->>'half_target'  AS half_target,
                            event_payload->>'qty'          AS qty,
                            event_payload->>'strategy'     AS strategy,
                            event_payload->>'broker'       AS broker,
                            event_payload->>'atr_value'    AS atr_value,
                            event_id,
                            event_time
                        FROM trading.event_store
                        WHERE event_type = 'PositionOpened'
                          AND event_time::date = %s::date
                        ORDER BY event_payload->>'ticker', event_time DESC
                    ),
                    closed AS (
                        SELECT DISTINCT event_payload->>'ticker' AS ticker
                        FROM trading.event_store
                        WHERE event_type = 'PositionClosed'
                          AND event_time::date = %s::date
                    ),
                    buy_reasons AS (
                        SELECT DISTINCT ON (event_payload->>'ticker')
                            event_payload->>'ticker' AS ticker,
                            event_payload->>'reason' AS reason
                        FROM trading.event_store
                        WHERE event_type = 'OrderRequested'
                          AND event_payload->>'side' = 'BUY'
                          AND event_time::date = %s::date
                        ORDER BY event_payload->>'ticker', event_time DESC
                    ),
                    buy_fills AS (
                        SELECT DISTINCT ON (event_payload->>'ticker')
                            event_payload->>'ticker'   AS ticker,
                            event_payload->>'order_id' AS fill_order_id
                        FROM trading.event_store
                        WHERE event_type = 'FillExecuted'
                          AND event_payload->>'side' = 'BUY'
                          AND event_time::date = %s::date
                        ORDER BY event_payload->>'ticker', event_time DESC
                    )
                    SELECT o.*, b.reason AS buy_reason, f.fill_order_id
                    FROM opened o
                    LEFT JOIN closed c ON o.ticker = c.ticker
                    LEFT JOIN buy_reasons b ON o.ticker = b.ticker
                    LEFT JOIN buy_fills f ON o.ticker = f.ticker
                    WHERE c.ticker IS NULL
                """, (today, today, today, today))

                rows = cur.fetchall()
                if not rows:
                    return {}

                columns = [desc[0] for desc in cur.description]
                positions = {}
                for row in rows:
                    r = dict(zip(columns, row))
                    ticker = r['ticker']
                    if not ticker:
                        continue

                    entry_price = _safe_float(r.get('entry_price'))
                    if not entry_price or entry_price <= 0:
                        continue

                    qty = _safe_int(r.get('qty'))
                    if not qty or qty <= 0:
                        continue

                    stop_price = _safe_float(r.get('stop_price')) or entry_price * 0.97
                    target_price = _safe_float(r.get('target_price')) or entry_price * 1.05
                    half_target = _safe_float(r.get('half_target')) or (entry_price + target_price) / 2

                    # Recover strategy: prefer PositionOpened.strategy,
                    # fall back to OrderRequested.reason parsing
                    strategy = r.get('strategy') or ''
                    if not strategy:
                        strategy = _parse_strategy_from_reason(
                            r.get('buy_reason', ''))

                    # Recover order_id: prefer FillExecuted.order_id (broker's real ID),
                    # fall back to PositionOpened event_id
                    order_id = r.get('fill_order_id') or str(r.get('event_id', 'db_recovery'))

                    positions[ticker] = {
                        'entry_price':  entry_price,
                        'entry_time':   r.get('entry_time', 'db_restored'),
                        'quantity':     qty,
                        'qty':          qty,
                        'stop_price':   stop_price,
                        'target_price': target_price,
                        'half_target':  half_target,
                        'partial_done': False,
                        'order_id':     order_id,
                        'atr_value':    _safe_float(r.get('atr_value')),
                        'strategy':     strategy,
                        '_broker':      r.get('broker') or 'unknown',
                        '_source':      'db_event_store',
                    }

                log.info(
                    "[state] DB recovery: %d open position(s) from event_store",
                    len(positions))
                return positions
        finally:
            conn.close()
    except Exception as exc:
        log.warning("[state] DB position recovery failed (non-fatal): %s", exc)
        return {}


def _extract_if_valid(data: dict, source: str):
    """Extract state from data if it's from today or has useful positions.

    Returns (positions, reclaimed_today, trade_log) or None if data
    should be skipped.
    """
    saved_date = data.get('_date', '')
    today = datetime.now(ET).strftime('%Y-%m-%d')

    positions = data.get('positions', {})
    reclaimed_today = set(data.get('reclaimed_today', []))
    trade_log = data.get('trade_log', [])

    # Filter out known test positions that pollute state.
    # Use exact ticker names — never substring match (would hit SATX, SATS, etc.)
    _KNOWN_TEST_TICKERS = {
        'DUP_TEST', 'FAIL_TEST', 'SAT1', 'SAT2', 'SAT3',
        'OVER_SELL', 'DEDUP1', 'TEST_TICKER',
        'CRASH1', 'CRASH2', 'CRASH3', 'TEST', 'TEST2',
    }
    # Also catch any ticker with TEST/CRASH prefix that's clearly not a real symbol
    test_tickers = set(positions.keys()) & _KNOWN_TEST_TICKERS
    for t in list(positions.keys()):
        if t.startswith('CRASH') or t.startswith('TEST_') or t.startswith('FAKE'):
            test_tickers.add(t)
    if test_tickers:
        log.warning(
            "Removing %d test position(s) from state: %s",
            len(test_tickers), test_tickers)
        for t in test_tickers:
            del positions[t]
        # Also purge test trades from trade_log to prevent phantom P&L
        before = len(trade_log)
        trade_log = [t for t in trade_log if t.get('ticker', '') not in test_tickers]
        removed = before - len(trade_log)
        if removed:
            log.warning("Removed %d test trade(s) from trade_log (phantom P&L)", removed)

    if saved_date == today:
        # Today's state — use it
        if positions:
            log.info(
                "Restored %d position(s) from %s state: %s",
                len(positions), source, list(positions.keys()))
        if trade_log:
            log.info("Restored %d completed trade(s) from earlier today.", len(trade_log))
        return positions, reclaimed_today, trade_log

    if not saved_date or saved_date == 'unknown':
        # Missing metadata — accept if file modified today and has positions
        if positions:
            file_is_recent = _file_modified_today(_STATE_FILE)
            if file_is_recent:
                log.warning(
                    "State file (%s) has no date metadata but contains %d position(s): %s. "
                    "File modified today — accepting to preserve entry/stop/target data. "
                    "Reconciliation will validate against brokers.",
                    source, len(positions), list(positions.keys()))
                return positions, set(), []
            else:
                log.warning(
                    "State file (%s) has no date metadata, %d position(s), "
                    "but file not modified today — discarding to avoid stale data.",
                    source, len(positions))
                return None
        return None

    # State is from a different day
    if positions:
        log.warning(
            "State file (%s) is from %s (not today %s) but has %d position(s). "
            "Discarding — DB Tier 2 or broker reconciliation will recover.",
            source, saved_date, today, len(positions))
    else:
        log.info("State file (%s) is from %s — discarding (no positions).", source, saved_date)

    return {}, set(), []


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_strategy_from_reason(reason: str) -> str:
    """Extract strategy from OrderRequested reason string.

    Examples:
        'pro:sr_flip:T1:long'           → 'pro:sr_flip'
        'pop:VWAP_RECLAIM:momentum_entry' → 'pop:VWAP_RECLAIM'
        'VWAP reclaim'                  → 'vwap_reclaim'
        ''                              → 'unknown'
    """
    if not reason:
        return 'unknown'
    parts = reason.split(':')
    if len(parts) >= 2:
        return parts[0] + ':' + parts[1]
    return 'vwap_reclaim'


def _file_modified_today(path: str) -> bool:
    """Check if a file (or any of its backups) was modified today (ET)."""
    today = datetime.now(ET).date()
    for p in [path, path + '.prev', path + '.prev2']:
        try:
            mtime = os.path.getmtime(p)
            file_date = datetime.fromtimestamp(mtime, tz=ET).date()
            if file_date == today:
                return True
        except OSError:
            continue
    return False


def _read_backup(path: str):
    """Read a backup state file directly (bypasses SafeStateFile)."""
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _safe_float(val) -> float:
    """Convert to float, returning 0.0 on failure."""
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe_int(val) -> int:
    """Convert to int, returning 0 on failure."""
    try:
        return int(float(val)) if val is not None else 0
    except (TypeError, ValueError):
        return 0
