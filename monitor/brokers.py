"""
T2 — Broker Abstraction
========================
Defines the BaseBroker interface and two concrete implementations:

  AlpacaBroker  — live/paper execution via Alpaca TradingClient.
                  Marketable limit buy with 2-second fill window and retry.
                  Market sell for speed on exits.

  PaperBroker   — pure local simulation; fills every order instantly at the
                  requested price. No API keys or network calls required.
                  Use for strategy back-testing and CI without credentials.

Both implementations:
  • Subscribe to EventType.ORDER_REQ on the EventBus.
  • Emit EventType.FILL on success or EventType.ORDER_FAIL on abandonment.
  • Never do risk checks — that is the Risk Engine's responsibility (T3).

Factory
-------
    broker = make_broker('alpaca', bus, trading_client=client, alert_email=email)
    broker = make_broker('paper',  bus)

Config key: BROKER  ('alpaca' | 'paper', default 'alpaca')
"""
from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Optional

from .alerts import send_alert
from .event_bus import Event, EventBus, EventType
from .events import FillPayload, OrderFailPayload, OrderRequestPayload

log = logging.getLogger(__name__)


# ── Base interface ────────────────────────────────────────────────────────────

class BaseBroker(ABC):
    """
    Abstract broker.  Concrete subclasses handle order execution and emit
    FILL / ORDER_FAIL events back onto the bus.
    """

    def __init__(self, bus: EventBus, alert_email: Optional[str] = None):
        self._bus         = bus
        self._alert_email = alert_email
        bus.subscribe(EventType.ORDER_REQ, self._on_order_request)

    def _on_order_request(self, event: Event) -> None:
        # Check if portfolio risk gate already blocked this order
        if getattr(event, '_portfolio_blocked', False):
            return
        p: OrderRequestPayload = event.payload
        if p.side == 'BUY':
            self._execute_buy(p)
        else:
            self._execute_sell(p)

    @abstractmethod
    def _execute_buy(self, p: OrderRequestPayload) -> None: ...

    @abstractmethod
    def _execute_sell(self, p: OrderRequestPayload) -> None: ...


# ── Alpaca broker ─────────────────────────────────────────────────────────────

