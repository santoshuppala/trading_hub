"""
Position Projection — backward-compatible dict view over FillLedger.

The 11 consumers of the positions dict expect:
    positions[ticker] = {
        entry_price, entry_time, quantity, qty, partial_done, order_id,
        stop_price, target_price, half_target, atr_value,
        strategy, _broker, _broker_qty, _source, _cost_basis
    }

PositionProjection derives this dict from FillLedger's open lots.
PositionProjectionProxy wraps it in a dict-like interface so existing
code works without changes.

MutablePositionDict intercepts stop/target mutations and writes them
through to PositionMeta (solves M2: trailing stop mutations not lost).
"""
from __future__ import annotations

import logging
from typing import Optional

from .fill_lot import QTY_EPSILON
from .fill_ledger import FillLedger

log = logging.getLogger(__name__)


class MutablePositionDict(dict):
    """Dict that writes through stop/target/atr mutations to PositionMeta.

    When signals.py does `pos['stop_price'] = new_stop`, this dict
    intercepts the write and forwards it to ledger.patch_meta().
    Without this, trailing stop updates would be lost on the next
    projection refresh.

    Only strategy-level fields are written through. Core fields like
    'quantity' and 'entry_price' are derived from lots and cannot be
    mutated directly.
    """
    _META_FIELDS = frozenset({
        'stop_price', 'target_price', 'half_target', 'atr_value', 'partial_done',
    })

    def __init__(self, data: dict, ticker: str, ledger: FillLedger):
        super().__init__(data)
        self._ticker = ticker
        self._ledger = ledger

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        # Write through strategy fields to PositionMeta
        if key in self._META_FIELDS:
            self._ledger.patch_meta(self._ticker, **{key: value})


class PositionProjection:
    """Derives legacy position dicts from FillLedger state.

    Each projected dict has the exact same keys that the 11 consumers
    read from the old positions dict.
    """

    def __init__(self, ledger: FillLedger):
        self._ledger = ledger

    def project(self) -> dict[str, dict]:
        """Build {ticker: position_dict} for ALL open positions."""
        result = {}
        for ticker in self._ledger.open_tickers():
            pos = self.project_ticker(ticker)
            if pos is not None:
                result[ticker] = pos
        return result

    def project_ticker(self, ticker: str) -> Optional[dict]:
        """Build a single position dict from open lots + PositionMeta.

        Returns None if ticker has no open lots.
        """
        open_lots = self._ledger.open_lots(ticker)
        if not open_lots:
            return None

        meta = self._ledger.get_meta(ticker)
        meta_snap = meta.snapshot() if meta else {
            'stop_price': 0.0, 'target_price': 0.0,
            'half_target': 0.0, 'atr_value': None,
            'partial_done': False,
        }

        total_qty = sum(s.remaining_qty for _, s in open_lots)
        if total_qty <= QTY_EPSILON:
            return None

        avg_entry = self._ledger.weighted_avg_entry(ticker)
        earliest_lot = min(open_lots, key=lambda x: x[0].timestamp)[0]

        # Per-broker qty breakdown
        broker_qty = self._ledger.broker_qty(ticker)
        primary_broker = (
            max(broker_qty, key=broker_qty.get) if broker_qty else 'unknown'
        )

        return {
            # Core position fields
            'entry_price':  avg_entry,
            'entry_time':   earliest_lot.timestamp.strftime('%H:%M:%S'),
            'quantity':     total_qty,
            'qty':          total_qty,       # backward compat alias
            'partial_done': meta_snap.get('partial_done', False),
            'order_id':     earliest_lot.order_id,

            # Strategy fields (from PositionMeta)
            'stop_price':   meta_snap.get('stop_price', 0.0),
            'target_price': meta_snap.get('target_price', 0.0),
            'half_target':  meta_snap.get('half_target', 0.0),
            'atr_value':    meta_snap.get('atr_value'),
            'strategy':     earliest_lot.strategy,

            # Broker routing
            '_broker':      primary_broker,
            '_broker_qty':  broker_qty if len(broker_qty) > 1 else None,

            # Provenance
            '_source':      'fill_ledger',
            '_cost_basis':  self._ledger.cost_basis(ticker),
        }


class PositionProjectionProxy(dict):
    """Drop-in replacement for self.positions dict.

    Wraps FillLedger and presents a dict interface identical to the old
    positions dict. All 11 consumers read from this transparently.

    Reads are projected from FillLedger. Writes to strategy fields
    (stop_price, target_price, etc.) are forwarded to PositionMeta via
    MutablePositionDict.

    Direct dict assignment (positions[ticker] = {...}) and deletion
    (del positions[ticker]) are blocked — callers must use FillLedger.append().
    """

    def __init__(self, ledger: FillLedger):
        # Don't call super().__init__() with data — we override all access
        super().__init__()
        self._ledger = ledger
        self._projection = PositionProjection(ledger)
        # Mutable dict cache: projected dicts wrapped in MutablePositionDict
        # Invalidated when ledger changes via _invalidate()
        self._cache: dict[str, MutablePositionDict] = {}

    def _get_or_project(self, ticker: str) -> Optional[MutablePositionDict]:
        """Get cached MutablePositionDict or project fresh."""
        if ticker not in self._ledger.open_tickers():
            self._cache.pop(ticker, None)
            return None
        if ticker not in self._cache:
            projected = self._projection.project_ticker(ticker)
            if projected is not None:
                self._cache[ticker] = MutablePositionDict(
                    projected, ticker, self._ledger,
                )
        return self._cache.get(ticker)

    # ── Dict read interface ───────────────────────────────────────────

    def __getitem__(self, ticker: str):
        p = self._get_or_project(ticker)
        if p is None:
            raise KeyError(ticker)
        return p

    def __contains__(self, ticker) -> bool:
        return ticker in self._ledger.open_tickers()

    def get(self, ticker, default=None):
        p = self._get_or_project(ticker)
        return p if p is not None else default

    def keys(self):
        return self._ledger.open_tickers()

    def __iter__(self):
        return iter(self._ledger.open_tickers())

    def __len__(self) -> int:
        return len(self._ledger.open_tickers())

    def __bool__(self) -> bool:
        return len(self) > 0

    def values(self):
        return [self[t] for t in self._ledger.open_tickers()]

    def items(self):
        return [(t, self[t]) for t in self._ledger.open_tickers()]

    # ── Dict write interface (blocked) ────────────────────────────────

    def __setitem__(self, key, value):
        raise TypeError(
            "Direct position dict assignment removed. "
            "Use FillLedger.append() to open/close positions."
        )

    def __delitem__(self, key):
        raise TypeError(
            "Direct position deletion removed. "
            "Positions close via SELL fills through FillLedger.append()."
        )

    def pop(self, key, *args):
        raise TypeError("Use FillLedger.append() with a SELL lot.")

    def update(self, *args, **kwargs):
        raise TypeError("Use FillLedger.append().")

    def clear(self):
        raise TypeError("Use FillLedger to manage positions.")

    # ── Cache invalidation ────────────────────────────────────────────

    def _invalidate(self, ticker: Optional[str] = None):
        """Invalidate projection cache after ledger changes.

        Call this after FillLedger.append() to ensure next read
        gets fresh projected data.
        """
        if ticker:
            self._cache.pop(ticker, None)
        else:
            self._cache.clear()
