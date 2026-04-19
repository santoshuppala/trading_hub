"""
TradierStreamClient — real-time WebSocket streaming for market quotes.

Uses the PRODUCTION Tradier token ($10/month) for streaming data.
Orders are NEVER placed through this client — it is read-only market data.
Order execution stays on the SANDBOX token via TradierBroker.

Architecture:
    Tradier PRODUCTION token → WebSocket → QUOTE events on EventBus
    Tradier SANDBOX token → REST orders (TradierBroker, separate)

Features:
    - Auto-reconnect with session refresh on disconnect
    - Dynamic ticker subscription (resend payload, no reconnect needed)
    - Price cache for latest quote per ticker
    - Emits EventType.QUOTE on every trade event for exit monitoring
    - Graceful degradation: if streaming fails, REST polling continues

Constraints:
    - Only ONE streaming session allowed per Tradier account
    - Session expires after 5 minutes of no connection
    - Sandbox tokens do NOT support streaming (scope-stream required)

Usage:
    stream = TradierStreamClient(
        token=os.getenv('TRADIER_TOKEN'),  # PRODUCTION token
        bus=monitor._bus,
    )
    stream.start(initial_tickers=['AAPL', 'MSFT'])
    stream.update_tickers({'AAPL', 'TSLA', 'SPY'})
    stream.stop()
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

import requests

try:
    import websocket
except ImportError:
    websocket = None  # graceful degradation if not installed

from .event_bus import Event, EventBus, EventType
from .events import QuotePayload

log = logging.getLogger(__name__)

# Session creation endpoint (PRODUCTION only — sandbox returns 401)
_SESSION_URL = 'https://api.tradier.com/v1/markets/events/session'

# WebSocket endpoint (returned by session creation, but hardcoded as fallback)
_WS_URL = 'wss://stream.tradier.com/v1/markets/events'

# Reconnect delay after disconnect
_RECONNECT_DELAY = 5  # seconds

# Maximum time without data before forcing reconnect
_HEARTBEAT_TIMEOUT = 60  # seconds


class TradierStreamClient:
    """Real-time quote/trade streaming via Tradier WebSocket.

    Uses PRODUCTION token for data (streaming requires paid account).
    Orders still go through SANDBOX token (TradierBroker).
    """

    def __init__(self, token: str, bus: EventBus):
        """
        Args:
            token: Tradier PRODUCTION API token (NOT sandbox).
            bus: EventBus for emitting QUOTE events.
        """
        if websocket is None:
            raise ImportError(
                "websocket-client required for streaming. "
                "Install with: pip install websocket-client"
            )
        self._token = token
        self._bus = bus
        self._tickers: set[str] = set()
        self._ws: Optional[websocket.WebSocket] = None
        self._session_id: Optional[str] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_message_time: float = 0.0

        # Correctness: session epoch prevents stale events from dead connections
        # Incremented on every reconnect. Messages tagged with epoch at receive time.
        # _handle_message() only processes events from the current epoch.
        self._session_epoch: int = 0
        self._active_epoch: int = 0  # epoch of the currently connected WebSocket

        # Latest price cache — {ticker: {bid, ask, last, timestamp}}
        self._prices: dict[str, dict] = {}
        self._prices_lock = threading.Lock()

        # V9: Cumulative volume cache — {ticker: cvol}
        # Used by RVOLEngine for instant real-time RVOL (no bar accumulation needed)
        self._cvol: dict[str, int] = {}

        # Stats
        self._messages_received = 0
        self._reconnect_count = 0

    # ── Public API ────────────────────────────────────────────────────

    def start(self, initial_tickers: list[str] | set[str] = None) -> bool:
        """Create session and connect WebSocket in background thread.

        Returns True if session created successfully.
        """
        if initial_tickers:
            self._tickers = set(initial_tickers)

        try:
            self._session_id = self._create_session()
        except Exception as exc:
            log.error("[TradierStream] Session creation failed: %s — "
                      "streaming unavailable (REST polling continues)", exc)
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name='TradierStreamClient',
        )
        self._thread.start()
        log.info("[TradierStream] Started | tickers=%d | session=%s",
                 len(self._tickers), self._session_id[:12] if self._session_id else '?')
        return True

    def stop(self) -> None:
        """Disconnect and stop background thread."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        log.info("[TradierStream] Stopped | messages=%d reconnects=%d",
                 self._messages_received, self._reconnect_count)

    def update_tickers(self, tickers: set[str]) -> None:
        """Update subscription list without reconnecting.

        Tradier WebSocket allows resending the payload to change symbols.
        """
        old = self._tickers
        self._tickers = set(tickers)
        if self._tickers != old and self._ws and self._session_id:
            try:
                self._send_subscription()
                added = self._tickers - old
                removed = old - self._tickers
                if added or removed:
                    log.info("[TradierStream] Subscription updated | "
                             "+%s -%s | total=%d",
                             sorted(added) if added else '{}',
                             sorted(removed) if removed else '{}',
                             len(self._tickers))
            except Exception as exc:
                log.warning("[TradierStream] Subscription update failed: %s", exc)

    def get_price(self, ticker: str) -> Optional[float]:
        """Get latest streaming price for a ticker (or None if not available)."""
        with self._prices_lock:
            entry = self._prices.get(ticker)
            return entry.get('last') if entry else None

    def get_quote(self, ticker: str, max_age_seconds: float = 5.0) -> Optional[dict]:
        """Get latest bid/ask/last for a ticker.

        Returns None if quote is older than max_age_seconds (stale protection).
        """
        with self._prices_lock:
            entry = self._prices.get(ticker)
            if not entry:
                return None
            age = time.monotonic() - entry.get('timestamp', 0)
            if age > max_age_seconds:
                return None  # stale quote — force REST fallback
            return entry.copy()

    def get_cvol(self, ticker: str) -> Optional[int]:
        """Get latest cumulative daily volume from streaming trades.

        Used by RVOLEngine for instant RVOL computation.
        Returns None if no trade received yet for this ticker.
        """
        return self._cvol.get(ticker)

    def get_all_cvol(self) -> dict[str, int]:
        """Get all cvol values. Returns {ticker: cvol}."""
        return dict(self._cvol)

    @property
    def is_connected(self) -> bool:
        """True if WebSocket is currently connected and receiving data."""
        if not self._running or not self._ws:
            return False
        return (time.monotonic() - self._last_message_time) < _HEARTBEAT_TIMEOUT

    @property
    def ticker_count(self) -> int:
        return len(self._tickers)

    # ── Session Management ────────────────────────────────────────────

    def _create_session(self) -> str:
        """Create a streaming session with the PRODUCTION token.

        Returns session ID (valid for 5 minutes — must connect quickly).
        """
        r = requests.post(
            _SESSION_URL,
            headers={
                'Authorization': f'Bearer {self._token}',
                'Accept': 'application/json',
            },
            timeout=10,
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"Tradier streaming session failed: {r.status_code} {r.text[:200]}"
            )
        data = r.json()
        session_id = data.get('stream', {}).get('sessionid', '')
        if not session_id:
            raise RuntimeError(f"No sessionid in response: {data}")
        return session_id

    # ── WebSocket Loop ────────────────────────────────────────────────

    def _run(self) -> None:
        """Background thread: connect, receive, auto-reconnect."""
        while self._running:
            try:
                self._connect_and_receive()
            except Exception as exc:
                if not self._running:
                    break
                self._reconnect_count += 1
                log.warning(
                    "[TradierStream] Disconnected (reconnect #%d): %s — "
                    "retrying in %ds",
                    self._reconnect_count, exc, _RECONNECT_DELAY,
                )
                time.sleep(_RECONNECT_DELAY)
                # Refresh session (may have expired)
                try:
                    self._session_id = self._create_session()
                    log.info("[TradierStream] Session refreshed: %s",
                             self._session_id[:12])
                except Exception as se:
                    log.warning("[TradierStream] Session refresh failed: %s", se)

    def _connect_and_receive(self) -> None:
        """Single WebSocket connection lifecycle."""
        if not self._tickers:
            log.debug("[TradierStream] No tickers to subscribe — sleeping")
            time.sleep(5)
            return

        # Increment epoch BEFORE connecting — any messages from old connection
        # will have a stale epoch and be rejected by _handle_message()
        self._session_epoch += 1
        current_epoch = self._session_epoch

        self._ws = websocket.create_connection(_WS_URL, timeout=30)
        self._active_epoch = current_epoch
        self._send_subscription()
        self._last_message_time = time.monotonic()

        log.info("[TradierStream] Connected epoch=%d | subscribed=%d tickers",
                 current_epoch, len(self._tickers))

        while self._running:
            try:
                msg = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                # Check heartbeat — if no data for too long, reconnect
                if (time.monotonic() - self._last_message_time) > _HEARTBEAT_TIMEOUT:
                    raise ConnectionError("No data for %ds" % _HEARTBEAT_TIMEOUT)
                continue

            if not msg or not msg.strip():
                continue

            self._last_message_time = time.monotonic()
            self._messages_received += 1

            try:
                data = json.loads(msg)
                self._handle_message(data)
            except json.JSONDecodeError:
                log.debug("[TradierStream] Bad JSON: %s", msg[:100])

    def _send_subscription(self) -> None:
        """Send subscription payload to WebSocket."""
        if not self._ws or not self._session_id:
            return
        payload = json.dumps({
            'symbols': sorted(self._tickers),
            'sessionid': self._session_id,
            'filter': ['quote', 'trade'],
            'linebreak': True,
        })
        self._ws.send(payload)

    # ── Message Handling ──────────────────────────────────────────────

    def _handle_message(self, data: dict) -> None:
        """Process a streaming quote or trade message.

        Correctness guards:
          - Session epoch: reject events from stale/dead WebSocket connections
          - Quote age: reject quotes older than 5 seconds (network delay)
        """
        msg_type = data.get('type')
        ticker = data.get('symbol')
        if not ticker:
            return

        # Guard: reject messages from stale session (reconnect race)
        if self._active_epoch != self._session_epoch:
            return  # this connection is dead, new one is active

        # Guard: reject quotes if last message was >5s ago (half-open socket)
        receive_time = time.monotonic()
        if self._last_message_time > 0:
            gap = receive_time - self._last_message_time
            if gap > 30:
                log.warning("[TradierStream] Message gap %.1fs — possible stale data", gap)

        if msg_type == 'quote':
            bid = float(data.get('bid', 0))
            ask = float(data.get('ask', 0))
            if bid <= 0 or ask <= 0 or ask < bid:
                return  # invalid quote
            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid if mid > 0 else 0

            with self._prices_lock:
                self._prices[ticker] = {
                    'bid': bid, 'ask': ask,
                    'last': mid,
                    'spread_pct': spread_pct,
                    'timestamp': time.monotonic(),
                }

            # Emit QUOTE event for exit monitoring
            try:
                self._bus.emit(Event(
                    type=EventType.QUOTE,
                    payload=QuotePayload(
                        ticker=ticker, bid=bid, ask=ask,
                        spread_pct=round(spread_pct, 6),
                    ),
                ))
            except Exception as exc:
                log.debug("[TradierStream] QUOTE emit failed for %s: %s",
                          ticker, exc)

        elif msg_type == 'trade':
            price = float(data.get('price', 0))
            size = int(data.get('size', 0))
            cvol = int(data.get('cvol', 0))
            if price <= 0:
                return

            # V9: Capture cumulative volume for real-time RVOL
            if cvol > 0:
                self._cvol[ticker] = cvol

            with self._prices_lock:
                existing = self._prices.get(ticker, {})
                self._prices[ticker] = {
                    'bid': existing.get('bid', price),
                    'ask': existing.get('ask', price),
                    'last': price,
                    'spread_pct': existing.get('spread_pct', 0),
                    'size': size,
                    'cvol': cvol,
                    'timestamp': time.monotonic(),
                }

            # Emit QUOTE event with trade price as bid=ask (no spread info)
            try:
                self._bus.emit(Event(
                    type=EventType.QUOTE,
                    payload=QuotePayload(
                        ticker=ticker, bid=price, ask=price,
                        spread_pct=0.0,
                    ),
                ))
            except Exception as exc:
                log.debug("[TradierStream] QUOTE emit failed for %s: %s",
                          ticker, exc)
