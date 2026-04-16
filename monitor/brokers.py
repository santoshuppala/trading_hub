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

from config import MAX_SLIPPAGE_PCT as _CFG_MAX_SLIPPAGE_PCT
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

    def __init__(self, bus: EventBus, alert_email: Optional[str] = None,
                 broker_name: str = 'alpaca'):
        self._bus         = bus
        self._alert_email = alert_email
        self._broker_name = broker_name
        bus.subscribe(EventType.ORDER_REQ, self._on_order_request)

    def _emit_fill(self, fill_payload: FillPayload, correlation_id=None) -> None:
        """Emit a FILL event tagged with this broker's name.

        V7 P2-2: Auto-sets correlation_id from _current_causation_id,
        linking FILL back to the ORDER_REQ that triggered it.
        Enables end-to-end tracing: SIGNAL → ORDER_REQ → FILL → POSITION.
        """
        cid = correlation_id or getattr(self, '_current_causation_id', None)
        evt = Event(EventType.FILL, fill_payload, correlation_id=cid)
        evt._routed_broker = self._broker_name
        self._bus.emit(evt)

    def _on_order_request(self, event: Event) -> None:
        # Check if portfolio risk gate already blocked this order
        if getattr(event, '_portfolio_blocked', False):
            return
        p: OrderRequestPayload = event.payload
        # V7 P0-1: Tag payload with event_id for deterministic client_order_id
        # (frozen dataclass — use object.__setattr__ to bypass)
        object.__setattr__(p, '_source_event_id', event.event_id)
        # V7 P2-2: Store ORDER_REQ event_id for causation chain.
        # _emit_fill() reads this to set correlation_id on FILL events.
        self._current_causation_id = event.event_id
        if p.side == 'BUY':
            self._execute_buy(p)
        else:
            self._execute_sell(p)
        self._current_causation_id = None

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
    MAX_SLIPPAGE_PCT = _CFG_MAX_SLIPPAGE_PCT  # from config.py (default 0.5%)

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
        from alpaca.trading.requests import (
            LimitOrderRequest, StopLossRequest, TakeProfitRequest,
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        if not self._client:
            send_alert(self._alert_email, f"BUY skipped (no Alpaca client): {p.qty} {p.ticker}")
            self._fail(p)
            return

        ask_price    = p.price
        original_ask = p.price

        for attempt in range(1, self.MAX_RETRIES + 1):
            limit_price = round(ask_price, 2)

            # ── Build bracket order if stop/target available ──────────
            bracket_kwargs = {}
            if p.stop_price and p.stop_price > 0 and p.stop_price < limit_price:
                bracket_kwargs['order_class'] = OrderClass.BRACKET
                bracket_kwargs['stop_loss'] = StopLossRequest(
                    stop_price=round(p.stop_price, 2),
                )
                if p.target_price and p.target_price > limit_price:
                    bracket_kwargs['take_profit'] = TakeProfitRequest(
                        limit_price=round(p.target_price, 2),
                    )

            # ── Submit ────────────────────────────────────────────────
            try:
                # V7 P0-1: Deterministic client_order_id from event context.
                # If process crashes after submit but before FILL confirmation,
                # restart + replay will generate the SAME client_order_id.
                # Alpaca rejects duplicate client_order_id → no double orders.
                event_id = getattr(p, '_source_event_id', '') or uuid.uuid4().hex
                client_oid = f"th-buy-{p.ticker}-{event_id[:12]}-{attempt}"

                req = LimitOrderRequest(
                    symbol=p.ticker,
                    qty=p.qty,
                    side=OrderSide.BUY,
                    limit_price=limit_price,
                    time_in_force=TimeInForce.DAY,
                    client_order_id=client_oid,
                    **bracket_kwargs,
                )
                order    = self._client.submit_order(req)
                order_id = str(order.id)
                order_type = "bracket" if bracket_kwargs.get('order_class') else "limit"
                stop_info = f" stop=${p.stop_price:.2f}" if p.stop_price else ""
                target_info = f" target=${p.target_price:.2f}" if p.target_price else ""
                log.info(
                    f"[attempt {attempt}] BUY {order_type} {p.qty} {p.ticker} "
                    f"@ ${limit_price:.2f}{stop_info}{target_info} (order {order_id})"
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

                # If partial fill, cancel remaining order to prevent untracked fills
                order_status = getattr(order, 'status', '')
                if hasattr(order_status, 'value'):
                    order_status = order_status.value
                if order_status == 'partially_filled':
                    log.warning(
                        "BUY %s partial fill: %d of %d — cancelling remainder",
                        p.ticker, filled_qty, p.qty,
                    )
                    try:
                        self._client.cancel_order_by_id(order_id)
                    except Exception:
                        pass
                    time.sleep(0.5)
                    # Re-check final filled qty after cancel
                    try:
                        final_order = self._client.get_order_by_id(order_id)
                        filled_qty = int(float(final_order.filled_qty or filled_qty))
                        avg_price = float(final_order.filled_avg_price or avg_price)
                    except Exception:
                        pass

                # Wait 1s for Alpaca to fully settle the buy order.
                time.sleep(1.0)

                # Extract engine/strategy from reason for rich alerts
                _reason = getattr(p, 'reason', '') or ''
                _engine = _reason.split(':')[0] if ':' in _reason else 'core'
                _strat = _reason.split(':')[1] if ':' in _reason and len(_reason.split(':')) > 1 else _reason

                send_alert(
                    self._alert_email,
                    f"BUY FILLED: {filled_qty} {p.ticker} @ ${avg_price:.2f}\n"
                    f"  Engine: {_engine.upper()} | Strategy: {_strat} | Broker: {self._broker_name}\n"
                    f"  Limit: ${limit_price:.2f} | Attempt: {attempt}/{self.MAX_RETRIES}\n"
                    f"  Stop: ${p.stop_price:.2f} | Target: ${p.target_price:.2f}" if p.stop_price else
                    f"BUY FILLED: {filled_qty} {p.ticker} @ ${avg_price:.2f} | "
                    f"Engine: {_engine.upper()} | Strategy: {_strat} | Broker: {self._broker_name}",
                )

                # Bracket order: if partial fill, cancel bracket children and resubmit
                # for the actual filled qty to prevent stop selling more than we hold
                if bracket_kwargs.get('order_class') and filled_qty < p.qty:
                    self._cancel_bracket_children(p.ticker)
                    # Resubmit stop-loss only for filled qty
                    if p.stop_price and p.stop_price > 0:
                        try:
                            from alpaca.trading.requests import StopOrderRequest
                            from alpaca.trading.enums import OrderSide as _OS, TimeInForce as _TIF
                            stop_req = StopOrderRequest(
                                symbol=p.ticker, qty=filled_qty,
                                side=_OS.SELL, stop_price=round(p.stop_price, 2),
                                time_in_force=_TIF.DAY,
                            )
                            self._client.submit_order(stop_req)
                            log.info("Resubmitted stop for partial fill: %s qty=%d stop=$%.2f",
                                     p.ticker, filled_qty, p.stop_price)
                        except Exception as exc:
                            log.warning("Stop resubmit failed for %s: %s", p.ticker, exc)

                self._emit_fill(FillPayload(
                    ticker=p.ticker, side='BUY', qty=filled_qty,
                    fill_price=avg_price, order_id=order_id, reason=p.reason,
                    stop_price=p.stop_price, target_price=p.target_price,
                    atr_value=p.atr_value,
                ))
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

    def _cancel_bracket_children(self, ticker: str) -> None:
        """Cancel any open bracket child orders (stop-loss/take-profit) for a ticker.
        Must be called before submitting a SELL to prevent double-execution."""
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = self._client.get_orders(filter=req)
            cancelled = 0
            for o in orders:
                if str(o.symbol) != ticker:
                    continue
                side = getattr(o, 'side', None)
                side_str = side.value if hasattr(side, 'value') else str(side)
                order_class = getattr(o, 'order_class', None)
                class_str = order_class.value if hasattr(order_class, 'value') else str(order_class)
                # Cancel bracket parents and any open sell orders for this ticker
                if class_str == 'bracket' or side_str.lower() == 'sell':
                    try:
                        self._client.cancel_order_by_id(str(o.id))
                        cancelled += 1
                    except Exception:
                        pass
            if cancelled:
                log.info("[AlpacaBroker] Cancelled %d bracket/stop orders for %s before SELL",
                         cancelled, ticker)
        except Exception as exc:
            log.warning("[AlpacaBroker] Failed to cancel bracket orders for %s: %s", ticker, exc)

    def _execute_sell(self, p: OrderRequestPayload) -> None:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        import uuid

        if not self._client:
            send_alert(self._alert_email, f"SELL skipped (no Alpaca client): {p.qty} {p.ticker}")
            self._fail(p)
            return

        # Cancel any bracket child orders (stop-loss/take-profit) before selling
        # to prevent double-execution (strategy sells + bracket stop fires = short)
        self._cancel_bracket_children(p.ticker)

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
                    except Exception as exc:
                        log.warning("Cancel open order %s for %s failed: %s", oo.id, p.ticker, exc)
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
        except Exception as exc:
            # V7.1: Position doesn't exist at Alpaca (likely closed by bracket stop).
            # Set actual_qty = 0 to prevent selling shares we don't hold → creating short.
            actual_qty = 0
            log.warning("Position lookup for %s failed — setting qty=0 to prevent short: %s",
                         p.ticker, exc)

        if actual_qty <= 0:
            log.warning(f"SELL skipped for {p.ticker}: no position at Alpaca (prevents accidental short)")
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
                except Exception as exc:
                    log.warning("SELL order status check failed (%s): %s", order_id, exc)
                    break

            if filled:
                _reason = getattr(p, 'reason', '') or ''
                send_alert(
                    self._alert_email,
                    f"SELL FILLED: {filled_qty} {p.ticker} @ ${fill_price:.2f} | "
                    f"Broker: {self._broker_name} | Reason: {_reason}",
                )
                self._emit_fill(FillPayload(
                    ticker=p.ticker, side='SELL', qty=filled_qty,
                    fill_price=fill_price, order_id=order_id, reason=p.reason,
                ))
            else:
                # V7 P0-5: Do NOT emit optimistic FILL. Verify with extended poll.
                # Optimistic fills caused phantom position closes in V6 when
                # order was actually pending/cancelled.
                verified = False
                for retry in range(3):
                    time.sleep(1.0)  # 1s, 2s, 3s extra wait
                    try:
                        final = self._client.get_order_by_id(order_id)
                        final_status = final.status.value if hasattr(final.status, 'value') else str(final.status)
                        final_qty = int(float(final.filled_qty or 0))

                        if final_status == 'filled':
                            fill_price = float(final.filled_avg_price or p.price)
                            filled_qty = int(float(final.filled_qty or actual_qty))
                            self._emit_fill(FillPayload(
                                ticker=p.ticker, side='SELL', qty=filled_qty,
                                fill_price=fill_price, order_id=order_id, reason=p.reason,
                            ))
                            verified = True
                            break
                        elif final_status == 'partially_filled' and final_qty > 0:
                            fill_price = float(final.filled_avg_price or p.price)
                            log.warning(
                                f"SELL {p.ticker} partial fill: {final_qty} of {actual_qty}"
                            )
                            self._emit_fill(FillPayload(
                                ticker=p.ticker, side='SELL', qty=final_qty,
                                fill_price=fill_price, order_id=order_id, reason=p.reason,
                            ))
                            verified = True
                            break
                        elif final_status in ('cancelled', 'expired', 'rejected'):
                            log.error(
                                f"SELL {p.ticker} terminal status={final_status} — "
                                f"NOT emitting FILL (order {order_id})"
                            )
                            self._fail(p)
                            verified = True
                            break
                    except Exception as exc:
                        log.warning("SELL verify retry %d failed for %s: %s",
                                    retry + 1, p.ticker, exc)

                if not verified:
                    # After 3 retries (8s total extra), still no confirmation.
                    # Emit ORDER_FAIL instead of optimistic FILL.
                    log.error(
                        f"SELL {p.ticker} UNVERIFIED after 8s — "
                        f"emitting ORDER_FAIL (order {order_id}). "
                        f"Manual check required!"
                    )
                    send_alert(
                        self._alert_email,
                        f"SELL UNVERIFIED: {actual_qty} {p.ticker} order {order_id} — "
                        f"check Alpaca manually!",
                        severity='CRITICAL',
                    )
                    self._fail(p)

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
                    self._emit_fill(FillPayload(
                        ticker=p.ticker, side='SELL', qty=filled_qty,
                        fill_price=fill_price, order_id=order_id, reason=p.reason,
                    ))
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
        self._emit_fill(FillPayload(
            ticker=p.ticker, side='BUY', qty=p.qty,
            fill_price=p.price, order_id=order_id, reason=p.reason,
            stop_price=p.stop_price, target_price=p.target_price,
            atr_value=p.atr_value,
        ))

    def _execute_sell(self, p: OrderRequestPayload) -> None:
        order_id = str(uuid.uuid4())
        log.info(f"[PAPER] SELL {p.qty:>4} {p.ticker:<6} @ ${p.price:.2f}  ({p.reason})")
        self._emit_fill(FillPayload(
            ticker=p.ticker, side='SELL', qty=p.qty,
            fill_price=p.price, order_id=order_id, reason=p.reason,
        ))


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
