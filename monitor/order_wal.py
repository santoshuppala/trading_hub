"""
Order Write-Ahead Log (WAL) — durable order lifecycle tracking.

Every order goes through a strict state machine. Each transition is
appended to a line-buffered file BEFORE the next action. If the system
crashes at any point, the WAL tells recovery exactly where the order was
and what to do next.

State Machine:
    INTENT → SUBMITTED → ACKED → FILLED → RECORDED   (happy path)
    INTENT → CANCELLED                                  (signal cancelled)
    SUBMITTED → REJECTED                                (broker rejected)
    ACKED → CANCELLED                                   (timeout/manual)
    ACKED → PARTIAL → FILLED → RECORDED                (partial fills)
    ORPHANED → RECORDED                                 (found at broker, not in WAL)

Latency: ~0.01ms per write (append + flush to SSD). Negligible vs 200ms+ broker calls.

File Format: JSON Lines (one JSON object per line, newline-delimited).
    {"seq":1,"state":"INTENT","client_id":"uuid-1","ticker":"AAPL","side":"BUY",...}
    {"seq":2,"state":"SUBMITTED","client_id":"uuid-1","broker_order_id":"ord-xyz",...}

Recovery:
    1. Replay WAL → group by client_id → find latest state per order
    2. Non-terminal orders (not RECORDED/CANCELLED/REJECTED):
       INTENT only     → never submitted, safe to ignore
       SUBMITTED/ACKED → query broker by client_order_id, import fill or cancel
       FILLED          → create FillLot from WAL data, mark RECORDED
    3. Terminal orders → skip

Usage:
    from monitor.order_wal import wal

    # Before order submission
    wal.intent(client_id, ticker='AAPL', side='BUY', qty=10, price=150.0)

    # After broker API returns
    wal.submitted(client_id, broker_order_id='ord-xyz', broker='alpaca')

    # After fill confirmed
    wal.filled(client_id, fill_price=150.05, fill_qty=10)

    # After FillLedger.append()
    wal.recorded(client_id, lot_id='lot-aaa')
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAL_DIR = os.path.join(PROJECT_ROOT, 'data')


class OrderState(str, Enum):
    INTENT    = 'INTENT'      # decided to trade, not yet submitted
    SUBMITTED = 'SUBMITTED'   # API call made, broker returned order_id
    ACKED     = 'ACKED'       # broker confirmed order is live at exchange
    PARTIAL   = 'PARTIAL'     # partially filled
    FILLED    = 'FILLED'      # fully filled, not yet recorded in FillLedger
    RECORDED  = 'RECORDED'    # fill recorded in FillLedger (terminal)
    CANCELLED = 'CANCELLED'   # cancelled by us or broker (terminal)
    REJECTED  = 'REJECTED'    # broker rejected (terminal)
    ORPHANED  = 'ORPHANED'    # found at broker during reconciliation, no WAL entry
    FAILED    = 'FAILED'      # order failed after all retries (terminal)

# Terminal states — no further transitions expected
TERMINAL_STATES = {
    OrderState.RECORDED,
    OrderState.CANCELLED,
    OrderState.REJECTED,
    OrderState.FAILED,
}

# Valid transitions — anything else is logged as a warning
VALID_TRANSITIONS = {
    OrderState.INTENT:    {OrderState.SUBMITTED, OrderState.CANCELLED, OrderState.FAILED},
    OrderState.SUBMITTED: {OrderState.ACKED, OrderState.FILLED, OrderState.REJECTED,
                           OrderState.CANCELLED, OrderState.FAILED},
    OrderState.ACKED:     {OrderState.PARTIAL, OrderState.FILLED, OrderState.CANCELLED},
    OrderState.PARTIAL:   {OrderState.FILLED, OrderState.CANCELLED},
    OrderState.FILLED:    {OrderState.RECORDED},
    OrderState.ORPHANED:  {OrderState.RECORDED, OrderState.CANCELLED},
}


@dataclass
class WALEntry:
    """A single WAL entry representing one state transition."""
    seq: int
    state: str
    client_id: str
    ts: float           # time.time()
    ticker: str = ''
    side: str = ''       # BUY | SELL
    qty: float = 0
    price: float = 0
    broker: str = ''
    broker_order_id: str = ''
    client_order_id: str = ''
    fill_price: float = 0
    fill_qty: float = 0
    lot_id: str = ''
    reason: str = ''
    attempt: int = 0
    extra: dict = None

    def to_dict(self) -> dict:
        d = {
            'seq': self.seq,
            'state': self.state,
            'client_id': self.client_id,
            'ts': self.ts,
        }
        # Only include non-default fields to keep WAL compact
        if self.ticker:          d['ticker'] = self.ticker
        if self.side:            d['side'] = self.side
        if self.qty:             d['qty'] = self.qty
        if self.price:           d['price'] = self.price
        if self.broker:          d['broker'] = self.broker
        if self.broker_order_id: d['broker_order_id'] = self.broker_order_id
        if self.client_order_id: d['client_order_id'] = self.client_order_id
        if self.fill_price:      d['fill_price'] = self.fill_price
        if self.fill_qty:        d['fill_qty'] = self.fill_qty
        if self.lot_id:          d['lot_id'] = self.lot_id
        if self.reason:          d['reason'] = self.reason
        if self.attempt:         d['attempt'] = self.attempt
        if self.extra:           d['extra'] = self.extra
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'WALEntry':
        return cls(
            seq=d.get('seq', 0),
            state=d.get('state', ''),
            client_id=d.get('client_id', ''),
            ts=d.get('ts', 0),
            ticker=d.get('ticker', ''),
            side=d.get('side', ''),
            qty=d.get('qty', 0),
            price=d.get('price', 0),
            broker=d.get('broker', ''),
            broker_order_id=d.get('broker_order_id', ''),
            client_order_id=d.get('client_order_id', ''),
            fill_price=d.get('fill_price', 0),
            fill_qty=d.get('fill_qty', 0),
            lot_id=d.get('lot_id', ''),
            reason=d.get('reason', ''),
            attempt=d.get('attempt', 0),
            extra=d.get('extra'),
        )


class OrderWAL:
    """
    Append-only Write-Ahead Log for order lifecycle tracking.

    Thread-safe. File is line-buffered (flushed after every write).
    Daily rotation: new file per trading day.

    Usage:
        wal = OrderWAL()
        client_id = wal.new_client_id()
        wal.intent(client_id, ticker='AAPL', side='BUY', qty=10, price=150.0)
        wal.submitted(client_id, broker_order_id='ord-xyz', broker='alpaca')
        wal.filled(client_id, fill_price=150.05, fill_qty=10)
        wal.recorded(client_id, lot_id='lot-aaa')
    """

    def __init__(self, wal_dir: str = WAL_DIR):
        self._wal_dir = wal_dir
        os.makedirs(wal_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = 0
        self._fd: Optional[object] = None
        self._current_date: str = ''
        self._initialized = False

        # In-memory index: client_id → latest state (for fast lookups)
        self._order_states: Dict[str, str] = {}

        # Lazy init: don't open file until first write or read.
        # Prevents test imports from creating WAL files in production data/.

    # ── File management ───────────────────────────────────────────────

    def _wal_path(self, date_str: str = '') -> str:
        if not date_str:
            date_str = datetime.now(ET).strftime('%Y%m%d')
        return os.path.join(self._wal_dir, f'order_wal_{date_str}.log')

    def _open_file(self):
        today = datetime.now(ET).strftime('%Y%m%d')
        if self._fd and self._current_date == today:
            return
        if self._fd:
            try:
                self._fd.close()
            except Exception:
                pass
        path = self._wal_path(today)
        self._fd = open(path, 'a', buffering=1)  # line-buffered
        self._current_date = today

        # Replay existing entries to set sequence counter and state index
        self._replay_file(path)

    def _replay_file(self, path: str):
        """Replay WAL file to rebuild in-memory state index."""
        if not os.path.exists(path):
            return
        try:
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        seq = entry.get('seq', 0)
                        if seq > self._seq:
                            self._seq = seq
                        cid = entry.get('client_id', '')
                        if cid:
                            self._order_states[cid] = entry.get('state', '')
                    except json.JSONDecodeError:
                        continue
        except Exception as exc:
            log.warning("[OrderWAL] Failed to replay %s: %s", path, exc)

    # ── Core write ────────────────────────────────────────────────────

    def _ensure_initialized(self):
        """Lazy init: open file on first access, not on import."""
        if not self._initialized:
            self._open_file()
            self._initialized = True

    def _write(self, entry: WALEntry):
        """Append entry to WAL. Thread-safe, line-buffered."""
        with self._lock:
            self._ensure_initialized()
            # Daily rotation check
            today = datetime.now(ET).strftime('%Y%m%d')
            if today != self._current_date:
                self._open_file()

            self._seq += 1
            entry.seq = self._seq

            # Validate state transition
            cid = entry.client_id
            prev_state = self._order_states.get(cid)
            if prev_state and prev_state not in {s.value for s in TERMINAL_STATES}:
                try:
                    prev = OrderState(prev_state)
                    curr = OrderState(entry.state)
                    if curr not in VALID_TRANSITIONS.get(prev, set()):
                        log.warning(
                            "[OrderWAL] Invalid transition %s → %s for %s (%s)",
                            prev_state, entry.state, cid[:8], entry.ticker)
                except ValueError:
                    pass

            # Write to file
            try:
                line = json.dumps(entry.to_dict(), separators=(',', ':'))
                self._fd.write(line + '\n')
                self._fd.flush()
                os.fsync(self._fd.fileno())
            except Exception as exc:
                log.error("[OrderWAL] Write failed: %s", exc)
                return

            # Update in-memory index
            self._order_states[cid] = entry.state

    # ── Public API: state transitions ─────────────────────────────────

    @staticmethod
    def new_client_id() -> str:
        """Generate a new unique client ID for an order."""
        return str(uuid.uuid4())

    def intent(self, client_id: str, *, ticker: str, side: str, qty: float,
               price: float, reason: str = '', **extra) -> None:
        """Record intent to place an order. Call BEFORE emitting ORDER_REQ."""
        self._write(WALEntry(
            seq=0, state=OrderState.INTENT.value, client_id=client_id,
            ts=time.time(), ticker=ticker, side=side, qty=qty, price=price,
            reason=reason, extra=extra if extra else None,
        ))

    def submitted(self, client_id: str, *, broker: str,
                  broker_order_id: str = '', client_order_id: str = '',
                  attempt: int = 0) -> None:
        """Record that the order was submitted to broker. Call AFTER API returns."""
        self._write(WALEntry(
            seq=0, state=OrderState.SUBMITTED.value, client_id=client_id,
            ts=time.time(), broker=broker, broker_order_id=broker_order_id,
            client_order_id=client_order_id, attempt=attempt,
        ))

    def acked(self, client_id: str, *, broker_order_id: str = '') -> None:
        """Record broker acknowledgment (order is live at exchange)."""
        self._write(WALEntry(
            seq=0, state=OrderState.ACKED.value, client_id=client_id,
            ts=time.time(), broker_order_id=broker_order_id,
        ))

    def filled(self, client_id: str, *, fill_price: float, fill_qty: float,
               broker_order_id: str = '') -> None:
        """Record fill confirmation. Call BEFORE emitting FILL event."""
        self._write(WALEntry(
            seq=0, state=OrderState.FILLED.value, client_id=client_id,
            ts=time.time(), fill_price=fill_price, fill_qty=fill_qty,
            broker_order_id=broker_order_id,
        ))

    def recorded(self, client_id: str, *, lot_id: str = '') -> None:
        """Record that fill was persisted to FillLedger. Terminal state."""
        self._write(WALEntry(
            seq=0, state=OrderState.RECORDED.value, client_id=client_id,
            ts=time.time(), lot_id=lot_id,
        ))

    def cancelled(self, client_id: str, *, reason: str = '') -> None:
        """Record order cancellation. Terminal state."""
        self._write(WALEntry(
            seq=0, state=OrderState.CANCELLED.value, client_id=client_id,
            ts=time.time(), reason=reason,
        ))

    def rejected(self, client_id: str, *, reason: str = '') -> None:
        """Record broker rejection. Terminal state."""
        self._write(WALEntry(
            seq=0, state=OrderState.REJECTED.value, client_id=client_id,
            ts=time.time(), reason=reason,
        ))

    def failed(self, client_id: str, *, reason: str = '') -> None:
        """Record order failure after all retries. Terminal state."""
        self._write(WALEntry(
            seq=0, state=OrderState.FAILED.value, client_id=client_id,
            ts=time.time(), reason=reason,
        ))

    # ── Recovery API ──────────────────────────────────────────────────

    def get_incomplete_orders(self, wal_date: str = '') -> List[Dict]:
        """Get all orders that haven't reached a terminal state.


        Returns list of dicts with merged fields from all WAL entries
        for each incomplete order. Used by startup recovery to determine
        what needs to be resolved with the broker.

        Args:
            wal_date: date string YYYYMMDD (default: today)
        """
        path = self._wal_path(wal_date)
        if not os.path.exists(path):
            return []

        # Group all entries by client_id
        orders: Dict[str, Dict] = OrderedDict()
        try:
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cid = entry.get('client_id', '')
                    if not cid:
                        continue
                    if cid not in orders:
                        orders[cid] = {}
                    # Merge fields (later entries override earlier ones)
                    orders[cid].update(entry)
        except Exception as exc:
            log.warning("[OrderWAL] Failed to read %s: %s", path, exc)
            return []

        # Filter to non-terminal orders
        incomplete = []
        for cid, merged in orders.items():
            state = merged.get('state', '')
            if state not in {s.value for s in TERMINAL_STATES}:
                incomplete.append(merged)

        return incomplete

    def get_all_orders_today(self) -> Dict[str, Dict]:
        """Get all orders for today, keyed by client_id. For diagnostics."""
        path = self._wal_path()
        if not os.path.exists(path):
            return {}

        orders: Dict[str, Dict] = OrderedDict()
        try:
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    cid = entry.get('client_id', '')
                    if cid:
                        if cid not in orders:
                            orders[cid] = {}
                        orders[cid].update(entry)
        except Exception:
            pass
        return orders

    def stats(self) -> dict:
        """WAL statistics for diagnostics."""
        with self._lock:
            states = {}
            for state in self._order_states.values():
                states[state] = states.get(state, 0) + 1
            return {
                'total_orders': len(self._order_states),
                'seq': self._seq,
                'states': states,
                'file': self._wal_path(),
                'incomplete': sum(
                    1 for s in self._order_states.values()
                    if s not in {st.value for st in TERMINAL_STATES}
                ),
            }

    def close(self):
        """Close the WAL file. Called on shutdown."""
        with self._lock:
            if self._fd:
                try:
                    self._fd.flush()
                    self._fd.close()
                except Exception:
                    pass
                self._fd = None


# ── Module-level singleton ────────────────────────────────────────────────
# Import as: from monitor.order_wal import wal
wal = OrderWAL()
