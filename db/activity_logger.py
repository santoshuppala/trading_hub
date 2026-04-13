"""
db/activity_logger.py — Log every trading event to TimescaleDB.

Replaces CSV-based signal instrumentation with proper database persistence.
Subscribes to all trading-relevant EventTypes at lowest priority (99)
and writes to trading.activity_log and trading.signal_analysis tables.

Also provides methods for:
  - System health snapshots (heartbeat data → system_health_log)
  - Pre-flight check logging (→ preflight_log)
  - Kill switch activation logging (→ kill_switch_log)

Usage
-----
    from db.activity_logger import ActivityLogger

    logger = ActivityLogger(bus=monitor._bus, writer=db_writer)
    logger.register()  # subscribes to all event types

    # In heartbeat loop:
    logger.log_health(tickers=169, positions=3, pnl=-45.0, ...)

    # In preflight:
    logger.log_preflight("tradier", "ok", 340, "status 200")

    # On kill switch:
    logger.log_kill_switch(pnl=-510, threshold=-500, positions_closed=["AAPL","NVDA"])
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from monitor.event_bus import Event, EventBus, EventType

log = logging.getLogger(__name__)


class ActivityLogger:
    """
    Logs every trading event to TimescaleDB activity_log and signal_analysis.
    """

    def __init__(self, bus: EventBus, writer: Any) -> None:
        self._bus = bus
        self._writer = writer

    def register(self) -> None:
        """Subscribe to all trading-relevant events at lowest priority."""
        event_types = [
            EventType.SIGNAL, EventType.ORDER_REQ, EventType.FILL,
            EventType.ORDER_FAIL, EventType.POSITION, EventType.RISK_BLOCK,
            EventType.POP_SIGNAL, EventType.PRO_STRATEGY_SIGNAL,
            EventType.HEARTBEAT,
        ]
        for et in event_types:
            self._bus.subscribe(et, self._on_event, priority=99)
        log.info("ActivityLogger registered %d event handlers → TimescaleDB", len(event_types))

    # ── Core event handler ───────────────────────────────────────────────────

    def _on_event(self, event: Event) -> None:
        """Route every event to activity_log + signal_analysis as appropriate."""
        try:
            p = event.payload
            et = event.type.name
            ticker = getattr(p, 'ticker', getattr(p, 'symbol', None))

            # ── activity_log (every event) ────────────────────────────────────
            row = {
                'ts':             datetime.now(timezone.utc),
                'event_type':     et,
                'ticker':         ticker,
                'action':         _str_attr(p, 'action', 'side'),
                'direction':      _str_attr(p, 'direction'),
                'price':          _float_attr(p, 'current_price', 'fill_price', 'entry_price', 'price'),
                'stop_price':     _float_attr(p, 'stop_price'),
                'target_price':   _float_attr(p, 'target_price', 'target_2'),
                'qty':            _int_attr(p, 'qty'),
                'reason':         _str_attr(p, 'reason', 'pop_reason'),
                'strategy':       _str_attr(p, 'strategy_name', 'strategy_type'),
                'tier':           _int_attr(p, 'tier'),
                'atr_value':      _float_attr(p, 'atr_value'),
                'rsi_value':      _float_attr(p, 'rsi_value'),
                'rvol':           _float_attr(p, 'rvol'),
                'vwap':           _float_attr(p, 'vwap'),
                'confidence':     _float_attr(p, 'confidence', 'strategy_confidence'),
                'event_id':       str(event.event_id) if event.event_id else None,
                'correlation_id': str(event.correlation_id) if event.correlation_id else None,
                'stream_seq':     event.stream_seq,
                'layer':          _infer_layer(et, p),
                'outcome':        _infer_outcome(et, p),
                'metadata':       None,
                'ingested_at':    datetime.now(timezone.utc),
            }
            self._writer.enqueue('activity_log', row)

            # ── signal_analysis (SIGNAL, POP_SIGNAL, PRO_STRATEGY_SIGNAL) ─────
            if et in ('SIGNAL', 'POP_SIGNAL', 'PRO_STRATEGY_SIGNAL'):
                self._log_signal_analysis(et, p, event)

            # ── signal_analysis update on RISK_BLOCK ──────────────────────────
            if et == 'RISK_BLOCK':
                self._log_block(p, event)

        except Exception as exc:
            log.debug("ActivityLogger._on_event error: %s", exc)

    # ── Signal analysis ──────────────────────────────────────────────────────

    def _log_signal_analysis(self, et: str, p: Any, event: Event) -> None:
        entry = _float_attr(p, 'current_price', 'entry_price')
        stop = _float_attr(p, 'stop_price')
        t1 = _float_attr(p, 'half_target', 'target_1')
        t2 = _float_attr(p, 'target_price', 'target_2')

        risk = (entry - stop) if (entry and stop and entry > stop) else None
        reward = (t2 - entry) if (t2 and entry and t2 > entry) else None
        rr = round(reward / risk, 2) if (risk and reward and risk > 0) else None

        vwap_val = _float_attr(p, 'vwap')
        vwap_dist = None
        if vwap_val and entry and vwap_val > 0:
            vwap_dist = round((entry - vwap_val) / vwap_val, 6)

        detectors = None
        if hasattr(p, 'detector_signals') and p.detector_signals:
            det = p.detector_signals
            detectors = json.loads(det) if isinstance(det, str) else det

        features = None
        if hasattr(p, 'features_json') and p.features_json:
            feat = p.features_json
            features = json.loads(feat) if isinstance(feat, str) else feat

        row = {
            'ts':                 datetime.now(timezone.utc),
            'ticker':             getattr(p, 'ticker', getattr(p, 'symbol', None)),
            'signal_type':        et,
            'action':             _str_attr(p, 'action'),
            'strategy':           _str_attr(p, 'strategy_name', 'strategy_type'),
            'tier':               _int_attr(p, 'tier'),
            'direction':          _str_attr(p, 'direction'),
            'entry_price':        entry,
            'stop_price':         stop,
            'target_1':           t1,
            'target_2':           t2,
            'risk_per_share':     round(risk, 4) if risk else None,
            'reward_per_share':   round(reward, 4) if reward else None,
            'rr_ratio':           rr,
            'atr_value':          _float_attr(p, 'atr_value'),
            'rsi_value':          _float_attr(p, 'rsi_value'),
            'rvol':               _float_attr(p, 'rvol'),
            'vwap':               vwap_val,
            'vwap_distance_pct':  vwap_dist,
            'confidence':         _float_attr(p, 'confidence', 'strategy_confidence'),
            'pop_reason':         _str_attr(p, 'pop_reason'),
            'detectors_fired':    detectors,
            'features_json':      features,
            'was_executed':       False,
            'was_blocked':        False,
            'block_reason':       None,
            'fill_price':         None,
            'fill_slippage_pct':  None,
            'event_id':           str(event.event_id) if event.event_id else None,
            'session_id':         None,
            'ingested_at':        datetime.now(timezone.utc),
        }
        self._writer.enqueue('signal_analysis', row)

    def _log_block(self, p: Any, event: Event) -> None:
        row = {
            'ts':                 datetime.now(timezone.utc),
            'ticker':             getattr(p, 'ticker', None),
            'signal_type':        'RISK_BLOCK',
            'action':             _str_attr(p, 'signal_action'),
            'strategy':           None,
            'tier':               None,
            'direction':          None,
            'entry_price':        None,
            'stop_price':         None,
            'target_1':           None,
            'target_2':           None,
            'risk_per_share':     None,
            'reward_per_share':   None,
            'rr_ratio':           None,
            'atr_value':          None,
            'rsi_value':          None,
            'rvol':               None,
            'vwap':               None,
            'vwap_distance_pct':  None,
            'confidence':         None,
            'pop_reason':         None,
            'detectors_fired':    None,
            'features_json':      None,
            'was_executed':       False,
            'was_blocked':        True,
            'block_reason':       getattr(p, 'reason', None),
            'fill_price':         None,
            'fill_slippage_pct':  None,
            'event_id':           str(event.event_id) if event.event_id else None,
            'session_id':         None,
            'ingested_at':        datetime.now(timezone.utc),
        }
        self._writer.enqueue('signal_analysis', row)

    # ── System health logging ────────────────────────────────────────────────

    def log_health(
        self,
        tickers_scanned: int = 0,
        open_positions: int = 0,
        trades_today: int = 0,
        wins_today: int = 0,
        losses_today: int = 0,
        daily_pnl: float = 0.0,
        system_pressure: float = 0.0,
        db_rows_written: int = 0,
        db_rows_dropped: int = 0,
        db_batches_flushed: int = 0,
        registry_count: int = 0,
        kill_switch_active: bool = False,
        queue_depths: Optional[Dict] = None,
        handler_avg_ms: Optional[Dict] = None,
    ) -> None:
        import os
        try:
            import psutil
            memory_mb = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
        except Exception:
            memory_mb = None

        row = {
            'ts':                   datetime.now(timezone.utc),
            'tickers_scanned':      tickers_scanned,
            'open_positions':       open_positions,
            'trades_today':         trades_today,
            'wins_today':           wins_today,
            'losses_today':         losses_today,
            'daily_pnl':            round(daily_pnl, 2),
            'system_pressure':      round(system_pressure, 4),
            'db_rows_written':      db_rows_written,
            'db_rows_dropped':      db_rows_dropped,
            'db_batches_flushed':   db_batches_flushed,
            'registry_count':       registry_count,
            'kill_switch_active':   kill_switch_active,
            'queue_depths':         json.dumps(queue_depths) if queue_depths else None,
            'handler_avg_ms':       json.dumps(handler_avg_ms) if handler_avg_ms else None,
            'memory_mb':            round(memory_mb, 1) if memory_mb else None,
            'ingested_at':          datetime.now(timezone.utc),
        }
        self._writer.enqueue('system_health_log', row)

    # ── Preflight logging ────────────────────────────────────────────────────

    def log_preflight(
        self, check_name: str, status: str,
        response_ms: int = 0, detail: str = '',
        session_id: Optional[str] = None,
    ) -> None:
        row = {
            'ts':           datetime.now(timezone.utc),
            'check_name':   check_name,
            'status':       status,
            'response_ms':  response_ms,
            'detail':       detail,
            'session_id':   session_id,
        }
        self._writer.enqueue('preflight_log', row)

    # ── Kill switch logging ──────────────────────────────────────────────────

    def log_kill_switch(
        self,
        daily_pnl: float,
        threshold: float,
        trades_count: int = 0,
        wins: int = 0,
        losses: int = 0,
        positions_closed: Optional[List[str]] = None,
        session_id: Optional[str] = None,
    ) -> None:
        row = {
            'ts':               datetime.now(timezone.utc),
            'daily_pnl':        round(daily_pnl, 2),
            'threshold':        round(threshold, 2),
            'trades_count':     trades_count,
            'wins':             wins,
            'losses':           losses,
            'positions_closed': positions_closed or [],
            'session_id':       session_id,
        }
        self._writer.enqueue('kill_switch_log', row)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _str_attr(obj: Any, *names: str) -> Optional[str]:
    for name in names:
        val = getattr(obj, name, None)
        if val is not None:
            return str(val)
    return None

def _float_attr(obj: Any, *names: str) -> Optional[float]:
    for name in names:
        val = getattr(obj, name, None)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None

def _int_attr(obj: Any, *names: str) -> Optional[int]:
    for name in names:
        val = getattr(obj, name, None)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return None

def _infer_layer(event_type: str, payload: Any) -> Optional[str]:
    reason = getattr(payload, 'reason', '') or ''
    if 'pro:' in reason:
        return 'T3.6'
    if 'pop:' in reason:
        return 'T3.5'
    if event_type == 'POP_SIGNAL':
        return 'T3.5'
    if event_type == 'PRO_STRATEGY_SIGNAL':
        return 'T3.6'
    if event_type in ('SIGNAL', 'RISK_BLOCK'):
        return 'T4'
    return None

def _infer_outcome(event_type: str, payload: Any) -> Optional[str]:
    if event_type == 'FILL':
        return 'filled'
    if event_type == 'ORDER_FAIL':
        return 'failed'
    if event_type == 'RISK_BLOCK':
        return 'blocked'
    if event_type == 'ORDER_REQ':
        return 'approved'
    if event_type in ('SIGNAL', 'POP_SIGNAL', 'PRO_STRATEGY_SIGNAL'):
        return 'signal'
    if event_type == 'POSITION':
        action = getattr(payload, 'action', '')
        return f'position_{action}'
    return None
