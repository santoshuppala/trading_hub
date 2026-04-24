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
from .edge_context import SignalContextCache
from .event_bus import Event, EventBus, EventType
from .events import FillPayload, PositionPayload, PositionSnapshot
from .fill_lot import FillLot, PositionMeta, QTY_EPSILON
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
        fill_ledger=None,
    ):
        self._bus             = bus
        self._positions       = positions
        self._reclaimed_today = reclaimed_today
        self._last_order_time = last_order_time
        self._trade_log       = trade_log
        self._alert_email     = alert_email
        self._lock            = threading.Lock()

        # V9: FillLedger shadow mode — when set, all fills are also
        # appended to the ledger for parallel tracking and validation.
        self._ledger = fill_ledger

        # V10: Cache edge context from signals for propagation to FillLot.
        # SIGNAL/PRO_STRATEGY_SIGNAL carry regime, time_bucket, etc. that
        # the broker doesn't know about. Cache by event_id so we can look
        # up context when the FILL arrives (linked via correlation_id chain).
        self._signal_ctx = SignalContextCache(max_size=1000)

        bus.subscribe(EventType.FILL, self._on_fill)
        # V8: Clean up phantom positions when SELL fails with "no position"
        bus.subscribe(EventType.ORDER_FAIL, self._on_order_fail, priority=0)
        # V10: Cache edge context from signal events
        bus.subscribe(EventType.SIGNAL, self._cache_signal_context, priority=99)
        bus.subscribe(EventType.PRO_STRATEGY_SIGNAL, self._cache_pro_signal_context, priority=99)
        # V10: Forward context through ORDER_REQ hop (SIGNAL→ORDER_REQ→FILL chain)
        bus.subscribe(EventType.ORDER_REQ, self._forward_signal_context, priority=99)

    # ── V10: Edge context caching ────────────────────────────────────────────

    def _cache_signal_context(self, event: Event) -> None:
        """Cache edge context from VWAP SIGNAL events."""
        try:
            p = event.payload
            if str(getattr(p, 'action', '')).upper() not in ('BUY',):
                return  # only cache BUY signals
            self._signal_ctx.store(event.event_id, {
                'timeframe':        getattr(p, 'timeframe', '1min'),
                'regime_at_entry':  getattr(p, 'regime_at_entry', ''),
                'time_bucket':      getattr(p, 'time_bucket', ''),
                'confidence':       getattr(p, 'confidence', 0.0),
                'confluence_score': getattr(p, 'confluence_score', 0.0),
                'tier':             getattr(p, 'tier', 0),
            })
        except Exception:
            pass

    def _cache_pro_signal_context(self, event: Event) -> None:
        """Cache edge context from PRO_STRATEGY_SIGNAL events."""
        try:
            p = event.payload
            self._signal_ctx.store(event.event_id, {
                'timeframe':        getattr(p, 'timeframe', '1min'),
                'regime_at_entry':  getattr(p, 'regime_at_entry', ''),
                'time_bucket':      getattr(p, 'time_bucket', ''),
                'confidence':       getattr(p, 'confidence', 0.0),
                'confluence_score': getattr(p, 'confluence_score', 0.0),
                'tier':             getattr(p, 'tier', 0),
            })
        except Exception:
            pass

    def _forward_signal_context(self, event: Event) -> None:
        """Forward cached edge context from SIGNAL→ORDER_REQ hop.

        Chain: SIGNAL(eid=A) → ORDER_REQ(corr=A, eid=B) → FILL(corr=B)
        We cached at A. Now also cache at B so FILL lookup works.
        """
        try:
            ctx = self._signal_ctx.peek(event.correlation_id)
            if ctx:
                self._signal_ctx.store(event.event_id, ctx)
        except Exception:
            pass

    # ── Phantom cleanup ──────────────────────────────────────────────────────

    def _on_order_fail(self, event: Event) -> None:
        """V8: Remove phantom positions when broker confirms no position exists.

        When a bracket stop fires at the broker, Core doesn't know until
        it tries to exit. The SELL fails with 'no position at alpaca/tradier'.
        Without cleanup, StrategyEngine retries every 60s → sell storm.
        """
        try:
            p = event.payload
            side_str = str(getattr(p, 'side', '')).upper()
            reason_str = str(getattr(p, 'reason', '')).lower()
            ticker = p.ticker

            if side_str != 'SELL':
                return
            if 'no position' not in reason_str:
                return

            with self._lock:
                if ticker in self._positions:
                    pos = self._positions[ticker]
                    pos_strategy = pos.get('strategy', 'unknown')
                    log.warning(
                        "[PositionManager] Removing phantom %s — broker confirmed "
                        "no position (strategy=%s). Bracket/stop likely fired externally.",
                        ticker, pos_strategy)
                    del self._positions[ticker]
                    self._persist()

                    # Emit POSITION CLOSED so other components clean up
                    self._bus.emit(Event(
                        type=EventType.POSITION,
                        payload=PositionPayload(
                            ticker=ticker,
                            action='CLOSED',
                            position=None,
                            pnl=0.0,
                            close_detail={
                                'reason': 'phantom_cleanup',
                                'strategy': pos_strategy,
                                'broker': pos.get('_broker', 'unknown'),
                            },
                        ),
                    ))

                    # V9: Shadow FillLedger — synthetic SELL for phantom
                    if self._ledger is not None:
                        try:
                            import uuid as _uuid
                            qty = float(pos.get('quantity', 0))
                            if qty > QTY_EPSILON:
                                # Use entry_price as estimated fill (E1: never $0)
                                est_price = pos.get('entry_price', 0)
                                phantom_lot = FillLot(
                                    lot_id=str(_uuid.uuid4()),
                                    ticker=ticker, side='SELL', qty=qty,
                                    fill_price=est_price,
                                    timestamp=datetime.now(ET),
                                    order_id='phantom_cleanup',
                                    broker=pos.get('_broker', 'unknown'),
                                    strategy=pos_strategy,
                                    reason='phantom_cleanup_broker_stop',
                                    synthetic=True,
                                )
                                self._ledger.append(phantom_lot)
                        except Exception as le:
                            log.warning("[PositionManager] Shadow phantom SELL failed: %s", le)

        except Exception as exc:
            log.warning("[PositionManager] _on_order_fail error: %s", exc)

    # ── Fill Handler ─────────────────────────────────────────────────────────

    def _on_fill(self, event: Event) -> None:
        # V9 (R7): Dedup with OrderedDict + TTL (was: buggy set truncation)
        # OrderedDict preserves insertion order, so oldest entries evicted first.
        # 1-hour TTL instead of count-based cap — handles long sessions correctly.
        import time as _time
        from collections import OrderedDict
        if not hasattr(self, '_processed_fill_ids'):
            self._processed_fill_ids = OrderedDict()
        if event.event_id in self._processed_fill_ids:
            log.debug("[PositionManager] Duplicate FILL skipped: %s", event.event_id)
            return
        self._processed_fill_ids[event.event_id] = _time.monotonic()
        # Evict entries older than 1 hour
        _cutoff = _time.monotonic() - 3600
        while self._processed_fill_ids:
            _oldest_id, _oldest_time = next(iter(self._processed_fill_ids.items()))
            if _oldest_time < _cutoff:
                del self._processed_fill_ids[_oldest_id]
            else:
                break

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

        # V8: Guard against duplicate BUY fills overwriting existing position
        if ticker in self._positions:
            existing = self._positions[ticker]
            log.error(
                "[PositionManager] DUPLICATE BUY for %s — already open "
                "(qty=%d entry=$%.2f strategy=%s). Keeping existing, ignoring fill.",
                ticker, existing.get('quantity', 0),
                existing.get('entry_price', 0),
                existing.get('strategy', 'unknown'))
            return

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

        import time as _time_mod
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
            '_opened_mono': _time_mod.monotonic(),  # V9: grace window for QUOTE exits
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
                # V7.2: Include strategy/broker so DB event_store has
                # full position data for crash recovery (not just CLOSED).
                close_detail={
                    'strategy': strategy,
                    'broker': _broker,
                    'reason': str(p.reason or ''),
                },
            ),
            correlation_id=parent.event_id,
        )
        # Propagate broker tag from the FILL event
        pos_event._routed_broker = getattr(parent, '_routed_broker', None)
        self._bus.emit(pos_event)
        self._persist()

        # ── V9: Shadow FillLedger write ──────────────────────────────────
        if self._ledger is not None:
            try:
                import uuid as _uuid
                # V10: Look up edge context cached from originating SIGNAL
                _edge_ctx = self._signal_ctx.get(
                    getattr(parent, 'correlation_id', None)
                ) or self._signal_ctx.get(
                    getattr(parent, 'event_id', None)
                ) or {}
                lot = FillLot(
                    lot_id=str(_uuid.uuid4()),
                    ticker=ticker,
                    side='BUY',
                    qty=float(qty),
                    fill_price=fill_price,
                    timestamp=now,
                    order_id=p.order_id,
                    broker=_broker,
                    strategy=strategy,
                    reason=str(p.reason or ''),
                    correlation_id=getattr(parent, 'event_id', None),
                    signal_price=getattr(p, 'signal_price', fill_price),
                    order_mode=getattr(p, 'order_mode', 'qty'),
                    init_stop=stop_price,
                    init_target=target_price,
                    init_atr=getattr(p, 'atr_value', None),
                    timeframe=_edge_ctx.get('timeframe', '1min'),
                    regime_at_entry=_edge_ctx.get('regime_at_entry', ''),
                    time_bucket=_edge_ctx.get('time_bucket', ''),
                    confidence=_edge_ctx.get('confidence', 0.0),
                    confluence_score=_edge_ctx.get('confluence_score', 0.0),
                    tier=_edge_ctx.get('tier', 0),
                )
                self._ledger.append(lot)
                self._ledger.set_meta(ticker, PositionMeta(
                    stop_price=stop_price,
                    target_price=target_price,
                    half_target=half_target,
                    atr_value=getattr(p, 'atr_value', None),
                    partial_done=False,
                ))
                # V10: WAL RECORDED — fill persisted to FillLedger
                _wal_cid = getattr(parent, '_wal_client_id', '')
                if _wal_cid:
                    from .order_wal import wal as _wal
                    _wal.recorded(_wal_cid, lot_id=lot.lot_id)
            except Exception as exc:
                log.warning("[PositionManager] Shadow ledger BUY failed: %s", exc)

    # ── Close / partial ───────────────────────────────────────────────────────

    def _close_position(self, p: FillPayload, parent: Event) -> None:
        ticker     = p.ticker
        fill_price = p.fill_price
        qty        = p.qty
        reason     = p.reason
        now        = datetime.now(ET)

        # V9 (R4): Duplicate SELL guard — prevent same order being processed twice
        # (e.g., from Redpanda replay or network retransmit)
        if not hasattr(self, '_processed_sell_keys'):
            self._processed_sell_keys = set()
        sell_key = f"{ticker}:{p.order_id}"
        if sell_key in self._processed_sell_keys:
            log.warning("[PositionManager] Duplicate SELL skipped: %s", sell_key)
            return
        self._processed_sell_keys.add(sell_key)
        if len(self._processed_sell_keys) > 5000:
            self._processed_sell_keys = set(list(self._processed_sell_keys)[-2500:])

        if ticker not in self._positions:
            log.warning(
                f"[PositionManager] SELL fill for {ticker} but no open position "
                f"(may have already been closed). order={p.order_id}"
            )
            return

        pos = self._positions[ticker]
        entry_price = pos['entry_price']
        pos_strategy = pos.get('strategy', 'unknown')

        # V9: Log when selling a reconciled position (observability for P&L tracing)
        _source = pos.get('_source', '')
        _entry_time = pos.get('entry_time', '')
        if _source in ('atr', 'pct_default') or 'reconcile' in str(_entry_time) or 'restored' in str(_entry_time):
            log.info(
                "[PositionManager] SELL reconciled %s: source=%s entry=$%.2f qty=%s",
                ticker, _source or _entry_time, entry_price, qty,
            )

        # V7.2: Detect overfill — broker sold more than local position qty
        position_qty = pos['quantity']
        if qty > position_qty:
            log.error(
                "[PositionManager] OVERFILL for %s: sold %d but position "
                "only had %d! Possible dual-broker collision or broker-side "
                "liquidation. Clamping to position qty.",
                ticker, qty, position_qty)
            send_alert(
                self._alert_email,
                f"OVERFILL {ticker}: sold {qty} > position {position_qty}. "
                f"Check both brokers for orphaned shares.",
            )
            qty = position_qty  # clamp to avoid negative remaining

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

        # V8: Update per-broker qty tracking on any sell
        _sell_broker = getattr(parent, '_routed_broker', None) if parent else None
        if _sell_broker and '_broker_qty' in pos:
            bq = pos['_broker_qty']
            if _sell_broker in bq:
                bq[_sell_broker] = max(0, bq[_sell_broker] - qty)
                if bq[_sell_broker] == 0:
                    del bq[_sell_broker]
                # If only one broker left, clean up _broker_qty
                if len(bq) <= 1:
                    pos.pop('_broker_qty', None)

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

            # V9: Shadow FillLedger write (partial SELL)
            self._shadow_sell_lot(p, parent, now, pnl, reason)
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

        # ── V9: Shadow FillLedger write (SELL) ───────────────────────────
        self._shadow_sell_lot(p, parent, now, pnl, reason)

    # ── Shadow ledger SELL helper ─────────────────────────────────────────────

    def _shadow_sell_lot(self, p: FillPayload, parent: Event,
                          now: datetime, pnl: float, reason: str) -> None:
        """Append a SELL lot to the shadow FillLedger (if attached)."""
        if self._ledger is None:
            return
        try:
            import uuid as _uuid
            _broker = getattr(parent, '_routed_broker', 'unknown') if parent else 'unknown'
            sell_lot = FillLot(
                lot_id=str(_uuid.uuid4()),
                ticker=p.ticker,
                side='SELL',
                qty=float(p.qty),
                fill_price=p.fill_price,
                timestamp=now,
                order_id=p.order_id,
                broker=_broker,
                strategy='',  # strategy comes from the position, not the sell
                reason=str(reason or ''),
                correlation_id=getattr(parent, 'event_id', None),
                signal_price=getattr(p, 'signal_price', p.fill_price),
                order_mode=getattr(p, 'order_mode', 'qty'),
            )
            matches = self._ledger.append(sell_lot)

            # V10: WAL RECORDED for sell
            _wal_cid = getattr(parent, '_wal_client_id', '')
            if _wal_cid:
                try:
                    from .order_wal import wal as _wal
                    _wal.recorded(_wal_cid, lot_id=sell_lot.lot_id)
                except Exception:
                    pass

            # Shadow validation: compare ledger P&L vs computed P&L
            if matches:
                ledger_pnl = sum(m.realized_pnl for m in matches)
                if abs(ledger_pnl - pnl) > 0.02:
                    log.warning(
                        "[PositionManager] SHADOW PNL DRIFT %s: "
                        "dict_pnl=$%.2f ledger_pnl=$%.2f (diff=$%.2f)",
                        p.ticker, pnl, ledger_pnl, ledger_pnl - pnl,
                    )

            # Update partial_done in ledger meta
            remaining = self._ledger.total_qty(p.ticker)
            if remaining > QTY_EPSILON and reason == 'PARTIAL_SELL':
                self._ledger.patch_meta(p.ticker, partial_done=True)

        except Exception as exc:
            log.warning("[PositionManager] Shadow ledger SELL failed: %s", exc)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist(self) -> None:
        try:
            save_state(self._positions, self._reclaimed_today, self._trade_log)
        except Exception as e:
            log.error(f"[PositionManager] persist failed: {e}")
