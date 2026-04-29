"""
AlpacaOptionsBroker: multi-leg options order execution via Alpaca API.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from monitor.event_bus import Event
from monitor.order_wal import wal as order_wal

log = logging.getLogger(__name__)

FILL_TIMEOUT_SEC       = 15.0   # single-leg fill timeout
MLEG_FILL_TIMEOUT_SEC  = 40.0   # multi-leg needs more time (wider spreads, thinner liquidity)
FILL_POLL_SEC          = 1.0
MAX_RETRIES            = 1      # 2 attempts total (submit + 1 retry)
WIDEN_INCREMENT        = 0.05   # widen limit price by $0.05 per retry


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
    Does NOT subscribe to EventType.ORDER_REQ -- takes direct method calls
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

    # ── Public interface ──────────────────────────────────────────────────────

    def execute(
        self,
        trade_spec,              # OptionsTradeSpec
        source_event: Event,
    ) -> bool:
        """
        Submit multi-leg order. Returns True on fill, False on failure.

        Single leg -> LimitOrderRequest with asset_class='us_option'.
        Multi-leg  -> POST /v2/orders with order_class='mleg'.

        On fill: OptionsEngine already emitted OPTIONS_SIGNAL (durable=True)
        before calling execute -- broker does not re-emit.
        Logs fill confirmation and alerts.

        Args:
            trade_spec: OptionsTradeSpec with legs and pricing
            source_event: original SIGNAL or BAR event that triggered this

        Returns:
            bool: True if filled successfully, False on failure
        """
        if not self._client:
            log.warning("[AlpacaOptionsBroker] client unavailable -- skipping execution")
            return False

        # V10: WAL INTENT for options entry
        wal_cid = order_wal.new_client_id()
        order_wal.intent(wal_cid,
                         ticker=trade_spec.ticker,
                         side='BUY',
                         qty=trade_spec.legs[0].qty,
                         price=abs(trade_spec.net_debit),
                         reason=f'options:{trade_spec.strategy_type}')
        self._current_wal_cid = wal_cid

        try:
            if len(trade_spec.legs) == 1:
                result = self._execute_single_leg(trade_spec)
            else:
                result = self._execute_multi_leg(trade_spec)

            if not result and wal_cid:
                order_wal.failed(wal_cid, reason=f'options_exec_failed:{trade_spec.ticker}')
            self._current_wal_cid = ''
            return result
        except Exception as e:
            log.error(
                "[AlpacaOptionsBroker] execution error for %s (%s): %s",
                trade_spec.ticker, trade_spec.strategy_type, e,
            )
            order_wal.failed(wal_cid, reason=f'options_exception:{e}')
            self._current_wal_cid = ''
            return False

    def get_order_status(self, order_id: str) -> Dict:
        """
        Check the current status of an order.

        Returns:
            dict with keys: status, filled_qty, filled_avg_price
            Returns error dict on any failure.
        """
        try:
            order = self._client.get_order_by_id(order_id)
            # order.status is an enum (OrderStatus.FILLED) — use .value for the raw string
            raw_status = order.status.value if hasattr(order.status, 'value') else str(order.status)
            return {
                "status": raw_status,
                "filled_qty": float(order.filled_qty or 0),
                "filled_avg_price": float(order.filled_avg_price or 0),
            }
        except Exception as e:
            log.error("[AlpacaOptionsBroker] get_order_status(%s) failed: %s", order_id, e)
            return {"status": "error", "filled_qty": 0, "filled_avg_price": 0}

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an open order by ID.

        Returns:
            True if cancel was accepted, False on failure.
        """
        try:
            self._client.cancel_order_by_id(order_id)
            log.info("[AlpacaOptionsBroker] cancelled order %s", order_id)
            return True
        except Exception as e:
            log.error("[AlpacaOptionsBroker] cancel_order(%s) failed: %s", order_id, e)
            return False

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
        log.info(
            "[AlpacaOptionsBroker] closing position: %s (%d legs to reverse)",
            ticker, len(legs),
        )

        # V10: WAL INTENT for options close
        wal_cid = order_wal.new_client_id()
        order_wal.intent(wal_cid, ticker=ticker, side='SELL',
                         qty=legs[0].qty if legs else 0, price=0.0,
                         reason=f'options_close:{ticker}')
        self._current_wal_cid = wal_cid

        try:
            from .strategies.base import OptionLeg, OptionsTradeSpec

            reversed_legs = []
            for leg in legs:
                reversed_side = "sell" if leg.side == "buy" else "buy"
                # V10: Enforce minimum limit price for closes.
                # If limit_price is $0 (option worthless/no bid), Alpaca rejects
                # with "limit price must be > 0". Fix:
                #   - If closing a long (selling): use max(limit_price, 0.01)
                #   - This submits at $0.01 minimum — fills if any buyer exists
                #   - If truly no buyer, order won't fill (acceptable: let expire)
                _close_price = leg.limit_price
                if reversed_side == "sell" and _close_price <= 0:
                    _close_price = 0.01
                    log.info("[AlpacaOptionsBroker] %s limit=$0 → using $0.01 minimum "
                             "(option near-worthless)", leg.symbol)
                elif reversed_side == "buy" and _close_price <= 0:
                    # Closing a short leg (buying back): use $0.01 as well
                    _close_price = 0.01

                reversed_legs.append(OptionLeg(
                    symbol=leg.symbol,
                    side=reversed_side,
                    qty=leg.qty,
                    ratio=leg.ratio,
                    limit_price=_close_price,
                ))

            close_spec = OptionsTradeSpec(
                strategy_type="close",
                ticker=ticker,
                expiry_date="",
                legs=reversed_legs,
                net_debit=0.0,      # will be set by market
                max_risk=0.0,
                max_reward=0.0,
                reason=f"closing {ticker} position ({len(legs)} legs)",
            )

            if len(reversed_legs) == 1:
                result = self._execute_single_leg(close_spec)
            else:
                result = self._execute_multi_leg(close_spec)

            if result:
                log.info("[AlpacaOptionsBroker] closed position %s successfully", ticker)
                order_wal.recorded(wal_cid, lot_id=f'options_close:{ticker}')
            else:
                log.error("[AlpacaOptionsBroker] failed to close position %s", ticker)
                order_wal.failed(wal_cid, reason=f'close_failed:{ticker}')
            self._current_wal_cid = ''
            return result

        except Exception as e:
            log.error("[AlpacaOptionsBroker] close_position(%s) error: %s", ticker, e)
            order_wal.failed(wal_cid, reason=f'close_exception:{e}')
            self._current_wal_cid = ''
            return False

    # ── Single-leg execution ──────────────────────────────────────────────────

    def _execute_single_leg(self, trade_spec) -> bool:
        """
        Submit a single-leg options limit order.

        Uses alpaca-py LimitOrderRequest. Polls for fill up to FILL_TIMEOUT_SEC.
        On timeout, cancels and retries at a wider price (up to MAX_RETRIES).
        """
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        leg = trade_spec.legs[0]
        limit_price = leg.limit_price
        side = OrderSide.BUY if leg.side == "buy" else OrderSide.SELL

        log.info(
            "[AlpacaOptionsBroker] single-leg: %s %s %s qty=%d @ $%.2f",
            trade_spec.strategy_type, trade_spec.ticker,
            leg.symbol, leg.qty, limit_price,
        )

        for attempt in range(MAX_RETRIES + 1):
            try:
                # Build order request -- alpaca-py recognises OCC symbols as options
                order_request = LimitOrderRequest(
                    symbol=leg.symbol,
                    qty=leg.qty,
                    side=side,
                    type="limit",
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(limit_price, 2),
                )

                order = self._client.submit_order(order_request)
                order_id = order.id
                log.info(
                    "[AlpacaOptionsBroker] order submitted id=%s | %s %s @ $%.2f (attempt %d/%d)",
                    order_id, leg.side, leg.symbol, limit_price,
                    attempt + 1, MAX_RETRIES + 1,
                )

                # V10: WAL SUBMITTED
                _wal_cid = getattr(self, '_current_wal_cid', '')
                if _wal_cid:
                    order_wal.submitted(_wal_cid, broker='alpaca_options',
                                        broker_order_id=str(order_id), attempt=attempt)

                # Poll for fill
                filled = self._poll_for_fill(order_id)
                if filled:
                    fill_info = self.get_order_status(str(order_id))
                    log.info(
                        "[AlpacaOptionsBroker] FILLED %s %s | qty=%s avg=$%.2f | %s",
                        leg.side, leg.symbol,
                        fill_info["filled_qty"], fill_info["filled_avg_price"],
                        trade_spec.strategy_type,
                    )
                    # V10: WAL FILLED + RECORDED
                    if _wal_cid:
                        order_wal.filled(_wal_cid,
                                         fill_price=fill_info["filled_avg_price"],
                                         fill_qty=fill_info["filled_qty"],
                                         broker_order_id=str(order_id))
                        order_wal.recorded(_wal_cid, lot_id=f'options:{trade_spec.ticker}:{order_id}')
                    return True

                # Not filled -- cancel and retry at wider price
                log.warning(
                    "[AlpacaOptionsBroker] order %s not filled in %.0fs, cancelling (attempt %d/%d)",
                    order_id, FILL_TIMEOUT_SEC, attempt + 1, MAX_RETRIES + 1,
                )
                self.cancel_order(str(order_id))
                time.sleep(0.5)  # brief pause after cancel

                # Widen price for next attempt
                if leg.side == "buy":
                    limit_price += WIDEN_INCREMENT
                else:
                    limit_price -= WIDEN_INCREMENT
                limit_price = max(limit_price, 0.01)

            except Exception as e:
                log.error(
                    "[AlpacaOptionsBroker] single-leg attempt %d/%d error for %s: %s",
                    attempt + 1, MAX_RETRIES + 1, leg.symbol, e,
                )

        log.error(
            "[AlpacaOptionsBroker] single-leg FAILED after %d attempts: %s %s %s",
            MAX_RETRIES + 1, trade_spec.strategy_type, trade_spec.ticker, leg.symbol,
        )
        return False

    # ── Multi-leg execution ───────────────────────────────────────────────────

    def _execute_multi_leg(self, trade_spec) -> bool:
        """
        Submit a multi-leg combo order using Alpaca's mleg order class.

        Uses the REST endpoint POST /v2/orders with order_class='mleg'
        since alpaca-py may not expose native mleg support.
        Falls back to httpx via TradingClient's internal request method.
        """
        legs = trade_spec.legs
        net_price = abs(trade_spec.net_debit)

        # Determine if this is a debit or credit order
        is_debit = trade_spec.net_debit > 0

        log.info(
            "[AlpacaOptionsBroker] multi-leg: %s %s | %d legs | net_%s=$%.2f",
            trade_spec.strategy_type, trade_spec.ticker,
            len(legs), "debit" if is_debit else "credit", net_price,
        )

        for attempt in range(MAX_RETRIES + 1):
            try:
                # Build the mleg order payload for Alpaca REST API
                # Alpaca mleg format: qty at top level (number of spreads),
                # each leg uses ratio_qty (not qty)
                order_legs = []
                for leg in legs:
                    order_legs.append({
                        "symbol": leg.symbol,
                        "side": leg.side.lower(),
                        "ratio_qty": str(leg.ratio),
                    })

                order_body = {
                    "qty": str(legs[0].qty),
                    "order_class": "mleg",
                    "time_in_force": "day",
                    "legs": order_legs,
                }

                # Close orders use market; entry orders use limit
                if trade_spec.strategy_type == "close":
                    order_body["type"] = "market"
                else:
                    order_body["type"] = "limit"
                    order_body["limit_price"] = str(round(net_price, 2))

                # Submit via TradingClient's internal HTTP method
                order = self._submit_mleg_order(order_body)
                if order is None:
                    log.error(
                        "[AlpacaOptionsBroker] mleg submit returned None (attempt %d/%d)",
                        attempt + 1, MAX_RETRIES + 1,
                    )
                    continue

                order_id = order.get("id") if isinstance(order, dict) else getattr(order, "id", None)
                if not order_id:
                    log.error("[AlpacaOptionsBroker] mleg order has no id: %s", order)
                    continue

                log.info(
                    "[AlpacaOptionsBroker] mleg order submitted id=%s | %s %s (attempt %d/%d)",
                    order_id, trade_spec.strategy_type, trade_spec.ticker,
                    attempt + 1, MAX_RETRIES + 1,
                )

                # V10: WAL SUBMITTED for multi-leg
                _wal_cid = getattr(self, '_current_wal_cid', '')
                if _wal_cid:
                    order_wal.submitted(_wal_cid, broker='alpaca_options',
                                        broker_order_id=str(order_id), attempt=attempt)

                # Poll for fill — multi-leg gets longer timeout (thinner liquidity)
                filled = self._poll_for_fill(str(order_id), timeout=MLEG_FILL_TIMEOUT_SEC)
                if filled:
                    fill_info = self.get_order_status(str(order_id))
                    log.info(
                        "[AlpacaOptionsBroker] FILLED mleg %s %s | avg=$%.2f | %d legs",
                        trade_spec.strategy_type, trade_spec.ticker,
                        fill_info["filled_avg_price"], len(legs),
                    )
                    # V10: WAL FILLED + RECORDED for multi-leg
                    if _wal_cid:
                        order_wal.filled(_wal_cid,
                                         fill_price=fill_info["filled_avg_price"],
                                         fill_qty=fill_info["filled_qty"],
                                         broker_order_id=str(order_id))
                        order_wal.recorded(_wal_cid,
                                           lot_id=f'options_mleg:{trade_spec.ticker}:{order_id}')
                    return True

                # Not filled -- check status once more, then cancel and retry
                log.warning(
                    "[AlpacaOptionsBroker] mleg order %s not filled in %.0fs, cancelling (attempt %d/%d)",
                    order_id, MLEG_FILL_TIMEOUT_SEC, attempt + 1, MAX_RETRIES + 1,
                )
                cancelled = self.cancel_order(str(order_id))
                if not cancelled:
                    # Cancel failed — may be filled or partially filled. Verify.
                    recheck = self.get_order_status(str(order_id))
                    recheck_status = recheck.get("status", "").lower()
                    # V10: Detect partial fills on multi-leg (shouldn't happen
                    # with Alpaca mleg but defensive)
                    if recheck_status == 'partially_filled':
                        log.error(
                            "[AlpacaOptionsBroker] PARTIAL FILL on mleg %s %s — "
                            "attempting cancel remainder. This should not happen "
                            "with Alpaca mleg orders.",
                            trade_spec.strategy_type, trade_spec.ticker)
                        self.cancel_order(str(order_id))
                        return False
                    if recheck_status == "filled":
                        log.info(
                            "[AlpacaOptionsBroker] FILLED mleg %s %s (detected after cancel race) | avg=$%.2f | %d legs",
                            trade_spec.strategy_type, trade_spec.ticker,
                            recheck["filled_avg_price"], len(legs),
                        )
                        # V10: WAL FILLED + RECORDED for cancel-race fill
                        if _wal_cid:
                            order_wal.filled(_wal_cid,
                                             fill_price=recheck["filled_avg_price"],
                                             fill_qty=recheck["filled_qty"],
                                             broker_order_id=str(order_id))
                            order_wal.recorded(_wal_cid,
                                               lot_id=f'options_mleg:{trade_spec.ticker}:{order_id}')
                        return True
                    # Cancel failed but not filled — don't retry (avoid duplicates)
                    log.warning(
                        "[AlpacaOptionsBroker] cancel failed and status=%s for %s — aborting to avoid duplicate",
                        recheck_status, trade_spec.ticker,
                    )
                    return False
                time.sleep(0.5)

                # Widen net limit price for next attempt
                if is_debit:
                    net_price += WIDEN_INCREMENT
                else:
                    net_price = max(net_price - WIDEN_INCREMENT, 0.01)

            except Exception as e:
                log.error(
                    "[AlpacaOptionsBroker] mleg attempt %d/%d error for %s %s: %s",
                    attempt + 1, MAX_RETRIES + 1,
                    trade_spec.strategy_type, trade_spec.ticker, e,
                )

        log.error(
            "[AlpacaOptionsBroker] mleg FAILED after %d attempts: %s %s (%d legs)",
            MAX_RETRIES + 1, trade_spec.strategy_type, trade_spec.ticker, len(legs),
        )
        return False

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _submit_mleg_order(self, order_body: dict) -> Optional[dict]:
        """
        Submit a multi-leg order via Alpaca REST API.

        Tries alpaca-py's internal _request method first, falls back to
        direct httpx POST if unavailable.

        Returns order response dict or None on failure.
        """
        try:
            # Try using the TradingClient's internal request method
            # alpaca-py exposes _request() or post() for raw API calls
            if hasattr(self._client, "_request"):
                # alpaca-py _request() prepends api_version (v2) to the path,
                # so pass "/orders" not "/v2/orders" to avoid /v2/v2/orders
                response = self._client._request("POST", "/orders", order_body)
                return response
            elif hasattr(self._client, "post"):
                response = self._client.post("/orders", order_body)
                return response
            else:
                # Direct httpx fallback using client's base_url and headers
                return self._submit_mleg_httpx(order_body)
        except Exception as e:
            log.error("[AlpacaOptionsBroker] _submit_mleg_order error: %s", e)
            return None

    def _submit_mleg_httpx(self, order_body: dict) -> Optional[dict]:
        """
        Fallback: submit mleg order via httpx using the TradingClient's credentials.
        """
        try:
            import httpx

            # Extract base URL and credentials from TradingClient
            # Paper: https://paper-api.alpaca.markets
            # Live:  https://api.alpaca.markets
            base_url = getattr(self._client, "_base_url", None)
            if base_url is None:
                # Infer from client attributes
                is_paper = getattr(self._client, "_paper", True)
                base_url = (
                    "https://paper-api.alpaca.markets"
                    if is_paper
                    else "https://api.alpaca.markets"
                )

            api_key = getattr(self._client, "_api_key", None)
            secret_key = getattr(self._client, "_secret_key", None)

            if not api_key or not secret_key:
                log.error("[AlpacaOptionsBroker] cannot extract credentials for httpx fallback")
                return None

            headers = {
                "APCA-API-KEY-ID": api_key,
                "APCA-API-SECRET-KEY": secret_key,
                "Content-Type": "application/json",
            }

            url = f"{base_url}/v2/orders"
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(url, json=order_body, headers=headers)
                resp.raise_for_status()
                return resp.json()

        except Exception as e:
            log.error("[AlpacaOptionsBroker] httpx mleg fallback error: %s", e)
            return None

    def _poll_for_fill(self, order_id: str, timeout: float = None) -> bool:
        """
        Poll order status until filled or timeout.

        Returns True if order reached 'filled' status within the given timeout.
        Defaults to FILL_TIMEOUT_SEC (single-leg). Multi-leg callers pass
        MLEG_FILL_TIMEOUT_SEC for the longer window.
        """
        deadline = time.monotonic() + (timeout or FILL_TIMEOUT_SEC)

        while time.monotonic() < deadline:
            try:
                status_info = self.get_order_status(order_id)
                status = status_info["status"].lower()

                if status == "filled":
                    return True
                if status in ("cancelled", "canceled", "expired", "rejected"):
                    log.warning(
                        "[AlpacaOptionsBroker] order %s reached terminal state: %s",
                        order_id, status,
                    )
                    return False

                # Still pending / partially_filled -- keep polling
                time.sleep(FILL_POLL_SEC)

            except Exception as e:
                log.error("[AlpacaOptionsBroker] poll error for %s: %s", order_id, e)
                time.sleep(FILL_POLL_SEC)

        return False
