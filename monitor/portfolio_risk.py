"""
Portfolio-Level Risk Gate — aggregate risk limits across all engines.

Sits above individual engine risk checks (RiskEngine, RiskAdapter, OptionsRiskGate).
Subscribes to ORDER_REQ at priority 0 (runs BEFORE any broker) and blocks
orders that would breach portfolio-level limits.

Limits enforced:
  1. Intraday drawdown (realized + unrealized)
  2. Notional exposure cap (total $ deployed)
  3. Pre-trade margin/buying power check
  4. Portfolio Greeks limits (delta, gamma)

All limits configurable via environment variables with safe defaults.
"""
import logging
import os
import threading
import time
from typing import Dict, Optional

from monitor.event_bus import EventBus, EventType, Event

log = logging.getLogger(__name__)

# ── Configurable limits ──────────────────────────────────────────────────────
MAX_INTRADAY_DRAWDOWN = float(os.getenv('MAX_INTRADAY_DRAWDOWN', -5000))  # $ total (realized + unrealized)
MAX_NOTIONAL_EXPOSURE = float(os.getenv('MAX_NOTIONAL_EXPOSURE', 100000))  # $ total across all engines
MAX_PORTFOLIO_DELTA   = float(os.getenv('MAX_PORTFOLIO_DELTA', 5.0))       # absolute delta units
MAX_PORTFOLIO_GAMMA   = float(os.getenv('MAX_PORTFOLIO_GAMMA', 1.0))       # absolute gamma


