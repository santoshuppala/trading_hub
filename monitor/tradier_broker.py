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
    """Equity + options broker using Tradier REST API.

    NOTE: Does not inherit BaseBroker because _execute_buy/_execute_sell
    have different signatures (extra parent_event param). However, it
    implements the same query interface (get_positions, get_equity,
    supports_fractional) so BrokerRegistry can iterate it generically.
    """

    def _sf(k, d):
        import os
        try: return float(os.getenv(k, str(d)))
        except (ValueError, TypeError): return d
    FILL_TIMEOUT_SEC = _sf('TRADIER_FILL_TIMEOUT', 8.0)
    FILL_POLL_SEC = _sf('TRADIER_FILL_POLL', 0.5)
    MAX_RETRIES = int(_sf('TRADIER_MAX_RETRIES', 3))
    RETRY_BACKOFF_BASE = _sf('TRADIER_BACKOFF_BASE', 1.0)

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
        # V10: WAL FILLED — record fill before emitting FILL event
        _wal_cid = getattr(self, '_current_wal_cid', '')
        if _wal_cid:
            try:
                from .order_wal import wal
                wal.filled(_wal_cid,
                           fill_price=fill_payload.fill_price,
                           fill_qty=fill_payload.qty,
                           broker_order_id=fill_payload.order_id)
            except Exception:
                pass

        evt = Event(EventType.FILL, fill_payload)
        evt._routed_broker = self._broker_name
        evt._wal_client_id = _wal_cid  # carry WAL ID to PositionManager
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
        # V10: Carry WAL client_id from ORDER_REQ event
        self._current_wal_cid = getattr(event, '_wal_client_id', '')

        if side == "BUY":
            self._execute_buy(p, event)
        elif side == "SELL":
            self._execute_sell(p, event)
        self._current_wal_cid = ''

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
                # V10: Use WAL client_id as tag for dedup on crash recovery.
                # Tradier's 'tag' field (up to 255 chars) is returned with
                # order status, allowing us to detect duplicate submissions.
                _tag = self._current_wal_cid or ''

                # V10: Support stop-limit BUY for two-stage entries
                # Stop-limit: order dormant until activation_price reached,
                # then fills at limit (price) or better. Eliminates adverse selection.
                _order_type = getattr(p, 'order_type', 'limit')
                _activation = getattr(p, 'activation_price', None)

                order_data = {
                    "class": "equity",
                    "symbol": ticker,
                    "side": "buy",
                    "quantity": str(qty),
                    "duration": "day",
                    "tag": _tag,
                }

                if _order_type == 'stop_limit' and _activation and _activation > 0:
                    order_data["type"] = "stop_limit"
                    order_data["stop"] = f"{_activation:.2f}"
                    order_data["price"] = f"{price:.2f}"
                    log.info("[TradierBroker] STOP-LIMIT BUY: %s qty=%d "
                             "activation=$%.2f limit=$%.2f",
                             ticker, qty, _activation, price)
                else:
                    order_data["type"] = "limit"
                    order_data["price"] = f"{price:.2f}"

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

                # V10: WAL SUBMITTED
                if self._current_wal_cid:
                    try:
                        from .order_wal import wal as _wal
                        _wal.submitted(self._current_wal_cid, broker=self._broker_name,
                                       broker_order_id=order_id, attempt=attempt)
                    except Exception:
                        pass

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
                    # V10: Ensure stop is ALWAYS below fill price.
                    # Signal stop is computed from expected entry, but actual fill
                    # can be lower (limit order improvement). If stop > fill,
                    # broker fires stop immediately = instant loss.
                    _stop = p.stop_price
                    if _stop and _stop > 0:
                        if _stop >= fill_price:
                            # Stop above entry — force it below by min(0.3% of price, ATR*0.5)
                            _min_offset = fill_price * 0.003
                            _atr_offset = (p.atr_value * 0.5) if p.atr_value else _min_offset
                            _stop = fill_price - max(_min_offset, _atr_offset)
                            log.warning(
                                "[TradierBroker] STOP ADJUSTED for %s: signal_stop=$%.2f > "
                                "fill=$%.2f — lowered to $%.2f (%.2f%% below entry)",
                                ticker, p.stop_price, fill_price, _stop,
                                (fill_price - _stop) / fill_price * 100)
                        self._submit_stop_order(ticker, filled_qty, _stop)

                    return

                # Not filled — cancel and retry with backoff
                self._cancel_order(order_id)
                backoff = self.RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                log.info(
                    "[TradierBroker] BUY %s not filled attempt %d/%d — "
                    "retrying in %.1fs",
                    ticker, attempt, self.MAX_RETRIES, backoff,
                )
                time.sleep(backoff)

            except Exception as exc:
                backoff = self.RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                log.warning(
                    "[TradierBroker] BUY %s error attempt %d: %s — "
                    "retrying in %.1fs",
                    ticker, attempt, exc, backoff,
                )
                time.sleep(backoff)

        # All retries exhausted
        log.warning(
            "[TradierBroker] BUY %s ABANDONED after %d attempts",
            ticker,
            self.MAX_RETRIES,
        )
        self._emit_order_fail(p, parent_event)

    # ── Equity sell (market) ─────────────────────────────────────────

    def _cancel_open_orders(self, ticker: str) -> None:
        """Cancel any open orders for a ticker before selling (prevents bracket double-sell).

        V8 fix: Tradier sandbox filter=open returns ALL orders (filled/rejected/etc).
        We filter client-side by status='pending' or 'open' only, and cache stale IDs
        to avoid hammering 401s on already-terminated orders.
        """
        if not hasattr(self, '_stale_order_ids'):
            self._stale_order_ids = set()
        try:
            # V8: Don't use filter=open — Tradier sandbox returns stale orders
            # but EXCLUDES actually-open stop orders. Filter client-side instead.
            resp = self._session.get(
                f"{self._base}/v1/accounts/{self._account}/orders",
                timeout=5,
            )
            if resp.status_code == 200:
                orders = resp.json().get("orders", {})
                if orders and orders != "null":
                    order_list = orders.get("order", [])
                    if isinstance(order_list, dict):
                        order_list = [order_list]
                    cancelled = 0
                    matching = sum(1 for o in order_list
                                   if o.get("symbol") == ticker
                                   and o.get("side") in ("sell", "sell_short")
                                   and o.get("status") in ("pending", "open", "partially_filled"))
                    log.info("[TradierBroker] _cancel_open_orders(%s): %d total orders, %d matching open sells",
                             ticker, len(order_list), matching)
                    for o in order_list:
                        sym = o.get("symbol", "")
                        side = o.get("side", "")
                        status = o.get("status", "")
                        oid = o.get("id")
                        # Only cancel truly open/pending orders for this ticker
                        if (sym == ticker
                                and side in ("sell", "sell_short")
                                and status in ("pending", "open", "partially_filled")
                                and oid
                                and oid not in self._stale_order_ids):
                            resp_cancel = self._session.delete(
                                f"{self._base}/v1/accounts/{self._account}/orders/{oid}",
                                timeout=5,
                            )
                            if resp_cancel.status_code == 200:
                                cancelled += 1
                            else:
                                self._stale_order_ids.add(oid)
                                log.debug("[TradierBroker] Cancel order %s: status=%d (cached as stale)",
                                          oid, resp_cancel.status_code)
                    if cancelled:
                        log.info("[TradierBroker] Cancelled %d open sell orders for %s", cancelled, ticker)
            elif resp.status_code == 401:
                log.warning("[TradierBroker] Open orders list returned 401 — token may be expired")
        except Exception as exc:
            log.warning("[TradierBroker] Failed to cancel orders for %s: %s", ticker, exc)

    def _submit_stop_order(self, ticker: str, qty: int, stop_price: float) -> None:
        """Submit a standalone stop-loss order for crash protection.

        This is NOT an OTOCO bracket — it's a simple stop order that lives
        independently. Easier to cancel before selling than OTOCO children.
        """
        try:
            _tag = self._current_wal_cid or ''
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
                    "tag": _tag,
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
                        # V7.2: Use min(requested, available) — never sell more
                        # than requested. Prevents PARTIAL_SELL from becoming
                        # full liquidation.
                        old_qty = qty
                        qty = min(qty, tradier_qty)
                        log.warning(
                            "[TradierBroker] SELL qty mismatch for %s: requested %d, "
                            "Tradier has %d — using %d (min)", ticker, old_qty, tradier_qty, qty)
                else:
                    log.warning(
                        "[TradierBroker] SELL skipped for %s: no positions at Tradier", ticker)
                    from monitor.events import OrderFailPayload
                    self._bus.emit(Event(EventType.ORDER_FAIL, OrderFailPayload(
                        ticker=ticker, side='SELL', qty=qty,
                        price=p.price, reason=f'{p.reason} (no position at tradier)')))
                    return
        except Exception as exc:
            log.error("[TradierBroker] Position check FAILED for %s: %s — "
                      "blocking sell (cannot verify broker state)", ticker, exc)
            self._emit_order_fail(p, parent_event)
            return

        # Cancel bracket child orders before selling
        self._cancel_open_orders(ticker)

        log.info(
            "[TradierBroker] SUBMIT SELL: %s qty=%d type=market reason=%s",
            ticker,
            qty,
            p.reason,
        )

        try:
            _tag = self._current_wal_cid or ''
            resp = self._session.post(
                f"{self._base}/v1/accounts/{self._account}/orders",
                data={
                    "class": "equity",
                    "symbol": ticker,
                    "side": "sell",
                    "quantity": str(qty),
                    "type": "market",
                    "duration": "day",
                    "tag": _tag,
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

            order_resp = resp.json()
            order = order_resp.get("order", {})
            order_id = str(order.get("id", ""))
            log.info("[TradierBroker] SELL %s order response: id=%s status=%s resp=%s",
                     ticker, order_id, order.get("status", "N/A"),
                     str(order_resp)[:300])

            if not order_id or order_id == "None":
                log.warning("[TradierBroker] SELL %s got empty order_id — response: %s",
                            ticker, str(order_resp)[:500])
                self._emit_order_fail(p, parent_event)
                return

            # V10: WAL SUBMITTED for sell
            if self._current_wal_cid:
                try:
                    from .order_wal import wal as _wal
                    _wal.submitted(self._current_wal_cid, broker=self._broker_name,
                                   broker_order_id=order_id)
                except Exception:
                    pass

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
                    log.debug("[TradierBroker] poll %s: HTTP %d", order_id, resp.status_code)
                    continue

                order = resp.json().get("order", {})
                status = order.get("status", "")
                log.debug("[TradierBroker] poll %s: status=%s exec_qty=%s",
                          order_id, status, order.get("exec_quantity", "N/A"))

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
                    reason_desc = order.get("reason_description", "")
                    if reason_desc:
                        log.warning("[TradierBroker] Order %s %s: %s", order_id, status, reason_desc[:200])
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
            log.warning("[TradierBroker] Cancel failed: %s", order_id, exc)

    def _emit_order_fail(
        self, p: OrderRequestPayload, parent_event: Event
    ) -> None:
        """Emit an ORDER_FAIL event matching the OrderFailPayload dataclass."""
        # V10: WAL FAILED
        _wal_cid = getattr(self, '_current_wal_cid', '')
        if _wal_cid:
            try:
                from .order_wal import wal as _wal
                _wal.failed(_wal_cid, reason=f'order_fail:{p.ticker}:{p.reason}')
            except Exception:
                pass
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

    def get_positions(self) -> list[dict]:
        """Fetch open positions in normalized format for BrokerRegistry.

        Returns list of {symbol, qty, avg_entry, cost_basis} dicts.
        """
        raw = self._get_positions_raw()
        result = []
        for p in raw:
            qty = float(p.get('quantity', 0))
            cost = float(p.get('cost_basis', 0))
            if qty > 0:
                result.append({
                    'symbol': p.get('symbol', ''),
                    'qty': qty,
                    'avg_entry': cost / qty if qty > 0 else 0.0,
                    'cost_basis': cost,
                })
        return result

    def _get_positions_raw(self) -> list:
        """Fetch raw position dicts from Tradier API."""
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

    def get_equity(self) -> float | None:
        """Fetch total account equity from Tradier."""
        bal = self.get_account_balance()
        if bal:
            return float(bal.get('total_equity', 0))
        return None

    @property
    def supports_fractional(self) -> bool:
        return False

    @property
    def supports_notional(self) -> bool:
        return False

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
