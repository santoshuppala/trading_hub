"""
FillLedger — append-only fill lot storage with FIFO matching.

The single source of truth for position tracking. All position state is
derived from the lot sequence — never stored directly.

Core API:
    append(lot)         — Add a fill lot. SELL lots trigger FIFO matching.
    open_lots(ticker)   — Get open BUY lots for a ticker (FIFO order).
    open_tickers()      — Set of tickers with open positions.
    total_qty(ticker)   — Sum of remaining qty across open lots.

P&L API:
    daily_realized_pnl(day) — Sum of realized P&L for a specific date.
    unrealized_pnl(prices)  — Unrealized P&L given current prices.

Persistence:
    persist()           — Write ALL today's lots + open lots to file.
    load()              — Read from file (Tier 1) or empty (Tier 2).

Thread safety:
    RLock on all operations. Re-entrant for projection reads.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .fill_lot import (
    FillLot, LotState, LotMatch, PositionMeta, QTY_EPSILON,
    fill_lot_to_dict, fill_lot_from_dict,
)
from .lot_matcher import LotMatcher

log = logging.getLogger(__name__)

ET = ZoneInfo('America/New_York')


class FillLedger:
    """Append-only fill lot ledger with FIFO matching and persistence."""

    def __init__(self, state_file: str = None):
        if state_file is None:
            from config import FILL_LEDGER_PATH
            state_file = FILL_LEDGER_PATH
        self._lots: list[FillLot] = []
        self._lot_states: dict[str, LotState] = {}          # lot_id → state
        self._matches: list[LotMatch] = []                   # accumulated matches
        self._position_meta: dict[str, PositionMeta] = {}    # ticker → meta
        self._matcher = LotMatcher()
        self._lock = threading.RLock()
        self._state_file_path = state_file

        # Performance caches (invalidated on append)
        self._open_tickers_cache: Optional[set[str]] = None

        # Dedup: prevent duplicate lot appends (e.g., from event replay)
        self._processed_lot_ids: set[str] = set()

        # Track last fill time for reconciliation dampening
        self._last_fill_time: float = 0.0

    # ═══════════════════════════════════════════════════════════════════
    # CORE API
    # ═══════════════════════════════════════════════════════════════════

    def append(self, lot: FillLot) -> list[LotMatch]:
        """Append a fill lot. If SELL, FIFO-match and return LotMatches.

        Policies:
          - Duplicate lot_id → skip (dedup), return []
          - BUY for existing ticker → ALLOWED (multiple lots per ticker)
          - SELL with no open lots → REJECTED (logged, not appended), return []
          - Overfill → matched up to total open qty, overfill logged

        Returns:
            List of LotMatch records (empty for BUY lots).
        """
        with self._lock:
            # Dedup check
            if lot.lot_id in self._processed_lot_ids:
                log.debug("[FillLedger] Duplicate lot skipped: %s", lot.lot_id)
                return []

            # SELL validation: must have open lots to match against
            if lot.side == 'SELL':
                open_lots = self._open_lots_unsafe(lot.ticker)
                if not open_lots:
                    log.error(
                        "[FillLedger] SELL for %s but no open lots. "
                        "Rejecting lot %s (possible late/phantom fill).",
                        lot.ticker, lot.lot_id,
                    )
                    return []

            # Append the lot
            self._lots.append(lot)
            self._processed_lot_ids.add(lot.lot_id)

            # Dedup cap: prevent unbounded memory growth
            if len(self._processed_lot_ids) > 20000:
                self._processed_lot_ids = set(
                    list(self._processed_lot_ids)[-10000:]
                )

            # Process based on side
            matches: list[LotMatch] = []

            if lot.side == 'BUY':
                self._lot_states[lot.lot_id] = LotState(
                    lot_id=lot.lot_id,
                    original_qty=lot.qty,
                    remaining_qty=lot.qty,
                )

            elif lot.side == 'SELL':
                open_lots = self._open_lots_unsafe(lot.ticker)
                matches = self._matcher.match_sell(lot, open_lots)
                self._matches.extend(matches)

                # Clean up PositionMeta if position fully closed
                remaining = self._total_qty_unsafe(lot.ticker)
                if remaining <= QTY_EPSILON:
                    self._position_meta.pop(lot.ticker, None)

            # Track fill time for reconciliation dampening
            import time
            self._last_fill_time = time.monotonic()

            # Invalidate caches
            self._invalidate_cache()

            # Persist after every mutation
            self._persist()

            if lot.side == 'BUY':
                log.info(
                    "[FillLedger] BUY lot %s: %s %.4f@$%.2f (%s, %s)",
                    lot.lot_id[:8], lot.ticker, lot.qty,
                    lot.fill_price, lot.broker, lot.strategy,
                )
            elif matches:
                total_pnl = sum(m.realized_pnl for m in matches)
                log.info(
                    "[FillLedger] SELL lot %s: %s %.4f@$%.2f → %d matches, "
                    "pnl=$%.2f",
                    lot.lot_id[:8], lot.ticker, lot.qty,
                    lot.fill_price, len(matches), total_pnl,
                )

            return matches

    # ═══════════════════════════════════════════════════════════════════
    # QUERY API
    # ═══════════════════════════════════════════════════════════════════

    def close_stale_lots(self, active_tickers: set) -> int:
        """Close open lots for tickers NOT in active_tickers.

        Called after startup reconciliation to clean up lots whose positions
        were closed at the broker while we were down (bracket stops, etc.).

        Args:
            active_tickers: set of tickers that actually have open positions
                           (from bot_state.json, which is reconciled with broker)

        Returns:
            Number of lots closed.
        """
        closed = 0
        with self._lock:
            open_tickers = self._open_tickers_cache or self.open_tickers()
            stale_tickers = open_tickers - active_tickers

            for ticker in stale_tickers:
                stale_lots = self._open_lots_unsafe(ticker)
                for lot, state in stale_lots:
                    # Close by setting remaining_qty to 0
                    state.remaining_qty = 0
                    closed += 1
                    log.warning(
                        "[FillLedger] Closed stale lot %s: %s %s@$%.2f "
                        "(position closed at broker, lot was orphaned)",
                        lot.lot_id[:8], ticker, lot.qty, lot.fill_price)

            if closed:
                self._open_tickers_cache = None  # invalidate cache
                # Replay FIFO matching to recompute _matches without stale lots
                self._matches = LotMatcher.replay_matches(self._lots, self._lot_states)
                self._persist()
                log.info("[FillLedger] Closed %d stale lots across %d tickers | "
                         "daily_pnl=$%.2f after re-match",
                         closed, len(stale_tickers), self.daily_realized_pnl())

        return closed

    def open_lots(self, ticker: str) -> list[tuple[FillLot, LotState]]:
        """Open BUY lots with remaining_qty > 0, in FIFO (append) order."""
        with self._lock:
            return list(self._open_lots_unsafe(ticker))

    def _open_lots_unsafe(self, ticker: str) -> list[tuple[FillLot, LotState]]:
        """Internal — caller must hold _lock."""
        return [
            (lot, self._lot_states[lot.lot_id])
            for lot in self._lots
            if (lot.ticker == ticker
                and lot.side == 'BUY'
                and lot.lot_id in self._lot_states
                and not self._lot_states[lot.lot_id].is_closed)
        ]

    def open_tickers(self) -> set[str]:
        """Set of tickers with at least one open lot. O(1) via cache."""
        with self._lock:
            if self._open_tickers_cache is None:
                self._open_tickers_cache = {
                    lot.ticker
                    for lot in self._lots
                    if (lot.side == 'BUY'
                        and lot.lot_id in self._lot_states
                        and not self._lot_states[lot.lot_id].is_closed)
                }
            return set(self._open_tickers_cache)  # return copy

    def total_qty(self, ticker: str) -> float:
        """Sum of remaining_qty across all open lots for ticker."""
        with self._lock:
            return self._total_qty_unsafe(ticker)

    def _total_qty_unsafe(self, ticker: str) -> float:
        return sum(s.remaining_qty for _, s in self._open_lots_unsafe(ticker))

    def weighted_avg_entry(self, ticker: str) -> float:
        """Qty-weighted average fill_price of open lots."""
        with self._lock:
            lots = self._open_lots_unsafe(ticker)
            total_qty = sum(s.remaining_qty for _, s in lots)
            if total_qty <= QTY_EPSILON:
                return 0.0
            return sum(
                lot.fill_price * s.remaining_qty for lot, s in lots
            ) / total_qty

    def cost_basis(self, ticker: str) -> float:
        """Total cost basis: sum(fill_price * remaining_qty) for open lots."""
        with self._lock:
            return sum(
                lot.fill_price * s.remaining_qty
                for lot, s in self._open_lots_unsafe(ticker)
            )

    def broker_qty(self, ticker: str) -> dict[str, float]:
        """Per-broker remaining quantity. {broker_name: total_remaining_qty}."""
        with self._lock:
            bq: dict[str, float] = {}
            for lot, state in self._open_lots_unsafe(ticker):
                bq[lot.broker] = bq.get(lot.broker, 0.0) + state.remaining_qty
            return bq

    # ═══════════════════════════════════════════════════════════════════
    # META API (for ExecutionFeedback, signals.py trailing stops)
    # ═══════════════════════════════════════════════════════════════════

    def get_meta(self, ticker: str) -> Optional[PositionMeta]:
        """Get mutable PositionMeta for a ticker (or None)."""
        return self._position_meta.get(ticker)

    def set_meta(self, ticker: str, meta: PositionMeta) -> None:
        """Set PositionMeta for a ticker (on position open)."""
        self._position_meta[ticker] = meta
        self._persist()

    def patch_meta(self, ticker: str, **fields) -> None:
        """Partial update of PositionMeta fields.

        NOTE: Does NOT persist on every patch — trailing stops update
        every BAR (~60s). Persistence happens on the next append() call
        or explicit persist(). This avoids 183 file writes per minute.
        """
        meta = self._position_meta.get(ticker)
        if meta:
            meta.update(**fields)

    # ═══════════════════════════════════════════════════════════════════
    # P&L API
    # ═══════════════════════════════════════════════════════════════════

    def realized_pnl(self) -> float:
        """Total realized P&L across all matches."""
        with self._lock:
            return sum(m.realized_pnl for m in self._matches)

    def daily_realized_pnl(self, day: Optional[date] = None) -> float:
        """Realized P&L for a specific date (default: today ET)."""
        if day is None:
            day = datetime.now(ET).date()
        with self._lock:
            return sum(
                m.realized_pnl for m in self._matches
                if m.matched_at.date() == day
            )

    def unrealized_pnl(self, prices: dict[str, float]) -> float:
        """Unrealized P&L given current market prices.

        Args:
            prices: {ticker: current_price} dict from latest BAR events.
        """
        with self._lock:
            total = 0.0
            for ticker in (self._open_tickers_cache or self.open_tickers()):
                current = prices.get(ticker, 0.0)
                if current <= 0:
                    continue
                for lot, state in self._open_lots_unsafe(ticker):
                    total += (current - lot.fill_price) * state.remaining_qty
            return round(total, 2)

    def all_matches(self) -> list[LotMatch]:
        """All accumulated LotMatch records."""
        with self._lock:
            return list(self._matches)

    def daily_matches(self, day: Optional[date] = None) -> list[LotMatch]:
        """LotMatch records for a specific date."""
        if day is None:
            day = datetime.now(ET).date()
        with self._lock:
            return [m for m in self._matches if m.matched_at.date() == day]

    # ═══════════════════════════════════════════════════════════════════
    # SMART ROUTER INTEGRATION
    # ═══════════════════════════════════════════════════════════════════

    def preview_sell_matches(self, ticker: str, qty: float) -> dict[str, float]:
        """Preview broker split for a SELL — does NOT mutate state.

        Advisory only. The actual FIFO match on FILL is authoritative.
        """
        with self._lock:
            return self._matcher.preview_matches(
                ticker, qty, self._open_lots_unsafe(ticker),
            )

    # ═══════════════════════════════════════════════════════════════════
    # RECONCILIATION HELPERS
    # ═══════════════════════════════════════════════════════════════════

    @property
    def seconds_since_last_fill(self) -> float:
        """Seconds since last fill was appended. For reconciliation dampening."""
        import time
        if self._last_fill_time == 0:
            return float('inf')
        return time.monotonic() - self._last_fill_time

    # ═══════════════════════════════════════════════════════════════════
    # CACHE
    # ═══════════════════════════════════════════════════════════════════

    def _invalidate_cache(self):
        """Called inside _lock after every append."""
        self._open_tickers_cache = None

    # ═══════════════════════════════════════════════════════════════════
    # PERSISTENCE
    # ═══════════════════════════════════════════════════════════════════

    def persist(self) -> None:
        """Explicit persist (thread-safe wrapper)."""
        with self._lock:
            self._persist()

    def _persist(self):
        """Persist ALL today's lots (open+closed) for daily P&L reconstruction.

        On restart, FIFO replay over all lots derives LotMatches and daily P&L.
        This ensures kill switch limits are NOT reset after a crash.

        Historical lots (prior days, fully closed) are archived to DB only.
        File size bounded: ~100 trades/day × ~200 bytes = ~20KB.
        """
        try:
            from lifecycle.safe_state import SafeStateFile
        except ImportError:
            log.warning("[FillLedger] SafeStateFile not available, skipping persist")
            return

        today = datetime.now(ET).date()

        # Collect lots to persist:
        # 1. ALL lots from today (open or closed) — for P&L reconstruction
        # 2. Open lots from prior days — for position tracking
        lots_to_persist = []
        for lot in self._lots:
            is_today = lot.timestamp.date() == today
            is_open_buy = (
                lot.side == 'BUY'
                and lot.lot_id in self._lot_states
                and not self._lot_states[lot.lot_id].is_closed
            )
            if is_today or is_open_buy:
                lots_to_persist.append(lot)

        # V10: Compute daily P&L and open position summary for warm-path readers
        # (watchdog, email). Pre-computed here so consumers don't need FIFO logic.
        _daily_pnl = sum(m.realized_pnl for m in self._matches
                         if m.matched_at.date() == today)
        _open_tickers = self._open_tickers_cache or self.open_tickers()
        _open_summary = {}
        for ticker in _open_tickers:
            lots = self._open_lots_unsafe(ticker)
            total_qty = sum(s.remaining_qty for _, s in lots)
            if total_qty > QTY_EPSILON:
                cost = sum(lot.fill_price * s.remaining_qty for lot, s in lots)
                avg_entry = cost / total_qty
                strategy = lots[0][0].strategy if lots else ''
                meta = self._position_meta.get(ticker)
                _open_summary[ticker] = {
                    'qty': round(total_qty, 4),
                    'avg_entry': round(avg_entry, 4),
                    'strategy': strategy,
                    'stop_price': round(meta.stop_price, 4) if meta else 0,
                    'target_price': round(meta.target_price, 4) if meta else 0,
                }

        state = {
            '_format': 'fill_ledger_v2',
            '_date': today.isoformat(),
            '_version': int(time.time()),
            '_timestamp': datetime.now(ET).isoformat(),
            # V10: Pre-computed for warm-path readers (watchdog, email)
            'daily_pnl': round(_daily_pnl, 2),
            'open_positions': _open_summary,
            'trade_count': sum(1 for m in self._matches if m.matched_at.date() == today),
            'total_matches': len(self._matches),
            # Raw data for FIFO replay on restart
            'lots': [fill_lot_to_dict(l) for l in lots_to_persist],
            'lot_states': {
                k: v.to_dict()
                for k, v in self._lot_states.items()
            },
            'position_meta': {
                k: v.to_dict()
                for k, v in self._position_meta.items()
                if k in _open_tickers
            },
        }

        sf = SafeStateFile(self._state_file_path, max_age_seconds=120.0)
        sf.write(state)

    def load(self) -> None:
        """Load state from file. Falls back to empty (reconciliation imports).

        On load:
        1. Read lots + lot_states + position_meta from file
        2. Replay FIFO matching to reconstruct LotMatch records
        3. This gives us accurate daily P&L for kill switch seeding
        """
        try:
            from lifecycle.safe_state import SafeStateFile
        except ImportError:
            log.warning("[FillLedger] SafeStateFile not available, starting empty")
            return

        sf = SafeStateFile(self._state_file_path, max_age_seconds=86400.0)
        data, fresh = sf.read()

        if data is None:
            log.info("[FillLedger] No state file found — starting empty "
                     "(reconciliation will import from brokers)")
            return

        fmt = data.get('_format', '')
        if fmt not in ('fill_ledger_v1', 'fill_ledger_v2'):
            log.warning("[FillLedger] Unknown format %r — starting empty", fmt)
            return

        with self._lock:
            # Deserialize lots
            self._lots = []
            for d in data.get('lots', []):
                try:
                    self._lots.append(fill_lot_from_dict(d))
                except Exception as exc:
                    log.warning("[FillLedger] Bad lot data, skipping: %s", exc)

            # Deserialize lot states
            self._lot_states = {}
            for lot_id, sd in data.get('lot_states', {}).items():
                try:
                    self._lot_states[lot_id] = LotState.from_dict(sd)
                except Exception as exc:
                    log.warning("[FillLedger] Bad state data for %s: %s", lot_id, exc)

            # Ensure every BUY lot has a state
            for lot in self._lots:
                if lot.side == 'BUY' and lot.lot_id not in self._lot_states:
                    self._lot_states[lot.lot_id] = LotState(
                        lot_id=lot.lot_id,
                        original_qty=lot.qty,
                        remaining_qty=lot.qty,
                    )

            # Replay FIFO matching to reconstruct matches (for daily P&L)
            self._matches = LotMatcher.replay_matches(
                self._lots, self._lot_states,
            )

            # Deserialize position meta
            self._position_meta = {}
            for ticker, md in data.get('position_meta', {}).items():
                try:
                    self._position_meta[ticker] = PositionMeta.from_dict(md)
                except Exception as exc:
                    log.warning("[FillLedger] Bad meta for %s: %s", ticker, exc)

            # Populate dedup set
            self._processed_lot_ids = {lot.lot_id for lot in self._lots}

            # Invalidate caches
            self._invalidate_cache()

            n_open = len(self.open_tickers())
            n_matches = len(self._matches)
            daily_pnl = self.daily_realized_pnl()
            log.info(
                "[FillLedger] Loaded %d lots, %d states, %d matches | "
                "open=%d tickers | daily_pnl=$%.2f",
                len(self._lots), len(self._lot_states), n_matches,
                n_open, daily_pnl,
            )

    # ═══════════════════════════════════════════════════════════════════
    # INTROSPECTION (for debugging / shadow validation)
    # ═══════════════════════════════════════════════════════════════════

    @property
    def lot_count(self) -> int:
        return len(self._lots)

    @property
    def open_position_count(self) -> int:
        return len(self.open_tickers())

    def summary(self) -> dict:
        """Summary dict for logging / status endpoints."""
        with self._lock:
            return {
                'total_lots': len(self._lots),
                'open_tickers': sorted(self.open_tickers()),
                'open_positions': len(self.open_tickers()),
                'total_matches': len(self._matches),
                'realized_pnl': self.realized_pnl(),
                'daily_realized_pnl': self.daily_realized_pnl(),
            }
