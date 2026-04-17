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
            case EventType.PRO_STRATEGY_SIGNAL:
                log.info(
                    f"[{eid}] PRO_SIGNAL {p.ticker} setup={p.strategy_name} "
                    f"tier={p.tier} dir={p.direction} conf={p.confidence:.2f} "
                    f"entry=${p.entry_price:.2f} stop=${p.stop_price:.2f}")
            case EventType.POP_SIGNAL:
                log.info(
                    f"[{eid}] POP_SIGNAL {p.symbol} type={p.strategy_type} "
                    f"reason={p.pop_reason} conf={p.strategy_confidence:.2f} "
                    f"entry=${p.entry_price:.2f} rvol={p.rvol:.1f}")
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

        # V8: Per-engine signal counters for heartbeat
        self._pro_signal_count = 0
        self._pop_signal_count = 0

        # Subscribe to SIGNAL and FILL to track recency
        # V8: Also track PRO_STRATEGY_SIGNAL and POP_SIGNAL to prevent
        # false silence alerts when Pro is active but VWAP isn't generating.
        bus.subscribe(EventType.SIGNAL, self._on_signal, priority=0)
        bus.subscribe(EventType.FILL,   self._on_fill,   priority=0)
        try:
            bus.subscribe(EventType.PRO_STRATEGY_SIGNAL, self._on_pro_signal, priority=0)
            bus.subscribe(EventType.POP_SIGNAL, self._on_pop_signal, priority=0)
        except Exception:
            pass  # event types may not exist in test environments

        # V7.2: Background heartbeat writer — independent of main loop latency.
        # When emit_batch() blocks for 10+ seconds processing sells, the main
        # loop can't call tick(). This daemon thread writes the heartbeat file
        # every 15 seconds regardless, preventing false HUNG detection.
        import threading
        self._hb_thread = threading.Thread(
            target=self._heartbeat_writer_loop, daemon=True, name='heartbeat-writer')
        self._hb_thread.start()

    def _on_signal(self, event: Event) -> None:
        """V7 P3-4: Track last SIGNAL time."""
        self._last_signal_time = time.monotonic()
        self._signal_alert_sent = False  # reset on new signal

    def _on_pro_signal(self, event: Event) -> None:
        """V8: Track PRO_STRATEGY_SIGNAL count + reset signal timer."""
        self._last_signal_time = time.monotonic()
        self._signal_alert_sent = False
        self._pro_signal_count += 1

    def _on_pop_signal(self, event: Event) -> None:
        """V8: Track POP_SIGNAL count + reset signal timer."""
        self._last_signal_time = time.monotonic()
        self._signal_alert_sent = False
        self._pop_signal_count += 1

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
        # V8: Log Pro/Pop signal counts in heartbeat
        log.info(
            "[HeartbeatEmitter] pro_signals=%d pop_signals=%d (session total)",
            self._pro_signal_count, self._pop_signal_count,
        )
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

    def _heartbeat_writer_loop(self) -> None:
        """V7.2: Background thread that writes heartbeat every 15 seconds.

        Decoupled from main loop so long-running emit_batch() calls
        (e.g., 17 sequential sells taking 11s) don't starve the heartbeat.
        """
        while True:
            try:
                time.sleep(15)
                self._write_heartbeat_file()
            except Exception:
                pass  # daemon thread — never crash

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
        # V7.2: Don't check until signals are actually possible.
        # Earliest signal = TRADE_START_TIME + MIN_BARS_REQUIRED minutes
        # (bars are ~1 min each). Then add the silence threshold itself,
        # since we need to wait that long after signals become possible.
        et_now = datetime.now(ET)
        if et_now.hour >= 15 and et_now.minute >= 45:
            return  # outside active trading window

        from config import TRADE_START_TIME, MIN_BARS_REQUIRED
        start_h, start_m = (int(x) for x in TRADE_START_TIME.split(':'))
        # Earliest possible signal time + silence threshold (in minutes)
        warmup_minutes = start_m + MIN_BARS_REQUIRED + (self._SIGNAL_SILENCE_ALERT_SEC // 60)
        earliest_h = start_h + warmup_minutes // 60
        earliest_m = warmup_minutes % 60
        if (et_now.hour, et_now.minute) < (earliest_h, earliest_m):
            return  # still in warmup — signals can't have been expected yet

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

        # V8: Per-strategy breakdown
        from collections import defaultdict
        strat_stats = defaultdict(lambda: {'count': 0, 'pnl': 0.0, 'wins': 0})
        for t in trade_log:
            strategy = t.get('strategy', t.get('reason', 'unknown'))
            # Extract strategy prefix (e.g., 'pro:sr_flip' → 'pro', 'vwap_reclaim' → 'vwap')
            if ':' in str(strategy):
                prefix = str(strategy).split(':')[0]
            elif strategy in ('VWAP reclaim', 'vwap_reclaim'):
                prefix = 'vwap'
            else:
                prefix = str(strategy).split('_')[0] if strategy else 'unknown'
            pnl = t.get('pnl', 0.0) or 0.0
            strat_stats[prefix]['count'] += 1
            strat_stats[prefix]['pnl'] += pnl
            if pnl >= 0:
                strat_stats[prefix]['wins'] += 1

        if strat_stats:
            lines.append("PER-STRATEGY BREAKDOWN:")
            for prefix, stats in sorted(strat_stats.items()):
                wr = stats['wins'] / stats['count'] if stats['count'] > 0 else 0
                lines.append(
                    f"  {prefix:12s}  trades={stats['count']:2d}  "
                    f"wins={stats['wins']}({wr:.0%})  pnl=${stats['pnl']:+.2f}"
                )
            lines.append("=" * 48)

        summary = "\n".join(lines)
        log.info("\n" + summary)
        send_alert(alert_email, summary)
