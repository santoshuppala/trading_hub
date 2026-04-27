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
        self._trading_client = trading_client  # V8: stored for live reconciliation
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

    # ── V9: Component injection for FillLedger / BrokerRegistry / EquityTracker ──

    def set_fill_ledger(self, ledger) -> None:
        """Attach FillLedger for shadow mode position tracking."""
        self._fill_ledger = ledger
        # Wire into PositionManager
        if hasattr(self, '_pos_manager'):
            self._pos_manager._ledger = ledger
        # V10: Wire into StateEngine as P&L authority
        if hasattr(self, '_state_engine'):
            self._state_engine.set_fill_ledger(ledger)
        log.info("[Monitor] FillLedger attached (shadow mode)")

    def set_broker_registry(self, registry) -> None:
        """Attach BrokerRegistry for extensible broker iteration."""
        self._broker_registry = registry
        log.info("[Monitor] BrokerRegistry attached (%d brokers)", len(registry))

    def set_equity_tracker(self, tracker) -> None:
        """Attach EquityTracker for hourly P&L drift detection."""
        self._equity_tracker = tracker
        log.info("[Monitor] EquityTracker attached")

    def set_stream_client(self, stream_client) -> None:
        """Attach TradierStreamClient for instant price lookups in RiskEngine."""
        if hasattr(self, '_risk') and self._risk:
            self._risk.set_stream_client(stream_client)
        self._stream_client = stream_client
        log.info("[Monitor] Stream client attached to RiskEngine")

    def set_bar_builder(self, bar_builder) -> None:
        """Attach BarBuilder for sub-second BAR delivery from WebSocket ticks.

        Once seeded (after first REST cycle), BarBuilder emits BAR events
        for tickers with active WebSocket ticks. REST BAR emission is skipped
        for those tickers to avoid double evaluation.
        """
        self._bar_builder = bar_builder
        log.info("[Monitor] BarBuilder attached — will seed after first REST fetch")

    # ── V9 (R3): Stale order cleanup ────────────────────────────────────────

    def _cleanup_stale_open_orders(self) -> None:
        """Cancel open orders older than 60 seconds at both brokers.

        Prevents orphaned orders from accumulating after partial fills,
        network timeouts, or process crashes.
        """
        import requests as _req
        from datetime import datetime, timedelta

        cancelled = 0
        now = datetime.now(ET)
        cutoff = now - timedelta(seconds=60)

        # Alpaca: cancel stale orders
        try:
            tc = getattr(self, '_trading_client', None)
            if tc:
                from alpaca.trading.requests import GetOrdersRequest
                from alpaca.trading.enums import QueryOrderStatus
                open_orders = tc.get_orders(
                    filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
                for order in open_orders:
                    created = order.created_at
                    if created and created.replace(tzinfo=None) < cutoff.replace(tzinfo=None):
                        try:
                            tc.cancel_order_by_id(str(order.id))
                            cancelled += 1
                            log.info("[order-cleanup] Cancelled stale Alpaca order: %s %s %s",
                                     order.side, order.symbol, order.id)
                        except Exception as _ce:
                            log.warning("[order-cleanup] Alpaca cancel failed for %s: %s",
                                        order.id, _ce)
        except Exception as exc:
            log.warning("[order-cleanup] Alpaca cleanup failed: %s", exc)

        # Tradier: cancel stale orders
        try:
            t_token = os.getenv('TRADIER_SANDBOX_TOKEN', '') or os.getenv('TRADIER_TOKEN', '')
            t_acct = os.getenv('TRADIER_ACCOUNT_ID', '')
            if t_token and t_acct:
                t_sandbox = os.getenv('TRADIER_SANDBOX', 'true').lower() == 'true'
                t_base = 'https://sandbox.tradier.com' if t_sandbox else 'https://api.tradier.com'
                t_headers = {'Authorization': f'Bearer {t_token}', 'Accept': 'application/json'}
                resp = _req.get(f'{t_base}/v1/accounts/{t_acct}/orders',
                                headers=t_headers, timeout=5)
                if resp.status_code == 200:
                    orders_data = resp.json().get('orders', {})
                    if orders_data and orders_data != 'null':
                        order_list = orders_data.get('order', [])
                        if isinstance(order_list, dict):
                            order_list = [order_list]
                        for o in order_list:
                            status = o.get('status', '')
                            if status in ('pending', 'open', 'partially_filled'):
                                created = o.get('create_date', '')
                                if created and created < cutoff.isoformat():
                                    oid = o.get('id')
                                    if oid:
                                        _req.delete(
                                            f'{t_base}/v1/accounts/{t_acct}/orders/{oid}',
                                            headers=t_headers, timeout=5)
                                        cancelled += 1
                                        log.info("[order-cleanup] Cancelled stale Tradier order: %s",
                                                 oid)
        except Exception as exc:
            log.warning("[order-cleanup] Tradier cleanup failed: %s", exc)

        if cancelled:
            log.info("[order-cleanup] Cancelled %d stale orders", cancelled)

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
            log.warning("[reconcile] ATR lookup failed for %s: %s", ticker, exc)
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
                # V8: Dual-broker overlap — merge into one logical position
                # Track per-broker qty so SmartRouter can split sells correctly
                if existing_broker and existing_broker != broker_name:
                    pos = self.positions[ticker]
                    existing_qty = pos.get('quantity', 0)
                    combined_qty = existing_qty + qty
                    broker_qty = pos.get('_broker_qty', {existing_broker: existing_qty})
                    broker_qty[broker_name] = qty
                    pos['_broker_qty'] = broker_qty
                    pos['quantity'] = combined_qty
                    pos['qty'] = combined_qty
                    # Primary broker = larger qty (for stop/target routing)
                    if qty > existing_qty:
                        pos['_broker'] = broker_name
                        pos['entry_price'] = avg_entry  # use larger broker's cost basis
                    log.warning(
                        "[reconcile] DUAL-BROKER MERGE for %s: %s=%d + %s=%d = %d total. "
                        "Primary broker: %s. SmartRouter will split sells.",
                        ticker, existing_broker, existing_qty,
                        broker_name, qty, combined_qty, pos['_broker'])
                    reconciled += 1
                    log.info(
                        "[reconcile] %s: verified at %s+%s (qty=%d entry=$%.2f). "
                        "Local stop=$%.2f target=$%.2f strategy=%s",
                        ticker, existing_broker, broker_name, combined_qty,
                        pos.get('entry_price', 0),
                        pos.get('stop_price', 0), pos.get('target_price', 0),
                        pos.get('strategy', 'unknown'))
                    return

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

                # 2. Entry price — broker cost basis is ALWAYS authoritative (V9)
                # Removed 1% drift threshold — any difference means local is wrong.
                # This fixes the $3,098 P&L gap from Apr 17 where reconciled
                # positions had ATR-estimated entries instead of actual cost basis.
                local_entry = pos.get('entry_price', 0)
                if avg_entry > 0 and local_entry > 0 and avg_entry != local_entry:
                    drift_pct = abs(avg_entry - local_entry) / local_entry
                    log.info(
                        "[reconcile] %s entry_price sync: local=$%.2f → "
                        "%s=$%.2f (%.2f%% drift) — using broker cost basis",
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
                strategy     = db_pos.get('strategy') or f'{broker_name}_reconciled'
                atr_value    = db_pos.get('atr_value')
                entry_time   = db_pos.get('entry_time', f'{broker_name}_restored')
                order_id     = db_pos.get('order_id') or f'{broker_name}_reconciliation'
                source = 'db'
            else:
                strategy = f'{broker_name}_reconciled'
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

            # V9: Shadow FillLedger — create synthetic BUY lot for imported position
            if hasattr(self, '_fill_ledger') and self._fill_ledger:
                try:
                    import uuid as _uuid
                    from .fill_lot import FillLot, PositionMeta
                    synth_lot = FillLot(
                        lot_id=str(_uuid.uuid4()),
                        ticker=ticker, side='BUY', qty=float(qty),
                        fill_price=avg_entry,
                        timestamp=datetime.now(ET),
                        order_id=order_id or f'{broker_name}_reconciliation',
                        broker=broker_name,
                        strategy=strategy,
                        reason='broker_reconciliation',
                        synthetic=True,
                        signal_price=avg_entry,
                        init_stop=stop_price,
                        init_target=target_price,
                        init_atr=atr_value,
                    )
                    self._fill_ledger.append(synth_lot)
                    self._fill_ledger.set_meta(ticker, PositionMeta(
                        stop_price=stop_price,
                        target_price=target_price,
                        half_target=half_target,
                        atr_value=atr_value,
                    ))
                except Exception as le:
                    log.warning("[reconcile] Shadow ledger import failed for %s: %s",
                              ticker, le)

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
        # Remove local positions not at ANY broker.
        # V9: Record missing SELLs in trade_log + FillLedger so P&L is tracked.
        # Queries ALL brokers for actual fill price — no P&L goes unrecorded.
        # V10: Dedup — skip tickers already externally closed in trade_log today.
        # Without this, every restart re-discovers DB-recovered phantom positions
        # and records duplicate external closes (ISRG bug: 5x on Apr 24).
        _already_closed = {t.get('ticker') for t in self.trade_log
                          if 'external_close' in t.get('reason', '')}
        all_broker_tickers = alpaca_tickers | tradier_tickers
        stale = [t for t in list(self.positions.keys()) if t not in all_broker_tickers]

        # Silently remove positions already closed in a prior restart
        for ticker in [t for t in stale if t in _already_closed]:
            del self.positions[ticker]
            log.info("[reconcile] %s already closed (dedup) — removing from state", ticker)

        stale = [t for t in stale if t not in _already_closed]
        for ticker in stale:
            pos = self.positions[ticker]
            entry_price = pos.get('entry_price', 0)
            qty = pos.get('quantity', pos.get('qty', 0))
            strategy = pos.get('strategy', 'unknown')
            pos_broker = pos.get('broker', 'unknown')

            # ── Get actual sell price from ALL brokers' order history ────
            sell_price = 0
            sell_source = 'unknown'

            # Try Tradier order history
            try:
                import requests as _req
                _t_token = os.getenv('TRADIER_SANDBOX_TOKEN', '')
                _t_acct = os.getenv('TRADIER_ACCOUNT_ID', '')
                if _t_token and _t_acct:
                    _t_sandbox = os.getenv('TRADIER_SANDBOX', 'true').lower() == 'true'
                    _t_base = 'https://sandbox.tradier.com' if _t_sandbox else 'https://api.tradier.com'
                    _t_headers = {'Authorization': f'Bearer {_t_token}', 'Accept': 'application/json'}
                    _resp = _req.get(f'{_t_base}/v1/accounts/{_t_acct}/orders',
                                     headers=_t_headers, timeout=5)
                    if _resp.status_code == 200:
                        _orders = _resp.json().get('orders', {})
                        if _orders and _orders != 'null':
                            _order_list = _orders.get('order', [])
                            if isinstance(_order_list, dict):
                                _order_list = [_order_list]
                            for _o in reversed(_order_list):
                                if (_o.get('symbol') == ticker
                                        and _o.get('side') == 'sell'
                                        and _o.get('status') == 'filled'
                                        and _o.get('avg_fill_price')):
                                    sell_price = float(_o['avg_fill_price'])
                                    sell_source = 'tradier_order_history'
                                    break
            except Exception as _e:
                log.warning("[reconcile] Tradier order history check failed for %s: %s", ticker, _e)

            # Try Alpaca order history
            if sell_price == 0:
                try:
                    _alpaca = getattr(self, '_broker', None)
                    if _alpaca and hasattr(_alpaca, '_client') and _alpaca._client:
                        from alpaca.trading.requests import GetOrdersRequest
                        from alpaca.trading.enums import QueryOrderStatus, OrderSide
                        _orders = _alpaca._client.get_orders(
                            filter=GetOrdersRequest(
                                status=QueryOrderStatus.CLOSED,
                                symbols=[ticker],
                                limit=10,
                            ))
                        for _o in _orders:
                            if (_o.side == OrderSide.SELL
                                    and _o.filled_avg_price is not None
                                    and float(_o.filled_qty or 0) > 0):
                                sell_price = float(_o.filled_avg_price)
                                sell_source = 'alpaca_order_history'
                                break
                except Exception as _e:
                    log.warning("[reconcile] Alpaca order history check failed for %s: %s", ticker, _e)

            # Fallback: current market price from cache
            if sell_price == 0:
                try:
                    bars = self._bars_cache.get(ticker)
                    if bars is not None and not bars.empty:
                        sell_price = float(bars['close'].iloc[-1])
                        sell_source = 'market_price_fallback'
                except Exception:
                    pass

            # Last resort: entry price (break-even assumption)
            if sell_price == 0:
                sell_price = entry_price
                sell_source = 'entry_price_fallback'

            pnl = round((sell_price - entry_price) * qty, 2)
            log.warning(
                "[reconcile] EXTERNAL CLOSE %s: entry=$%.2f exit=$%.2f qty=%s "
                "pnl=$%+.2f source=%s broker=%s",
                ticker, entry_price, sell_price, qty, pnl, sell_source, pos_broker)

            # Record in trade_log
            self.trade_log.append({
                'ticker': ticker,
                'entry_price': entry_price,
                'exit_price': sell_price,
                'quantity': qty,
                'pnl': pnl,
                'strategy': strategy,
                'reason': f'external_close:{sell_source}',
                'broker': pos_broker,
            })

            # Record SELL lot in FillLedger
            if hasattr(self, '_fill_ledger') and self._fill_ledger:
                try:
                    from monitor.fill_lot import FillLot
                    from datetime import datetime, timezone
                    import uuid
                    sell_lot = FillLot(
                        lot_id=str(uuid.uuid4()),
                        ticker=ticker,
                        side='SELL',
                        qty=float(qty),
                        fill_price=sell_price,
                        order_id=f'reconcile_{sell_source}',
                        broker=pos_broker,
                        strategy=strategy,
                        reason=f'external_close:{sell_source}',
                        timestamp=datetime.now(timezone.utc),
                    )
                    matches = self._fill_ledger.append(sell_lot)
                    ledger_pnl = sum(m.realized_pnl for m in matches) if matches else 0
                    log.info("[FillLedger] External SELL: %s %s@$%.2f → %d matches, pnl=$%.2f",
                             ticker, qty, sell_price, len(matches), ledger_pnl)

                    # Cross-check: FillLedger P&L vs trade_log P&L
                    if abs(ledger_pnl - pnl) > 0.02:
                        log.warning("[reconcile] P&L MISMATCH %s: trade_log=$%.2f vs ledger=$%.2f",
                                    ticker, pnl, ledger_pnl)
                except Exception as e:
                    log.warning("[FillLedger] External SELL record failed for %s: %s", ticker, e)

            # Alert
            send_alert(self._alert_email,
                       f"EXTERNAL CLOSE: {ticker} {qty} shares @ ${sell_price:.2f} "
                       f"(entry ${entry_price:.2f}, PnL ${pnl:+.2f})\n"
                       f"Source: {sell_source} | Broker: {pos_broker} | Strategy: {strategy}",
                       severity='WARNING')

            del self.positions[ticker]
            reconciled += 1

        if reconciled:
            log.warning(
                f"[reconcile] Reconciled {reconciled} position(s). "
                f"Verify stop/target prices — defaults are ±3%/±5% from avg entry."
            )
        else:
            log.info("[reconcile] Local positions are in sync with brokers.")

        # ── 4. Open order validation ─────────────────────────────────────
        # Cancel stale/orphaned orders at both brokers that don't correspond
        # to any tracked position. Prevents "more shares than position" rejections.
        try:
            self._reconcile_open_orders(trading_client, all_broker_tickers)
        except Exception as exc:
            log.warning("[reconcile] Open order cleanup failed (non-fatal): %s", exc)

        # V7.2: Return broker ticker sets so SmartRouter can be re-seeded
        return alpaca_tickers, tradier_tickers

    def _reconcile_open_orders(self, trading_client, tracked_tickers: set) -> None:
        """V8: Cancel stale open orders at both brokers during reconciliation.

        Stale orders (from previous sessions, crashed processes, or bracket stops)
        can block new sells with 'more shares than position' errors.
        Only cancels orders for tickers NOT in our tracked positions, or
        stop/sell orders that exceed tracked position qty.
        """
        import requests as _req

        # ── Alpaca open orders ────────────────────────────────────────────
        try:
            if trading_client:
                from alpaca.trading.requests import GetOrdersRequest
                from alpaca.trading.enums import QueryOrderStatus
                open_orders = trading_client.get_orders(
                    filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
                cancelled = 0
                for o in open_orders:
                    ticker = str(o.symbol)
                    # Cancel sell orders for tickers we don't track
                    if ticker not in self.positions and str(o.side).upper() == 'SELL':
                        trading_client.cancel_order_by_id(str(o.id))
                        cancelled += 1
                    # Cancel stop orders that exceed our tracked qty
                    elif ticker in self.positions and str(o.type) in ('stop', 'stop_limit'):
                        pos_qty = self.positions[ticker].get('quantity', 0)
                        order_qty = int(float(o.qty or 0))
                        if order_qty > pos_qty:
                            trading_client.cancel_order_by_id(str(o.id))
                            cancelled += 1
                            log.info("[reconcile] Cancelled oversized Alpaca stop for %s: "
                                     "order_qty=%d > pos_qty=%d", ticker, order_qty, pos_qty)
                if cancelled:
                    log.info("[reconcile] Cancelled %d stale Alpaca orders", cancelled)
                else:
                    log.info("[reconcile] Alpaca orders: all in sync")
        except Exception as exc:
            log.warning("[reconcile] Alpaca order cleanup failed: %s", exc)

        # ── Tradier open orders ───────────────────────────────────────────
        try:
            t_token = os.getenv('TRADIER_SANDBOX_TOKEN', '') or os.getenv('TRADIER_TOKEN', '')
            t_acct = os.getenv('TRADIER_ACCOUNT_ID', '')
            if t_token and t_acct:
                t_sandbox = os.getenv('TRADIER_SANDBOX', 'true').lower() == 'true'
                t_base = 'https://sandbox.tradier.com' if t_sandbox else 'https://api.tradier.com'
                t_headers = {'Authorization': f'Bearer {t_token}', 'Accept': 'application/json'}
                resp = _req.get(f'{t_base}/v1/accounts/{t_acct}/orders',
                               headers=t_headers, timeout=10)
                if resp.status_code == 200:
                    orders = resp.json().get('orders', {})
                    if orders and orders != 'null':
                        order_list = orders.get('order', [])
                        if isinstance(order_list, dict):
                            order_list = [order_list]
                        cancelled = 0
                        for o in order_list:
                            status = o.get('status', '')
                            if status not in ('pending', 'open', 'partially_filled'):
                                continue
                            ticker = o.get('symbol', '')
                            oid = o.get('id')
                            side = o.get('side', '')
                            order_qty = int(float(o.get('quantity', 0)))
                            if not oid:
                                continue
                            # Cancel sell orders for tickers we don't track
                            if ticker not in self.positions and side in ('sell', 'sell_short'):
                                _req.delete(f'{t_base}/v1/accounts/{t_acct}/orders/{oid}',
                                           headers=t_headers, timeout=5)
                                cancelled += 1
                            # Cancel stop orders that exceed tracked qty
                            elif ticker in self.positions and o.get('type') == 'stop':
                                pos_qty = self.positions[ticker].get('quantity', 0)
                                if order_qty > pos_qty:
                                    _req.delete(f'{t_base}/v1/accounts/{t_acct}/orders/{oid}',
                                               headers=t_headers, timeout=5)
                                    cancelled += 1
                                    log.info("[reconcile] Cancelled oversized Tradier stop for %s: "
                                             "order_qty=%d > pos_qty=%d", ticker, order_qty, pos_qty)
                        if cancelled:
                            log.info("[reconcile] Cancelled %d stale Tradier orders", cancelled)
                        else:
                            log.info("[reconcile] Tradier orders: all in sync")
        except Exception as exc:
            log.warning("[reconcile] Tradier order cleanup failed: %s", exc)

    # ── Live reconciliation (periodic, lightweight) ────────────────────────

    def _get_phantom_exit_price(self, ticker: str, broker: str, entry_price: float) -> float:
        """V10: Query broker for the actual exit fill price of a phantom position.

        When a position disappears from broker (stop fired externally),
        we need the real fill price — not entry price — for accurate P&L.
        """
        try:
            if broker in ('tradier', 'tradier_reconciled'):
                import requests
                t_token = os.getenv('TRADIER_SANDBOX_TOKEN', '') or os.getenv('TRADIER_TOKEN', '')
                t_acct = os.getenv('TRADIER_ACCOUNT_ID', '')
                t_sandbox = os.getenv('TRADIER_SANDBOX', 'true').lower() == 'true'
                t_base = 'https://sandbox.tradier.com' if t_sandbox else 'https://api.tradier.com'
                if t_token and t_acct:
                    resp = requests.get(
                        f'{t_base}/v1/accounts/{t_acct}/orders',
                        headers={'Authorization': f'Bearer {t_token}', 'Accept': 'application/json'},
                        params={'filter': 'filled'},
                        timeout=5)
                    if resp.ok:
                        orders = resp.json().get('orders', {})
                        if orders and orders != 'null':
                            order_list = orders.get('order', [])
                            if isinstance(order_list, dict):
                                order_list = [order_list]
                            # Find the most recent filled SELL for this ticker
                            for o in reversed(order_list):
                                if (o.get('symbol') == ticker
                                        and o.get('side') == 'sell'
                                        and o.get('status') == 'filled'):
                                    fill_price = float(o.get('avg_fill_price', 0))
                                    if fill_price > 0:
                                        log.info("[live-reconcile] Found broker exit for %s: $%.2f",
                                                 ticker, fill_price)
                                        return fill_price

            elif broker in ('alpaca', 'alpaca_reconciled'):
                tc = getattr(self, '_trading_client', None)
                if tc:
                    from alpaca.trading.requests import GetOrdersRequest
                    from alpaca.trading.enums import QueryOrderStatus, OrderSide
                    orders = tc.get_orders(filter=GetOrdersRequest(
                        status=QueryOrderStatus.CLOSED,
                        symbols=[ticker],
                        side=OrderSide.SELL,
                        limit=5))
                    for o in orders:
                        if str(o.filled_avg_price):
                            fill_price = float(o.filled_avg_price)
                            if fill_price > 0:
                                log.info("[live-reconcile] Found broker exit for %s: $%.2f",
                                         ticker, fill_price)
                                return fill_price

        except Exception as e:
            log.warning("[live-reconcile] Broker exit price lookup failed for %s: %s", ticker, e)

        # Fallback: entry price (P&L = $0, better than wrong number)
        return entry_price

    def _live_reconcile(self) -> None:
        """V8: Periodic lightweight reconciliation during trading hours.

        Compares bot_state positions against actual broker positions every 10 min.
        Fixes three types of drift:
          1. Phantom: in state but not at broker → remove from state
          2. Orphan: at broker but not in state → import into state
          3. Qty mismatch: state qty != broker qty → update state
        """
        import requests as _req

        broker_positions = {}  # {ticker: {'qty': N, 'entry': X, 'broker': name}}

        # ── Fetch Alpaca positions ────────────────────────────────────────
        try:
            tc = getattr(self, '_trading_client', None)
            if tc:
                for p in tc.get_all_positions():
                    sym = str(p.symbol)
                    # Skip options (symbol > 10 chars or contains '/')
                    if len(sym) > 10 or '/' in sym:
                        continue
                    qty = int(float(p.qty or 0))
                    entry = float(p.avg_entry_price or 0)
                    if qty > 0:
                        if sym in broker_positions:
                            # Dual broker — add qty
                            broker_positions[sym]['qty'] += qty
                            broker_positions[sym].setdefault('_broker_qty', {})[
                                'alpaca'] = qty
                        else:
                            broker_positions[sym] = {
                                'qty': qty, 'entry': entry, 'broker': 'alpaca'}
        except Exception as exc:
            log.warning("[live-reconcile] Alpaca fetch failed: %s", exc)
            return  # don't reconcile on partial data

        # ── Fetch Tradier positions ───────────────────────────────────────
        try:
            t_token = os.getenv('TRADIER_SANDBOX_TOKEN', '') or os.getenv('TRADIER_TOKEN', '')
            t_acct = os.getenv('TRADIER_ACCOUNT_ID', '')
            if t_token and t_acct:
                t_sandbox = os.getenv('TRADIER_SANDBOX', 'true').lower() == 'true'
                t_base = 'https://sandbox.tradier.com' if t_sandbox else 'https://api.tradier.com'
                t_headers = {'Authorization': f'Bearer {t_token}', 'Accept': 'application/json'}
                resp = _req.get(f'{t_base}/v1/accounts/{t_acct}/positions',
                               headers=t_headers, timeout=5)
                if resp.status_code == 200:
                    t_data = resp.json().get('positions', {})
                    if t_data and t_data != 'null':
                        t_list = t_data.get('position', [])
                        if isinstance(t_list, dict):
                            t_list = [t_list]
                        for p in t_list:
                            sym = p.get('symbol', '')
                            qty = int(float(p.get('quantity', 0)))
                            entry = float(p.get('cost_basis', 0)) / qty if qty > 0 else 0
                            if qty > 0 and sym:
                                if sym in broker_positions:
                                    broker_positions[sym]['qty'] += qty
                                    broker_positions[sym].setdefault('_broker_qty', {})[
                                        'tradier'] = qty
                                    # Keep larger broker as primary
                                    if qty > broker_positions[sym].get('_broker_qty', {}).get(
                                            broker_positions[sym]['broker'], 0):
                                        broker_positions[sym]['broker'] = 'tradier'
                                        broker_positions[sym]['entry'] = entry
                                else:
                                    broker_positions[sym] = {
                                        'qty': qty, 'entry': entry, 'broker': 'tradier'}
        except Exception as exc:
            log.warning("[live-reconcile] Tradier fetch failed: %s", exc)
            return

        # ── Compare against local state ───────────────────────────────────
        fixes = 0

        # 1. Phantoms — in state but not at any broker
        for ticker in list(self.positions.keys()):
            if ticker not in broker_positions:
                pos = self.positions[ticker]
                entry_price = pos.get('entry_price', 0)
                qty = pos.get('quantity', 0)
                strategy = pos.get('strategy', 'unknown')
                pos_broker = pos.get('_broker', 'unknown')

                # V10: Query broker for actual exit fill price (stop may have fired)
                exit_price = entry_price  # fallback: break-even
                try:
                    exit_price = self._get_phantom_exit_price(ticker, pos_broker, entry_price)
                except Exception as ep_exc:
                    log.warning("[live-reconcile] Could not get exit price for %s: %s", ticker, ep_exc)

                pnl = round((exit_price - entry_price) * qty, 2)
                log.warning("[live-reconcile] PHANTOM %s — in state but not at broker. "
                            "Closing (entry=$%.2f exit=$%.2f qty=%d pnl=$%.2f strategy=%s)",
                            ticker, entry_price, exit_price, qty, pnl, strategy)

                # V10: Record SELL lot in FillLedger with REAL exit price
                if hasattr(self, '_fill_ledger') and self._fill_ledger:
                    try:
                        from monitor.fill_lot import FillLot
                        import uuid
                        sell_lot = FillLot(
                            lot_id=str(uuid.uuid4()),
                            ticker=ticker,
                            side='SELL',
                            qty=float(qty),
                            fill_price=exit_price,
                            timestamp=datetime.now(ET),
                            order_id=f'live_reconcile_phantom',
                            broker=pos_broker,
                            strategy=strategy,
                            reason='phantom_live_reconcile',
                        )
                        matches = self._fill_ledger.append(sell_lot)
                        ledger_pnl = sum(m.realized_pnl for m in matches) if matches else 0
                        log.info("[live-reconcile] FillLedger SELL: %s %s@$%.2f → %d matches, pnl=$%.2f",
                                 ticker, qty, exit_price, len(matches) if matches else 0, ledger_pnl)
                    except Exception as e:
                        log.warning("[live-reconcile] FillLedger phantom SELL failed for %s: %s", ticker, e)

                del self.positions[ticker]

                # V10: Emit POSITION CLOSED so StateEngine + HeartbeatEmitter update
                try:
                    from .events import PositionPayload
                    self._bus.emit(Event(
                        type=EventType.POSITION,
                        payload=PositionPayload(
                            ticker=ticker,
                            action='CLOSED',
                            position=None,
                            pnl=pnl,
                            close_detail={
                                'strategy': strategy,
                                'broker': pos_broker,
                                'reason': 'phantom_live_reconcile',
                                'entry_price': entry_price,
                                'exit_price': exit_price,
                                'qty': qty,
                            },
                        ),
                    ))
                except Exception as _exc:
                    log.warning("[live_reconcile] POSITION emit failed for %s: %s", ticker, _exc)

                fixes += 1

        # 2. Orphans — at broker but not in state
        for ticker, bp in broker_positions.items():
            if ticker not in self.positions:
                log.warning("[live-reconcile] ORPHAN %s — %d shares at %s, not in state. Importing.",
                            ticker, bp['qty'], bp['broker'])
                # Import with ATR-based defaults (same as startup reconciliation)
                atr = self._fetch_last_atr(ticker)
                entry = bp['entry']
                if atr and atr > 0:
                    stop = round(entry - 2.0 * atr, 4)
                    target = round(entry + 3.0 * atr, 4)
                else:
                    stop = round(entry * 0.97, 4)
                    target = round(entry * 1.05, 4)
                self.positions[ticker] = {
                    'entry_price': entry,
                    'entry_time': f'{bp["broker"]}_live_reconcile',
                    'quantity': bp['qty'],
                    'qty': bp['qty'],
                    'partial_done': False,
                    'stop_price': stop,
                    'target_price': target,
                    'half_target': round((entry + target) / 2, 4),
                    'atr_value': atr or round((target - stop) / 2, 4),
                    'strategy': f'{bp["broker"]}_reconciled',
                    '_broker': bp['broker'],
                }
                if '_broker_qty' in bp:
                    self.positions[ticker]['_broker_qty'] = bp['_broker_qty']
                self._reclaimed_today.add(ticker)
                fixes += 1

        # 3. Qty mismatches — state qty != broker qty
        for ticker, bp in broker_positions.items():
            if ticker in self.positions:
                local_qty = self.positions[ticker].get('quantity', 0)
                broker_qty = bp['qty']
                if local_qty != broker_qty:
                    log.warning("[live-reconcile] QTY MISMATCH %s: state=%d broker=%d. Fixing.",
                                ticker, local_qty, broker_qty)
                    self.positions[ticker]['quantity'] = broker_qty
                    self.positions[ticker]['qty'] = broker_qty
                    if '_broker_qty' in bp:
                        self.positions[ticker]['_broker_qty'] = bp['_broker_qty']
                    fixes += 1

        # 4. V9: Entry price — broker cost basis is authoritative
        for ticker, bp in broker_positions.items():
            if ticker in self.positions:
                local_entry = self.positions[ticker].get('entry_price', 0)
                broker_entry = bp.get('entry', 0)
                if broker_entry > 0 and local_entry > 0 and broker_entry != local_entry:
                    log.info("[live-reconcile] ENTRY_PRICE sync %s: local=$%.2f → broker=$%.2f",
                             ticker, local_entry, broker_entry)
                    self.positions[ticker]['entry_price'] = broker_entry
                    fixes += 1

        if fixes:
            log.warning("[live-reconcile] Fixed %d position(s). State now matches brokers.", fixes)
            # Persist immediately
            try:
                from .state import save_state
                save_state(dict(self.positions), set(self._reclaimed_today),
                           list(self.trade_log))
            except Exception:
                pass
            # V10: Alert on reconciliation fixes (phantom/orphan = potential P&L issue)
            try:
                from .alerts import send_alert
                from config import ALERT_EMAIL
                if ALERT_EMAIL:
                    send_alert(
                        ALERT_EMAIL,
                        f"RECONCILIATION: {fixes} position fix(es) applied.\n"
                        f"Local positions: {len(self.positions)}\n"
                        f"Broker positions: {len(broker_positions)}\n"
                        f"Check logs for details.",
                        severity='WARNING',
                    )
            except Exception:
                pass
        else:
            log.info("[live-reconcile] Positions in sync (%d local, %d broker)",
                     len(self.positions), len(broker_positions))

    # ── Run loop ─────────────────────────────────────────────────────────────

    def run(self):
        """Main monitoring loop. Runs in the background thread started by start()."""
        self.running = True
        self._consecutive_loop_errors = 0
        try:
            self._run_loop()
        except Exception as e:
            log.critical(f"[Monitor] Fatal error in run loop: {e}", exc_info=True)
            send_alert(self._alert_email, f"Monitor crashed — manual restart required: {e}")
            self.running = False
            _remove_lock()

    def _run_loop(self):
        """Inner loop — resilient to individual cycle failures."""
        while self.running:
          try:
            self._run_one_cycle()
            self._consecutive_loop_errors = 0
          except Exception as cycle_err:
            self._consecutive_loop_errors = getattr(self, '_consecutive_loop_errors', 0) + 1
            log.error(
                "[Monitor] Cycle error #%d: %s — retrying in 30s",
                self._consecutive_loop_errors, cycle_err, exc_info=True,
            )
            if self._consecutive_loop_errors >= 10:
                log.critical("[Monitor] 10 consecutive cycle errors — escalating")
                send_alert(
                    self._alert_email,
                    f"Monitor stuck: {self._consecutive_loop_errors} consecutive errors. "
                    f"Last: {cycle_err}",
                    severity='CRITICAL',
                )
                self._consecutive_loop_errors = 0  # reset, keep trying
            time.sleep(30)

    def _run_one_cycle(self):
        """Single fetch-analyze-emit cycle. Exceptions propagate to _run_loop."""
        self._reset_daily_state()

        # Refresh momentum screener every 30 minutes
        try:
            self.tickers, self._last_momentum_refresh = self._screener.refresh(
                self.base_tickers,
                self.tickers,
                self._last_momentum_refresh,
            )
        except Exception as e:
            log.warning("[Monitor] Screener refresh failed: %s — using previous tickers", e)
        self._heartbeat.set_n_tickers(len(self.tickers))

        # Single batch API call
        log.info(
            f"[{datetime.now(ET).strftime('%H:%M:%S')}] "
            f"Fetching bars for {len(self.tickers)} tickers …"
        )
        new_bars, new_rvol = self._data.fetch_batch_bars(self.tickers)

        # V8: Merge new data into existing cache (don't replace).
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

        # V10: Track data freshness — detect silent fetch failures
        if new_bars:
            self._last_successful_fetch = time.monotonic()
        elif not hasattr(self, '_last_successful_fetch'):
            self._last_successful_fetch = time.monotonic()

        _fetch_age = time.monotonic() - getattr(self, '_last_successful_fetch', time.monotonic())
        if _fetch_age > 120 and not hasattr(self, '_stale_alert_sent'):
            log.critical("[DataQuality] NO DATA for %.0fs — data pipeline may be dead", _fetch_age)
            send_alert(self._alert_email,
                       f"DATA PIPELINE STALE: No bars fetched for {int(_fetch_age)}s. "
                       f"Check API connectivity and rate limits.",
                       severity='CRITICAL')
            self._stale_alert_sent = True
        elif _fetch_age <= 120:
            self._stale_alert_sent = False  # reset once data flows again

        # V10: Data coverage circuit breaker
        # <30% for 3 consecutive cycles → halt new entries (exits still run)
        # Threshold lowered from 50%→30% because Tradier routinely serves
        # 40-60% of tickers within the 20s batch timeout.
        # Grace period: first 15 min after market open (data sources ramp up slowly)
        if not hasattr(self, '_low_coverage_count'):
            self._low_coverage_count = 0
            self._data_circuit_open = False

        if self.tickers:
            coverage = len(new_bars) / len(self.tickers)
            _now_et = datetime.now(ET)
            _minutes_since_open = (_now_et.hour - 9) * 60 + (_now_et.minute - 30)
            _in_grace = _minutes_since_open < 15  # first 15 min = low coverage normal

            if coverage < 0.30 and not _in_grace:
                self._low_coverage_count += 1
                if self._low_coverage_count >= 3 and not self._data_circuit_open:
                    self._data_circuit_open = True
                    log.critical(
                        "[DataQuality] CIRCUIT BREAKER: %.0f%% coverage for %d cycles — "
                        "halting new entries", coverage * 100, self._low_coverage_count)
                    send_alert(self._alert_email,
                               f"DATA CIRCUIT BREAKER: {coverage:.0%} coverage. "
                               f"New entries halted until data recovers.",
                               severity='CRITICAL')
            elif coverage < 0.90:
                log.warning(
                    "[DataQuality] Only %.0f%% tickers fetched (%d/%d) — data incomplete",
                    coverage * 100, len(new_bars), len(self.tickers))
                self._low_coverage_count = 0
            else:
                if self._data_circuit_open:
                    log.info("[DataQuality] Coverage recovered to %.0f%% — resuming entries",
                             coverage * 100)
                self._low_coverage_count = 0
                self._data_circuit_open = False

        # V8: Tiered scanning — classify tickers before emit_batch
        scan_tickers = self._ticker_scanner.classify(self.tickers, self._bars_cache)

        # V10: Seed BarBuilder from REST cache.
        # First call seeds all tickers with 30+ bars.
        # Subsequent calls (every 5 min) reseed to pick up:
        #   - New tickers that now have enough bars (early session ramp-up)
        #   - Updated RVOL baselines
        #   - History gaps from WebSocket disconnects
        _bb = getattr(self, '_bar_builder', None)
        if _bb and self._bars_cache:
            _reseed_interval = 300  # 5 minutes
            _last_seed = getattr(self, '_last_bb_seed_time', 0)
            _should_seed = (not _bb.is_seeded
                            or (time.monotonic() - _last_seed) > _reseed_interval)
            if _should_seed:
                try:
                    _seeded = _bb.seed_from_cache(
                        self._bars_cache,
                        rvol_cache=getattr(self, '_rvol_cache', None),
                    )
                    self._last_bb_seed_time = time.monotonic()
                    if _seeded > 0:
                        log.info("[Monitor] BarBuilder seeded with %d tickers from REST cache",
                                 _seeded)
                except Exception as exc:
                    log.warning("[Monitor] BarBuilder seeding failed (non-fatal): %s", exc)

        # V10: Update BarBuilder hot tickers (open positions only).
        # Only these get sub-second BAR emission — prevents EventBus queue overflow
        # from 225 tickers × 1 BAR/minute flooding the queue.
        if _bb:
            _bb.set_hot_tickers(set(self.positions.keys()))

        # Emit BAR events for tickers NOT covered by BarBuilder.
        # BarBuilder covers a ticker when: seeded + received WebSocket ticks
        # this minute + builder is active (flush thread alive).
        # Tickers without WebSocket ticks (illiquid) or when streaming is
        # down get REST BAR events as fallback — no ticker is ever left without data.
        _bb_active = _bb and _bb.is_active()
        now   = datetime.now(ET)
        today = now.date()
        bar_events = []
        _skipped_empty = 0
        _skipped_nocache = 0
        _skipped_bb = 0
        for ticker in scan_tickers:
            # V10: Data circuit breaker — only emit for open positions (exits)
            if getattr(self, '_data_circuit_open', False) and ticker not in self.positions:
                continue

            # Skip if BarBuilder is actively covering this ticker
            if _bb_active and _bb.covers_ticker(ticker):
                _skipped_bb += 1
                continue
            ev = self._build_bar_event(ticker, today)
            if ev is not None:
                bar_events.append(ev)
            else:
                if ticker not in self._bars_cache or self._bars_cache[ticker] is None:
                    _skipped_nocache += 1
                else:
                    _skipped_empty += 1
        if bar_events:
            try:
                self._bus.emit_batch(bar_events)
            except Exception as e:
                log.error(f"emit_batch(BAR) failed: {e}")
        if _skipped_bb > 0:
            log.debug("[Monitor] %d tickers covered by BarBuilder (sub-second), "
                      "%d via REST", _skipped_bb, len(bar_events))
        if not bar_events and not _skipped_bb and scan_tickers:
            log.warning(
                "[Monitor] 0 BAR events from %d scan tickers "
                "(no_cache=%d empty_today=%d cache_size=%d)",
                len(scan_tickers), _skipped_nocache, _skipped_empty,
                len(self._bars_cache),
            )

        # Tick heartbeat
        self._heartbeat.tick()

        # V8: Pop periodic scans + ticker conviction ranking + live reconciliation
        if not hasattr(self, '_last_pop_scan'):
            self._last_pop_scan = 0.0
            self._last_headline_check = 0.0
            self._last_ranking = 0.0
            self._last_live_reconcile = 0.0
        now_mono = time.monotonic()
        if now_mono - self._last_pop_scan > 600:
            self._last_pop_scan = now_mono
            self._refresh_pop_tickers()
        if now_mono - self._last_headline_check > 120:
            self._last_headline_check = now_mono
            self._quick_headline_check()
        if now_mono - self._last_ranking > 600:
            self._last_ranking = now_mono
            self._rank_tickers()

        # V10: Live position reconciliation every 5 min (was 10 min)
        # Reduced from 10→5 after April 22 $50 P&L drift across restarts.
        if now_mono - self._last_live_reconcile > 300:
            self._last_live_reconcile = now_mono
            try:
                self._live_reconcile()
            except Exception as exc:
                log.warning("[live-reconcile] failed: %s", exc)

        # V10: Equity drift check every 5 min (was 60 min)
        # Catches P&L divergence between local tracking and broker reality.
        if hasattr(self, '_equity_tracker') and self._equity_tracker:
            if now_mono - getattr(self, '_last_equity_check', 0) > 300:
                self._last_equity_check = now_mono
                try:
                    self._equity_tracker.check_drift()
                except Exception as exc:
                    log.warning("[equity] drift check failed: %s", exc)

        # V10: Sync streaming subscription with current ticker universe.
        # Detects new tickers (momentum scanner, headlines, ranking, new positions)
        # and immediately: (1) subscribes WebSocket, (2) fetches REST bars,
        # (3) seeds BarBuilder — so every ticker on the radar has full data.
        self._sync_stream_and_data()

        # V10: Stale order cleanup every 60s (was 300s — too slow for active trading)
        if now_mono - getattr(self, '_last_order_cleanup', 0) > 60:
            self._last_order_cleanup = now_mono
            try:
                self._cleanup_stale_open_orders()
            except Exception as exc:
                log.warning("[order-cleanup] failed: %s", exc)

        # Periodic state persist
        if self.positions:
            try:
                from .state import save_state
                pos_snapshot = dict(self.positions)
                reclaimed_snapshot = set(self._reclaimed_today)
                trade_snapshot = list(self.trade_log)
                save_state(pos_snapshot, reclaimed_snapshot, trade_snapshot)
            except Exception:
                pass

        # Sleep until next cycle — V9: dual-frequency for HOT tickers
        now = datetime.now(ET)
        market_open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now < market_open_time:
            wait = max(10, (market_open_time - now).total_seconds() - 30)
            log.info(f"Pre-market: sleeping {wait / 60:.1f} min until near open.")
            time.sleep(wait)
        else:
            _hot_interval = 12
            _slow_remaining = 60
            while _slow_remaining > 0 and self.running:
                time.sleep(_hot_interval)
                _slow_remaining -= _hot_interval

                # V10: Use live connection status, not just the one-time flag.
                # If WebSocket disconnects, _stream_client.is_connected goes False
                # → hot REST polling resumes automatically.
                _sc = getattr(self, '_stream_client', None)
                _stream_live = (_sc and _sc.is_connected) if _sc else False
                if not _stream_live:
                    hot_tickers = list(self.positions.keys())
                    if hot_tickers:
                        try:
                            hot_bars, hot_rvol = self._data.fetch_batch_bars(hot_tickers)
                            self._bars_cache.update(hot_bars)
                            self._rvol_cache.update(hot_rvol)
                            today = datetime.now(ET).date()
                            hot_events = [
                                self._build_bar_event(t, today)
                                for t in hot_tickers
                            ]
                            hot_events = [e for e in hot_events if e is not None]
                            if hot_events:
                                self._bus.emit_batch(hot_events)
                                log.debug("[HOT_FETCH] %d tickers refreshed", len(hot_events))
                        except Exception as exc:
                            log.debug("[HOT_FETCH] failed: %s", exc)

                self._heartbeat.tick()

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

    # ── V10: Stream + Data Sync ─────────────────────────────────────────────

    def _sync_stream_and_data(self) -> None:
        """Ensure every ticker on the radar has full data for trade decisions.

        Detects tickers that entered the universe (momentum scanner, headlines,
        ranking, new positions) since last sync and:
          1. Subscribes WebSocket stream → ticks start flowing immediately
          2. Fetches REST bars → bars_cache populated → detectors can fire
          3. Seeds BarBuilder → sub-second BAR delivery for these tickers

        Without this, newly-added tickers sit in the universe with no data
        for up to 60s (next REST cycle) — missing actionable signals.
        """
        _sc = getattr(self, '_stream_client', None)
        _bb = getattr(self, '_bar_builder', None)

        all_tickers = set(self.tickers) | set(self.positions.keys())
        prev_tickers = getattr(self, '_last_stream_tickers', set())

        new_tickers = all_tickers - prev_tickers
        if not new_tickers and all_tickers == prev_tickers:
            return  # nothing changed

        # 1. Update WebSocket subscription (includes new + existing)
        if _sc and _sc.is_connected:
            try:
                _sc.update_tickers(all_tickers)
            except Exception as exc:
                log.debug("[stream-sync] subscription update failed: %s", exc)

        # 2. Fetch REST bars immediately for new tickers (don't wait 60s)
        if new_tickers:
            try:
                new_bars, new_rvol = self._data.fetch_batch_bars(list(new_tickers))
                self._bars_cache.update(new_bars)
                self._rvol_cache.update(new_rvol)
                log.info("[stream-sync] Fetched bars for %d new tickers: %s",
                         len(new_tickers), sorted(new_tickers))
            except Exception as exc:
                log.warning("[stream-sync] REST fetch for new tickers failed: %s", exc)

            # 3. Seed BarBuilder for new tickers immediately
            if _bb and _bb.is_seeded:
                try:
                    # Only seed the new tickers (not full cache)
                    new_cache = {t: self._bars_cache[t]
                                 for t in new_tickers
                                 if t in self._bars_cache}
                    new_rvol_cache = {t: self._rvol_cache.get(t)
                                      for t in new_tickers
                                      if t in self._rvol_cache}
                    if new_cache:
                        _seeded = _bb.seed_from_cache(new_cache, new_rvol_cache)
                        if _seeded:
                            log.info("[stream-sync] BarBuilder seeded %d new tickers",
                                     _seeded)
                except Exception as exc:
                    log.debug("[stream-sync] BarBuilder seed for new tickers failed: %s", exc)

        self._last_stream_tickers = all_tickers

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

    def _rank_tickers(self):
        """V8: Rank all tickers by conviction using hedge-fund-grade signals.

        Logs top tickers with reasoning. Rankings influence:
        - Which tickers get priority in tiered scanning
        - Position sizing via conviction multiplier
        - Strategy selection (momentum vs swing vs mean_reversion)
        """
        try:
            from data_sources.ticker_ranking import rank_tickers, TickerRanking
            rankings = rank_tickers(self.tickers)
            top = [r for r in rankings if r.conviction > 0.2][:15]

            if top:
                log.info("[TickerRanking] Top %d by conviction:", len(top))
                for r in top[:10]:
                    log.info("  %s conv=%.2f %s %s | %s",
                             r.ticker, r.conviction, r.strategy,
                             r.entry_urgency, r.reasoning[:60])

                # Store rankings for use by tiered scanner (hot tickers)
                self._conviction_rankings = {r.ticker: r for r in rankings}

                # Add 'immediate' urgency tickers to the hot set if not already scanning
                for r in top:
                    if r.entry_urgency == 'immediate' and r.ticker not in self.tickers:
                        self.tickers.append(r.ticker)
                        log.info("[TickerRanking] Added %s (conv=%.2f, %s) to scan universe",
                                 r.ticker, r.conviction, r.catalyst)
        except Exception as exc:
            log.debug("[TickerRanking] Ranking failed: %s", exc)

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
