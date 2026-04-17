"""
PopLifecycleAdapter — maps PopStrategyEngine + PopExecutor state to lifecycle interface.

Pop has its own Alpaca account (APCA_POPUP_KEY / APCA_POPUP_SECRET_KEY).
PopExecutor tracks positions as Set[str] and cooldowns as Dict[str, float].
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List

from .base import AbstractEngineAdapter

log = logging.getLogger(__name__)


class PopLifecycleAdapter(AbstractEngineAdapter):

    def __init__(self, pop_engine):
        """
        Args:
            pop_engine: PopStrategyEngine instance (has ._executor: PopExecutor)
        """
        self._engine = pop_engine
        self._executor = getattr(pop_engine, '_executor', None)
        self._trade_log: List[dict] = []
        self._daily_pnl: float = 0.0

    def get_state(self) -> dict:
        positions = list(self._executor._positions) if self._executor else []
        last_order = {}
        if self._executor:
            now = time.monotonic()
            last_order = {k: round(now - v, 1)
                          for k, v in self._executor._last_order_time.items()}

        return {
            'positions': positions,
            'last_order_offsets': last_order,
            'trade_log': self._trade_log,
            'daily_pnl': self._daily_pnl,
        }

    def restore_state(self, state: dict) -> None:
        if not self._executor:
            return

        positions = state.get('positions', [])
        with self._executor._lock:
            self._executor._positions = set(positions)

        now = time.monotonic()
        for ticker, offset in state.get('last_order_offsets', {}).items():
            self._executor._last_order_time[ticker] = now - offset

        self._trade_log = state.get('trade_log', [])
        self._daily_pnl = state.get('daily_pnl', 0.0)

        log.info("[pop] Restored state: %d positions, %d trades, pnl=$%.2f",
                 len(positions), len(self._trade_log), self._daily_pnl)

    def get_positions(self) -> Dict[str, Any]:
        if not self._executor:
            return {}
        with self._executor._lock:
            return {t: 1 for t in self._executor._positions}

    def get_trade_log(self) -> List[dict]:
        return self._trade_log

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def get_daily_stats(self) -> dict:
        wins = sum(1 for t in self._trade_log if (t.get('pnl') or 0) >= 0)
        pos_count = len(self._executor._positions) if self._executor else 0
        return {
            'trades': len(self._trade_log),
            'wins': wins,
            'pnl': self._daily_pnl,
            'open_positions': pos_count,
        }

    def get_broker_positions(self) -> Dict[str, int]:
        """Query Pop's Alpaca account for open positions."""
        try:
            from alpaca.trading.client import TradingClient
            key = os.getenv('APCA_POPUP_KEY', '')
            secret = os.getenv('APCA_POPUP_SECRET_KEY', '')
            if not key or not secret:
                return {}
            client = TradingClient(key, secret, paper=True)
            positions = client.get_all_positions()
            return {str(p.symbol): int(float(p.qty)) for p in positions
                    if int(float(p.qty)) > 0}
        except Exception as exc:
            log.warning("[pop] Broker position fetch failed: %s", exc)
            return {}

    def force_close_all(self, reason: str) -> None:
        """Close all pop positions via Pop's Alpaca account."""
        if not self._executor:
            return
        with self._executor._lock:
            tickers = list(self._executor._positions)
        if not tickers:
            return

        log.warning("[pop] FORCE CLOSE %d positions (reason=%s): %s",
                    len(tickers), reason, tickers)
        try:
            from alpaca.trading.client import TradingClient
            key = os.getenv('APCA_POPUP_KEY', '')
            secret = os.getenv('APCA_POPUP_SECRET_KEY', '')
            if key and secret:
                client = TradingClient(key, secret, paper=True)
                for ticker in tickers:
                    try:
                        client.close_position(ticker)
                        log.info("[pop] Closed %s", ticker)
                    except Exception as exc:
                        log.warning("[pop] Failed to close %s: %s", ticker, exc)
        except Exception as exc:
            log.error("[pop] Force close failed: %s", exc)

        with self._executor._lock:
            self._executor._positions.clear()

    def cancel_stale_orders(self) -> int:
        """Cancel stale orders on Pop's Alpaca account."""
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            key = os.getenv('APCA_POPUP_KEY', '')
            secret = os.getenv('APCA_POPUP_SECRET_KEY', '')
            if not key or not secret:
                return 0
            client = TradingClient(key, secret, paper=True)
            orders = client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
            cancelled = 0
            for o in orders:
                try:
                    client.cancel_order_by_id(str(o.id))
                    cancelled += 1
                except Exception:
                    pass
            return cancelled
        except Exception:
            return 0

    def verify_connectivity(self) -> bool:
        """Check Pop's Alpaca account is reachable."""
        try:
            from alpaca.trading.client import TradingClient
            key = os.getenv('APCA_POPUP_KEY', '')
            secret = os.getenv('APCA_POPUP_SECRET_KEY', '')
            if not key or not secret:
                return False
            client = TradingClient(key, secret, paper=True)
            client.get_account()
            return True
        except Exception:
            return False

    def add_position(self, ticker: str, info: Any) -> None:
        if self._executor:
            with self._executor._lock:
                self._executor._positions.add(ticker)

    def remove_position(self, ticker: str) -> None:
        if self._executor:
            with self._executor._lock:
                self._executor._positions.discard(ticker)
