"""
Fill Lot Data Model — immutable records for lot-based position tracking.

Every broker fill is recorded as an immutable FillLot. Positions are DERIVED
from open lots, never stored directly. P&L is computed via FIFO lot matching.

Data classes:
    FillLot       — immutable record of a single broker fill (BUY or SELL)
    LotState      — mutable remaining-qty tracker for a BUY lot
    LotMatch      — result of FIFO matching a SELL against BUY lots
    PositionMeta  — mutable strategy state (stop/target/atr), separate from lots

Constants:
    QTY_EPSILON   — float comparison tolerance (1e-9) for qty exhaustion
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# Float comparison tolerance: any qty <= this is treated as zero.
# Prevents IEEE 754 artifacts like 0.2365 - 0.2365 = 5.55e-17 from
# keeping a lot "open" with dust qty.
QTY_EPSILON = 1e-9


@dataclass(frozen=True)
class FillLot:
    """Immutable record of a single broker fill.

    Each FillLot represents one execution event at the broker — a BUY or SELL
    for a specific qty at a specific price.  Lots are append-only and never
    modified after creation.

    Fields split into three groups:
      - Core: what happened (ticker, side, qty, price, timestamp)
      - Context: why it happened (strategy, reason, correlation_id)
      - Audit: traceability (signal_price, order_mode, synthetic flag)
    """
    # ── Core ──────────────────────────────────────────────────────────
    lot_id:         str                    # UUID4, globally unique
    ticker:         str
    side:           str                    # 'BUY' | 'SELL'
    qty:            float                  # fractional-aware (Alpaca: 0.01+, Tradier: whole)
    fill_price:     float                  # actual broker execution price
    timestamp:      datetime               # fill time (ET)

    # ── Context ───────────────────────────────────────────────────────
    order_id:       str                    # broker order ID
    broker:         str                    # 'alpaca' | 'tradier'
    strategy:       str                    # 'pro:sr_flip', 'vwap_reclaim', etc.
    reason:         str                    # 'VWAP reclaim', 'SELL_STOP', etc.
    correlation_id: Optional[str] = None   # links to originating SIGNAL event

    # ── Audit / Future-proofing ───────────────────────────────────────
    direction:      str = 'long'           # 'long' only for now. 'short' reserved.
    signal_price:   float = 0.0            # bar close at signal time (slippage tracking)
    order_mode:     str = 'qty'            # 'qty' | 'notional'
    synthetic:      bool = False           # True for reconciliation-created lots

    # Initial stop/target from signal — immutable audit trail.
    # Current values live in PositionMeta (mutable, patched by ExecutionFeedback).
    init_stop:      float = 0.0
    init_target:    float = 0.0
    init_atr:       Optional[float] = None

    # ── Edge context (V10: captured at signal time for edge model) ────
    timeframe:        str = '1min'           # '1min' or '5min'
    regime_at_entry:  str = ''               # 'BULL_TREND', 'RANGE_BOUND', etc.
    time_bucket:      str = ''               # '0930_0945', '0945_1030', etc.
    confidence:       float = 0.0            # signal confidence [0, 1]
    confluence_score: float = 0.0            # aggregate detector agreement [0, 1]
    tier:             int = 0                # strategy tier (1|2|3, 0=unknown)

    def __post_init__(self):
        if self.qty <= QTY_EPSILON:
            raise ValueError(f"FillLot qty must be > 0, got {self.qty}")
        if self.side not in ('BUY', 'SELL'):
            raise ValueError(f"FillLot side must be 'BUY' or 'SELL', got {self.side!r}")
        if self.direction not in ('long', 'short'):
            raise ValueError(f"FillLot direction must be 'long' or 'short', got {self.direction!r}")


@dataclass
class LotState:
    """Tracks the remaining open quantity for a single BUY lot.

    Created when a BUY FillLot is appended. Decremented by the FIFO matcher
    when SELL lots consume from this lot. Once remaining_qty reaches zero
    (within QTY_EPSILON), the lot is considered closed.

    The matched_sells list provides an audit trail of which SELL lots
    consumed from this BUY lot.
    """
    lot_id:         str
    original_qty:   float
    remaining_qty:  float
    matched_sells:  list[str] = field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        """True when this lot has been fully consumed by SELL matches."""
        return self.remaining_qty <= QTY_EPSILON

    def to_dict(self) -> dict:
        """Serialize for JSON persistence."""
        return {
            'lot_id': self.lot_id,
            'original_qty': self.original_qty,
            'remaining_qty': self.remaining_qty,
            'matched_sells': list(self.matched_sells),
        }

    @classmethod
    def from_dict(cls, d: dict) -> LotState:
        """Deserialize from JSON."""
        return cls(
            lot_id=d['lot_id'],
            original_qty=d['original_qty'],
            remaining_qty=d['remaining_qty'],
            matched_sells=d.get('matched_sells', []),
        )


@dataclass(frozen=True)
class LotMatch:
    """Result of FIFO-matching a SELL lot against a BUY lot.

    Each LotMatch represents a portion of a SELL fill that was matched
    against a specific BUY lot. A single SELL fill may produce multiple
    LotMatch records if it spans multiple BUY lots.

    realized_pnl = (sell_price - buy_price) * matched_qty  (for long positions)
    """
    buy_lot_id:     str                    # the BUY lot that was consumed
    sell_lot_id:    str                    # the SELL lot that closed it
    matched_qty:    float
    buy_price:      float                  # fill_price of the BUY lot
    sell_price:     float                  # fill_price of the SELL lot
    realized_pnl:   float                  # (sell - buy) * qty
    ticker:         str
    matched_at:     datetime               # when the match occurred (for daily P&L)
    estimated:      bool = False           # True if fill_price was estimated (phantom close)


@dataclass
class PositionMeta:
    """Mutable strategy state for a position — NOT per-lot.

    Ownership hierarchy:
      FillLot.init_stop/init_target  = immutable audit trail from signal time
      PositionMeta.stop_price/target = CURRENT values (mutable)

    Who writes:
      - PositionManager.set_meta()   on position open (initial values from signal)
      - ExecutionFeedback.patch_meta() after fill (real values from strategy)
      - signals.py trailing stop     updates stop_price on each BAR

    Who reads:
      - StrategyEngine               for exit decisions (stop/target comparison)
      - PositionProjection           to build backward-compat positions dict

    Thread safety:
      PositionMeta._lock protects all mutations. Reads via snapshot() are
      also locked to prevent torn reads.
    """
    stop_price:     float = 0.0
    target_price:   float = 0.0
    half_target:    float = 0.0
    atr_value:      Optional[float] = None
    partial_done:   bool = False
    _lock:          threading.Lock = field(default_factory=threading.Lock,
                                           repr=False, compare=False)

    def update(self, **fields) -> None:
        """Thread-safe partial update of strategy fields."""
        with self._lock:
            for k, v in fields.items():
                if k.startswith('_'):
                    continue  # skip private fields
                if hasattr(self, k):
                    setattr(self, k, v)

    def snapshot(self) -> dict:
        """Thread-safe read of all strategy fields as a plain dict."""
        with self._lock:
            return {
                'stop_price':   self.stop_price,
                'target_price': self.target_price,
                'half_target':  self.half_target,
                'atr_value':    self.atr_value,
                'partial_done': self.partial_done,
            }

    def to_dict(self) -> dict:
        """Serialize for JSON persistence (same as snapshot)."""
        return self.snapshot()

    @classmethod
    def from_dict(cls, d: dict) -> PositionMeta:
        """Deserialize from JSON."""
        return cls(
            stop_price=d.get('stop_price', 0.0),
            target_price=d.get('target_price', 0.0),
            half_target=d.get('half_target', 0.0),
            atr_value=d.get('atr_value'),
            partial_done=d.get('partial_done', False),
        )


# ── Serialization helpers for FillLot ─────────────────────────────────────

def fill_lot_to_dict(lot: FillLot) -> dict:
    """Serialize a FillLot to a JSON-compatible dict."""
    return {
        'lot_id':         lot.lot_id,
        'ticker':         lot.ticker,
        'side':           lot.side,
        'qty':            lot.qty,
        'fill_price':     lot.fill_price,
        'timestamp':      lot.timestamp.isoformat(),
        'order_id':       lot.order_id,
        'broker':         lot.broker,
        'strategy':       lot.strategy,
        'reason':         lot.reason,
        'correlation_id': lot.correlation_id,
        'direction':      lot.direction,
        'signal_price':   lot.signal_price,
        'order_mode':     lot.order_mode,
        'synthetic':      lot.synthetic,
        'init_stop':      lot.init_stop,
        'init_target':    lot.init_target,
        'init_atr':       lot.init_atr,
        # V10: Edge context
        'timeframe':        lot.timeframe,
        'regime_at_entry':  lot.regime_at_entry,
        'time_bucket':      lot.time_bucket,
        'confidence':       lot.confidence,
        'confluence_score': lot.confluence_score,
        'tier':             lot.tier,
    }


def fill_lot_from_dict(d: dict) -> FillLot:
    """Deserialize a FillLot from a JSON dict."""
    ts = d['timestamp']
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    return FillLot(
        lot_id=d['lot_id'],
        ticker=d['ticker'],
        side=d['side'],
        qty=float(d['qty']),
        fill_price=float(d['fill_price']),
        timestamp=ts,
        order_id=d['order_id'],
        broker=d['broker'],
        strategy=d.get('strategy', 'unknown'),
        reason=d.get('reason', ''),
        correlation_id=d.get('correlation_id'),
        direction=d.get('direction', 'long'),
        signal_price=float(d.get('signal_price', 0)),
        order_mode=d.get('order_mode', 'qty'),
        synthetic=d.get('synthetic', False),
        init_stop=float(d.get('init_stop', 0)),
        init_target=float(d.get('init_target', 0)),
        init_atr=d.get('init_atr'),
        # V10: Edge context (defaults allow loading old data without these fields)
        timeframe=d.get('timeframe', '1min'),
        regime_at_entry=d.get('regime_at_entry', ''),
        time_bucket=d.get('time_bucket', ''),
        confidence=float(d.get('confidence', 0)),
        confluence_score=float(d.get('confluence_score', 0)),
        tier=int(d.get('tier', 0)),
    )
