"""
db/event_sourcing_subscriber.py — Event Sourcing Implementation

Replaces db/subscriber.py with immutable event store architecture.

Key principles:
1. All events written to event_store (immutable, append-only)
2. Complete timestamp trails (event_time, received_time, processed_time, persisted_time)
3. Projections built FROM events, never the other way around
4. Full audit trail with latency tracking
5. Schema-flexible via JSONB event_payload

This ensures:
- ✓ Complete traceability of every event
- ✓ Ability to replay events for debugging/recovery
- ✓ Temporal queries at any point in history
- ✓ Latency visibility (ingest, queue, processing)
- ✓ Compliance with financial audit standards
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from monitor.event_bus import Event, EventType
from monitor.events import (
    BarPayload, SignalPayload, OrderRequestPayload, FillPayload,
    PositionPayload, RiskBlockPayload, PopSignalPayload,
    ProStrategySignalPayload, OptionsSignalPayload,
)

from .writer import DBWriter

log = logging.getLogger(__name__)

# ── Event Type Mapping ──────────────────────────────────────────────────────
# Maps EventBus EventType to event_store event_type strings

EVENT_TYPE_MAP = {
    EventType.BAR:                  'BarReceived',
    EventType.SIGNAL:               'StrategySignal',
    EventType.ORDER_REQ:            'OrderRequested',
    EventType.FILL:                 'FillExecuted',
    EventType.POSITION:             'PositionChanged',  # Will map to PositionOpened/Closed in payload
    EventType.RISK_BLOCK:           'RiskBlocked',
    EventType.POP_SIGNAL:           'PopStrategySignal',
    EventType.PRO_STRATEGY_SIGNAL:  'ProStrategySignal',
    EventType.HEARTBEAT:            'HeartbeatEmitted',
    EventType.OPTIONS_SIGNAL:       'OptionsSignal',
    EventType.QUOTE:                'QuoteReceived',
}

_NOW = lambda: datetime.now(timezone.utc)

def _ensure_aware(dt):
    """Convert naive datetime to UTC-aware. Pass through if already aware."""
    if dt is None:
        return _NOW()
    if hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)


class EventSourcingSubscriber:
    """
    Wires all EventBus subscriptions to the event store.

    Every event is written as an immutable record with complete timestamp trails.
    This replaces the old approach of writing to individual tables.

    Parameters
    ----------
    bus      : EventBus — the shared monitor bus
    writer   : DBWriter — the async batch writer
    session_id : str — current monitor session ID (for tracing)

    Call register() before bus.start().
    """

    def __init__(self, bus: Any, writer: DBWriter, session_id: Optional[str] = None) -> None:
        self._bus       = bus
        self._writer    = writer
        self._session_id = session_id
        self._event_seq = 0  # Local sequence counter for ordering
        self._aggregate_versions: Dict[str, int] = {}  # aggregate_id → version counter

    def register(self) -> None:
        """Subscribe to all event types at LOW priority (priority=10)."""
        handlers = {
            EventType.BAR:                  self._on_bar,
            EventType.SIGNAL:               self._on_signal,
            EventType.ORDER_REQ:            self._on_order_req,
            EventType.FILL:                 self._on_fill,
            EventType.POSITION:             self._on_position,
            EventType.RISK_BLOCK:           self._on_risk_block,
            EventType.POP_SIGNAL:           self._on_pop_signal,
            EventType.PRO_STRATEGY_SIGNAL:  self._on_pro_strategy_signal,
            EventType.HEARTBEAT:            self._on_heartbeat,
            EventType.OPTIONS_SIGNAL:       self._on_options_signal,
            EventType.QUOTE:                self._on_quote,
        }
        for event_type, handler in handlers.items():
            self._bus.subscribe(event_type, handler, priority=10)
        log.info(
            "EventSourcingSubscriber registered %d event handlers (session=%s)",
            len(handlers), self._session_id
        )

    # ── Core Event Store Writer ────────────────────────────────────────────

    def _write_event(
        self,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        event_payload: Dict[str, Any],
        event: Event,
        source_system: str = "Unknown",
    ) -> None:
        """
        Write an immutable event to the event store.

        Parameters
        ----------
        event_type : str
            Type of event (e.g., 'PositionOpened', 'FillExecuted')
        aggregate_type : str
            What entity changed (e.g., 'Position', 'Trade')
        aggregate_id : str
            Which entity (e.g., 'position_ARM', 'trade_123')
        event_payload : dict
            Complete event data (flexible schema)
        event : Event
            The original EventBus event (for metadata)
        source_system : str
            Which system created this event
        """
        try:
            self._event_seq += 1

            # Increment aggregate version (tracks entity lifecycle)
            agg_ver = self._aggregate_versions.get(aggregate_id, 0) + 1
            self._aggregate_versions[aggregate_id] = agg_ver

            # Build the immutable event record
            row: Dict[str, Any] = {
                # Event Identity
                "event_id":           str(event.event_id),
                "event_sequence":     self._event_seq,
                "event_type":         event_type,
                "event_version":      1,

                # Complete Timestamp Trail (CRITICAL)
                # All timestamps must be timezone-aware (asyncpg/PG17 strict mode)
                "event_time":         _ensure_aware(event.timestamp),
                "received_time":      _NOW(),
                "queued_time":        None,
                "processed_time":     _NOW(),

                # Aggregate (entity that changed)
                "aggregate_id":       aggregate_id,
                "aggregate_type":     aggregate_type,
                "aggregate_version":  agg_ver,

                # Event Payload (complete, immutable data)
                "event_payload":      json.dumps(event_payload),

                # Event Context (linking)
                "correlation_id":     str(event.correlation_id) if event.correlation_id else None,
                "causation_id":       None,  # Could track "this fill caused this position"
                "parent_event_id":    None,

                # System Context
                "source_system":      source_system,
                "source_version":     "v5.1",  # Should come from config
                "session_id":         self._session_id,
            }

            # Enqueue for batch writing
            self._writer.enqueue("event_store", row)

        except Exception as exc:
            log.error("EventSourcingSubscriber._write_event error: %s", exc)

    # ── Event Handlers (one per EventBus event type) ────────────────────────

    def _on_bar(self, event: Event) -> None:
        """Bar price data received."""
        try:
            p: BarPayload = event.payload
            if p.df is None or p.df.empty:
                return

            last = p.df.iloc[-1]
            payload = {
                "ticker":   p.ticker,
                "open":     float(last.get("open", last.get("o", 0))),
                "high":     float(last.get("high", last.get("h", 0))),
                "low":      float(last.get("low", last.get("l", 0))),
                "close":    float(last.get("close", last.get("c", 0))),
                "volume":   int(last.get("volume", last.get("v", 0))),
                "vwap":     _safe_float(last.get("vwap")),
                "rvol":     _safe_float(getattr(p, "rvol_df", None)),
            }

            self._write_event(
                event_type="BarReceived",
                aggregate_type="Bar",
                aggregate_id=f"bar_{p.ticker}_{event.timestamp.isoformat()}",
                event_payload=payload,
                event=event,
                source_system="BarDataLoader",
            )
        except Exception as exc:
            log.debug("EventSourcingSubscriber._on_bar error: %s", exc)

    def _on_signal(self, event: Event) -> None:
        """Strategy signal generated."""
        try:
            p: SignalPayload = event.payload
            payload = {
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
            }

            self._write_event(
                event_type="StrategySignal",
                aggregate_type="Signal",
                aggregate_id=f"signal_{p.ticker}_{event.timestamp.isoformat()}",
                event_payload=payload,
                event=event,
                source_system="StrategyEngine",
            )
        except Exception as exc:
            log.debug("EventSourcingSubscriber._on_signal error: %s", exc)

    def _on_order_req(self, event: Event) -> None:
        """Order request created."""
        try:
            p: OrderRequestPayload = event.payload
            payload = {
                "ticker":         p.ticker,
                "side":           p.side.value,
                "qty":            int(p.qty),
                "price":          float(p.price),
                "reason":         p.reason,
                "stop_price":     _safe_float(p.stop_price),
                "target_price":   _safe_float(p.target_price),
                "atr_value":      _safe_float(p.atr_value),
            }

            self._write_event(
                event_type="OrderRequested",
                aggregate_type="Order",
                aggregate_id=f"order_{p.ticker}_{event.timestamp.isoformat()}",
                event_payload=payload,
                event=event,
                source_system="RiskEngine",
            )
        except Exception as exc:
            log.debug("EventSourcingSubscriber._on_order_req error: %s", exc)

    def _on_fill(self, event: Event) -> None:
        """Fill order executed."""
        try:
            p: FillPayload = event.payload
            payload = {
                "ticker":         p.ticker,
                "side":           p.side.value,
                "qty":            int(p.qty),
                "fill_price":     float(p.fill_price),
                "order_id":       p.order_id,
                "reason":         p.reason,
                "stop_price":     _safe_float(p.stop_price),
                "target_price":   _safe_float(p.target_price),
                "atr_value":      _safe_float(p.atr_value),
            }

            self._write_event(
                event_type="FillExecuted",
                aggregate_type="Fill",
                aggregate_id=f"fill_{p.ticker}_{p.order_id}",
                event_payload=payload,
                event=event,
                source_system="Broker",
            )
        except Exception as exc:
            log.debug("EventSourcingSubscriber._on_fill error: %s", exc)

    def _on_position(self, event: Event) -> None:
        """Position lifecycle event (opened/closed/partial exit)."""
        try:
            p: PositionPayload = event.payload
            snap = p.position

            # Map position action to event type
            action_to_event = {
                'OPENED':       'PositionOpened',
                'PARTIAL_EXIT': 'PartialExited',
                'CLOSED':       'PositionClosed',
            }

            event_type = action_to_event.get(p.action, 'PositionChanged')

            payload = {
                "ticker":         p.ticker,
                "action":         p.action,
                "qty":            int(snap.qty) if snap else None,
                "entry_price":    float(snap.entry_price) if snap else None,
                "entry_time":     snap.entry_time if snap else None,
                "current_price":  float(snap.current_price) if snap and hasattr(snap, "current_price") else None,
                "stop_price":     float(snap.stop_price) if snap and snap.stop_price else None,
                "target_price":   float(snap.target_price) if snap and snap.target_price else None,
                "half_target":    float(snap.half_target) if snap and snap.half_target else None,
                "pnl":            float(p.pnl) if p.pnl is not None else None,
            }

            self._write_event(
                event_type=event_type,
                aggregate_type="Position",
                aggregate_id=f"position_{p.ticker}",
                event_payload=payload,
                event=event,
                source_system="PositionManager",
            )
        except Exception as exc:
            log.debug("EventSourcingSubscriber._on_position error: %s", exc)

    def _on_risk_block(self, event: Event) -> None:
        """Risk adapter blocked a position."""
        try:
            p: RiskBlockPayload = event.payload
            payload = {
                "ticker":         p.ticker,
                "reason":         p.reason,
                "signal_action":  p.signal_action.value,
            }

            self._write_event(
                event_type="RiskBlocked",
                aggregate_type="Risk",
                aggregate_id=f"risk_{p.ticker}_{event.timestamp.isoformat()}",
                event_payload=payload,
                event=event,
                source_system="RiskAdapter",
            )
        except Exception as exc:
            log.debug("EventSourcingSubscriber._on_risk_block error: %s", exc)

    def _on_pop_signal(self, event: Event) -> None:
        """Pop strategy engine signal."""
        try:
            p: PopSignalPayload = event.payload
            features = None
            if hasattr(p, "features_json") and p.features_json:
                features = json.loads(p.features_json) if isinstance(p.features_json, str) else p.features_json

            payload = {
                "symbol":                p.symbol,
                "strategy_type":         p.strategy_type,
                "entry_price":           float(p.entry_price),
                "stop_price":            float(p.stop_price),
                "target_1":              float(p.target_1),
                "target_2":              float(p.target_2),
                "pop_reason":            p.pop_reason,
                "atr_value":             _safe_float(p.atr_value),
                "rvol":                  _safe_float(p.rvol),
                "vwap_distance":         _safe_float(p.vwap_distance),
                "strategy_confidence":   _safe_float(p.strategy_confidence),
                "features":              features,
            }

            self._write_event(
                event_type="PopStrategySignal",
                aggregate_type="Signal",
                aggregate_id=f"signal_pop_{p.symbol}_{event.timestamp.isoformat()}",
                event_payload=payload,
                event=event,
                source_system="PopStrategyEngine",
            )
        except Exception as exc:
            log.debug("EventSourcingSubscriber._on_pop_signal error: %s", exc)

    def _on_pro_strategy_signal(self, event: Event) -> None:
        """Pro setup engine signal."""
        try:
            p: ProStrategySignalPayload = event.payload
            det = {}
            if hasattr(p, "detector_signals") and p.detector_signals:
                if isinstance(p.detector_signals, str):
                    det = json.loads(p.detector_signals)
                elif isinstance(p.detector_signals, dict):
                    det = p.detector_signals

            payload = {
                "ticker":            p.ticker,
                "strategy_name":     p.strategy_name,
                "tier":              int(p.tier),
                "direction":         p.direction,
                "entry_price":       float(p.entry_price),
                "stop_price":        float(p.stop_price),
                "target_1":          float(p.target_1),
                "target_2":          float(p.target_2),
                "atr_value":         _safe_float(p.atr_value),
                "rvol":              _safe_float(p.rvol),
                "rsi_value":         _safe_float(p.rsi_value),
                "vwap":              _safe_float(p.vwap),
                "confidence":        _safe_float(p.confidence),
                "detector_signals":  det,
            }

            self._write_event(
                event_type="ProStrategySignal",
                aggregate_type="Signal",
                aggregate_id=f"signal_pro_{p.ticker}_{event.timestamp.isoformat()}",
                event_payload=payload,
                event=event,
                source_system="ProSetupEngine",
            )
        except Exception as exc:
            log.debug("EventSourcingSubscriber._on_pro_strategy_signal error: %s", exc)

    def _on_heartbeat(self, event: Event) -> None:
        """Heartbeat emitted (system health check)."""
        try:
            p = event.payload
            payload = {
                "open_positions": getattr(p, "open_positions", None),
                "scan_count":     getattr(p, "scan_count", None),
                "trades":         getattr(p, "n_trades", None),
                "wins":           getattr(p, "n_wins", None),
                "pnl":            getattr(p, "total_pnl", None),
            }

            self._write_event(
                event_type="HeartbeatEmitted",
                aggregate_type="Heartbeat",
                aggregate_id=f"heartbeat_{event.timestamp.isoformat()}",
                event_payload=payload,
                event=event,
                source_system="Monitor",
            )
        except Exception as exc:
            log.debug("EventSourcingSubscriber._on_heartbeat error: %s", exc)


# ── Helpers ────────────────────────────────────────────────────────────────

    def _on_options_signal(self, event: Event) -> None:
        """Options engine signal — captures full strategy spec for ML analysis."""
        try:
            p: OptionsSignalPayload = event.payload
            legs = None
            if hasattr(p, "legs_json") and p.legs_json:
                legs = json.loads(p.legs_json) if isinstance(p.legs_json, str) else p.legs_json

            payload = {
                "ticker":            p.ticker,
                "strategy_type":     str(p.strategy_type),
                "underlying_price":  float(p.underlying_price),
                "expiry_date":       p.expiry_date,
                "net_debit":         float(p.net_debit),
                "max_risk":          float(p.max_risk),
                "max_reward":        float(p.max_reward),
                "atr_value":         _safe_float(p.atr_value),
                "rvol":              _safe_float(p.rvol),
                "rsi_value":         _safe_float(p.rsi_value),
                "legs":              legs,
                "source":            p.source,
            }

            self._write_event(
                event_type="OptionsSignal",
                aggregate_type="Signal",
                aggregate_id=f"signal_options_{p.ticker}_{event.timestamp.isoformat()}",
                event_payload=payload,
                event=event,
                source_system="OptionsEngine",
            )
        except Exception as exc:
            log.debug("EventSourcingSubscriber._on_options_signal error: %s", exc)

    def _on_quote(self, event: Event) -> None:
        """Quote received — bid/ask data critical for slippage analysis."""
        try:
            p = event.payload
            payload = {
                "ticker":      getattr(p, "ticker", None),
                "bid":         _safe_float(getattr(p, "bid", None)),
                "ask":         _safe_float(getattr(p, "ask", None)),
                "spread_pct":  _safe_float(getattr(p, "spread_pct", None)),
            }

            self._write_event(
                event_type="QuoteReceived",
                aggregate_type="Quote",
                aggregate_id=f"quote_{payload['ticker']}_{event.timestamp.isoformat()}",
                event_payload=payload,
                event=event,
                source_system="RiskEngine",
            )
        except Exception as exc:
            log.debug("EventSourcingSubscriber._on_quote error: %s", exc)


def _safe_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
