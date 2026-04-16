"""
TradierBroker — execute equity and options trades via Tradier REST API.

Implements the same interface as AlpacaBroker:
  - Subscribes to ORDER_REQ events on the EventBus
  - Emits FILL on success, ORDER_FAIL on failure
  - Supports equity orders, single-leg options, and multi-leg options

Supports sandbox (paper) and production modes.

API reference:
  https://documentation.tradier.com/brokerage-api/trading/getting-started
"""
import logging
import time
import requests
from typing import Optional

from monitor.event_bus import EventBus, EventType, Event
from monitor.events import FillPayload, OrderFailPayload, OrderRequestPayload

log = logging.getLogger(__name__)


class TradierBroker:
    """Equity + options broker using Tradier REST API."""

    FILL_TIMEOUT_SEC = 5.0
    FILL_POLL_SEC = 0.5
    MAX_RETRIES = 2

    def __init__(
        self,
        bus: EventBus,
        token: str,
        account_id: str,
        sandbox: bool = True,
        subscribe: bool = True,
    ):
        self._bus = bus
        self._token = token
        self._account = account_id
        self._base = (
            "https://sandbox.tradier.com" if sandbox else "https://api.tradier.com"
        )
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
        )
        self._sandbox = sandbox
        self._broker_name = 'tradier'

        if subscribe:
            bus.subscribe(EventType.ORDER_REQ, self._on_order_req, priority=1)

        mode = "sandbox" if sandbox else "LIVE"
        log.info("[TradierBroker] ready | account=%s | mode=%s", account_id, mode)

    def _emit_fill(self, fill_payload) -> None:
        """Emit a FILL event tagged with tradier broker name."""
        evt = Event(EventType.FILL, fill_payload)
        evt._routed_broker = self._broker_name
        self._bus.emit(evt)
        log.info("[TradierBroker] FILL event emitted → will be persisted by EventSourcingSubscriber")

    # ── Event handler ────────────────────────────────────────────────

    def _on_order_req(self, event: Event) -> None:
        """Handle ORDER_REQ events from the bus."""
        # Check if portfolio risk gate already blocked this order
        if getattr(event, '_portfolio_blocked', False):
            return
        p: OrderRequestPayload = event.payload
        side = str(getattr(p, "side", "")).upper()

        if side == "BUY":
            self._execute_buy(p, event)
        elif side == "SELL":
            self._execute_sell(p, event)

    # ── Equity buy (limit) ───────────────────────────────────────────

    def _execute_buy(self, p: OrderRequestPayload, parent_event: Event) -> None:
        """Submit a limit buy order via Tradier with retry logic."""
        ticker = p.ticker
        qty = p.qty
        price = float(p.price)

        log.info(
            "[TradierBroker] SUBMIT BUY: %s qty=%d limit=$%.2f", ticker, qty, price
        )

        # Tradier uses simple limit orders (no OTOCO brackets) — the strategy
        # engine monitors exits in-process. Alpaca handles broker-side brackets.
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                order_data = {
                    "class": "equity",
                    "symbol": ticker,
                    "side": "buy",
                    "quantity": str(qty),
                    "type": "limit",
                    "price": f"{price:.2f}",
                        "duration": "day",
                    }

                resp = self._session.post(
                    f"{self._base}/v1/accounts/{self._account}/orders",
                    data=order_data,
                    timeout=10,
                )

                if resp.status_code != 200:
                    log.warning(
                        "[TradierBroker] BUY %s rejected: %s",
                        ticker,
                        resp.text[:200],
                    )
                    continue

                order = resp.json().get("order", {})
                order_id = str(order.get("id", ""))

                if not order_id:
                    log.warning(
                        "[TradierBroker] BUY %s no order ID returned", ticker
                    )
                    continue

                # Poll for fill
                filled, fill_price, filled_qty = self._poll_fill(
                    order_id, qty, price
                )

                if filled:
                    log.info(
                        "[TradierBroker] FILLED BUY %s qty=%d @ $%.2f",
                        ticker,
                        filled_qty,
                        fill_price,
                    )
                    self._emit_fill(FillPayload(
                        ticker=ticker,
                        side="BUY",
                        qty=filled_qty,
                        fill_price=fill_price,
                        order_id=order_id,
                        reason=p.reason,
                        stop_price=p.stop_price,
                        target_price=p.target_price,
                        atr_value=p.atr_value,
                    ))

                    # Submit standalone stop-loss order for crash protection
                    if p.stop_price and p.stop_price > 0:
                        self._submit_stop_order(ticker, filled_qty, p.stop_price)

                    return

                # Not filled — cancel and retry
                self._cancel_order(order_id)
                log.info(
                    "[TradierBroker] BUY %s not filled attempt %d/%d",
                    ticker,
                    attempt,
                    self.MAX_RETRIES,
                )

            except Exception as exc:
                log.warning(
                    "[TradierBroker] BUY %s error attempt %d: %s",
                    ticker,
                    attempt,
                    exc,
                )

        # All retries exhausted
        log.warning(
            "[TradierBroker] BUY %s ABANDONED after %d attempts",
            ticker,
            self.MAX_RETRIES,
        )
        self._emit_order_fail(p, parent_event)

    # ── Equity sell (market) ─────────────────────────────────────────

    def _cancel_open_orders(self, ticker: str) -> None:
        """Cancel any open orders for a ticker before selling (prevents bracket double-sell)."""
        try:
            resp = self._session.get(
                f"{self._base}/v1/accounts/{self._account}/orders",
                params={"filter": "open"},
                timeout=5,
            )
            if resp.status_code == 200:
                orders = resp.json().get("orders", {})
                if orders and orders != "null":
                    order_list = orders.get("order", [])
                    if isinstance(order_list, dict):
                        order_list = [order_list]
                    cancelled = 0
                    for o in order_list:
                        sym = o.get("symbol", "")
                        side = o.get("side", "")
                        if sym == ticker and side in ("sell", "sell_short"):
                            oid = o.get("id")
                            if oid:
                                self._session.delete(
                                    f"{self._base}/v1/accounts/{self._account}/orders/{oid}",
                                    timeout=5,
                                )
                                cancelled += 1
                    if cancelled:
                        log.info("[TradierBroker] Cancelled %d open sell orders for %s", cancelled, ticker)
        except Exception as exc:
            log.warning("[TradierBroker] Failed to cancel orders for %s: %s", ticker, exc)

    def _submit_stop_order(self, ticker: str, qty: int, stop_price: float) -> None:
        """Submit a standalone stop-loss order for crash protection.

        This is NOT an OTOCO bracket — it's a simple stop order that lives
        independently. Easier to cancel before selling than OTOCO children.
        """
        try:
            resp = self._session.post(
                f"{self._base}/v1/accounts/{self._account}/orders",
                data={
                    "class": "equity",
                    "symbol": ticker,
                    "side": "sell",
                    "quantity": str(qty),
                    "type": "stop",
                    "stop": f"{stop_price:.2f}",
                    "duration": "day",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                order_id = resp.json().get("order", {}).get("id", "")
                log.info(
                    "[TradierBroker] STOP ORDER placed: %s qty=%d stop=$%.2f (order %s)",
                    ticker, qty, stop_price, order_id,
                )
            else:
                log.warning(
                    "[TradierBroker] STOP ORDER failed for %s: %s",
                    ticker, resp.text[:200],
                )
        except Exception as exc:
            log.warning("[TradierBroker] STOP ORDER error for %s: %s", ticker, exc)

    def _execute_sell(self, p: OrderRequestPayload, parent_event: Event) -> None:
        """Submit a market sell order via Tradier."""
        ticker = p.ticker
        qty = p.qty

        # V7.1: Verify position exists at Tradier before selling.
        # If Tradier stop already fired, position is gone — don't submit a sell
        # that will be rejected (and prevent Core from retrying on wrong broker).
        try:
            pos_resp = self._session.get(
                f"{self._base}/v1/accounts/{self._account}/positions",
                headers={"Authorization": f"Bearer {self._token}",
                         "Accept": "application/json"},
                timeout=5)
            if pos_resp.status_code == 200:
                positions = pos_resp.json().get('positions', {})
                if positions and positions != 'null':
                    pos_list = positions.get('position', [])
                    if isinstance(pos_list, dict):
                        pos_list = [pos_list]
                    tradier_qty = 0
                    for tp in pos_list:
                        if tp.get('symbol') == ticker:
                            tradier_qty = int(float(tp.get('quantity', 0)))
                            break
                    if tradier_qty <= 0:
                        log.warning(
                            "[TradierBroker] SELL skipped for %s: no position at Tradier "
                            "(qty=%d, likely closed by stop order)", ticker, tradier_qty)
                        from monitor.events import OrderFailPayload
                        self._bus.emit(Event(EventType.ORDER_FAIL, OrderFailPayload(
                            ticker=ticker, side='SELL', qty=qty,
                            price=p.price, reason=f'{p.reason} (no position at tradier)')))
                        return
                    if tradier_qty != qty:
                        log.warning(
                            "[TradierBroker] SELL qty mismatch for %s: requested %d, "
                            "Tradier has %d — using Tradier qty", ticker, qty, tradier_qty)
                        qty = tradier_qty
                else:
                    log.warning(
                        "[TradierBroker] SELL skipped for %s: no positions at Tradier", ticker)
                    from monitor.events import OrderFailPayload
                    self._bus.emit(Event(EventType.ORDER_FAIL, OrderFailPayload(
                        ticker=ticker, side='SELL', qty=qty,
                        price=p.price, reason=f'{p.reason} (no position at tradier)')))
                    return
        except Exception as exc:
            log.debug("[TradierBroker] Position check failed for %s (proceeding with sell): %s",
                      ticker, exc)

        # Cancel bracket child orders before selling
        self._cancel_open_orders(ticker)

        log.info(
            "[TradierBroker] SUBMIT SELL: %s qty=%d type=market reason=%s",
            ticker,
            qty,
            p.reason,
        )

        try:
            resp = self._session.post(
                f"{self._base}/v1/accounts/{self._account}/orders",
                data={
                    "class": "equity",
                    "symbol": ticker,
                    "side": "sell",
                    "quantity": str(qty),
                    "type": "market",
                    "duration": "day",
                },
                timeout=10,
            )

            if resp.status_code != 200:
                log.warning(
                    "[TradierBroker] SELL %s rejected: %s",
                    ticker,
                    resp.text[:200],
                )
                self._emit_order_fail(p, parent_event)
                return

            order = resp.json().get("order", {})
            order_id = str(order.get("id", ""))

            filled, fill_price, filled_qty = self._poll_fill(
                order_id, qty, float(p.price)
            )

            if filled:
                log.info(
                    "[TradierBroker] FILLED SELL %s qty=%d @ $%.2f",
                    ticker,
                    filled_qty,
                    fill_price,
                )
                self._emit_fill(FillPayload(
                    ticker=ticker,
                    side="SELL",
                    qty=filled_qty,
                    fill_price=fill_price,
                    order_id=order_id,
                    reason=p.reason,
                ))
            else:
                log.warning("[TradierBroker] SELL %s not filled", ticker)
                self._emit_order_fail(p, parent_event)

        except Exception as exc:
            log.error("[TradierBroker] SELL %s error: %s", ticker, exc)
            self._emit_order_fail(p, parent_event)

    # ── Options: single-leg ──────────────────────────────────────────

    def submit_option_order(
        self,
        underlying: str,
        option_symbol: str,
        side: str,
        qty: int,
        order_type: str = "market",
        price: Optional[float] = None,
        duration: str = "day",
    ) -> Optional[str]:
        """
        Submit a single-leg option order.

        Parameters
        ----------
        underlying    : e.g. 'AAPL'
        option_symbol : OCC symbol, e.g. 'AAPL250620C00200000'
        side          : 'buy_to_open' | 'buy_to_close' | 'sell_to_open' | 'sell_to_close'
        qty           : number of contracts
        order_type    : 'market' | 'limit'
        price         : required when order_type='limit'
        duration      : 'day' | 'gtc'

        Returns the Tradier order ID on success, None on failure.
        """
        data = {
            "class": "option",
            "symbol": underlying,
            "option_symbol": option_symbol,
            "side": side,
            "quantity": str(qty),
            "type": order_type,
            "duration": duration,
        }
        if order_type == "limit" and price is not None:
            data["price"] = f"{price:.2f}"

        log.info(
            "[TradierBroker] OPTION %s %s %d x %s @ %s",
            side,
            underlying,
            qty,
            option_symbol,
            f"${price:.2f}" if price else "MKT",
        )

        try:
            resp = self._session.post(
                f"{self._base}/v1/accounts/{self._account}/orders",
                data=data,
                timeout=10,
            )
            if resp.status_code != 200:
                log.warning(
                    "[TradierBroker] OPTION order rejected: %s", resp.text[:200]
                )
                return None

            order = resp.json().get("order", {})
            order_id = str(order.get("id", ""))
            log.info("[TradierBroker] OPTION order submitted: %s", order_id)
            return order_id if order_id else None

        except Exception as exc:
            log.error("[TradierBroker] OPTION order error: %s", exc)
            return None

    # ── Options: multi-leg ───────────────────────────────────────────

    def submit_multileg_order(
        self,
        underlying: str,
        legs: list[dict],
        order_type: str = "market",
        price: Optional[float] = None,
        duration: str = "day",
    ) -> Optional[str]:
        """
        Submit a multi-leg option order (spread, iron condor, etc.).

        Parameters
        ----------
        underlying : e.g. 'SPY'
        legs       : list of dicts, each with keys:
                       'option_symbol' : OCC symbol
                       'side'          : 'buy_to_open' | 'sell_to_open' | etc.
                       'quantity'      : int
        order_type : 'market' | 'debit' | 'credit' | 'even'
        price      : net debit/credit when order_type is 'debit' or 'credit'
        duration   : 'day' | 'gtc'

        Returns the Tradier order ID on success, None on failure.
        """
        data = {
            "class": "multileg",
            "symbol": underlying,
            "type": order_type,
            "duration": duration,
        }
        if price is not None:
            data["price"] = f"{price:.2f}"

        for i, leg in enumerate(legs):
            data[f"option_symbol[{i}]"] = leg["option_symbol"]
            data[f"side[{i}]"] = leg["side"]
            data[f"quantity[{i}]"] = str(leg["quantity"])

        log.info(
            "[TradierBroker] MULTILEG %s %d legs @ %s",
            underlying,
            len(legs),
            f"${price:.2f}" if price else "MKT",
        )

        try:
            resp = self._session.post(
                f"{self._base}/v1/accounts/{self._account}/orders",
                data=data,
                timeout=10,
            )
            if resp.status_code != 200:
                log.warning(
                    "[TradierBroker] MULTILEG order rejected: %s", resp.text[:200]
                )
                return None

            order = resp.json().get("order", {})
            order_id = str(order.get("id", ""))
            log.info("[TradierBroker] MULTILEG order submitted: %s", order_id)
            return order_id if order_id else None

        except Exception as exc:
            log.error("[TradierBroker] MULTILEG order error: %s", exc)
            return None

    # ── Order polling / cancellation ─────────────────────────────────

    def _poll_fill(
        self, order_id: str, expected_qty: int, fallback_price: float
    ) -> tuple:
        """Poll order status until filled or timeout. Returns (filled, price, qty)."""
        deadline = time.monotonic() + self.FILL_TIMEOUT_SEC
        while time.monotonic() < deadline:
            time.sleep(self.FILL_POLL_SEC)
            try:
                resp = self._session.get(
                    f"{self._base}/v1/accounts/{self._account}/orders/{order_id}",
                    timeout=5,
                )
                if resp.status_code != 200:
                    continue

                order = resp.json().get("order", {})
                status = order.get("status", "")

                if status in ("filled", "partially_filled"):
                    fill_price = float(
                        order.get("avg_fill_price", fallback_price)
                    )
                    filled_qty = int(
                        float(order.get("exec_quantity", expected_qty))
                    )
                    if status == "partially_filled":
                        log.warning(
                            "[TradierBroker] Partial fill: %d of %d — "
                            "cancelling remainder",
                            filled_qty, expected_qty,
                        )
                        # Cancel remaining to prevent untracked fills
                        self._cancel_order(order_id)
                        import time as _t; _t.sleep(0.5)
                        # Re-check final qty
                        try:
                            resp2 = self._session.get(
                                f"{self._base}/v1/accounts/{self._account}/orders/{order_id}",
                                timeout=5,
                            )
                            if resp2.status_code == 200:
                                final = resp2.json().get("order", {})
                                filled_qty = int(float(final.get("exec_quantity", filled_qty)))
                                fill_price = float(final.get("avg_fill_price", fill_price))
                        except Exception:
                            pass
                    return True, fill_price, filled_qty
                elif status in ("canceled", "expired", "rejected"):
                    # Check if any shares filled before cancel
                    exec_qty = int(float(order.get("exec_quantity", 0)))
                    if exec_qty > 0:
                        fill_price = float(order.get("avg_fill_price", fallback_price))
                        return True, fill_price, exec_qty
                    return False, fallback_price, 0
            except Exception:
                continue

        return False, fallback_price, 0

    def get_order_status(self, order_id: str) -> dict:
        """Fetch full order details by ID."""
        try:
            resp = self._session.get(
                f"{self._base}/v1/accounts/{self._account}/orders/{order_id}",
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("order", {})
        except Exception as exc:
            log.warning("[TradierBroker] Order status fetch failed: %s", exc)
        return {}

    def _cancel_order(self, order_id: str) -> None:
        """Cancel an open order."""
        try:
            self._session.delete(
                f"{self._base}/v1/accounts/{self._account}/orders/{order_id}",
                timeout=5,
            )
        except Exception as exc:
            log.debug("[TradierBroker] Cancel %s failed: %s", order_id, exc)

    def _emit_order_fail(
        self, p: OrderRequestPayload, parent_event: Event
    ) -> None:
        """Emit an ORDER_FAIL event matching the OrderFailPayload dataclass."""
        try:
            self._bus.emit(
                Event(
                    type=EventType.ORDER_FAIL,
                    payload=OrderFailPayload(
                        ticker=p.ticker,
                        side=p.side,
                        qty=p.qty,
                        price=p.price,
                        reason=p.reason,
                    ),
                    correlation_id=parent_event.event_id,
                )
            )
        except Exception:
            pass

    # ── Account info ─────────────────────────────────────────────────

    def get_account_balance(self) -> dict:
        """Fetch account balances."""
        try:
            resp = self._session.get(
                f"{self._base}/v1/accounts/{self._account}/balances",
                timeout=5,
            )
            if resp.status_code == 200:
                return resp.json().get("balances", {})
        except Exception as exc:
            log.warning("[TradierBroker] Balance fetch failed: %s", exc)
        return {}

    def get_positions(self) -> list:
        """Fetch open positions."""
        try:
            resp = self._session.get(
                f"{self._base}/v1/accounts/{self._account}/positions",
                timeout=5,
            )
            if resp.status_code == 200:
                pos = resp.json().get("positions", {})
                if pos and pos != "null":
                    position_list = pos.get("position", [])
                    if isinstance(position_list, dict):
                        position_list = [position_list]
                    return position_list
        except Exception as exc:
            log.warning("[TradierBroker] Positions fetch failed: %s", exc)
        return []

    def cancel_all_orders(self) -> None:
        """Cancel all open orders."""
        try:
            resp = self._session.get(
                f"{self._base}/v1/accounts/{self._account}/orders",
                params={"status": "open"},
                timeout=5,
            )
            if resp.status_code == 200:
                orders = resp.json().get("orders", {})
                if orders and orders != "null":
                    order_list = orders.get("order", [])
                    if isinstance(order_list, dict):
                        order_list = [order_list]
                    for o in order_list:
                        self._cancel_order(str(o.get("id", "")))
                    log.info(
                        "[TradierBroker] Cancelled %d open orders", len(order_list)
                    )
        except Exception as exc:
            log.warning("[TradierBroker] Cancel all failed: %s", exc)
