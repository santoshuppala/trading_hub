"""
T9 — Monitor Orchestrator
==========================
Thin wiring layer that creates the EventBus, instantiates all engines,
and runs the main data-fetch loop.

Architecture
------------
  Data fetch loop (this file)
    └─ emits BAR events ──────────────────────────────────────┐
                                                              ▼
  StrategyEngine  ← BAR → SIGNAL(buy / sell_* / partial_sell)
  RiskEngine      ← SIGNAL → ORDER_REQ | RISK_BLOCK
  Broker          ← ORDER_REQ → FILL | ORDER_FAIL
  ExecutionFeedback ← SIGNAL(buy) + FILL → patches positions
  PositionManager ← FILL → POSITION (opens / closes / partial)
  StateEngine     ← POSITION → clean snapshot for UI
  EventLogger     ← * → structured log lines
  HeartbeatEmitter ← tick() → HEARTBEAT every 60 s

Public interface (backward-compatible)
---------------------------------------
  Attributes: tickers, positions, trade_log, running
  Methods   : start(), stop()

Configuration
-------------
  All tunable values come from config.py / environment variables.
  DATA_SOURCE : 'tradier' | 'alpaca'
  BROKER      : 'alpaca'  | 'paper'
  TRADE_BUDGET: dollars allocated per trade (default 1000)
"""
from __future__ import annotations

import atexit
import logging
import os
import subprocess
import time
import threading
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET        = ZoneInfo('America/New_York')
LOCK_FILE = os.path.join(os.path.dirname(__file__), '..', '.monitor.lock')


# ── Process-level singleton lock ─────────────────────────────────────────────

def _pid_is_monitor(pid: int) -> bool:
    try:
        result = subprocess.run(
            ['ps', '-p', str(pid), '-o', 'comm='],
            capture_output=True, text=True, timeout=2,
        )
        return 'python' in result.stdout.lower()
    except Exception as exc:
        log.debug("PID check for %d failed: %s", pid, exc)
        return True


def _is_running_elsewhere():
    try:
        with open(LOCK_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        if not _pid_is_monitor(pid):
            log.warning(f"Stale lock (PID {pid} is not a Python process). Removing.")
            os.remove(LOCK_FILE)
            return False, None
        return True, pid
    except (FileNotFoundError, ValueError):
        return False, None
    except OSError:
        log.warning("Stale lock file found (process no longer running). Removing.")
        os.remove(LOCK_FILE)
        return False, None


def _write_lock():
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))


def _remove_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


# ── Orchestrator ─────────────────────────────────────────────────────────────

from alpaca.trading.client import TradingClient
from config import ORPHAN_STOP_PCT, ORPHAN_TARGET_PCT

from .alerts import send_alert
from .brokers import make_broker
from .data_client import make_data_client
from .event_bus import BarPayload, Event, EventBus, EventType, DispatchMode
from .event_log import DurableEventLog
from .execution_feedback import ExecutionFeedback
from .observability import EODSummary, EventLogger, HeartbeatEmitter
from .position_manager import PositionManager
from .risk_engine import RiskEngine
from .screener import MomentumScreener
from .state import load_state
from .state_engine import StateEngine
from .strategy_engine import StrategyEngine


