"""
ProLifecycleAdapter — maps ProSetupEngine + RiskAdapter state to lifecycle interface.

Pro engine sends ORDER_REQ to core via IPC (Redpanda). It doesn't execute trades
directly — core's broker handles execution. So:
- Positions are tracked in RiskAdapter._positions (Set[str])
- No direct broker access (positions live at core's Alpaca/Tradier)
- Reconciliation uses DistributedPositionRegistry (file-locked cross-process)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

from .base import AbstractEngineAdapter

log = logging.getLogger(__name__)


class ProLifecycleAdapter(AbstractEngineAdapter):

    def __init__(self, pro_engine, risk_adapter=None):
        """
        Args:
            pro_engine: ProSetupEngine instance
            risk_adapter: RiskAdapter instance (holds position/cooldown state)
        """
        self._engine = pro_engine
        self._risk = risk_adapter or getattr(pro_engine, '_risk_adapter', None)
        self._trade_log: List[dict] = []
        self._daily_pnl: float = 0.0

    def get_state(self) -> dict:
        positions = list(self._risk._positions) if self._risk else []
        last_order = {}
        last_signal = {}
        if self._risk:
            # Convert monotonic timestamps to relative offsets for persistence
            now = time.monotonic()
            last_order = {k: round(now - v, 1) for k, v in self._risk._last_order.items()}
            last_signal = {k: round(now - v, 1) for k, v in self._risk._last_signal.items()}

        return {
            'positions': positions,
            'last_order_offsets': last_order,
            'last_signal_offsets': last_signal,
            'trade_log': self._trade_log,
            'daily_pnl': self._daily_pnl,
        }

    def restore_state(self, state: dict) -> None:
        if not self._risk:
            return

        positions = state.get('positions', [])
        self._risk._positions = set(positions)

        # Restore cooldown timestamps from relative offsets
        now = time.monotonic()
        for ticker, offset in state.get('last_order_offsets', {}).items():
            self._risk._last_order[ticker] = now - offset
        for key, offset in state.get('last_signal_offsets', {}).items():
            self._risk._last_signal[key] = now - offset

        self._trade_log = state.get('trade_log', [])
        self._daily_pnl = state.get('daily_pnl', 0.0)

        log.info("[pro] Restored state: %d positions, %d trades, pnl=$%.2f",
                 len(positions), len(self._trade_log), self._daily_pnl)

    def get_positions(self) -> Dict[str, Any]:
        if not self._risk:
            return {}
        return {t: 1 for t in self._risk._positions}

    def get_trade_log(self) -> List[dict]:
        return self._trade_log

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def get_daily_stats(self) -> dict:
        wins = sum(1 for t in self._trade_log if (t.get('pnl') or 0) >= 0)
        return {
            'trades': len(self._trade_log),
            'wins': wins,
            'pnl': self._daily_pnl,
            'open_positions': len(self._risk._positions) if self._risk else 0,
        }

    def get_broker_positions(self) -> Dict[str, int]:
        """Pro doesn't have direct broker access. Use position registry."""
        try:
            from monitor.distributed_registry import DistributedPositionRegistry
            registry = DistributedPositionRegistry()
            all_positions = registry.get_all()
            return {t: 1 for t, layer in all_positions.items() if layer == 'pro_setups'}
        except Exception:
            return {}

    def force_close_all(self, reason: str) -> None:
        """Pro can't close directly — it would need to send SELL ORDER_REQs to core.
        For now, just clear local tracking and log warning."""
        if not self._risk:
            return
        tickers = list(self._risk._positions)
        if tickers:
            log.warning("[pro] FORCE CLOSE requested (reason=%s) for %s — "
                        "Pro cannot sell directly, positions at core's broker",
                        reason, tickers)
            # Clear local tracking — core should handle the actual sells
            self._risk._positions.clear()

    def cancel_stale_orders(self) -> int:
        """Pro sends orders via IPC, not directly to broker. Nothing to cancel."""
        return 0

    def verify_connectivity(self) -> bool:
        """Check that shared cache is readable (Pro reads bars from cache)."""
        try:
            from monitor.shared_cache import CacheReader
            reader = CacheReader()
            bars, _ = reader.get_bars()
            return bars is not None
        except Exception:
            return False

    def add_position(self, ticker: str, info: Any) -> None:
        if self._risk:
            self._risk._positions.add(ticker)

    def remove_position(self, ticker: str) -> None:
        if self._risk:
            self._risk._positions.discard(ticker)
