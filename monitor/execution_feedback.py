"""
T7 — Execution Feedback Loop
==============================
Bridges the gap between the Strategy Engine's computed stop/target/ATR and
the Position Manager's open position dict.

The Problem
-----------
The signal chain is:

  StrategyEngine  →  SIGNAL(stop, target, half_target, atr_value)
  RiskEngine      →  ORDER_REQ(side, qty, price)          ← stop/target dropped
  Broker          →  FILL(ticker, side, qty, fill_price)
  PositionManager →  opens position with *placeholder* stops (0.5% / 1%)

Because ORDER_REQ does not carry stop/target metadata (by design — the broker
should not need strategy parameters), the position dict is opened with
conservative fallback values.  This component fixes that by caching the
pending SIGNAL and applying its computed values to the position as soon as the
corresponding FILL arrives.

What it does
------------
1. Subscribes to SIGNAL events with action='buy'.
   Caches the payload keyed by ticker: {ticker: SignalPayload}.

2. Subscribes to FILL events.
   On buy fill → looks up the cached signal for that ticker, then patches
   the position dict in `positions` with the real stop_price, target_price,
   half_target, and atr_value.  Emits an internal POSITION update so the
   StateEngine snapshot stays consistent.

3. Subscribes to ORDER_FAIL events.
   Clears the cached pending signal so stale data doesn't pollute the next
   attempt.

4. TTL eviction — cached signals older than SIGNAL_TTL_SEC are discarded at
   each fill event to prevent memory growth during a long session.

Shared state
------------
`positions` is the same dict held by PositionManager, RiskEngine, and
StrategyEngine.  ExecutionFeedback writes *only* to entries that already exist
(i.e., after PositionManager has opened them) and only touches the four
strategy-derived fields.

Events consumed
---------------
  EventType.SIGNAL     (action='buy' only)
  EventType.FILL       (side='buy' only for patching; side='sell' → TTL sweep)
  EventType.ORDER_FAIL (clears pending cache entry)

Events emitted
--------------
  None — mutates the shared positions dict in-place.

Usage
-----
    fb = ExecutionFeedback(bus, positions)
    # No further wiring needed; subscribes on construction.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

from .event_bus import Event, EventBus, EventType
from .events import FillPayload, OrderFailPayload, SignalPayload

log = logging.getLogger(__name__)

SIGNAL_TTL_SEC = 120.0   # discard a cached buy signal after 2 minutes


class ExecutionFeedback:
    """
    Patches freshly-opened positions with strategy-computed stop/target/ATR.

    Thread-safety: handlers run on the event bus thread (same thread as all
    other handlers in synchronous mode).  No extra lock is needed.
    """

    def __init__(self, bus: EventBus, positions: dict):
        self._positions = positions
        # {ticker: (SignalPayload, monotonic_timestamp)}
        self._pending: Dict[str, Tuple[SignalPayload, float]] = {}

        bus.subscribe(EventType.SIGNAL,     self._on_signal)
        bus.subscribe(EventType.FILL,       self._on_fill)
        bus.subscribe(EventType.ORDER_FAIL, self._on_order_fail)

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _on_signal(self, event: Event) -> None:
        p: SignalPayload = event.payload
        if p.action != 'buy':
            return
        self._pending[p.ticker] = (p, time.monotonic())
        log.debug(
            f"[ExecFeedback] Cached buy signal for {p.ticker} "
            f"stop=${p.stop_price:.2f} target=${p.target_price:.2f}"
        )

    def _on_fill(self, event: Event) -> None:
        p: FillPayload = event.payload
        self._evict_stale()

        if p.side != 'buy':
            # Clear any stale pending entry for this ticker on sell — guards
            # against a signal cached after a failed buy being applied to the
            # next open position on the same ticker.
            self._pending.pop(p.ticker, None)
            return

        ticker = p.ticker
        entry  = self._pending.pop(ticker, None)
        if entry is None:
            log.debug(f"[ExecFeedback] No pending signal for {ticker} fill — skipping patch.")
            return

        sig, _ = entry
        pos = self._positions.get(ticker)
        if pos is None:
            log.warning(
                f"[ExecFeedback] Fill for {ticker} but position not yet in dict "
                f"(PositionManager may not have processed the event yet)."
            )
            return

        # Patch the four strategy-derived fields
        pos['stop_price']   = sig.stop_price
        pos['target_price'] = sig.target_price
        pos['half_target']  = sig.half_target
        pos['atr_value']    = sig.atr_value

        log.info(
            f"[ExecFeedback] Patched {ticker}: "
            f"stop=${sig.stop_price:.2f} target=${sig.target_price:.2f} "
            f"half=${sig.half_target:.2f} atr={sig.atr_value:.4f}"
        )

    def _on_order_fail(self, event: Event) -> None:
        p: OrderFailPayload = event.payload
        removed = self._pending.pop(p.ticker, None)
        if removed:
            log.info(
                f"[ExecFeedback] Cleared pending signal for {p.ticker} "
                f"after ORDER_FAIL."
            )

    # ── TTL eviction ──────────────────────────────────────────────────────────

    def _evict_stale(self) -> None:
        cutoff = time.monotonic() - SIGNAL_TTL_SEC
        stale  = [t for t, (_, ts) in self._pending.items() if ts < cutoff]
        for ticker in stale:
            log.debug(f"[ExecFeedback] Evicted stale pending signal for {ticker}.")
            del self._pending[ticker]

    # ── Debug helper ──────────────────────────────────────────────────────────

    def pending_tickers(self) -> list:
        """Return list of tickers with cached buy signals awaiting fill."""
        return list(self._pending.keys())
