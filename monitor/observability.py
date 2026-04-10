"""
T7 — Observability Layer
=========================
Structured event logging and periodic heartbeat for the trading bot.

Components
----------
EventLogger
    Subscribes to every EventType and writes a human-readable, level-
    appropriate log line for each event.  No side effects; pure observer.

HeartbeatEmitter
    Emits EventType.HEARTBEAT every `interval_sec` seconds (default 60).
    Reads portfolio state from StateEngine.snapshot() and packages it into a
    HeartbeatPayload.  Runs on the caller's thread (the monitor run loop
    calls `tick()` once per main loop iteration).

EODSummary
    Call `send_eod_summary()` at end-of-day to email/log a trade summary.

Usage
-----
    from .observability import EventLogger, HeartbeatEmitter, EODSummary

    EventLogger(bus)                          # auto-wires on construction

    hb = HeartbeatEmitter(bus, state_engine, interval_sec=60)
    # inside run loop:
    hb.tick()

    # at session end:
    EODSummary.send(trade_log, alert_email)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .alerts import send_alert
from .event_bus import Event, EventBus, EventType
from .events import (
    BarPayload,
    FillPayload,
    HeartbeatPayload,
    OrderFailPayload,
    OrderRequestPayload,
    PositionPayload,
    RiskBlockPayload,
    SignalPayload,
)

ET  = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)


# ── EventLogger ───────────────────────────────────────────────────────────────

class EventLogger:
    """
    Passive observer — logs every event that flows through the bus.
    One concise log line per event; DEBUG for high-frequency events (BAR),
    INFO for everything else.
    """

    def __init__(self, bus: EventBus):
        for et in EventType:
            bus.subscribe(et, self._on_event)

    def _on_event(self, event: Event) -> None:
        p   = event.payload
        eid = event.event_id[:8]      # first 8 chars are enough for correlation

        match event.type:
            case EventType.BAR:
                bars: BarPayload = p
                log.debug(
                    f"[{eid}] BAR {bars.ticker} "
                    f"rows={len(bars.df)} rvol={'yes' if bars.rvol_df is not None else 'no'}"
                )
            case EventType.SIGNAL:
                s: SignalPayload = p
                log.info(
                    f"[{eid}] SIGNAL {s.ticker} action={s.action} "
                    f"price=${s.current_price:.2f} rsi={s.rsi_value:.1f} rvol={s.rvol:.1f}x"
                )
            case EventType.ORDER_REQ:
                o: OrderRequestPayload = p
                log.info(
                    f"[{eid}] ORDER_REQ {o.side.upper()} {o.qty} {o.ticker} "
                    f"@ ${o.price:.2f} reason={o.reason}"
                )
            case EventType.FILL:
                f: FillPayload = p
                log.info(
                    f"[{eid}] FILL {f.side.upper()} {f.qty} {f.ticker} "
                    f"@ ${f.fill_price:.2f} order={f.order_id}"
                )
            case EventType.ORDER_FAIL:
                of: OrderFailPayload = p
                log.warning(
                    f"[{eid}] ORDER_FAIL {of.side.upper()} {of.qty} {of.ticker} "
                    f"@ ${of.price:.2f} reason={of.reason}"
                )
            case EventType.POSITION:
                pos: PositionPayload = p
                pnl_str = f" pnl=${pos.pnl:+.2f}" if pos.pnl is not None else ""
                log.info(
                    f"[{eid}] POSITION {pos.ticker} action={pos.action}{pnl_str}"
                )
            case EventType.RISK_BLOCK:
                rb: RiskBlockPayload = p
                log.info(
                    f"[{eid}] RISK_BLOCK {rb.ticker} signal={rb.signal_action} "
                    f"reason={rb.reason}"
                )
            case EventType.HEARTBEAT:
                hb: HeartbeatPayload = p
                win_rate = (
                    f"{hb.n_wins / hb.n_trades:.0%}" if hb.n_trades > 0 else "n/a"
                )
                log.info(
                    f"[HEARTBEAT] tickers={hb.n_tickers} positions={hb.n_positions} "
                    f"open={hb.open_tickers} trades={hb.n_trades} "
                    f"wins={hb.n_wins}({win_rate}) pnl=${hb.total_pnl:+.2f}"
                )
            case _:
                log.debug(f"[{eid}] {event.type.name} payload={p!r}")


# ── HeartbeatEmitter ──────────────────────────────────────────────────────────

class HeartbeatEmitter:
    """
    Emits a HEARTBEAT event at most once per `interval_sec`.
    Call `tick()` from the main run loop; it is a no-op if the interval has
    not elapsed.
    """

    def __init__(
        self,
        bus: EventBus,
        state_engine,           # StateEngine instance (avoids circular import)
        n_tickers: int = 0,
        interval_sec: float = 60.0,
    ):
        self._bus          = bus
        self._state        = state_engine
        self._n_tickers    = n_tickers
        self._interval     = interval_sec
        self._last_beat    = 0.0

    def set_n_tickers(self, n: int) -> None:
        """Update the current watchlist size (called by the monitor run loop)."""
        self._n_tickers = n

    def tick(self) -> None:
        now = time.monotonic()
        if now - self._last_beat < self._interval:
            return
        self._last_beat = now
        self._emit()

    def _emit(self) -> None:
        snap = self._state.snapshot()
        self._bus.emit(Event(
            type=EventType.HEARTBEAT,
            payload=HeartbeatPayload(
                n_tickers=self._n_tickers,
                n_positions=len(snap['positions']),
                open_tickers=snap['open_tickers'],
                n_trades=snap['n_trades'],
                n_wins=snap['n_wins'],
                total_pnl=snap['total_pnl'],
            ),
        ))


# ── EODSummary ────────────────────────────────────────────────────────────────

class EODSummary:
    """
    Sends (or logs) an end-of-day trade summary.
    Call once at session shutdown.
    """

    @staticmethod
    def send(trade_log: list, alert_email: Optional[str] = None) -> None:
        if not trade_log:
            msg = "Trading session ended — no trades today."
            log.info(msg)
            send_alert(alert_email, msg)
            return

        total_pnl = sum(t.get('pnl', 0.0) or 0.0 for t in trade_log)
        n_wins    = sum(1 for t in trade_log if (t.get('pnl') or 0.0) >= 0)
        win_rate  = n_wins / len(trade_log) if trade_log else 0.0

        lines = [
            "=" * 48,
            f"EOD SUMMARY  {datetime.now(ET).strftime('%Y-%m-%d')}",
            "=" * 48,
            f"Trades : {len(trade_log)}",
            f"Wins   : {n_wins} ({win_rate:.0%})",
            f"Total PnL: ${total_pnl:+.2f}",
            "-" * 48,
        ]
        for t in trade_log:
            pnl    = t.get('pnl', 0.0) or 0.0
            symbol = "✓" if pnl >= 0 else "✗"
            lines.append(
                f"  {symbol} {t.get('ticker','?'):6s} "
                f"{t.get('qty',0):>4} sh "
                f"entry=${t.get('entry_price',0):.2f} "
                f"exit=${t.get('exit_price',0):.2f} "
                f"pnl=${pnl:+.2f}  ({t.get('reason','')})"
            )
        lines.append("=" * 48)

        summary = "\n".join(lines)
        log.info("\n" + summary)
        send_alert(alert_email, summary)
