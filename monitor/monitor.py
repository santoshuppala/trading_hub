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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_float(val) -> float:
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0

def _safe_int(val) -> int:
    try:
        return int(float(val)) if val is not None else 0
    except (TypeError, ValueError):
        return 0


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
from .ticker_scanner import TickerScanner


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
        # V7.2: Returns broker ticker sets so SmartRouter can be re-seeded.
        self._reconciled_alpaca, self._reconciled_tradier = \
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

        # V8: Tiered ticker scanner (Option D)
        self._ticker_scanner = TickerScanner(positions=self.positions)

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

    # ── DB position cache for reconciliation enrichment ─────────────────────

    @staticmethod
    def _load_db_position_cache() -> dict:
        """
        V7.2: Query DB for today's open positions to enrich broker imports.

        Returns {ticker: {stop_price, target_price, strategy, broker, ...}}
        so reconciliation can use real values instead of 3%/5% defaults.
        """
        try:
            import psycopg2
            from config import DATABASE_URL
        except Exception:
            return {}

        today_str = datetime.now(ET).strftime('%Y-%m-%d')
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        WITH opened AS (
                            SELECT DISTINCT ON (event_payload->>'ticker')
                                event_payload->>'ticker'       AS ticker,
                                event_payload->>'entry_price'  AS entry_price,
                                event_payload->>'entry_time'   AS entry_time,
                                event_payload->>'stop_price'   AS stop_price,
                                event_payload->>'target_price' AS target_price,
                                event_payload->>'half_target'  AS half_target,
                                event_payload->>'qty'          AS qty,
                                event_payload->>'strategy'     AS strategy,
                                event_payload->>'broker'       AS broker,
                                event_payload->>'atr_value'    AS atr_value
                            FROM trading.event_store
                            WHERE event_type = 'PositionOpened'
                              AND event_time::date = %s::date
                            ORDER BY event_payload->>'ticker', event_time DESC
                        ),
                        closed AS (
                            SELECT DISTINCT event_payload->>'ticker' AS ticker
                            FROM trading.event_store
                            WHERE event_type = 'PositionClosed'
                              AND event_time::date = %s::date
                        ),
                        buy_reasons AS (
                            SELECT DISTINCT ON (event_payload->>'ticker')
                                event_payload->>'ticker' AS ticker,
                                event_payload->>'reason' AS reason
                            FROM trading.event_store
                            WHERE event_type = 'OrderRequested'
                              AND event_payload->>'side' = 'BUY'
                              AND event_time::date = %s::date
                            ORDER BY event_payload->>'ticker', event_time DESC
                        ),
                        buy_fills AS (
                            SELECT DISTINCT ON (event_payload->>'ticker')
                                event_payload->>'ticker'   AS ticker,
                                event_payload->>'order_id' AS fill_order_id
                            FROM trading.event_store
                            WHERE event_type = 'FillExecuted'
                              AND event_payload->>'side' = 'BUY'
                              AND event_time::date = %s::date
                            ORDER BY event_payload->>'ticker', event_time DESC
                        )
                        SELECT o.*, b.reason AS buy_reason, f.fill_order_id
                        FROM opened o
                        LEFT JOIN closed c ON o.ticker = c.ticker
                        LEFT JOIN buy_reasons b ON o.ticker = b.ticker
                        LEFT JOIN buy_fills f ON o.ticker = f.ticker
                        WHERE c.ticker IS NULL
                    """, (today_str, today_str, today_str, today_str))

                    rows = cur.fetchall()
                    columns = [desc[0] for desc in cur.description]
                    cache = {}
                    for row in rows:
                        r = dict(zip(columns, row))
                        ticker = r.get('ticker')
                        if ticker:
                            # Recover strategy: prefer PositionOpened.strategy,
                            # fall back to OrderRequested.reason parsing
                            strategy = r.get('strategy') or ''
                            if not strategy:
                                reason = r.get('buy_reason', '') or ''
                                if ':' in reason:
                                    parts = reason.split(':')
                                    strategy = parts[0] + ':' + parts[1]
                                elif reason:
                                    strategy = 'vwap_reclaim'
                                else:
                                    strategy = 'unknown'
                            cache[ticker] = {
                                'entry_price':  _safe_float(r.get('entry_price')),
                                'entry_time':   r.get('entry_time', ''),
                                'stop_price':   _safe_float(r.get('stop_price')),
                                'target_price': _safe_float(r.get('target_price')),
                                'half_target':  _safe_float(r.get('half_target')),
                                'qty':          _safe_int(r.get('qty')),
                                'strategy':     strategy,
                                'broker':       r.get('broker') or 'unknown',
                                'atr_value':    _safe_float(r.get('atr_value')),
                                'order_id':     r.get('fill_order_id') or 'unknown',
                            }
                    if cache:
                        log.info(
                            "[reconcile] DB cache: %d open position(s) for enrichment: %s",
                            len(cache), list(cache.keys()))
                    return cache
            finally:
                conn.close()
        except Exception as exc:
            log.warning("[reconcile] DB position cache load failed (non-fatal): %s", exc)
            return {}

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

    def _sync_broker_positions(self, trading_client) -> tuple:
        """
        V7.1: Import open positions from BOTH Alpaca AND Tradier.

        Called once during __init__ — before engines are wired — so that
        RiskEngine, PositionManager, and StateEngine all start with a
        complete and accurate view of open positions.

        For each broker position NOT in self.positions we:
          - Add a minimal position record to self.positions
          - Add ticker to self._reclaimed_today (prevents duplicate entry)
          - Log WARNING so the discrepancy is visible
        """
        reconciled = 0

        # V7.2: Pre-load DB positions for enrichment.
        # When a broker position is not in local state, we look here for
        # the real stop/target/strategy instead of using garbage defaults.
        _db_position_cache = self._load_db_position_cache()

        def _import_position(ticker, avg_entry, qty, broker_name):
            """Import a single broker position into local state."""
            nonlocal reconciled

            if ticker in self.positions:
                existing_broker = self.positions[ticker].get('_broker', '')
                # V7.2: Detect dual-broker overlap — same ticker at both brokers
                if existing_broker and existing_broker != broker_name:
                    log.error(
                        "[reconcile] DUAL-BROKER OVERLAP for %s: already imported "
                        "from %s (%d shares), now also at %s (%d shares). "
                        "Using %s (larger qty) as authoritative.",
                        ticker, existing_broker,
                        self.positions[ticker].get('quantity', 0),
                        broker_name, qty, broker_name if qty > self.positions[ticker].get('quantity', 0) else existing_broker)
                    # Keep the larger position as authoritative
                    if qty <= self.positions[ticker].get('quantity', 0):
                        return  # existing is larger, skip
                    # Fall through to update with larger qty from this broker

                # V7.2: Verify local state against broker truth.
                # Broker is authoritative for: qty, entry_price (cost basis).
                # Local is authoritative for: stop, target, strategy (broker doesn't track these).
                pos = self.positions[ticker]
                changed = False

                # 1. Qty — broker wins
                local_qty = pos.get('quantity', 0)
                if qty > 0 and qty != local_qty:
                    log.warning(
                        "[reconcile] %s qty: local=%d, %s=%d — using broker",
                        ticker, local_qty, broker_name, qty)
                    pos['quantity'] = qty
                    pos['qty'] = qty
                    changed = True

                # 2. Entry price — broker's cost basis wins if local drifted
                local_entry = pos.get('entry_price', 0)
                if avg_entry > 0 and local_entry > 0:
                    drift_pct = abs(avg_entry - local_entry) / local_entry
                    if drift_pct > 0.01:  # >1% drift = something is wrong
                        log.warning(
                            "[reconcile] %s entry_price drift: local=$%.2f, "
                            "%s=$%.2f (%.1f%%) — using broker cost basis",
                            ticker, local_entry, broker_name, avg_entry,
                            drift_pct * 100)
                        pos['entry_price'] = avg_entry
                        changed = True

                # 3. Broker tag — always update
                pos['_broker'] = broker_name

                # 4. Log what we're keeping from local state
                if changed:
                    reconciled += 1
                log.info(
                    "[reconcile] %s: verified at %s (qty=%d entry=$%.2f). "
                    "Local stop=$%.2f target=$%.2f strategy=%s",
                    ticker, broker_name, pos.get('quantity', 0),
                    pos.get('entry_price', 0),
                    pos.get('stop_price', 0), pos.get('target_price', 0),
                    pos.get('strategy', 'unknown'))
                return

            if qty <= 0 or avg_entry <= 0:
                return

            # V7.2: Try DB event_store first for real stop/target/strategy,
            # then ATR-based, then percentage fallback.
            db_pos = _db_position_cache.get(ticker)
            if db_pos:
                stop_price   = db_pos.get('stop_price') or 0
                target_price = db_pos.get('target_price') or 0
                half_target  = db_pos.get('half_target') or 0
                strategy     = db_pos.get('strategy') or 'unknown'
                atr_value    = db_pos.get('atr_value')
                entry_time   = db_pos.get('entry_time', f'{broker_name}_restored')
                order_id     = db_pos.get('order_id') or f'{broker_name}_reconciliation'
                source = 'db'
            else:
                strategy = 'unknown'
                atr_value = None
                entry_time = f'{broker_name}_restored'
                order_id = f'{broker_name}_reconciliation'
                stop_price = 0
                target_price = 0
                half_target = 0
                source = 'default'

            # Fill in missing stop/target from ATR or percentage
            if not stop_price or stop_price <= 0:
                atr = self._fetch_last_atr(ticker)
                if atr and atr > 0:
                    stop_price   = round(avg_entry - 2.0 * atr, 4)
                    target_price = round(avg_entry + 3.0 * atr, 4)
                    source = 'atr'
                else:
                    stop_price   = round(avg_entry * (1 - ORPHAN_STOP_PCT), 4)
                    target_price = round(avg_entry * (1 + ORPHAN_TARGET_PCT), 4)
                    source = 'pct_default'
                half_target = round((avg_entry + target_price) / 2, 4)
                atr_value = round((target_price - stop_price) / 2, 4)

            self.positions[ticker] = {
                'entry_price':  avg_entry,
                'qty':          qty,
                'quantity':     qty,
                'stop_price':   stop_price,
                'target_price': target_price,
                'half_target':  half_target,
                'partial_done': False,
                'entry_time':   entry_time,
                'order_id':     order_id,
                'atr_value':    atr_value,
                'strategy':     strategy,
                '_broker':      broker_name,
                '_source':      source,
            }
            self._reclaimed_today.add(ticker)
            log.warning(
                "[reconcile] Imported %s position: %d %s @ $%.2f "
                "(stop=$%.2f target=$%.2f strategy=%s source=%s)",
                broker_name, qty, ticker, avg_entry, stop_price, target_price,
                strategy, source)
            reconciled += 1

        # ── 1. Alpaca positions ───────────────────────────────────────
        alpaca_tickers = set()
        if trading_client:
            try:
                alpaca_positions = trading_client.get_all_positions()
                for ap in alpaca_positions:
                    ticker = str(ap.symbol)
                    alpaca_tickers.add(ticker)
                    try:
                        avg_entry = float(ap.avg_entry_price or 0)
                        qty = int(float(ap.qty or 0))
                        _import_position(ticker, avg_entry, qty, 'alpaca')
                    except (TypeError, ValueError):
                        log.warning("[reconcile] Bad Alpaca data for %s", ticker)
            except Exception as e:
                log.warning("[reconcile] Could not fetch Alpaca positions: %s", e)

        # ── 2. Tradier positions (V8: with 3-attempt retry + 5s backoff) ──
        tradier_tickers = set()
        try:
            import requests as _req
            t_token = os.getenv('TRADIER_SANDBOX_TOKEN', '')
            t_acct = os.getenv('TRADIER_ACCOUNT_ID', '')
            if t_token and t_acct:
                t_sandbox = os.getenv('TRADIER_SANDBOX', 'true').lower() == 'true'
                t_base = 'https://sandbox.tradier.com' if t_sandbox else 'https://api.tradier.com'
                t_headers = {'Authorization': f'Bearer {t_token}', 'Accept': 'application/json'}
                resp = None
                for _attempt in range(3):
                    try:
                        resp = _req.get(f'{t_base}/v1/accounts/{t_acct}/positions',
                                       headers=t_headers, timeout=5)
                        if resp.status_code == 200:
                            break
                        log.warning("[reconcile] Tradier positions HTTP %d (attempt %d/3)",
                                    resp.status_code, _attempt + 1)
                    except Exception as _e:
                        log.warning("[reconcile] Tradier positions fetch error (attempt %d/3): %s",
                                    _attempt + 1, _e)
                    if _attempt < 2:
                        time.sleep(5)
                if resp is not None and resp.status_code == 200:
                    t_data = resp.json().get('positions', {})
                    if t_data and t_data != 'null':
                        t_list = t_data.get('position', [])
                        if isinstance(t_list, dict):
                            t_list = [t_list]
                        for p in t_list:
                            ticker = p.get('symbol', '')
                            qty = int(float(p.get('quantity', 0)))
                            if qty > 0 and ticker:
                                tradier_tickers.add(ticker)
                                avg_entry = float(p.get('cost_basis', 0)) / qty if qty > 0 else 0
                                _import_position(ticker, avg_entry, qty, 'tradier')
                        log.info("[reconcile] Tradier: %d positions found", len(tradier_tickers))
        except Exception as e:
            log.warning("[reconcile] Could not fetch Tradier positions: %s", e)

        # ── 3. Reverse reconciliation ─────────────────────────────────
        # Remove local positions not at ANY broker
        all_broker_tickers = alpaca_tickers | tradier_tickers
        stale = [t for t in list(self.positions.keys()) if t not in all_broker_tickers]
        for ticker in stale:
            log.warning(
                "[reconcile] Removing stale position %s — not found at any broker "
                "(may have been closed by bracket stop)", ticker)
            del self.positions[ticker]
            reconciled += 1

        if reconciled:
            log.warning(
                f"[reconcile] Reconciled {reconciled} position(s). "
                f"Verify stop/target prices — defaults are ±3%/±5% from avg entry."
            )
        else:
            log.info("[reconcile] Local positions are in sync with brokers.")

        # V7.2: Return broker ticker sets so SmartRouter can be re-seeded
        return alpaca_tickers, tradier_tickers

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
            new_bars, new_rvol = self._data.fetch_batch_bars(self.tickers)

            # V8: Merge new data into existing cache (don't replace).
            # If API returns partial data (71/185), tickers from previous cycle
            # keep their bars instead of disappearing. Evict entries >5 min old.
            if not hasattr(self, '_bars_cache_times'):
                self._bars_cache_times = {}
            now_mono = time.monotonic()
            self._bars_cache.update(new_bars)
            self._rvol_cache.update(new_rvol)
            self._bars_cache_times.update({t: now_mono for t in new_bars})
            # Evict stale entries (>5 min since last refresh)
            stale_tickers = [t for t, ts in self._bars_cache_times.items()
                             if now_mono - ts > 300]
            for t in stale_tickers:
                self._bars_cache.pop(t, None)
                self._bars_cache_times.pop(t, None)

            # V8: Alert if data coverage drops below 90%
            if self.tickers:
                coverage = len(new_bars) / len(self.tickers)
                if coverage < 0.90:
                    log.warning(
                        "[DataQuality] Only %.0f%% tickers fetched (%d/%d) — data incomplete",
                        coverage * 100, len(new_bars), len(self.tickers))

            # V8: Tiered scanning — classify tickers before emit_batch
            scan_tickers = self._ticker_scanner.classify(self.tickers, self._bars_cache)

            # Emit BAR events for all tickers.
            # emit_batch() acquires each lock once for the whole batch (issue 4),
            # vs emit() which would acquire locks N times.
            now   = datetime.now(ET)
            today = now.date()
            bar_events = []
            for ticker in scan_tickers:
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

            # V8: Pop periodic scans — ticker discovery from alt data
            if not hasattr(self, '_last_pop_scan'):
                self._last_pop_scan = 0.0
                self._last_headline_check = 0.0
            now_mono = time.monotonic()
            # Full scan every 10 min (reads alt_data_cache.json)
            if now_mono - self._last_pop_scan > 600:
                self._last_pop_scan = now_mono
                self._refresh_pop_tickers()
            # Quick headline check every 2 min
            if now_mono - self._last_headline_check > 120:
                self._last_headline_check = now_mono
                self._quick_headline_check()

            # V7.2: Periodic state persist every loop cycle.
            # PositionManager persists on every mutation, but if Core gets
            # SIGKILL'd between mutations the state file goes stale. This
            # ensures state is at most ~60s old when a force-kill hits.
            # Snapshot the dict to avoid RuntimeError if PositionManager
            # mutates it concurrently in an EventBus handler thread.
            if self.positions:
                try:
                    from .state import save_state
                    pos_snapshot = dict(self.positions)
                    reclaimed_snapshot = set(self._reclaimed_today)
                    trade_snapshot = list(self.trade_log)
                    save_state(pos_snapshot, reclaimed_snapshot, trade_snapshot)
                except Exception:
                    pass  # best-effort; PositionManager is the primary writer

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
            # V8: Don't fall back to full_df (leaks yesterday's data → wrong VWAP)
            log.warning("Date filter failed for %s: %s — skipping ticker", ticker, exc)
            return None
        if today_df.empty:
            return None

        # V8: Drop NaN rows and zero-volume bars before emitting
        # NaN propagates to VWAP/RSI/ATR → garbage signals
        # Zero volume causes RVOL division by zero
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        available = [c for c in required_cols if c in today_df.columns]
        if len(available) < 5:
            return None
        today_df = today_df.dropna(subset=available)
        today_df = today_df[today_df['volume'] > 0]
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

    # ── V8: Pop periodic scanning ──────────────────────────────────────────

    def _refresh_pop_tickers(self):
        """V8: Every 10 min — scan alt_data_cache for momentum tickers."""
        try:
            from data_sources.alt_data_reader import alt_data
            cache = alt_data.get_all() if hasattr(alt_data, 'get_all') else None
            if not cache:
                return

            new_count = 0
            per_ticker = cache.get('per_ticker', {})
            for ticker, data in per_ticker.items():
                if ticker in self.tickers:
                    continue
                # Simple Pop rules: high RVOL or high sentiment
                rvol = data.get('rvol', 1.0)
                sentiment = data.get('sentiment_delta', 0)
                if rvol > 3.0 or abs(sentiment) > 0.3:
                    self.tickers.append(ticker)
                    new_count += 1

            if new_count:
                log.info("[PopScan] Added %d momentum tickers to universe (%d total)",
                         new_count, len(self.tickers))
        except Exception as exc:
            log.debug("[PopScan] Full scan failed: %s", exc)

    def _quick_headline_check(self):
        """V8: Every 2 min — check for breaking news spikes."""
        try:
            from data_sources.alt_data_reader import alt_data
            headlines = alt_data.recent_headlines(5) if hasattr(alt_data, 'recent_headlines') else None
            if not headlines:
                return

            for ticker, count in headlines.items():
                if count >= 3 and ticker not in self.tickers:
                    self.tickers.append(ticker)
                    log.info("[PopScan] Breaking news: %s (%d headlines in 5 min) — added",
                             ticker, count)
        except Exception as exc:
            log.debug("[PopScan] Headline check failed: %s", exc)

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
        # V8: Actually email the EOD summary (was hardcoded to None → log only)
        EODSummary.send(self.trade_log, alert_email=self._alert_email)
        self._durable_log.close()   # flush remaining Redpanda messages
        self._bus.shutdown()        # drain async dispatchers (no-op in sync mode)
        _remove_lock()
        log.info("Real-time monitoring stopped.")