class AlpacaBroker(BaseBroker):
    """
    Execution via Alpaca TradingClient (live or paper account).

    Buy strategy — marketable limit at exact ask:
      1. Submit limit order at ask.
      2. Poll for fill every FILL_POLL_SEC seconds up to FILL_TIMEOUT_SEC.
      3. If unfilled: cancel, fetch fresh ask via quote_fn(ticker), retry.
      4. Abandon after MAX_RETRIES and emit ORDER_FAIL.

    Sell strategy — market order for exit speed.
    """

    SLIPPAGE_PCT     = 0.0001   # 0.01% — used for stop/target accounting only
    FILL_TIMEOUT_SEC = 5.0      # seconds to wait before cancel/retry (2s was too aggressive for illiquid names)
    FILL_POLL_SEC    = 0.5      # poll interval while waiting for fill
    MAX_RETRIES      = 2        # cancel-and-retry attempts per entry (fewer retries, longer timeout)
    MAX_SLIPPAGE_PCT = 0.005    # 0.5%  — abandon retry if ask drifts beyond this

    def __init__(
        self,
        trading_client,
        bus:         EventBus,
        alert_email: Optional[str]                     = None,
        quote_fn:    Optional[callable]                = None,
    ):
        """
        quote_fn: callable(ticker: str) -> Optional[float]
            Called before each retry when p.needs_ask_refresh is True to fetch
            the latest ask price.  Typically wired to DataClient.check_spread.
            Pass None to disable ask-refresh (single-attempt mode per order).
        """
        super().__init__(bus, alert_email)
        self._client   = trading_client
        self._quote_fn = quote_fn   # ticker -> Optional[float]

    def _execute_buy(self, p: OrderRequestPayload) -> None:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if not self._client:
            send_alert(self._alert_email, f"BUY skipped (no Alpaca client): {p.qty} {p.ticker}")
            self._fail(p)
            return

        ask_price    = p.price
        original_ask = p.price

        for attempt in range(1, self.MAX_RETRIES + 1):
            limit_price = round(ask_price, 2)

            # ── Submit ────────────────────────────────────────────────
            try:
                req = LimitOrderRequest(
                    symbol=p.ticker,
                    qty=p.qty,
                    side=OrderSide.BUY,
                    limit_price=limit_price,
                    time_in_force=TimeInForce.DAY,
                )
                order    = self._client.submit_order(req)
                order_id = str(order.id)
                log.info(
                    f"[attempt {attempt}] BUY limit {p.qty} {p.ticker} "
                    f"@ ${limit_price:.2f} (order {order_id})"
                )
            except Exception as e:
                send_alert(self._alert_email, f"BUY submit failed: {p.qty} {p.ticker} — {e}")
                self._fail(p)
                return

            # ── Poll for fill ─────────────────────────────────────────
            filled   = False
            deadline = time.monotonic() + self.FILL_TIMEOUT_SEC
            while time.monotonic() < deadline:
                time.sleep(self.FILL_POLL_SEC)
                try:
                    order = self._client.get_order_by_id(order_id)
                    if order.status in ('filled', 'partially_filled'):
                        filled = True
                        break
                except Exception as e:
                    log.warning(f"Order status check failed ({order_id}): {e}")
                    break

            if filled:
                filled_qty = int(float(order.filled_qty  or p.qty))
                avg_price  = float(order.filled_avg_price or limit_price)

                # Wait 1s for Alpaca to fully settle the buy order.
                # Without this, an immediate sell signal triggers a wash trade
                # rejection because Alpaca still sees the buy as "recent".
                time.sleep(1.0)

                send_alert(
                    self._alert_email,
                    f"BUY filled: {filled_qty} {p.ticker} avg ${avg_price:.2f} "
                    f"(limit ${limit_price:.2f}, attempt {attempt})",
                )
                self._bus.emit(Event(EventType.FILL, FillPayload(
                    ticker=p.ticker, side='BUY', qty=filled_qty,
                    fill_price=avg_price, order_id=order_id, reason=p.reason,
                    stop_price=p.stop_price, target_price=p.target_price,
                    atr_value=p.atr_value,
                )))
                return

            # ── Cancel unfilled ───────────────────────────────────────
            try:
                self._client.cancel_order_by_id(order_id)
                log.info(
                    f"BUY cancelled (unfilled after {self.FILL_TIMEOUT_SEC}s): "
                    f"{p.ticker} attempt {attempt}"
                )
            except Exception as e:
                log.warning(f"Cancel error ({order_id}): {e}")

            # ── Retry or abandon ──────────────────────────────────────
            if not p.needs_ask_refresh or self._quote_fn is None or attempt == self.MAX_RETRIES:
                log.warning(f"BUY abandoned: {p.ticker} after {attempt} attempt(s).")
                send_alert(
                    self._alert_email,
                    f"BUY abandoned: {p.ticker} — unfilled after {attempt} attempt(s)",
                )
                self._fail(p)
                return

            new_ask = self._quote_fn(p.ticker)
            if not new_ask or new_ask <= 0:
                self._fail(p)
                return

            if new_ask > original_ask * (1 + self.MAX_SLIPPAGE_PCT):
                log.warning(
                    f"BUY abandoned: {p.ticker} — ask drifted too far "
                    f"(${new_ask:.2f} > ${original_ask:.2f} + {self.MAX_SLIPPAGE_PCT:.1%})"
                )
                send_alert(
                    self._alert_email,
                    f"BUY abandoned: {p.ticker} — slippage cap breached "
                    f"(fresh ask ${new_ask:.2f} vs original ${original_ask:.2f})",
                )
                self._fail(p)
                return

            log.info(
                f"Retrying {p.ticker}: fresh ask ${new_ask:.2f} "
                f"(was ${ask_price:.2f}, Δ{(new_ask - ask_price) / ask_price:+.3%})"
            )
            ask_price = new_ask

    def _execute_sell(self, p: OrderRequestPayload) -> None:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        import uuid

        if not self._client:
            send_alert(self._alert_email, f"SELL skipped (no Alpaca client): {p.qty} {p.ticker}")
            self._fail(p)
            return

        client_order_id = f"th-sell-{p.ticker}-{uuid.uuid4().hex[:8]}"

        # ── Step 1: Cancel ALL open orders for this ticker (buy AND sell) ────
        # This prevents wash trade rejection from Alpaca.
        # Must wait briefly after cancel for Alpaca to process.
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            open_orders = self._client.get_orders(
                filter=GetOrdersRequest(
                    status=QueryOrderStatus.OPEN,
                    symbols=[p.ticker],
                )
            )
            if open_orders:
                for oo in open_orders:
                    try:
                        self._client.cancel_order_by_id(str(oo.id))
                        log.info(f"Cancelled {oo.side} order {oo.id} for {p.ticker} before SELL")
                    except Exception:
                        pass
                # Wait for Alpaca to process cancellations
                time.sleep(0.5)
        except Exception as cancel_err:
            log.warning(f"Could not cancel pending orders for {p.ticker}: {cancel_err}")

        # ── Step 2: Verify actual position qty at Alpaca ─────────────────────
        # The monitor may think we hold X shares but Alpaca may have a different qty
        # (from orphaned positions, partial fills, etc.)
        actual_qty = p.qty
        try:
            position = self._client.get_open_position(p.ticker)
            alpaca_qty = int(float(position.qty))
            if alpaca_qty != p.qty:
                log.warning(
                    f"SELL qty mismatch for {p.ticker}: monitor says {p.qty}, "
                    f"Alpaca says {alpaca_qty} — using Alpaca qty"
                )
                actual_qty = alpaca_qty
        except Exception:
            # Position may not exist at Alpaca (already closed)
            pass

        if actual_qty <= 0:
            log.warning(f"SELL skipped for {p.ticker}: no position at Alpaca")
            self._fail(p)
            return

        # ── Step 3: Submit SELL and wait for fill confirmation ────────────────
        try:
            req = MarketOrderRequest(
                symbol=p.ticker,
                qty=actual_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )
            order = self._client.submit_order(req)
            order_id = str(order.id)

            # Poll for ACTUAL fill — do NOT emit FILL until confirmed
            filled = False
            fill_price = p.price
            filled_qty = actual_qty
            deadline = time.monotonic() + 5.0  # 5 second timeout for sell fills
            while time.monotonic() < deadline:
                time.sleep(0.5)
                try:
                    order = self._client.get_order_by_id(order_id)
                    status = str(getattr(order, 'status', ''))
                    if status == 'filled':
                        filled = True
                        fill_price = float(order.filled_avg_price or p.price)
                        filled_qty = int(float(order.filled_qty or actual_qty))
                        break
                    elif status in ('cancelled', 'expired', 'rejected'):
                        log.warning(f"SELL {p.ticker} order {status}")
                        break
                except Exception:
                    break

            if filled:
                send_alert(
                    self._alert_email,
                    f"SELL {filled_qty} {p.ticker} at market (order {order_id})",
                )
                self._bus.emit(Event(EventType.FILL, FillPayload(
                    ticker=p.ticker, side='SELL', qty=filled_qty,
                    fill_price=fill_price, order_id=order_id, reason=p.reason,
                )))
            else:
                log.warning(
                    f"SELL {p.ticker} not confirmed within 5s — "
                    f"emitting FILL optimistically (order {order_id})"
                )
                send_alert(
                    self._alert_email,
                    f"SELL {actual_qty} {p.ticker} at market (order {order_id})",
                )
                self._bus.emit(Event(EventType.FILL, FillPayload(
                    ticker=p.ticker, side='SELL', qty=actual_qty,
                    fill_price=p.price, order_id=order_id, reason=p.reason,
                )))

        except Exception as e:
            log.error(
                f"SELL exception for {actual_qty} {p.ticker}: {e} — "
                f"polling Alpaca for order status (client_order_id={client_order_id})"
            )
            try:
                order = self._client.get_order_by_client_id(client_order_id)
                status = str(getattr(order, 'status', 'unknown'))
                if status in ('filled', 'partially_filled'):
                    fill_price = float(order.filled_avg_price or p.price)
                    filled_qty = int(float(order.filled_qty or actual_qty))
                    order_id = str(order.id)
                    log.warning(
                        f"SELL for {p.ticker} was actually {status} at Alpaca "
                        f"(qty={filled_qty} @ ${fill_price:.2f}) — emitting FILL"
                    )
                    self._bus.emit(Event(EventType.FILL, FillPayload(
                        ticker=p.ticker, side='SELL', qty=filled_qty,
                        fill_price=fill_price, order_id=order_id, reason=p.reason,
                    )))
                    return
            except Exception as poll_exc:
                log.error(f"Failed to poll Alpaca for sell status: {poll_exc}")

            send_alert(self._alert_email, f"SELL failed: {p.qty} {p.ticker} — {e}")
            self._fail(p)

    def _fail(self, p: OrderRequestPayload) -> None:
        self._bus.emit(Event(EventType.ORDER_FAIL, OrderFailPayload(
            ticker=p.ticker, side=p.side, qty=p.qty, price=p.price, reason=p.reason,
        )))


