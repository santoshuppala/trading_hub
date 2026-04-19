"""
FIFO Lot Matcher — matches SELL fills against open BUY lots.

Implements First-In-First-Out matching: the oldest BUY lot is consumed first.
Handles all edge cases:
  - Clean match: SELL qty equals one lot exactly
  - Multi-lot: SELL spans multiple BUY lots
  - Partial consumption: SELL consumes part of a lot
  - Overfill: SELL qty exceeds total open lots (clamped, logged)
  - Float precision: QTY_EPSILON tolerance prevents dust qty

Also provides preview_matches() for SmartRouter to determine broker split
BEFORE submitting a SELL order (advisory only — actual match on FILL is
authoritative).
"""
from __future__ import annotations

import logging
from typing import Optional

from .fill_lot import FillLot, LotState, LotMatch, QTY_EPSILON

log = logging.getLogger(__name__)


class LotMatcher:
    """FIFO lot matching engine."""

    def match_sell(
        self,
        sell_lot: FillLot,
        open_lots: list[tuple[FillLot, LotState]],
    ) -> list[LotMatch]:
        """Match a SELL fill against open BUY lots using FIFO.

        Consumes oldest BUY lots first (by position in the list, which
        must be sorted by timestamp/append order).

        Mutates LotState.remaining_qty in-place for consumed lots.

        Args:
            sell_lot: The SELL FillLot to match.
            open_lots: List of (FillLot, LotState) tuples, FIFO-ordered.

        Returns:
            List of LotMatch records. Empty if no lots to match.

        Side effects:
            - LotState.remaining_qty decremented for consumed lots
            - LotState.matched_sells updated with sell_lot.lot_id
        """
        if not open_lots:
            return []

        remaining = sell_lot.qty
        total_open = sum(s.remaining_qty for _, s in open_lots)

        # Overfill detection: SELL qty > total open qty
        overfill_qty = 0.0
        if remaining > total_open + QTY_EPSILON:
            overfill_qty = remaining - total_open
            remaining = total_open  # clamp to what's available
            log.error(
                "[LotMatcher] OVERFILL %s: sell %.6f > open %.6f, "
                "clamping to open. Overfill=%.6f shares unmatched.",
                sell_lot.ticker, sell_lot.qty, total_open, overfill_qty,
            )

        matches: list[LotMatch] = []

        for buy_lot, state in open_lots:
            if remaining <= QTY_EPSILON:
                break

            # Consume min(remaining_sell, remaining_lot)
            match_qty = min(remaining, state.remaining_qty)

            # P&L: for long positions, pnl = (sell - buy) * qty
            pnl = round(
                (sell_lot.fill_price - buy_lot.fill_price) * match_qty, 2
            )

            matches.append(LotMatch(
                buy_lot_id=buy_lot.lot_id,
                sell_lot_id=sell_lot.lot_id,
                matched_qty=match_qty,
                buy_price=buy_lot.fill_price,
                sell_price=sell_lot.fill_price,
                realized_pnl=pnl,
                ticker=sell_lot.ticker,
                matched_at=sell_lot.timestamp,
                estimated=sell_lot.synthetic,
            ))

            # Decrement lot state
            state.remaining_qty = round(state.remaining_qty - match_qty, 6)
            if state.remaining_qty <= QTY_EPSILON:
                state.remaining_qty = 0.0  # snap to zero
            state.matched_sells.append(sell_lot.lot_id)

            remaining = round(remaining - match_qty, 6)

        if overfill_qty > QTY_EPSILON:
            log.warning(
                "[LotMatcher] %s: %.6f shares from SELL lot %s could not be "
                "matched (overfill). These shares have no corresponding BUY lot.",
                sell_lot.ticker, overfill_qty, sell_lot.lot_id,
            )

        return matches

    def preview_matches(
        self,
        ticker: str,
        qty: float,
        open_lots: list[tuple[FillLot, LotState]],
    ) -> dict[str, float]:
        """Preview which brokers would be consumed by a SELL of qty shares.

        Returns {broker_name: qty_consumed} WITHOUT mutating any state.
        Used by SmartRouter to determine broker split before submitting
        SELL orders.

        NOTE: This is advisory only. The actual FIFO match on FILL event
        is authoritative (lots may change between preview and fill).

        Args:
            ticker: The ticker being sold (for logging only).
            qty: The total quantity to sell.
            open_lots: FIFO-ordered list of (FillLot, LotState) tuples.

        Returns:
            Dict mapping broker name to consumed quantity.
            Example: {'alpaca': 5.0, 'tradier': 2.0}
        """
        remaining = qty
        broker_qty: dict[str, float] = {}

        for buy_lot, state in open_lots:
            if remaining <= QTY_EPSILON:
                break
            match_qty = min(remaining, state.remaining_qty)
            broker_qty[buy_lot.broker] = (
                broker_qty.get(buy_lot.broker, 0.0) + match_qty
            )
            remaining = round(remaining - match_qty, 6)

        return broker_qty

    @staticmethod
    def replay_matches(
        lots: list[FillLot],
        lot_states: dict[str, LotState],
    ) -> list[LotMatch]:
        """Re-derive all matches from a list of lots via deterministic FIFO.

        Used on startup to reconstruct LotMatch records (and daily P&L)
        from persisted lots. Matches are never persisted separately —
        they are always derived from the lot sequence.

        Args:
            lots: All lots in append order (BUY and SELL).
            lot_states: Pre-loaded LotState dict. Will be MUTATED to
                        reflect the final remaining_qty after replay.

        Returns:
            List of all LotMatch records from FIFO replay.
        """
        matcher = LotMatcher()
        all_matches: list[LotMatch] = []

        # Reset all lot states to original qty for replay
        for state in lot_states.values():
            state.remaining_qty = state.original_qty
            state.matched_sells = []

        for lot in lots:
            if lot.side == 'BUY':
                # Ensure state exists (should have been pre-created)
                if lot.lot_id not in lot_states:
                    lot_states[lot.lot_id] = LotState(
                        lot_id=lot.lot_id,
                        original_qty=lot.qty,
                        remaining_qty=lot.qty,
                    )
                continue

            if lot.side == 'SELL':
                # Gather open BUY lots for this ticker, FIFO order
                open_buys = [
                    (l, lot_states[l.lot_id])
                    for l in lots
                    if l.ticker == lot.ticker
                    and l.side == 'BUY'
                    and l.lot_id in lot_states
                    and not lot_states[l.lot_id].is_closed
                ]
                matches = matcher.match_sell(lot, open_buys)
                all_matches.extend(matches)

        return all_matches