class RealTimeMonitor:
    """
    Orchestrates all trading engines via the EventBus.

    __init__  : wires up every engine; restores intraday state from disk.
    start()   : spawns the run loop in a daemon thread.
    stop()    : signals the loop to stop; waits for thread.
    run()     : the main loop — fetches bars, emits BAR events, ticks heartbeat.
    """

    def __init__(
        self,
        tickers,
        strategy_name,
        strategy_params,
        open_cost=0.0,
        close_cost=0.0,
        alert_email=None,
        alpaca_api_key=None,
        alpaca_secret_key=None,
        tradier_token=None,
        paper=True,
        max_positions=5,
        order_cooldown=300,
        per_ticker_params=None,
        data_source='tradier',
        trade_budget=1000,
        redpanda_brokers=None,
    ):
        # ── Watchlist ──────────────────────────────────────────────────────
        self._alert_email = alert_email
        self.base_tickers = list(tickers)
        self.tickers      = list(tickers)

        # ── Shared mutable state ───────────────────────────────────────────
        saved_pos, saved_reclaimed, saved_trades = load_state()
        self.positions       = saved_pos
        self.trade_log       = saved_trades
        self._reclaimed_today   = saved_reclaimed
        self._last_order_time   = {}
        self._last_reset_date   = datetime.now(ET).date()
        self._last_momentum_refresh = None

        # ── Caches (for the data fetch layer) ─────────────────────────────
        self._bars_cache = {}
        self._rvol_cache = {}

        # ── Control ───────────────────────────────────────────────────────
        self.running = False
        self.thread  = None

        # ── Credentials ───────────────────────────────────────────────────
        api_key    = alpaca_api_key    or os.getenv('APCA_API_KEY_ID', '')
        api_secret = alpaca_secret_key or os.getenv('APCA_API_SECRET_KEY', '')
        token      = tradier_token     or os.getenv('TRADIER_TOKEN', '')

        # ── Data client ───────────────────────────────────────────────────
        data_client = make_data_client(
            data_source,
            tradier_token=token,
            alpaca_api_key=api_key,
            alpaca_secret=api_secret,
        )
        self._data     = data_client
        self._screener = MomentumScreener(data_client)
        log.info(f"Data client: {data_source}")

        # ── Alpaca trading client (order execution) ────────────────────────
        if api_key and api_secret:
            trading_client = TradingClient(api_key, api_secret, paper=paper)
            log.info(f"Alpaca trading client initialised (paper={paper}).")
        else:
            trading_client = None
            log.warning("Alpaca keys not provided — order execution unavailable.")

        # ── Alpaca position reconciliation ────────────────────────────────
        # Query broker for actual open positions and import any that local
        # state lost track of (e.g. positions opened in a previous session
        # that didn't survive a restart cleanly).
        self._sync_broker_positions(trading_client)

        # ── Event Bus ─────────────────────────────────────────────────────
        # durable_fail_fast=True ensures ORDER_REQ/FILL are NOT delivered to
        # handlers if the Redpanda before-emit hook fails — prevents untracked
        # order execution that would be invisible to CrashRecovery.
        self._bus = EventBus(durable_fail_fast=True)

        # ── Engines (subscription order = handler priority) ────────────────
        #   1. EventLogger   — passive; subscribes last so it sees everything
        #   2. StrategyEngine — converts BAR → SIGNAL
        #   3. RiskEngine     — converts SIGNAL → ORDER_REQ | RISK_BLOCK
        #   4. Broker         — converts ORDER_REQ → FILL | ORDER_FAIL
        #   5. ExecutionFeedback — patches positions after buy fill
        #   6. PositionManager — opens/closes positions on FILL
        #   7. StateEngine    — maintains read-only snapshot from POSITION
        #   8. HeartbeatEmitter — periodic HEARTBEAT (called from run loop)

        self._strategy = StrategyEngine(
            bus=self._bus,
            positions=self.positions,
            strategy_params=strategy_params,
            per_ticker_params=per_ticker_params or {},
            data_client=data_client,
        )

        self._risk = RiskEngine(
            bus=self._bus,
            positions=self.positions,
            reclaimed_today=self._reclaimed_today,
            last_order_time=self._last_order_time,
            data_client=data_client,
            max_positions=max_positions,
            order_cooldown=order_cooldown,
            trade_budget=trade_budget,
            alert_email=alert_email,
        )

        broker_source = os.getenv('BROKER', 'alpaca')
        self._broker = make_broker(
            source=broker_source,
            bus=self._bus,
            trading_client=trading_client,
            alert_email=alert_email,
        )

        self._exec_feedback = ExecutionFeedback(
            bus=self._bus,
            positions=self.positions,
        )

        self._pos_manager = PositionManager(
            bus=self._bus,
            positions=self.positions,
            reclaimed_today=self._reclaimed_today,
            last_order_time=self._last_order_time,
            trade_log=self.trade_log,
            alert_email=alert_email,
        )

        self._state_engine = StateEngine(self._bus)
        # Seed snapshot from restored on-disk state so HeartbeatEmitter and
        # the UI show correct data before any new POSITION events arrive.
        self._state_engine.seed(self.positions, self.trade_log)

        self._heartbeat = HeartbeatEmitter(
            bus=self._bus,
            state_engine=self._state_engine,
            n_tickers=len(self.tickers),
            interval_sec=60.0,
        )

        # EventLogger last — it observes but never emits
        EventLogger(self._bus)

        # DurableEventLog — Redpanda producer; subscribes after EventLogger
        # so it is the very last observer.  Disabled if no broker configured.
        rp_brokers = (
            redpanda_brokers
            or os.getenv('REDPANDA_BROKERS', '127.0.0.1:9092')
        )
        self._durable_log = DurableEventLog(self._bus, brokers=rp_brokers)
        # Issue 7: register synchronous produce+flush hook so ORDER_REQ / FILL
        # events are Redpanda-acked before any in-process handler runs.
        self._durable_log.register_durable_hook(self._bus)

        log.info(
            f"RealTimeMonitor ready | broker={broker_source} "
            f"data={data_source} budget=${trade_budget} "
            f"max_pos={max_positions} cooldown={order_cooldown}s "
            f"redpanda={rp_brokers}"
        )

    # ── ATR lookup for reconciliation ────────────────────────────────────────

    @staticmethod
    def _fetch_last_atr(ticker: str) -> Optional[float]:
        """
        Best-effort fetch of the last known ATR for *ticker* from event_store.

        Uses a direct psycopg2 connection so it works in the synchronous
        __init__ context (the asyncpg pool may not be ready yet).
        Returns None if the DB is unavailable or no ATR is recorded.
        """
        try:
            import psycopg2
            from config import DATABASE_URL
        except Exception as exc:
            log.debug("Import for ATR lookup failed: %s", exc)
            return None

        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT (event_payload->>'atr_value')::FLOAT
                          FROM trading.event_store
                         WHERE event_type = 'StrategySignal'
                           AND event_payload->>'ticker' = %s
                         ORDER BY event_time DESC
                         LIMIT 1
                        """,
                        (ticker,),
                    )
                    row = cur.fetchone()
                    return row[0] if row and row[0] else None
            finally:
                conn.close()
        except Exception as exc:
            log.debug("[reconcile] ATR lookup failed for %s: %s", ticker, exc)
            return None

    # ── Startup reconciliation ───────────────────────────────────────────────

    def _sync_broker_positions(self, trading_client) -> None:
        """
        Import any Alpaca open positions that local state lost track of.

        Called once during __init__ — before engines are wired — so that
        RiskEngine, PositionManager, and StateEngine all start with a
        complete and accurate view of open positions.

        For each Alpaca position NOT in self.positions we:
          • Add a minimal position record to self.positions.
          • Add the ticker to self._reclaimed_today so RiskEngine treats it
            as an existing position and won't open a duplicate.
          • Log a WARNING so the discrepancy is visible in the log.
        """
        if not trading_client:
            return
        try:
            alpaca_positions = trading_client.get_all_positions()
        except Exception as e:
            log.warning(f"[reconcile] Could not fetch Alpaca positions: {e}")
            return

        reconciled = 0
        for ap in alpaca_positions:
            ticker = str(ap.symbol)
            if ticker in self.positions:
                # Verify qty matches — fix if diverged (crash recovery, partial fills)
                try:
                    alpaca_qty = int(float(ap.qty or 0))
                    local_qty = self.positions[ticker].get('quantity', 0)
                    if alpaca_qty > 0 and alpaca_qty != local_qty:
                        log.warning(
                            "[reconcile] qty mismatch for %s: local=%d Alpaca=%d — updating to Alpaca",
                            ticker, local_qty, alpaca_qty,
                        )
                        self.positions[ticker]['quantity'] = alpaca_qty
                        self.positions[ticker]['qty'] = alpaca_qty
                        reconciled += 1
                except (TypeError, ValueError):
                    pass
                continue

            try:
                avg_entry = float(ap.avg_entry_price or 0)
                qty       = int(float(ap.qty or 0))
            except (TypeError, ValueError):
                log.warning(f"[reconcile] Bad position data from Alpaca for {ticker}; skipping")
                continue

            if qty <= 0 or avg_entry <= 0:
                continue

            # Try ATR-based stop from event_store; fall back to fixed 3%
            atr = self._fetch_last_atr(ticker)
            if atr and atr > 0:
                stop_price   = round(avg_entry - 2.0 * atr, 4)
                target_price = round(avg_entry + 3.0 * atr, 4)
                log.warning(
                    "Orphaned %s: ATR-based stop $%.2f (ATR=%.2f)",
                    ticker, stop_price, atr,
                )
            else:
                stop_price   = round(avg_entry * (1 - ORPHAN_STOP_PCT), 4)
                target_price = round(avg_entry * (1 + ORPHAN_TARGET_PCT), 4)
                log.warning(
                    "Orphaned %s: default 3%% stop $%.2f (no ATR available)",
                    ticker, stop_price,
                )
            self.positions[ticker] = {
                'entry_price':  avg_entry,
                'qty':          qty,
                'quantity':     qty,
                'stop_price':   stop_price,
                'target_price': target_price,
                'half_target':  round((avg_entry + target_price) / 2, 4),
                'partial_done': False,
                'entry_time':   'alpaca_restored',
                'order_id':     'alpaca_reconciliation',
                'atr_value':    round((target_price - stop_price) / 2, 4),
            }
            # Note: reclaimed_today mutations are GIL-atomic in CPython.
            # Per-ticker partitioning in EventBus ensures no two threads check+add
            # the same ticker simultaneously.
            self._reclaimed_today.add(ticker)
            log.warning(
                f"[reconcile] Imported orphaned Alpaca position: "
                f"{qty} {ticker} @ ${avg_entry:.2f} "
                f"(was absent from bot_state.json)"
            )
            reconciled += 1

        # Reverse reconciliation: remove local positions not at any broker
        # Prevents selling positions that were already closed by bracket stops
        alpaca_tickers = {str(ap.symbol) for ap in alpaca_positions}
        tradier_tickers = set()
        try:
            import requests as _req
            t_token = os.getenv('TRADIER_SANDBOX_TOKEN', '')
            t_acct = os.getenv('TRADIER_ACCOUNT_ID', '')
            if t_token and t_acct:
                t_base = 'https://sandbox.tradier.com'
                t_headers = {'Authorization': f'Bearer {t_token}', 'Accept': 'application/json'}
                resp = _req.get(f'{t_base}/v1/accounts/{t_acct}/positions',
                               headers=t_headers, timeout=5)
                if resp.status_code == 200:
                    t_positions = resp.json().get('positions', {})
                    if t_positions and t_positions != 'null':
                        t_list = t_positions.get('position', [])
                        if isinstance(t_list, dict):
                            t_list = [t_list]
                        for p in t_list:
                            if int(float(p.get('quantity', 0))) > 0:
                                tradier_tickers.add(p['symbol'])
        except Exception:
            pass

        all_broker_tickers = alpaca_tickers | tradier_tickers
        stale = [t for t in list(self.positions.keys()) if t not in all_broker_tickers]
        for ticker in stale:
            log.warning(
                "[reconcile] Removing stale position %s — not found at any broker "
                "(may have been closed by bracket stop)", ticker
            )
            del self.positions[ticker]
            reconciled += 1

        if reconciled:
            log.warning(
                f"[reconcile] Reconciled {reconciled} position(s). "
                f"Verify stop/target prices — defaults are ±3%/±5% from avg entry."
            )
        else:
            log.info("[reconcile] Local positions are in sync with Alpaca.")

    # ── Run loop ─────────────────────────────────────────────────────────────

    def run(self):
        """Main monitoring loop. Runs in the background thread started by start()."""
        self.running = True
        try:
            self._run_loop()
        except Exception as e:
            log.critical(f"[Monitor] Fatal error in run loop: {e}", exc_info=True)
            send_alert(self._alert_email, f"Monitor crashed — manual restart required: {e}")
            self.running = False
            _remove_lock()

    def _run_loop(self):
        """Inner loop extracted so run() can wrap it in a single try/except."""
        while self.running:
            self._reset_daily_state()

            # Refresh momentum screener every 30 minutes
            self.tickers, self._last_momentum_refresh = self._screener.refresh(
                self.base_tickers,
                self.tickers,
                self._last_momentum_refresh,
            )
            self._heartbeat.set_n_tickers(len(self.tickers))

            # Single batch API call
            log.info(
                f"[{datetime.now(ET).strftime('%H:%M:%S')}] "
                f"Fetching bars for {len(self.tickers)} tickers …"
            )
            self._bars_cache, self._rvol_cache = self._data.fetch_batch_bars(self.tickers)

            # Emit BAR events for all tickers.
            # emit_batch() acquires each lock once for the whole batch (issue 4),
            # vs emit() which would acquire locks N times.
            now   = datetime.now(ET)
            today = now.date()
            bar_events = []
            for ticker in self.tickers:
                ev = self._build_bar_event(ticker, today)
                if ev is not None:
                    bar_events.append(ev)
            if bar_events:
                try:
                    self._bus.emit_batch(bar_events)
                except Exception as e:
                    log.error(f"emit_batch(BAR) failed: {e}")

            # Tick heartbeat (emits at most once per 60 s)
            self._heartbeat.tick()

            # Sleep until next cycle
            now = datetime.now(ET)
            market_open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
            if now < market_open_time:
                wait = max(10, (market_open_time - now).total_seconds() - 30)
                log.info(f"Pre-market: sleeping {wait / 60:.1f} min until near open.")
                time.sleep(wait)
            else:
                time.sleep(60)

    def _build_bar_event(self, ticker: str, today) -> Optional[Event]:
        """Slice today's bars from the cache and return a BAR Event (or None)."""
        full_df = self._bars_cache.get(ticker)
        rvol_df = self._rvol_cache.get(ticker)
        if full_df is None or full_df.empty:
            return None
        try:
            today_df = full_df[full_df.index.date == today]
        except Exception as exc:
            log.debug("Date filter failed for %s: %s", ticker, exc)
            today_df = full_df
        if today_df.empty:
            return None
        return Event(
            type=EventType.BAR,
            payload=BarPayload(
                ticker=ticker,
                df=today_df,
                rvol_df=rvol_df,
            ),
        )

    def _reset_daily_state(self):
        today = datetime.now(ET).date()
        if today != self._last_reset_date:
            # Note: reclaimed_today mutations are GIL-atomic in CPython.
            # Per-ticker partitioning in EventBus ensures no two threads check+add
            # the same ticker simultaneously.
            self._reclaimed_today.clear()
            self._last_reset_date = today

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        # Skip lock check when running under supervisor (isolated mode)
        if not os.environ.get('SUPERVISED_MODE'):
            already_running, pid = _is_running_elsewhere()
            if already_running:
                msg = (
                    f"ERROR: Monitor already running (PID {pid}).\n"
                    f"Stop it first.  Lock: {os.path.abspath(LOCK_FILE)}"
                )
                log.error(msg)
                raise RuntimeError(msg)

        if not self.running:
            if not os.environ.get('SUPERVISED_MODE'):
                _write_lock()
                atexit.register(_remove_lock)
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            log.info(f"Real-time monitoring started (PID {os.getpid()}).")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=10)
        EODSummary.send(self.trade_log, alert_email=None)
        self._durable_log.close()   # flush remaining Redpanda messages
        self._bus.shutdown()        # drain async dispatchers (no-op in sync mode)
        _remove_lock()
        log.info("Real-time monitoring stopped.")
