"""
RiskAdapter — tier-aware risk gate for the pro_setups subsystem.

Wraps tier-based stop/exit logic into the existing RiskEngine OUTPUT format
(OrderRequestPayload) without modifying RiskEngine itself.

Responsibilities
----------------
1. Enforce max concurrent pro-setup positions.
2. Enforce per-ticker order cooldown.
3. Prevent duplicate positions in the same ticker.
4. Validate minimum R:R ratio per tier.
5. Size position using risk-based sizing (2% of budget per trade).
6. Emit ORDER_REQ (durable=True) → existing AlpacaBroker executes.
7. Track open pro positions via FILL and POSITION events.

The existing RiskEngine is NOT bypassed — ORDER_REQ is the shared
protocol.  AlpacaBroker does not distinguish between ORDER_REQ sources.

Execution credentials: APCA_API_KEY_ID / APCA_API_SECRET_KEY (main account).
Same account as the VWAP Reclaim strategy.  Max-positions cap shared via
POSITION events ensures no oversized book.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Set

from monitor.event_bus import EventBus, EventType, Event
from monitor.events import OrderRequestPayload, Side, PositionAction

log = logging.getLogger(__name__)


# ── Tier-specific minimum R:R ratios ─────────────────────────────────────────
_MIN_RR: Dict[int, float] = {1: 1.5, 2: 2.0, 3: 3.5}

# Risk per trade: 2% of budget (adjustable)
_RISK_PCT: float = 0.02


class RiskAdapter:
    """
    Independent risk gate for pro_setups.

    All state is local — ``_positions`` and ``_last_order`` are maintained
    by listening to FILL and POSITION events on the shared bus.

    Parameters
    ----------
    bus            : shared EventBus instance
    max_positions  : maximum concurrent pro-setup open positions
    order_cooldown : seconds between two orders on the same ticker
    trade_budget   : total dollars allocated to pro-setup trades
    """

    def __init__(
        self,
        bus:            EventBus,
        max_positions:  int,
        order_cooldown: int,
        trade_budget:   float,
    ) -> None:
        self._bus            = bus
        self._max_positions  = max_positions
        self._order_cooldown = order_cooldown
        self._trade_budget   = trade_budget

        self._positions: Set[str]         = set()    # tickers with open pro positions
        self._last_order: Dict[str, float] = {}       # ticker → monotonic timestamp

        # Subscribe at priority=0 (after routing handlers) to track state
        bus.subscribe(EventType.FILL,     self._on_fill,     priority=0)
        bus.subscribe(EventType.POSITION, self._on_position, priority=0)
        log.info(
            "[RiskAdapter] ready  max_pos=%d  cooldown=%ds  budget=$%.0f",
            max_positions, order_cooldown, trade_budget,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def validate_and_emit(
        self,
        ticker:        str,
        direction:     str,
        entry_price:   float,
        stop_price:    float,
        target_1:      float,
        target_2:      float,
        atr_value:     float,
        tier:          int,
        strategy_name: str,
        confidence:    float,
        source_event:  Event,
    ) -> None:
        """
        Run all risk checks.  If all pass, size the position and emit
        ORDER_REQ (durable=True) → AlpacaBroker.

        All blocked signals are logged with the reason so they appear in
        the daily log for review.
        """
        tag = f"[RiskAdapter][{ticker}][{strategy_name}][T{tier}]"

        # ── Check 1: only long entries go through (short execution not yet wired) ──
        if direction != 'long':
            log.info("%s SKIPPED: direction=%s (short execution not enabled)", tag, direction)
            return

        # ── Check 2: max concurrent positions ─────────────────────────────────────
        if len(self._positions) >= self._max_positions:
            log.info(
                "%s BLOCKED: max_positions=%d reached (open: %s)",
                tag, self._max_positions, sorted(self._positions),
            )
            return

        # ── Check 3: per-ticker cooldown ──────────────────────────────────────────
        now     = time.monotonic()
        elapsed = now - self._last_order.get(ticker, 0.0)
        if elapsed < self._order_cooldown:
            log.info(
                "%s BLOCKED: cooldown %.0fs remaining",
                tag, self._order_cooldown - elapsed,
            )
            return

        # ── Check 4: no duplicate position ────────────────────────────────────────
        if ticker in self._positions:
            log.info("%s BLOCKED: already holding position", tag)
            return

        # ── Check 5: minimum R:R ──────────────────────────────────────────────────
        risk   = entry_price - stop_price       # always positive for long
        reward = target_2    - entry_price
        if risk <= 0:
            log.warning("%s BLOCKED: risk=%.4f (stop above entry?)", tag, risk)
            return
        rr = reward / risk
        min_rr = _MIN_RR.get(tier, 1.5)
        if rr < min_rr:
            log.info(
                "%s BLOCKED: R:R=%.2f < min %.2f  (risk=%.4f reward=%.4f)",
                tag, rr, min_rr, risk, reward,
            )
            return

        # ── Check 6: ATR sanity ───────────────────────────────────────────────────
        if atr_value <= 0:
            log.warning("%s BLOCKED: atr_value=%.4f invalid", tag, atr_value)
            return

        # ── Position sizing (risk-based) ──────────────────────────────────────────
        max_dollar_risk = self._trade_budget * _RISK_PCT
        qty_by_risk     = int(max_dollar_risk / risk)
        qty_by_budget   = int(self._trade_budget / entry_price)
        qty             = min(max(qty_by_risk, 1), max(qty_by_budget, 1))

        if qty <= 0:
            log.warning("%s BLOCKED: qty=0 (budget=%.0f entry=%.4f)", tag, self._trade_budget, entry_price)
            return

        # ── Emit ORDER_REQ ────────────────────────────────────────────────────────
        reason  = f"pro:{strategy_name}:T{tier}:long"
        payload = OrderRequestPayload(
            ticker           = ticker,
            side             = Side.BUY,
            qty              = qty,
            price            = round(entry_price, 2),
            reason           = reason,
            needs_ask_refresh= True,
            stop_price       = round(stop_price,  4),
            target_price     = round(target_2,    4),
            atr_value        = round(atr_value,   4),
        )

        log.info(
            "%s ENTRY  ▶  dir=long  entry=%.4f  stop=%.4f  "
            "target1=%.4f  target2=%.4f  qty=%d  R:R=%.2f  "
            "risk_$=%.2f  conf=%.0f%%  ATR=%.4f",
            tag,
            entry_price, stop_price,
            target_1, target_2,
            qty, rr,
            risk * qty,
            confidence * 100,
            atr_value,
        )

        # Mark cooldown immediately to prevent duplicate orders this tick
        self._last_order[ticker] = now
        self._positions.add(ticker)

        self._bus.emit(
            Event(
                type           = EventType.ORDER_REQ,
                payload        = payload,
                correlation_id = source_event.event_id,
            ),
            durable=True,
        )

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_fill(self, event: Event) -> None:
        p = event.payload
        if not p.reason.startswith('pro:'):
            return   # not a pro-setup fill
        if p.side == Side.SELL:
            self._positions.discard(p.ticker)
            log.info(
                "[RiskAdapter][%s] EXIT fill  side=sell  qty=%d  "
                "fill=%.4f  reason=%s",
                p.ticker, p.qty, p.fill_price, p.reason,
            )

    def _on_position(self, event: Event) -> None:
        p = event.payload
        if p.action == PositionAction.CLOSED:
            removed = self._positions.discard(p.ticker)
            if removed is None and p.ticker in self._positions:
                # discard returns None always; check via 'in' before
                pass
            log.debug(
                "[RiskAdapter][%s] position CLOSED — removed from tracking",
                p.ticker,
            )
