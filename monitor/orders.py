import logging
import time
from zoneinfo import ZoneInfo

from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from .alerts import send_alert

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)


class OrderManager:
    """Handles order placement via Alpaca TradingClient."""

    SLIPPAGE_PCT     = 0.0001  # 0.01% — used for stop/target accounting only
    MAX_SPREAD_PCT   = 0.002   # skip entry if bid/ask spread > 0.2%
    FILL_TIMEOUT_SEC = 2.0     # seconds to wait for fill before cancel/retry
    FILL_POLL_SEC    = 0.25    # polling interval while waiting for fill
    MAX_RETRIES      = 3       # max cancel-and-retry attempts per entry

    def __init__(self, trading_client, alert_email):
        self._client = trading_client
        self._alert_email = alert_email

    def place_buy(self, ticker, qty, ask_price, refresh_ask=None):
        """
        Marketable limit buy — placed exactly at the ask.

        If the order does not fill within FILL_TIMEOUT_SEC seconds it is
        cancelled and retried with a fresh ask price (up to MAX_RETRIES times).

        Parameters
        ----------
        ticker      : str
        qty         : int
        ask_price   : float  — current ask for the first attempt
        refresh_ask : callable () -> float | None
                      Called before each retry to get the latest ask.
                      Pass None to skip retries (single-attempt mode).

        Returns
        -------
        str | None  — order ID of the filled order, or None if all attempts fail.
        """
        if not self._client:
            send_alert(
                self._alert_email,
                f"Order skipped (no Alpaca client): BUY {qty} {ticker}",
            )
            return None

        for attempt in range(1, self.MAX_RETRIES + 1):
            limit_price = round(ask_price, 2)

            # ── Submit ────────────────────────────────────────────────
            try:
                req = LimitOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=OrderSide.BUY,
                    limit_price=limit_price,
                    time_in_force=TimeInForce.DAY,
                )
                order = self._client.submit_order(req)
                order_id = str(order.id)
                log.info(
                    f"[attempt {attempt}] BUY limit {qty} {ticker} @ ${limit_price:.2f} "
                    f"(order {order_id})"
                )
            except Exception as e:
                send_alert(self._alert_email, f"Order submit failed: BUY {qty} {ticker} — {e}")
                return None

            # ── Poll for fill ─────────────────────────────────────────
            filled = False
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
                filled_qty = int(float(order.filled_qty or qty))
                avg_price  = float(order.filled_avg_price or limit_price)
                send_alert(
                    self._alert_email,
                    f"BUY filled: {filled_qty} {ticker} avg ${avg_price:.2f} "
                    f"(limit ${limit_price:.2f}, attempt {attempt})",
                )
                return order_id

            # ── Cancel unfilled order ─────────────────────────────────
            try:
                self._client.cancel_order_by_id(order_id)
                log.info(
                    f"BUY cancelled (unfilled after {self.FILL_TIMEOUT_SEC}s): "
                    f"{ticker} attempt {attempt}"
                )
            except Exception as e:
                log.warning(f"Cancel error ({order_id}): {e}")

            # ── Decide whether to retry ───────────────────────────────
            if refresh_ask is None or attempt == self.MAX_RETRIES:
                log.warning(f"BUY abandoned: {ticker} after {attempt} attempt(s).")
                send_alert(
                    self._alert_email,
                    f"BUY abandoned: {ticker} — unfilled after {attempt} attempt(s)",
                )
                return None

            new_ask = refresh_ask()
            if not new_ask or new_ask <= 0:
                return None
            log.info(
                f"Retrying {ticker}: fresh ask ${new_ask:.2f} "
                f"(was ${ask_price:.2f}, Δ{(new_ask - ask_price) / ask_price:+.3%})"
            )
            ask_price = new_ask

        return None  # exhausted retries

    def place_sell(self, ticker, qty):
        """
        Market sell — speed over price on exits.
        Returns order ID string on success, None on failure.
        """
        if not self._client:
            send_alert(self._alert_email, f"Order skipped (no Alpaca client): SELL {qty} {ticker}")
            return None
        try:
            req = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(req)
            send_alert(self._alert_email, f"SELL {qty} {ticker} at market (order {order.id})")
            return str(order.id)
        except Exception as e:
            send_alert(self._alert_email, f"Order failed: SELL {qty} {ticker} — {e}")
            return None