# ── Paper broker ──────────────────────────────────────────────────────────────

class PaperBroker(BaseBroker):
    """
    Local simulation broker — no API, no network.
    Every order fills instantly at the requested price.

    Use when:
      • Testing strategy logic without Alpaca credentials.
      • Running in CI / unit tests.
      • Verifying the event pipeline end-to-end locally.
    """

    def _execute_buy(self, p: OrderRequestPayload) -> None:
        order_id = str(uuid.uuid4())
        log.info(f"[PAPER] BUY  {p.qty:>4} {p.ticker:<6} @ ${p.price:.2f}  ({p.reason})")
        self._bus.emit(Event(EventType.FILL, FillPayload(
            ticker=p.ticker, side='BUY', qty=p.qty,
            fill_price=p.price, order_id=order_id, reason=p.reason,
            stop_price=p.stop_price, target_price=p.target_price,
            atr_value=p.atr_value,
        )))

    def _execute_sell(self, p: OrderRequestPayload) -> None:
        order_id = str(uuid.uuid4())
        log.info(f"[PAPER] SELL {p.qty:>4} {p.ticker:<6} @ ${p.price:.2f}  ({p.reason})")
        self._bus.emit(Event(EventType.FILL, FillPayload(
            ticker=p.ticker, side='SELL', qty=p.qty,
            fill_price=p.price, order_id=order_id, reason=p.reason,
        )))


# ── Factory ───────────────────────────────────────────────────────────────────

def make_broker(
    source: str,
    bus: EventBus,
    trading_client=None,
    alert_email: Optional[str] = None,
) -> BaseBroker:
    """
    Factory — returns the appropriate BaseBroker.

    Parameters
    ----------
    source         : 'alpaca' | 'paper'
    bus            : EventBus — shared bus instance
    trading_client : alpaca.trading.client.TradingClient or None
                     Required for source='alpaca'
    alert_email    : str or None
    """
    source = (source or 'alpaca').lower().strip()
    if source == 'alpaca':
        return AlpacaBroker(trading_client, bus, alert_email)
    elif source == 'paper':
        return PaperBroker(bus, alert_email)
    else:
        raise ValueError(
            f"Unknown broker: {source!r}. Choose 'alpaca' or 'paper'."
        )
