"""
T6 — State Engine
==================
Single source of truth for the portfolio's read-only snapshot.

The PositionManager mutates the shared `positions` / `trade_log` dicts
in-place (T5).  The State Engine subscribes to POSITION events and
maintains a *clean copy* of that state that the UI, Observability layer,
and Heartbeat emitter can read without acquiring any application-level lock.

The copy is only updated when a POSITION event arrives, so it is always
consistent at the event boundary — never mid-mutation.

Events consumed
---------------
  EventType.POSITION  (payload: PositionPayload)
  EventType.HEARTBEAT (payload: HeartbeatPayload) — reads snapshot to
                       populate the heartbeat, then re-emits a filled one
                       (optional; wiring is done in the monitor orchestrator)

Public read interface
---------------------
  engine.snapshot()   → dict  {
      'positions':       {ticker: pos_dict},
      'trade_log':       [trade_dict, ...],
      'total_pnl':       float,
      'n_trades':        int,
      'n_wins':          int,
      'open_tickers':    [str, ...],
  }

Usage
-----
    state_engine = StateEngine(bus)
    ...
    snap = state_engine.snapshot()
"""
from __future__ import annotations

import copy
import logging
import threading
from typing import Dict, List

from .event_bus import Event, EventBus, EventType
from .events import PositionPayload

log = logging.getLogger(__name__)


class StateEngine:
    """
    Read-only portfolio snapshot, updated on POSITION events.

    Thread-safe: a RLock guards all mutations to the snapshot.
    """

    def __init__(self, bus: EventBus):
        self._lock      = threading.RLock()
        self._positions: Dict[str, dict] = {}
        self._trade_log: List[dict]      = []
        self._fill_ledger = None  # V10: set via set_fill_ledger()

        bus.subscribe(EventType.POSITION, self._on_position)

    def set_fill_ledger(self, ledger) -> None:
        """V10: Attach FillLedger as P&L authority for snapshot()."""
        self._fill_ledger = ledger

    def seed(self, positions: dict, trade_log: list) -> None:
        """
        Pre-populate snapshot from restored on-disk state.

        Call once after __init__ when restarting mid-session so that
        HeartbeatEmitter / UI see the correct positions and trade history
        before any new POSITION events arrive.
        """
        with self._lock:
            self._positions = {k: copy.deepcopy(v) for k, v in positions.items()}
            self._trade_log = copy.deepcopy(trade_log)
        log.info(
            f"[StateEngine] Seeded: {len(self._positions)} open positions, "
            f"{len(self._trade_log)} completed trades"
        )

    # ── Event handler ────────────────────────────────────────────────────────

    def _on_position(self, event: Event) -> None:
        p: PositionPayload = event.payload

        with self._lock:
            if p.action == 'OPENED' and p.position is not None:
                self._positions[p.ticker] = copy.deepcopy(p.position)
                log.debug(f"[StateEngine] Opened {p.ticker}")

            elif p.action == 'PARTIAL_EXIT' and p.position is not None:
                self._positions[p.ticker] = copy.deepcopy(p.position)
                if p.pnl is not None:
                    self._trade_log.append({
                        'ticker': p.ticker,
                        'pnl':    p.pnl,
                        'reason': 'partial_exit',
                    })
                log.debug(f"[StateEngine] Partial exit {p.ticker} pnl={p.pnl}")

            elif p.action == 'CLOSED':
                self._positions.pop(p.ticker, None)
                if p.pnl is not None:
                    self._trade_log.append({
                        'ticker': p.ticker,
                        'pnl':    p.pnl,
                        'reason': 'closed',
                    })
                log.debug(f"[StateEngine] Closed {p.ticker} pnl={p.pnl}")

    # ── Public read interface ────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """
        Return an immutable-by-convention snapshot of current portfolio state.
        Caller should treat the returned dicts/lists as read-only.

        V10: P&L from FillLedger (FIFO-matched, survives restarts) when available.
        Fallback: sum of trade_log P&L (in-memory, lost on restart).
        """
        with self._lock:
            positions    = copy.deepcopy(self._positions)
            trade_log    = copy.deepcopy(self._trade_log)

        # V10: FillLedger is P&L authority
        if self._fill_ledger:
            try:
                total_pnl = self._fill_ledger.daily_realized_pnl()
                # Use FillLedger's match count for today's trades
                from datetime import datetime
                from zoneinfo import ZoneInfo
                today = datetime.now(ZoneInfo('America/New_York')).date()
                n_trades = sum(1 for m in self._fill_ledger._matches
                               if m.matched_at.date() == today)
                n_wins = sum(1 for m in self._fill_ledger._matches
                             if m.matched_at.date() == today and m.realized_pnl >= 0)
            except Exception:
                total_pnl = sum(t.get('pnl', 0.0) or 0.0 for t in trade_log)
                n_trades = len(trade_log)
                n_wins = sum(1 for t in trade_log if (t.get('pnl') or 0.0) >= 0)
        else:
            total_pnl = sum(t.get('pnl', 0.0) or 0.0 for t in trade_log)
            n_trades  = len(trade_log)
            n_wins    = sum(1 for t in trade_log if (t.get('pnl') or 0.0) >= 0)

        return {
            'positions':    positions,
            'trade_log':    trade_log,
            'total_pnl':    round(total_pnl, 2),
            'n_trades':     n_trades,
            'n_wins':       n_wins,
            'open_tickers': sorted(positions.keys()),
        }
