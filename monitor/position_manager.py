"""
T5 — Position Manager
======================
Manages open positions: opens them on fill, applies partial exits and
trailing stops, closes them on full exit, and persists state after every
mutation.

This component is the single writer for `positions`, `reclaimed_today`,
`last_order_time`, and `trade_log`.  All other components hold read-only
references to these dicts/lists.

Events consumed
---------------
  EventType.FILL   — open a position on buy fill; close/partially-close on
                     sell fill.

Events emitted
--------------
  EventType.POSITION — payload: PositionPayload
      action='opened'       after a buy fill
      action='partial_exit' after a partial sell fill
      action='closed'       after a final sell fill

Persistence
-----------
  `save_state()` is called after every position mutation so that a restart
  can recover intraday state from `bot_state.json`.

Usage
-----
    pm = PositionManager(
        bus=bus,
        positions=positions,           # shared mutable dict
        reclaimed_today=reclaimed,     # shared mutable set
        last_order_time=order_times,   # shared mutable dict
        trade_log=trade_log,           # shared mutable list
        alert_email=email,
    )
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from .alerts import send_alert
from .event_bus import Event, EventBus, EventType
from .events import FillPayload, PositionPayload, PositionSnapshot
from .state import save_state

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)


class PositionManager:
    """
    Owns all position lifecycle logic.

    The shared dicts (`positions`, `reclaimed_today`, `last_order_time`,
    `trade_log`) are mutated in-place here; RiskEngine and StrategyEngine
    observe the same objects via their own references.
    """

    def __init__(
        self,
        bus: EventBus,
        positions: dict,
        reclaimed_today: Set[str],
        last_order_time: Dict[str, float],
        trade_log: List[dict],
        alert_email: Optional[str] = None,
    ):
        self._bus             = bus
        self._positions       = positions
        self._reclaimed_today = reclaimed_today
        self._last_order_time = last_order_time
        self._trade_log       = trade_log
        self._alert_email     = alert_email
        self._lock            = threading.Lock()

        bus.subscribe(EventType.FILL, self._on_fill)

    # ── Handler ───────────────────────────────────────────────────────────────

    def _on_fill(self, event: Event) -> None:
        # Dedup: skip if we've already processed this FILL event
        if not hasattr(self, '_processed_fill_ids'):
            self._processed_fill_ids = set()
        if event.event_id in self._processed_fill_ids:
            log.debug("[PositionManager] Duplicate FILL skipped: %s", event.event_id)
            return
        self._processed_fill_ids.add(event.event_id)
        # Prevent unbounded growth
        if len(self._processed_fill_ids) > 10000:
            self._processed_fill_ids = set(list(self._processed_fill_ids)[-5000:])

        p: FillPayload = event.payload
        with self._lock:
            if p.side == 'BUY':
                self._open_position(p, event)
            else:
                self._close_position(p, event)

    # ── Open ──────────────────────────────────────────────────────────────────

    def _open_position(self, p: FillPayload, parent: Event) -> None:
        ticker     = p.ticker
        fill_price = p.fill_price
        qty        = p.qty
        now        = datetime.now(ET)

        # Reconstruct stop / target from reason string if available.
        # These values are computed by RiskEngine and stored in position on open.
        # For now we store a position dict with placeholders; the StrategyEngine
        # will have already emitted the SIGNAL with stop/target which the caller
        # (RiskEngine) embeds in the OrderRequest reason.  A richer wiring (T8)
        # will pass these via the correlation chain; for now we write them as None
        # and let the next BAR event's exit check use the real values from the
        # SignalPayload that preceded this fill (stored on the position dict by
        # the wire-up layer in monitor.py).
        #
        # The position dict written here is the authoritative record; monitor.py
        # will backfill stop/target on the same tick if it is still orchestrating.
        import time as _time
        self._last_order_time[ticker] = _time.time()
        self._reclaimed_today.add(ticker)

        # Use FillPayload's stop/target when present (Pro/Pop signals carry these).
        # Fall back to percentage-based placeholders only if not provided.
        has_stop = p.stop_price and p.stop_price > 0
        has_target = p.target_price and p.target_price > 0

        if has_stop:
            stop_price = p.stop_price
        else:
            stop_price = fill_price * 0.995  # 0.5% fallback

        if has_target:
            target_price = p.target_price
            half_target = (fill_price + target_price) / 2
        else:
            target_price = fill_price * 1.01  # 1% fallback
            half_target = fill_price * 1.005

        # Extract strategy origin from reason (e.g., "pro:sr_flip:T1:long" → "pro:sr_flip")
        reason_str = str(p.reason or '')
        strategy = reason_str.split(':')[0] + ':' + reason_str.split(':')[1] if ':' in reason_str else 'vwap_reclaim'

        pos = {
            'entry_price':  fill_price,
            'entry_time':   now.strftime('%H:%M:%S'),
            'quantity':     qty,
            'partial_done': False,
            'order_id':     p.order_id,
            'stop_price':   stop_price,
            'target_price': target_price,
            'half_target':  half_target,
            'atr_value':    getattr(p, 'atr_value', None),
            'strategy':     strategy,
        }
        self._positions[ticker] = pos

        # Extract engine/strategy/broker for rich alerts
        _broker = getattr(parent, '_routed_broker', 'unknown') if parent else 'unknown'
        _engine = strategy.split(':')[0] if ':' in strategy else 'core'
        _strat = strategy.split(':')[1] if ':' in strategy else strategy

        send_alert(
            self._alert_email,
            f"POSITION OPENED: {ticker} {qty} shares @ ${fill_price:.2f}\n"
            f"  Engine: {_engine.upper()} | Strategy: {_strat} | Broker: {_broker}\n"
            f"  Stop: ${pos['stop_price']:.2f} | Target: ${pos['target_price']:.2f}\n"
            f"  Order: {p.order_id} | Reason: {p.reason}"
        )
        log.info(
            f"[PositionManager] Opened {ticker}: qty={qty} entry=${fill_price:.2f} "
            f"order={p.order_id}"
        )

        pos_event = Event(
            type=EventType.POSITION,
            payload=PositionPayload(
                ticker=ticker,
                action='OPENED',
                position=PositionSnapshot(
                    entry_price=pos['entry_price'],
                    entry_time=pos['entry_time'],
                    quantity=pos['quantity'],
                    partial_done=pos['partial_done'],
                    order_id=pos['order_id'],
                    stop_price=pos['stop_price'],
                    target_price=pos['target_price'],
                    half_target=pos['half_target'],
                    atr_value=pos.get('atr_value'),
                ),
            ),
            correlation_id=parent.event_id,
        )
        # Propagate broker tag from the FILL event
        pos_event._routed_broker = getattr(parent, '_routed_broker', None)
        self._bus.emit(pos_event)
        self._persist()

    # ── Close / partial ───────────────────────────────────────────────────────

    def _close_position(self, p: FillPayload, parent: Event) -> None:
        ticker     = p.ticker
        fill_price = p.fill_price
        qty        = p.qty
        reason     = p.reason
        now        = datetime.now(ET)

        if ticker not in self._positions:
            log.warning(
                f"[PositionManager] SELL fill for {ticker} but no open position "
                f"(may have already been closed). order={p.order_id}"
            )
            return

        pos = self._positions[ticker]
        entry_price = pos['entry_price']
        pos_strategy = pos.get('strategy', 'unknown')
        pnl         = round((fill_price - entry_price) * qty, 2)

        trade = {
            'ticker':      ticker,
            'entry_price': entry_price,
            'entry_time':  pos.get('entry_time', ''),
            'exit_price':  fill_price,
            'qty':         qty,
            'pnl':         pnl,
            'reason':      reason,
            'time':        now.strftime('%H:%M:%S'),
            'is_win':      pnl >= 0,
        }

        # Partial exit: qty sold is less than total position
        remaining = pos['quantity'] - qty
        if remaining > 0 and reason == 'PARTIAL_SELL':
            pos['quantity']     = remaining
            pos['partial_done'] = True
            # Move stop to breakeven only if it's currently below entry (don't lower a trailing stop)
            pos['stop_price']   = max(pos.get('stop_price', 0), entry_price)

            self._trade_log.append(trade)
            _broker = getattr(parent, '_routed_broker', 'unknown') if parent else 'unknown'
            _strategy = pos.get('strategy', 'unknown')
            _engine = _strategy.split(':')[0] if ':' in _strategy else 'core'

            send_alert(
                self._alert_email,
                f"PARTIAL EXIT: {ticker} sold {qty} @ ${fill_price:.2f} PnL ${pnl:+.2f}\n"
                f"  Engine: {_engine.upper()} | Strategy: {_strategy} | Broker: {_broker}\n"
                f"  Remaining: {remaining} shares | Stop: breakeven ${entry_price:.2f}"
            )
            log.info(
                f"[PositionManager] Partial exit {ticker}: sold {qty}, "
                f"remaining={remaining} pnl=${pnl:+.2f}"
            )

            partial_event = Event(
                type=EventType.POSITION,
                payload=PositionPayload(
                    ticker=ticker,
                    action='PARTIAL_EXIT',
                    position=PositionSnapshot(
                        entry_price=pos['entry_price'],
                        entry_time=pos.get('entry_time', ''),
                        quantity=pos['quantity'],
                        partial_done=pos['partial_done'],
                        order_id=pos.get('order_id', ''),
                        stop_price=pos['stop_price'],
                        target_price=pos['target_price'],
                        half_target=pos['half_target'],
                        atr_value=pos.get('atr_value'),
                    ),
                    pnl=pnl,
                ),
                correlation_id=parent.event_id,
            )
            partial_event._routed_broker = getattr(parent, '_routed_broker', None)
            self._bus.emit(partial_event)
            self._persist()
            return

        # Full close
        del self._positions[ticker]
        import time as _time
        self._last_order_time[ticker] = _time.time()

        # V7: Registry release handled by RegistryGate on POSITION CLOSED event
        # (centralized — only Core writes to registry)

        self._trade_log.append(trade)
        _broker = getattr(parent, '_routed_broker', 'unknown') if parent else 'unknown'
        _strategy = trade.get('strategy', pos_strategy) if 'strategy' in trade else pos_strategy
        _engine = _strategy.split(':')[0] if ':' in _strategy else 'core'
        _win = 'WIN' if pnl >= 0 else 'LOSS'

        send_alert(
            self._alert_email,
            f"POSITION CLOSED ({_win}): {ticker} {qty} shares @ ${fill_price:.2f}\n"
            f"  Engine: {_engine.upper()} | Strategy: {_strategy} | Broker: {_broker}\n"
            f"  Entry: ${entry_price:.2f} → Exit: ${fill_price:.2f} | PnL: ${pnl:+.2f}\n"
            f"  Reason: {reason}"
        )
        log.info(
            f"[PositionManager] Closed {ticker}: qty={qty} exit=${fill_price:.2f} "
            f"pnl=${pnl:+.2f} reason={reason}"
        )

        broker = getattr(parent, '_routed_broker', None) or 'unknown'
        close_event = Event(
            type=EventType.POSITION,
            payload=PositionPayload(
                ticker=ticker,
                action='CLOSED',
                position=None,
                pnl=pnl,
                close_detail={
                    'qty': qty,
                    'entry_price': entry_price,
                    'entry_time': pos.get('entry_time', ''),
                    'exit_price': fill_price,
                    'reason': reason,
                    'strategy': pos.get('strategy', 'vwap_reclaim'),
                    'broker': broker,
                },
            ),
            correlation_id=parent.event_id,
        )
        close_event._routed_broker = broker
        self._bus.emit(close_event)
        self._persist()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist(self) -> None:
        try:
            save_state(self._positions, self._reclaimed_today, self._trade_log)
        except Exception as e:
            log.error(f"[PositionManager] persist failed: {e}")
