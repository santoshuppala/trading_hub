"""
db/subscriber.py — EventBus → DBWriter bridge.

Subscribes to every EventType at LOW priority (so trading layers
always run first).  Converts each payload to a row dict and calls
writer.enqueue().  Never raises — errors are logged and swallowed so
that a DB issue never disrupts the trading pipeline.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict

from monitor.event_bus import Event, EventType
from monitor.events import (
    BarPayload, SignalPayload, OrderRequestPayload, FillPayload,
    PositionPayload, RiskBlockPayload, PopSignalPayload,
    ProStrategySignalPayload,
)

from .writer import DBWriter

log = logging.getLogger(__name__)

_NOW = lambda: datetime.now(timezone.utc)


class DBSubscriber:
    """
    Wires all EventBus subscriptions that feed the TimescaleDB writer.

    Parameters
    ----------
    bus    : EventBus — the shared monitor bus
    writer : DBWriter — the async batch writer (must already be started)

    Call ``register()`` before ``bus.start()``.
    """

    def __init__(self, bus: Any, writer: DBWriter) -> None:
        self._bus    = bus
        self._writer = writer

    def register(self) -> None:
        """Subscribe to all event types at LOW priority (priority=10)."""
        handlers = {
            EventType.BAR:                    self._on_bar,
            EventType.SIGNAL:                 self._on_signal,
            EventType.ORDER_REQ:              self._on_order_req,
            EventType.FILL:                   self._on_fill,
            EventType.POSITION:               self._on_position,
            EventType.RISK_BLOCK:             self._on_risk_block,
            EventType.POP_SIGNAL:             self._on_pop_signal,
            EventType.PRO_STRATEGY_SIGNAL:    self._on_pro_strategy_signal,
            EventType.HEARTBEAT:              self._on_heartbeat,
        }
        for event_type, handler in handlers.items():
            self._bus.subscribe(event_type, handler, priority=10)
        log.info("DBSubscriber registered %d event handlers", len(handlers))

    # ── Handlers ──────────────────────────────────────────────────────────────

    def _on_bar(self, event: Event) -> None:
        try:
            p: BarPayload = event.payload
            df = p.df
            if df is None or df.empty:
                return
            last = df.iloc[-1]
            row: Dict[str, Any] = {
                "ts":          _NOW(),
                "ticker":      p.ticker,
                "open":        float(last.get("open",  last.get("o",  0))),
                "high":        float(last.get("high",  last.get("h",  0))),
                "low":         float(last.get("low",   last.get("l",  0))),
                "close":       float(last.get("close", last.get("c",  0))),
                "volume":      int(last.get("volume",  last.get("v",  0))),
                "vwap":        _safe_float(last.get("vwap")),
                "rvol":        _safe_float(getattr(p, "rvol_df", None)),
                "atr":         None,
                "rsi":         None,
                "event_id":    str(event.event_id),
                "ingested_at": _NOW(),
            }
            self._writer.enqueue("bar_events", row)
        except Exception as exc:
            log.debug("DBSubscriber._on_bar error: %s", exc)

    def _on_signal(self, event: Event) -> None:
        try:
            p: SignalPayload = event.payload
            row = {
                "ts":             _NOW(),
                "ticker":         p.ticker,
                "action":         p.action.value,
                "current_price":  float(p.current_price),
                "ask_price":      _safe_float(p.ask_price),
                "atr_value":      _safe_float(p.atr_value),
                "rsi_value":      _safe_float(p.rsi_value),
                "rvol":           _safe_float(p.rvol),
                "vwap":           _safe_float(p.vwap),
                "stop_price":     _safe_float(p.stop_price),
                "target_price":   _safe_float(p.target_price),
                "half_target":    _safe_float(p.half_target),
                "event_id":       str(event.event_id),
                "correlation_id": _str_or_none(event.correlation_id),
                "ingested_at":    _NOW(),
            }
            self._writer.enqueue("signal_events", row)
        except Exception as exc:
            log.debug("DBSubscriber._on_signal error: %s", exc)

    def _on_order_req(self, event: Event) -> None:
        try:
            p: OrderRequestPayload = event.payload
            row = {
                "ts":             _NOW(),
                "ticker":         p.ticker,
                "side":           p.side.value,
                "qty":            int(p.qty),
                "price":          float(p.price),
                "reason":         p.reason,
                "stop_price":     _safe_float(p.stop_price),
                "target_price":   _safe_float(p.target_price),
                "atr_value":      _safe_float(p.atr_value),
                "event_id":       str(event.event_id),
                "correlation_id": _str_or_none(event.correlation_id),
                "ingested_at":    _NOW(),
            }
            self._writer.enqueue("order_req_events", row)
        except Exception as exc:
            log.debug("DBSubscriber._on_order_req error: %s", exc)

    def _on_fill(self, event: Event) -> None:
        try:
            p: FillPayload = event.payload
            row = {
                "ts":             _NOW(),
                "ticker":         p.ticker,
                "side":           p.side.value,
                "qty":            int(p.qty),
                "fill_price":     float(p.fill_price),
                "order_id":       p.order_id,
                "reason":         p.reason,
                "stop_price":     _safe_float(p.stop_price),
                "target_price":   _safe_float(p.target_price),
                "atr_value":      _safe_float(p.atr_value),
                "event_id":       str(event.event_id),
                "correlation_id": _str_or_none(event.correlation_id),
                "ingested_at":    _NOW(),
            }
            self._writer.enqueue("fill_events", row)
        except Exception as exc:
            log.debug("DBSubscriber._on_fill error: %s", exc)

    def _on_position(self, event: Event) -> None:
        try:
            p: PositionPayload = event.payload
            snap = p.position
            row = {
                "ts":             _NOW(),
                "ticker":         p.ticker,
                "action":         p.action.value,
                "qty":            int(snap.qty)          if snap else None,
                "entry_price":    float(snap.entry_price) if snap else None,
                "current_price":  float(snap.current_price) if snap and hasattr(snap, "current_price") else None,
                "stop_price":     float(snap.stop_price)  if snap and snap.stop_price else None,
                "target_price":   float(snap.target_price) if snap and snap.target_price else None,
                "unrealised_pnl": float(p.pnl) if p.pnl is not None else None,
                "realised_pnl":   None,
                "event_id":       str(event.event_id),
                "correlation_id": _str_or_none(event.correlation_id),
                "ingested_at":    _NOW(),
            }
            self._writer.enqueue("position_events", row)
        except Exception as exc:
            log.debug("DBSubscriber._on_position error: %s", exc)

    def _on_risk_block(self, event: Event) -> None:
        try:
            p: RiskBlockPayload = event.payload
            row = {
                "ts":             _NOW(),
                "ticker":         p.ticker,
                "reason":         p.reason,
                "signal_action":  p.signal_action.value,
                "event_id":       str(event.event_id),
                "correlation_id": _str_or_none(event.correlation_id),
                "ingested_at":    _NOW(),
            }
            self._writer.enqueue("risk_block_events", row)
        except Exception as exc:
            log.debug("DBSubscriber._on_risk_block error: %s", exc)

    def _on_pop_signal(self, event: Event) -> None:
        try:
            p: PopSignalPayload = event.payload
            features = None
            if hasattr(p, "features_json") and p.features_json:
                features = json.loads(p.features_json) if isinstance(p.features_json, str) else p.features_json
            row = {
                "ts":                   _NOW(),
                "symbol":               p.symbol,
                "strategy_type":        p.strategy_type,
                "entry_price":          float(p.entry_price),
                "stop_price":           float(p.stop_price),
                "target_1":             float(p.target_1),
                "target_2":             float(p.target_2),
                "pop_reason":           p.pop_reason,
                "atr_value":            _safe_float(p.atr_value),
                "rvol":                 _safe_float(p.rvol),
                "vwap_distance":        _safe_float(p.vwap_distance),
                "strategy_confidence":  _safe_float(p.strategy_confidence),
                "features_json":        features,
                "event_id":             str(event.event_id),
                "correlation_id":       _str_or_none(event.correlation_id),
                "ingested_at":          _NOW(),
            }
            self._writer.enqueue("pop_signal_events", row)
        except Exception as exc:
            log.debug("DBSubscriber._on_pop_signal error: %s", exc)

    def _on_pro_strategy_signal(self, event: Event) -> None:
        try:
            p: ProStrategySignalPayload = event.payload
            det = None
            if hasattr(p, "detector_signals") and p.detector_signals:
                det = json.loads(p.detector_signals) if isinstance(p.detector_signals, str) else p.detector_signals
            row = {
                "ts":               _NOW(),
                "ticker":           p.ticker,
                "strategy_name":    p.strategy_name,
                "tier":             int(p.tier),
                "direction":        p.direction,
                "entry_price":      float(p.entry_price),
                "stop_price":       float(p.stop_price),
                "target_1":         float(p.target_1),
                "target_2":         float(p.target_2),
                "atr_value":        _safe_float(p.atr_value),
                "rvol":             _safe_float(p.rvol),
                "rsi_value":        _safe_float(p.rsi_value),
                "vwap":             _safe_float(p.vwap),
                "confidence":       _safe_float(p.confidence),
                "detector_signals": det,
                "event_id":         str(event.event_id),
                "correlation_id":   _str_or_none(event.correlation_id),
                "ingested_at":      _NOW(),
            }
            self._writer.enqueue("pro_strategy_signal_events", row)
        except Exception as exc:
            log.debug("DBSubscriber._on_pro_strategy_signal error: %s", exc)

    def _on_heartbeat(self, event: Event) -> None:
        try:
            p = event.payload
            row = {
                "ts":             _NOW(),
                "open_positions": getattr(p, "open_positions", None),
                "scan_count":     getattr(p, "scan_count", None),
                "event_id":       str(event.event_id),
                "ingested_at":    _NOW(),
            }
            self._writer.enqueue("heartbeat_events", row)
        except Exception as exc:
            log.debug("DBSubscriber._on_heartbeat error: %s", exc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _str_or_none(v: Any) -> str | None:
    return str(v) if v is not None else None
