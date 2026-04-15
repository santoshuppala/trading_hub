"""
OptionsLifecycleAdapter — maps OptionsEngine state to lifecycle interface.

Options has its own Alpaca account (APCA_OPTIONS_KEY / APCA_OPTIONS_SECRET).
Most complex state: positions with legs/greeks, portfolio-level Greeks, risk gate.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from .base import AbstractEngineAdapter

log = logging.getLogger(__name__)


class OptionsLifecycleAdapter(AbstractEngineAdapter):

    def __init__(self, options_engine):
        """
        Args:
            options_engine: OptionsEngine instance
        """
        self._engine = options_engine

    def get_state(self) -> dict:
        # Serialize positions (OptionsPosition objects → dicts)
        positions = {}
        with self._engine._positions_lock:
            for ticker, pos in self._engine._positions.items():
                positions[ticker] = {
                    'strategy_type': pos.strategy_type,
                    'entry_cost': pos.entry_cost,
                    'max_risk': pos.max_risk,
                    'max_reward': pos.max_reward,
                    'expiry_date': str(pos.expiry_date),
                    'entry_time': pos.entry_time,
                }

        risk = self._engine._risk
        risk_state = {
            'open_positions': dict(risk._open_positions),
            'deployed_capital': risk._deployed_capital,
            'daily_trade_count': risk._daily_trade_count,
        }

        return {
            'positions': positions,
            'daily_pnl': self._engine._daily_pnl,
            'daily_entries': self._engine._daily_entries,
            'daily_exits': self._engine._daily_exits,
            'portfolio_greeks': {
                'delta': self._engine._portfolio_delta,
                'theta': self._engine._portfolio_theta,
                'vega': self._engine._portfolio_vega,
            },
            'risk_gate': risk_state,
            'trade_log': self._get_trade_log_from_engine(),
        }

    def restore_state(self, state: dict) -> None:
        # Restore daily counters
        self._engine._daily_pnl = state.get('daily_pnl', 0.0)
        self._engine._daily_entries = state.get('daily_entries', 0)
        self._engine._daily_exits = state.get('daily_exits', 0)

        # Restore portfolio Greeks
        greeks = state.get('portfolio_greeks', {})
        self._engine._portfolio_delta = greeks.get('delta', 0.0)
        self._engine._portfolio_theta = greeks.get('theta', 0.0)
        self._engine._portfolio_vega = greeks.get('vega', 0.0)

        # Restore risk gate state
        risk_state = state.get('risk_gate', {})
        risk = self._engine._risk
        with risk._lock:
            risk._open_positions = risk_state.get('open_positions', {})
            risk._deployed_capital = risk_state.get('deployed_capital', 0.0)
            risk._daily_trade_count = risk_state.get('daily_trade_count', 0)

        # Note: We do NOT restore _positions (OptionsPosition objects) from state file.
        # Instead, reconciliation with broker will rebuild actual positions.
        # The state file positions are just for reference/logging.

        positions = state.get('positions', {})
        log.info("[options] Restored state: %d position refs, %d trades, "
                 "pnl=$%.2f, deployed=$%.2f",
                 len(positions), self._engine._daily_entries,
                 self._engine._daily_pnl, risk._deployed_capital)

    def get_positions(self) -> Dict[str, Any]:
        with self._engine._positions_lock:
            return {t: {'strategy': p.strategy_type, 'max_risk': p.max_risk}
                    for t, p in self._engine._positions.items()}

    def get_trade_log(self) -> List[dict]:
        return self._get_trade_log_from_engine()

    def _get_trade_log_from_engine(self) -> List[dict]:
        """Options engine doesn't maintain a trade_log list like Core.
        We track closes via daily_exits counter. For EOD report, query DB."""
        return []  # TODO: query from event_store if needed

    def get_daily_pnl(self) -> float:
        return self._engine._daily_pnl

    def get_daily_stats(self) -> dict:
        return {
            'trades': self._engine._daily_entries,
            'wins': 0,  # Options doesn't track wins separately
            'pnl': self._engine._daily_pnl,
            'open_positions': len(self._engine._positions),
            'extra': {
                'entries': self._engine._daily_entries,
                'exits': self._engine._daily_exits,
                'portfolio_delta': round(self._engine._portfolio_delta, 2),
                'portfolio_theta': round(self._engine._portfolio_theta, 2),
                'portfolio_vega': round(self._engine._portfolio_vega, 2),
                'deployed_capital': round(self._engine._risk._deployed_capital, 2),
            },
        }

    def get_broker_positions(self) -> Dict[str, int]:
        """Query Options Alpaca account for open positions."""
        try:
            from alpaca.trading.client import TradingClient
            key = os.getenv('APCA_OPTIONS_KEY', '')
            secret = os.getenv('APCA_OPTIONS_SECRET', '')
            if not key or not secret:
                return {}
            client = TradingClient(key, secret, paper=True)
            positions = client.get_all_positions()
            # Group option legs by underlying ticker
            from collections import defaultdict
            by_ticker = defaultdict(int)
            for p in positions:
                sym = str(p.symbol)
                # Extract underlying from option symbol (AAPL260508C00265000 → AAPL)
                for i, ch in enumerate(sym):
                    if ch.isdigit():
                        ticker = sym[:i]
                        break
                else:
                    ticker = sym
                by_ticker[ticker] += 1
            return dict(by_ticker)
        except Exception as exc:
            log.warning("[options] Broker position fetch failed: %s", exc)
            return {}

    def force_close_all(self, reason: str) -> None:
        """Close all options positions via OptionsEngine.close_all_positions()."""
        try:
            self._engine.close_all_positions(reason=reason)
        except Exception as exc:
            log.error("[options] Force close failed: %s", exc)

    def cancel_stale_orders(self) -> int:
        """Cancel stale orders on Options Alpaca account."""
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            key = os.getenv('APCA_OPTIONS_KEY', '')
            secret = os.getenv('APCA_OPTIONS_SECRET', '')
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
        """Check Options Alpaca account is reachable."""
        try:
            from alpaca.trading.client import TradingClient
            key = os.getenv('APCA_OPTIONS_KEY', '')
            secret = os.getenv('APCA_OPTIONS_SECRET', '')
            if not key or not secret:
                return False
            client = TradingClient(key, secret, paper=True)
            client.get_account()
            return True
        except Exception:
            return False

    def add_position(self, ticker: str, info: Any) -> None:
        """Add a position reference (actual OptionsPosition requires full spec)."""
        log.info("[options] Orphaned position %s detected at broker — "
                 "requires manual review (cannot auto-rebuild option legs)", ticker)

    def remove_position(self, ticker: str) -> None:
        with self._engine._positions_lock:
            if ticker in self._engine._positions:
                del self._engine._positions[ticker]
        self._engine._risk.release(ticker)