class PortfolioRiskGate:
    """
    Portfolio-level risk enforcement across all trading engines.

    Subscribes to:
      - ORDER_REQ (priority 0) — block orders that breach limits
      - FILL — track realized P&L and positions
      - POSITION — track position changes

    Must be registered BEFORE any broker on the EventBus so it can
    block orders before they reach execution.
    """

    def __init__(self, bus: EventBus, monitor=None):
        """
        Args:
            bus: shared EventBus
            monitor: RealTimeMonitor instance (for position/trade_log access)
        """
        self._bus = bus
        self._monitor = monitor
        self._lock = threading.Lock()

        # Tracking state
        self._realized_pnl = 0.0
        self._positions: Dict[str, dict] = {}  # {ticker: {qty, entry_price, current_price}}
        self._halted = False
        self._block_count = 0

        # Subscribe at priority 0 (before brokers at priority 1)
        bus.subscribe(EventType.ORDER_REQ, self._on_order_req, priority=0)
        bus.subscribe(EventType.FILL, self._on_fill, priority=0)
        bus.subscribe(EventType.POSITION, self._on_position, priority=0)

        log.info(
            "[PortfolioRisk] ready | max_drawdown=$%.0f | max_notional=$%.0f | "
            "max_delta=%.1f | max_gamma=%.1f",
            MAX_INTRADAY_DRAWDOWN, MAX_NOTIONAL_EXPOSURE,
            MAX_PORTFOLIO_DELTA, MAX_PORTFOLIO_GAMMA,
        )

    def _on_order_req(self, event: Event) -> None:
        """Pre-trade risk check — block if portfolio limits breached."""
        if self._halted:
            self._block("HALTED", event, "portfolio risk halt active")
            return

        p = event.payload
        ticker = p.ticker
        side = str(getattr(p, 'side', '')).upper()
        qty = getattr(p, 'qty', 0)
        price = float(getattr(p, 'price', 0))

        # Sells always pass (reducing risk)
        if side == 'SELL':
            return

        # ── Check 1: Intraday drawdown ─────────────────────────────────
        total_pnl = self._get_total_pnl()
        if total_pnl <= MAX_INTRADAY_DRAWDOWN:
            self._halted = True
            self._block(ticker, event,
                        f"intraday drawdown ${total_pnl:+.2f} breached limit ${MAX_INTRADAY_DRAWDOWN:+.2f}")
            log.error("[PortfolioRisk] DRAWDOWN HALT: total P&L $%.2f (limit $%.2f)",
                      total_pnl, MAX_INTRADAY_DRAWDOWN)
            return

        # ── Check 2: Notional exposure cap ─────────────────────────────
        current_notional = self._get_total_notional()
        order_notional = qty * price
        if current_notional + order_notional > MAX_NOTIONAL_EXPOSURE:
            self._block(ticker, event,
                        f"notional ${current_notional + order_notional:,.0f} would exceed "
                        f"cap ${MAX_NOTIONAL_EXPOSURE:,.0f}")
            return

        # ── Check 3: Pre-trade margin check ────────────────────────────
        buying_power = self._get_buying_power()
        if buying_power is not None and order_notional > buying_power:
            self._block(ticker, event,
                        f"order ${order_notional:,.0f} exceeds buying power ${buying_power:,.0f}")
            return

        # ── Check 4: Portfolio Greeks limits ────────────────────────────
        portfolio_delta, portfolio_gamma = self._get_portfolio_greeks()
        if abs(portfolio_delta) > MAX_PORTFOLIO_DELTA:
            self._block(ticker, event,
                        f"portfolio delta {portfolio_delta:+.2f} exceeds limit {MAX_PORTFOLIO_DELTA}")
            return
        if portfolio_gamma > MAX_PORTFOLIO_GAMMA:
            self._block(ticker, event,
                        f"portfolio gamma {portfolio_gamma:.3f} exceeds limit {MAX_PORTFOLIO_GAMMA}")
            return

    def _on_fill(self, event: Event) -> None:
        """Track fills for P&L and position updates."""
        p = event.payload
        ticker = getattr(p, 'ticker', '')
        side = str(getattr(p, 'side', '')).upper()
        qty = getattr(p, 'qty', 0)
        fill_price = float(getattr(p, 'fill_price', 0))

        with self._lock:
            if side == 'BUY':
                if ticker in self._positions:
                    pos = self._positions[ticker]
                    # Average in
                    total_qty = pos['qty'] + qty
                    pos['entry_price'] = (pos['entry_price'] * pos['qty'] + fill_price * qty) / total_qty
                    pos['qty'] = total_qty
                    pos['current_price'] = fill_price
                else:
                    self._positions[ticker] = {
                        'qty': qty,
                        'entry_price': fill_price,
                        'current_price': fill_price,
                    }
            elif side == 'SELL':
                if ticker in self._positions:
                    pos = self._positions[ticker]
                    pnl = (fill_price - pos['entry_price']) * qty
                    self._realized_pnl += pnl
                    pos['qty'] -= qty
                    if pos['qty'] <= 0:
                        del self._positions[ticker]

    def _on_position(self, event: Event) -> None:
        """Track position changes for unrealized P&L."""
        p = event.payload
        if hasattr(p, 'pnl') and p.pnl is not None:
            action = str(getattr(p, 'action', ''))
            if action in ('CLOSED', 'closed'):
                with self._lock:
                    ticker = getattr(p, 'ticker', '')
                    self._positions.pop(ticker, None)

    def _get_total_pnl(self) -> float:
        """Get realized + unrealized P&L."""
        with self._lock:
            unrealized = sum(
                (pos['current_price'] - pos['entry_price']) * pos['qty']
                for pos in self._positions.values()
            )
            return self._realized_pnl + unrealized

    def _get_total_notional(self) -> float:
        """Get total notional exposure (sum of position values)."""
        with self._lock:
            return sum(
                pos['current_price'] * pos['qty']
                for pos in self._positions.values()
            )

    def _get_buying_power(self) -> Optional[float]:
        """Fetch buying power from Alpaca (best-effort)."""
        try:
            import requests
            api_key = os.getenv('APCA_API_KEY_ID', '')
            api_secret = os.getenv('APCA_API_SECRET_KEY', '')
            if not api_key:
                return None
            base = os.getenv('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets')
            r = requests.get(
                f'{base}/v2/account',
                headers={'APCA-API-KEY-ID': api_key, 'APCA-API-SECRET-KEY': api_secret},
                timeout=3,
            )
            if r.status_code == 200:
                return float(r.json().get('buying_power', 0))
        except Exception:
            pass
        return None

    def _get_portfolio_greeks(self) -> tuple:
        """Get aggregate portfolio delta and gamma from options positions."""
        try:
            from options.portfolio_greeks import PortfolioGreeksTracker
            # Try to get the last snapshot
            tracker = PortfolioGreeksTracker()
            snap = tracker.snapshot()
            return snap.get('total_delta', 0), snap.get('total_gamma', 0)
        except Exception:
            return 0.0, 0.0

    def _block(self, ticker: str, event: Event, reason: str) -> None:
        """Block an order and emit RISK_BLOCK event."""
        self._block_count += 1
        log.warning("[PortfolioRisk] BLOCKED %s: %s", ticker, reason)

        try:
            from monitor.events import RiskBlockPayload
            self._bus.emit(Event(
                type=EventType.RISK_BLOCK,
                payload=RiskBlockPayload(
                    ticker=ticker,
                    signal_action=str(getattr(event.payload, 'side', 'BUY')),
                    reason=f"portfolio_risk: {reason}",
                ),
                correlation_id=event.event_id,
            ))
        except Exception:
            pass

        # Mark the event as blocked so downstream brokers skip it
        try:
            object.__setattr__(event, '_portfolio_blocked', True)
        except Exception:
            pass

    def status(self) -> dict:
        """Return current portfolio risk status."""
        unrealized = sum(
            (pos['current_price'] - pos['entry_price']) * pos['qty']
            for pos in self._positions.values()
        )
        return {
            'halted': self._halted,
            'realized_pnl': round(self._realized_pnl, 2),
            'unrealized_pnl': round(unrealized, 2),
            'total_pnl': round(self._realized_pnl + unrealized, 2),
            'notional_exposure': round(self._get_total_notional(), 2),
            'positions_count': len(self._positions),
            'blocks_today': self._block_count,
        }
