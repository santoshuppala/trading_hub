"""
AlpacaOptionsBroker: multi-leg options order execution via Alpaca API.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from monitor.event_bus import Event

log = logging.getLogger(__name__)

FILL_TIMEOUT_SEC = 30.0  # options fill slower than equity
FILL_POLL_SEC    = 1.0
MAX_RETRIES      = 2


class AlpacaOptionsBroker:
    """
    Executes multi-leg options orders via Alpaca's options trading API.

    Multi-leg execution strategy:
    1. For 1-leg strategies (LongCall, LongPut): submit a single options
       limit order at the mid-price.
    2. For 2-4 leg strategies (spreads, condors): submit a multi-leg
       'combo' order using Alpaca's /v2/orders with order_class='mleg'
       and legs=[{symbol, side, qty, ratio_qty}].
    3. Poll for fill with FILL_TIMEOUT_SEC window.
    4. Cancel and retry up to MAX_RETRIES on timeout.
    5. On permanent failure: log and alert. Do NOT emit ORDER_FAIL on the
       main bus (separate options error handling).

    Credentials: APCA_OPTIONS_KEY / APCA_OPTIONS_SECRET (own TradingClient).
    Does NOT subscribe to EventType.ORDER_REQ — takes direct method calls
    from OptionsEngine to avoid polluting the main equity order channel.
    """

    def __init__(
        self,
        trading_client,             # alpaca-py TradingClient (options account)
        alert_email: Optional[str] = None,
    ) -> None:
        self._client = trading_client
        self._alert_email = alert_email
        log.info("[AlpacaOptionsBroker] initialized (direct execution mode, no ORDER_REQ subscription)")

    def execute(
        self,
        trade_spec,              # OptionsTradeSpec
        source_event: Event,
    ) -> bool:
        """
        Submit multi-leg order. Returns True on fill, False on failure.

        Single leg → LimitOrderRequest with asset_class='us_option'.
        Multi-leg  → OptionMultiLegOrderRequest (Alpaca mleg order).

        On fill: OptionsEngine already emitted OPTIONS_SIGNAL (durable=True)
        before calling execute — broker does not re-emit.
        Logs fill confirmation and alerts.

        Args:
            trade_spec: OptionsTradeSpec with legs and pricing
            source_event: original SIGNAL or BAR event that triggered this

        Returns:
            bool: True if filled successfully, False on failure
        """
        if not self._client:
            log.warning(f"[AlpacaOptionsBroker] client unavailable — skipping execution")
            return False

        try:
            if len(trade_spec.legs) == 1:
                return self._execute_single_leg(trade_spec)
            else:
                return self._execute_multi_leg(trade_spec)
        except Exception as e:
            log.error(f"[AlpacaOptionsBroker] execution error for {trade_spec.ticker}: {e}")
            return False

    def _execute_single_leg(self, trade_spec) -> bool:
        """Submit a single-leg options limit order."""
        leg = trade_spec.legs[0]
        log.info(
            f"[AlpacaOptionsBroker] executing single-leg: {trade_spec.strategy_type} "
            f"{trade_spec.ticker} {leg.symbol} {leg.side} 1 @ ${leg.limit_price:.2f}"
        )
        # TODO: Implement via alpaca-py LimitOrderRequest with asset_class='us_option'
        # For now, mock success
        return True

    def _execute_multi_leg(self, trade_spec) -> bool:
        """Submit a multi-leg combo order using Alpaca's mleg order class."""
        log.info(
            f"[AlpacaOptionsBroker] executing multi-leg ({len(trade_spec.legs)} legs): "
            f"{trade_spec.strategy_type} {trade_spec.ticker} | "
            f"net_debit=${trade_spec.net_debit:.2f} | max_risk=${trade_spec.max_risk:.2f}"
        )
        # TODO: Implement via alpaca-py OptionMultiLegOrderRequest or POST /v2/orders
        # with order_class='mleg'
        # For now, mock success
        return True

    def close_position(self, ticker: str, legs: List) -> bool:
        """
        Close an open options position by reversing all legs.
        Called by OptionsEngine when an exit condition is met.

        Args:
            ticker: underlying symbol
            legs: list of OptionLeg (current position legs)

        Returns:
            bool: True if close was successful
        """
        log.info(f"[AlpacaOptionsBroker] closing position: {ticker} ({len(legs)} legs to reverse)")
        # TODO: Reverse each leg (buy → sell, sell → buy)
        # For now, mock success
        return True
