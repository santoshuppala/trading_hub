import logging
from zoneinfo import ZoneInfo

from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from .alerts import send_alert

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)


class OrderManager:
    """Handles order placement via Alpaca TradingClient."""

    SLIPPAGE_PCT = 0.0001    # 0.01% per side
    LIMIT_BUFFER_PCT = 0.0005  # how far above ask we're willing to pay (0.05%)
    MAX_SPREAD_PCT = 0.002   # skip entry if bid/ask spread > 0.2%

    def __init__(self, trading_client, alert_email):
        """
        Parameters
        ----------
        trading_client : alpaca.trading.client.TradingClient or None
        alert_email : str or None
        """
        self._client = trading_client
        self._alert_email = alert_email

    def place_buy(self, ticker, qty, ask_price):
        """
        Place a marketable limit buy order slightly above ask so it fills
        immediately but rejects if price spikes before fill.

        Returns order ID string on success, None on failure.
        """
        if not self._client:
            send_alert(self._alert_email, f"Order skipped (no Alpaca client): BUY {qty} {ticker}")
            return None
        try:
            limit_price = round(ask_price * (1 + self.LIMIT_BUFFER_PCT), 2)
            order_request = LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY,
                limit_price=limit_price,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(order_request)
            send_alert(
                self._alert_email,
                f"Order placed: BUY {qty} {ticker} limit ${limit_price:.2f} (ask ~${ask_price:.2f})",
            )
            return order.id
        except Exception as e:
            send_alert(self._alert_email, f"Order failed: BUY {qty} {ticker} - {e}")
            return None

    def place_sell(self, ticker, qty):
        """
        Place a market sell order. Exit speed is critical so market orders
        are preferred over limit for sells.

        Returns order ID string on success, None on failure.
        """
        if not self._client:
            send_alert(self._alert_email, f"Order skipped (no Alpaca client): SELL {qty} {ticker}")
            return None
        try:
            order_request = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            order = self._client.submit_order(order_request)
            send_alert(self._alert_email, f"Order placed: SELL {qty} {ticker} at market")
            return order.id
        except Exception as e:
            send_alert(self._alert_email, f"Order failed: SELL {qty} {ticker} - {e}")
            return None
