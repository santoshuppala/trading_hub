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

    V7 P3-2: Writes heartbeat timestamp to data/heartbeat.json for supervisor
    health checks. If heartbeat goes stale (>120s), supervisor can force-kill.

    V7 P3-4: Tracks "time since last SIGNAL" and "time since last FILL".
    Alerts if > 30 minutes during market hours (silent failure detection).
    """

    # V7 P3-4: Silent failure thresholds
    _SIGNAL_SILENCE_ALERT_SEC = 1800  # 30 minutes without SIGNAL = alert
    _FILL_SILENCE_ALERT_SEC   = 3600  # 60 minutes without FILL = alert (fills are rarer)

    def __init__(
        self,
        bus: EventBus,
        state_engine,           # StateEngine instance (avoids circular import)
        n_tickers: int = 0,
        interval_sec: float = 60.0,
        alert_email: Optional[str] = None,
    ):
        self._bus          = bus
        self._state        = state_engine
        self._n_tickers    = n_tickers
        self._interval     = interval_sec
        self._last_beat    = 0.0
        self._alert_email  = alert_email

        # V7 P3-2: Heartbeat file for supervisor health checks
        import os
        self._heartbeat_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'data', 'heartbeat.json')

        # V7 P3-4: Track last event times for silent failure detection
        self._last_signal_time = time.monotonic()
        self._last_fill_time   = time.monotonic()
        self._signal_alert_sent = False
        self._fill_alert_sent   = False

        # Subscribe to SIGNAL and FILL to track recency
        bus.subscribe(EventType.SIGNAL, self._on_signal, priority=0)
        bus.subscribe(EventType.FILL,   self._on_fill,   priority=0)

    def _on_signal(self, event: Event) -> None:
        """V7 P3-4: Track last SIGNAL time."""
        self._last_signal_time = time.monotonic()
        self._signal_alert_sent = False  # reset on new signal

    def _on_fill(self, event: Event) -> None:
        """V7 P3-4: Track last FILL time."""
        self._last_fill_time = time.monotonic()
        self._fill_alert_sent = False

    def set_n_tickers(self, n: int) -> None:
        """Update the current watchlist size (called by the monitor run loop)."""
        self._n_tickers = n

    def tick(self) -> None:
        now = time.monotonic()
        if now - self._last_beat < self._interval:
            return
        self._last_beat = now
        self._emit()
        self._write_heartbeat_file()
        self._check_silent_failures(now)

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

    def _write_heartbeat_file(self) -> None:
        """V7 P3-2: Write heartbeat timestamp for supervisor health checks.

        Supervisor reads this file and force-kills if timestamp > 120s old.
        """
        import json, os
        try:
            os.makedirs(os.path.dirname(self._heartbeat_file), exist_ok=True)
            data = {
                'timestamp': datetime.now(ET).isoformat(),
                'wall_clock': time.time(),
                'n_tickers': self._n_tickers,
                'last_signal_age_sec': round(time.monotonic() - self._last_signal_time, 1),
                'last_fill_age_sec': round(time.monotonic() - self._last_fill_time, 1),
            }
            tmp = self._heartbeat_file + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, self._heartbeat_file)
        except Exception:
            pass  # heartbeat file is best-effort

    def _check_silent_failures(self, now: float) -> None:
        """V7 P3-4: Alert if no SIGNAL or FILL for too long during market hours."""
        # Only check during market hours (9:30 AM - 3:45 PM ET)
        et_now = datetime.now(ET)
        if et_now.hour < 10 or (et_now.hour >= 15 and et_now.minute >= 45):
            return  # outside active trading window

        signal_age = now - self._last_signal_time
        if signal_age > self._SIGNAL_SILENCE_ALERT_SEC and not self._signal_alert_sent:
            self._signal_alert_sent = True
            msg = (f"SILENT FAILURE: No SIGNAL events for {signal_age/60:.0f} minutes "
                   f"during market hours. StrategyEngine may be stalled.")
            log.critical(msg)
            send_alert(self._alert_email, msg, severity='CRITICAL')

        fill_age = now - self._last_fill_time
        if fill_age > self._FILL_SILENCE_ALERT_SEC and not self._fill_alert_sent:
            self._fill_alert_sent = True
            msg = (f"SILENT FAILURE: No FILL events for {fill_age/60:.0f} minutes "
                   f"during market hours. Order execution may be stalled.")
            log.warning(msg)
            send_alert(self._alert_email, msg, severity='WARNING')


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
