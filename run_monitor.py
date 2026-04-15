"""
Standalone monitor launcher — runs without Streamlit UI.
Scheduled daily at 6:00 AM PST (9:00 AM ET, 30 min before open).
Stops at 4:00 PM ET (market close). New entries blocked at 3:00 PM, exits monitored until close.
"""
import os
import signal
import sys
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

# SIGTERM handler: convert to KeyboardInterrupt so the finally block runs
# and atexit callbacks fire (which removes the lock file).
def _handle_sigterm(signum, frame):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, _handle_sigterm)

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))
from monitor import RealTimeMonitor
from config import (
    TICKERS, STRATEGY, STRATEGY_PARAMS,
    OPEN_COST, CLOSE_COST, MAX_POSITIONS, ORDER_COOLDOWN, TRADE_BUDGET,
    ALERT_EMAIL, ALPACA_API_KEY, ALPACA_SECRET, TRADIER_TOKEN,
    PAPER_TRADING, DATA_SOURCE, BROKER,
    ALPACA_POPUP_KEY, ALPACA_PUPUP_SECRET_KEY,
    POP_PAPER_TRADING, POP_MAX_POSITIONS, POP_TRADE_BUDGET, POP_ORDER_COOLDOWN,
    PRO_MAX_POSITIONS, PRO_TRADE_BUDGET, PRO_ORDER_COOLDOWN,
    ALPACA_OPTIONS_KEY, ALPACA_OPTIONS_SECRET,
    OPTIONS_PAPER_TRADING, OPTIONS_MAX_POSITIONS, OPTIONS_TRADE_BUDGET,
    OPTIONS_TOTAL_BUDGET, OPTIONS_ORDER_COOLDOWN,
    OPTIONS_MIN_DTE, OPTIONS_MAX_DTE, OPTIONS_LEAPS_DTE,
    GLOBAL_MAX_POSITIONS,
    DB_ENABLED, DATABASE_URL,
)
from monitor.position_registry import registry
from monitor.event_bus import EventType, Event

# ── Logging ───────────────────────────────────────────────────────────────────
log_dir = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"monitor_{datetime.now().strftime('%Y-%m-%d')}.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── Main ──────────────────────────────────────────────────────────────────────
def _fetch_account_snapshot(api_key, api_secret, paper):
    """Fetch equity, last_equity (previous close), and cash from Alpaca account API."""
    import requests as _req
    base = 'https://paper-api.alpaca.markets' if paper else 'https://api.alpaca.markets'
    r = _req.get(
        f'{base}/v2/account',
        headers={'APCA-API-KEY-ID': api_key, 'APCA-API-SECRET-KEY': api_secret},
        timeout=10,
    )
    r.raise_for_status()
    acct = r.json()
    return {
        'equity':       float(acct.get('equity', 0)),
        'last_equity':  float(acct.get('last_equity', 0)),   # previous close equity
        'cash':         float(acct.get('cash', 0)),
        'buying_power': float(acct.get('buying_power', 0)),
        'portfolio_value': float(acct.get('portfolio_value', 0)),
        'status':       acct.get('status', '?'),
    }


def _fetch_daily_fees(api_key, api_secret, paper, date_str=None):
    """Fetch total regulatory + commission fees from Alpaca account activities for a given date.

    Returns dict with 'total_fees', 'fee_count', and 'fee_details' list.
    Alpaca activity types: FEE (regulatory), CFEE (commission).
    """
    import requests as _req
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if date_str is None:
        date_str = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')

    base = 'https://paper-api.alpaca.markets' if paper else 'https://api.alpaca.markets'
    headers = {'APCA-API-KEY-ID': api_key, 'APCA-API-SECRET-KEY': api_secret}

    total_fees = 0.0
    fee_details = []

    for activity_type in ('FEE', 'CFEE'):
        try:
            r = _req.get(
                f'{base}/v2/account/activities/{activity_type}',
                headers=headers,
                params={'date': date_str, 'direction': 'desc', 'page_size': 100},
                timeout=10,
            )
            if r.status_code == 200:
                activities = r.json()
                for act in activities:
                    amount = abs(float(act.get('net_amount', 0) or act.get('qty', 0) or 0))
                    total_fees += amount
                    fee_details.append({
                        'type': activity_type,
                        'amount': amount,
                        'symbol': act.get('symbol', ''),
                        'description': act.get('description', ''),
                    })
        except Exception:
            pass  # fees are best-effort; don't crash the monitor

    return {
        'total_fees': round(total_fees, 4),
        'fee_count':  len(fee_details),
        'fee_details': fee_details,
    }


def _fetch_filled_orders(api_key, api_secret, paper, date_str=None):
    """Fetch all filled orders from Alpaca for a given date.

    Returns list of dicts with ticker, side, qty, filled_avg_price, filled_at.
    Used to detect orphaned/missed trades not captured in the monitor's trade_log.
    """
    import requests as _req
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if date_str is None:
        date_str = datetime.now(ZoneInfo('America/New_York')).strftime('%Y-%m-%d')

    base = 'https://paper-api.alpaca.markets' if paper else 'https://api.alpaca.markets'
    headers = {'APCA-API-KEY-ID': api_key, 'APCA-API-SECRET-KEY': api_secret}

    filled_orders = []
    try:
        r = _req.get(
            f'{base}/v2/orders',
            headers=headers,
            params={
                'status': 'closed',
                'after': f'{date_str}T00:00:00Z',
                'until': f'{date_str}T23:59:59Z',
                'limit': 500,
                'direction': 'asc',
            },
            timeout=15,
        )
        if r.status_code == 200:
            for order in r.json():
                if order.get('status') == 'filled' and order.get('filled_qty'):
                    filled_orders.append({
                        'id':         order.get('id', ''),
                        'ticker':     order.get('symbol', ''),
                        'side':       order.get('side', '').upper(),
                        'qty':        int(float(order.get('filled_qty', 0))),
                        'fill_price': float(order.get('filled_avg_price', 0)),
                        'filled_at':  order.get('filled_at', ''),
                        'order_type': order.get('type', ''),
                    })
    except Exception:
        pass  # best-effort; don't crash the monitor

    return filled_orders


class KillSwitchGuard:
    """Check kill switch on every FILL/POSITION event -- fires within ms, not 60s."""

    def __init__(self, bus, max_daily_loss, monitor, log):
        self.max_daily_loss = max_daily_loss
        self.monitor = monitor
        self.halted = False
        self._log = log
        bus.subscribe(EventType.FILL, self._on_fill, priority=0)
        bus.subscribe(EventType.POSITION, self._on_position, priority=0)

    def _on_fill(self, event):
        self._check()

    def _on_position(self, event):
        if hasattr(event.payload, 'action') and event.payload.action == 'CLOSED':
            self._check()

    def _check(self):
        if self.halted:
            return
        trades = self.monitor.trade_log
        total_pnl = sum(t['pnl'] for t in trades)
        if total_pnl <= self.max_daily_loss:
            self.halted = True
            wins = sum(1 for t in trades if t['is_win'])
            self._log.error(
                f"KILL SWITCH (immediate): daily P&L ${total_pnl:+.2f} "
                f"breached ${self.max_daily_loss:+.2f} | "
                f"trades={len(trades)} wins={wins} losses={len(trades) - wins}"
            )


def main():
    import asyncio
    import hashlib
    import json
    import threading

    # Initialize global position registry with aggregate limit
    registry._global_max = GLOBAL_MAX_POSITIONS
    log.info(f"Starting monitor | Strategy: {STRATEGY} | Tickers: {len(TICKERS)} | Paper: {PAPER_TRADING} | Data: {DATA_SOURCE} | GlobalMaxPositions: {GLOBAL_MAX_POSITIONS}")

    # ── Database layer (optional — runs in a background asyncio loop) ─────────
    db_loop    = None
    db_thread  = None
    db_writer  = None
    db_sub     = None
    db_session = None

    if DB_ENABLED:
        try:
            from db import init_db, close_db, get_pool
            from db.writer import DBWriter, init_writer
            from db.event_sourcing_subscriber import EventSourcingSubscriber
            from db.writer import SessionManager

            # Spin up a dedicated asyncio event loop in a daemon thread
            db_loop = asyncio.new_event_loop()

            def _run_db_loop():
                asyncio.set_event_loop(db_loop)
                db_loop.run_forever()

            db_thread = threading.Thread(target=_run_db_loop, name="db-event-loop", daemon=True)
            db_thread.start()

            # Initialize pool + writer on the DB loop
            pool_future = asyncio.run_coroutine_threadsafe(init_db(DATABASE_URL), db_loop)
            pool_future.result(timeout=15)  # wait up to 15s for DB connection

            db_writer = init_writer(db_loop)
            start_future = asyncio.run_coroutine_threadsafe(db_writer.start(), db_loop)
            start_future.result(timeout=5)

            # Start session
            db_session = SessionManager()
            config_hash = hashlib.sha256(
                json.dumps(STRATEGY_PARAMS, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]
            session_future = asyncio.run_coroutine_threadsafe(
                db_session.start(
                    mode="paper" if PAPER_TRADING else "live",
                    broker=BROKER,
                    tickers=list(TICKERS),
                    config_hash=config_hash,
                ),
                db_loop,
            )
            session_id = session_future.result(timeout=5)
            log.info(f"DB layer ready | session={session_id} | pool connected")
        except Exception as exc:
            log.warning(f"DB layer failed to initialize — continuing without persistence: {exc}")
            DB_ENABLED_RUNTIME = False
            db_loop = db_writer = db_sub = db_session = None
        else:
            DB_ENABLED_RUNTIME = True
    else:
        DB_ENABLED_RUNTIME = False
        log.info("DB layer disabled (DB_ENABLED=false)")

    # ── Crash recovery from Redpanda (supplements bot_state.json) ───────────
    redpanda_brokers = os.getenv('REDPANDA_BROKERS', '127.0.0.1:9092')
    try:
        from monitor.event_log import CrashRecovery
        recovery = CrashRecovery(brokers=redpanda_brokers)
        recovered_positions, recovered_trades = recovery.rebuild()
        if recovered_positions:
            log.info(
                f"CrashRecovery: rebuilt {len(recovered_positions)} position(s) "
                f"and {len(recovered_trades)} trade(s) from Redpanda"
            )
    except Exception as exc:
        log.warning(f"CrashRecovery failed (non-fatal — using bot_state.json only): {exc}")
        recovered_positions, recovered_trades = {}, []

    monitor = RealTimeMonitor(
        tickers=TICKERS,
        redpanda_brokers=redpanda_brokers,
        strategy_name=STRATEGY,
        strategy_params=STRATEGY_PARAMS,
        open_cost=OPEN_COST,
        close_cost=CLOSE_COST,
        alert_email=ALERT_EMAIL,
        alpaca_api_key=ALPACA_API_KEY,
        alpaca_secret_key=ALPACA_SECRET,
        tradier_token=TRADIER_TOKEN,
        paper=PAPER_TRADING,
        max_positions=MAX_POSITIONS,
        order_cooldown=ORDER_COOLDOWN,
        trade_budget=TRADE_BUDGET,
        data_source=DATA_SOURCE,
    )

    # ── Merge Redpanda-recovered state into monitor (supplements bot_state.json) ──
    if recovered_positions:
        for ticker, pos in recovered_positions.items():
            if ticker not in monitor.positions:
                monitor.positions[ticker] = pos
                log.info(f"CrashRecovery: restored position {ticker} from Redpanda")
    if recovered_trades:
        existing_ids = {(t['ticker'], t.get('time','')) for t in monitor.trade_log}
        for trade in recovered_trades:
            if (trade['ticker'], trade.get('time','')) not in existing_ids:
                monitor.trade_log.append(trade)

    # ── Wire DB subscriber to EventBus (must happen before monitor.start()) ──
    if DB_ENABLED_RUNTIME and db_writer is not None:
        from db.event_sourcing_subscriber import EventSourcingSubscriber
        db_sub = EventSourcingSubscriber(bus=monitor._bus, writer=db_writer, session_id=session_id)
        db_sub.register()
        log.info("EventSourcingSubscriber registered — all events persisted with correct timestamps")

    # ── Pro-setups engine (11 strategies, shared Alpaca account) ─────────────
    from pro_setups.engine import ProSetupEngine
    pro_engine = ProSetupEngine(
        bus            = monitor._bus,
        max_positions  = PRO_MAX_POSITIONS,
        order_cooldown = PRO_ORDER_COOLDOWN,
        trade_budget   = float(PRO_TRADE_BUDGET),
    )
    log.info(
        f"ProSetupEngine ready | max_positions={PRO_MAX_POSITIONS} | "
        f"budget=${PRO_TRADE_BUDGET} | cooldown={PRO_ORDER_COOLDOWN}s"
    )

    # ── T3.5: Pop-strategy engine (dedicated Alpaca account) ──────────────────
    # Production data sources: Benzinga (news) + StockTwits (social)
    from config import BENZINGA_API_KEY, STOCKTWITS_TOKEN
    news_source = None
    social_source = None

    if BENZINGA_API_KEY:
        from pop_screener.benzinga_news import BenzingaNewsSentimentSource
        news_source = BenzingaNewsSentimentSource(api_key=BENZINGA_API_KEY)
        log.info("Benzinga news adapter loaded (live headlines + sentiment)")
    else:
        log.warning("BENZINGA_API_KEY not set — using mock news source")

    from pop_screener.stocktwits_social import StockTwitsSocialSource
    social_source = StockTwitsSocialSource(
        access_token=STOCKTWITS_TOKEN or None,
    )
    log.info(f"StockTwits social adapter loaded (auth={'token' if STOCKTWITS_TOKEN else 'public'})")

    from pop_strategy_engine import PopStrategyEngine
    pop_engine = PopStrategyEngine(
        bus=monitor._bus,
        pop_alpaca_key=ALPACA_POPUP_KEY,
        pop_alpaca_secret=ALPACA_PUPUP_SECRET_KEY,
        pop_paper=POP_PAPER_TRADING,
        pop_max_positions=POP_MAX_POSITIONS,
        pop_trade_budget=float(POP_TRADE_BUDGET),
        pop_order_cooldown=POP_ORDER_COOLDOWN,
        alert_email=ALERT_EMAIL,
        news_source=news_source,
        social_source=social_source,
    )
    log.info(
        f"PopStrategyEngine ready | paper={POP_PAPER_TRADING} | "
        f"max_positions={POP_MAX_POSITIONS} | budget=${POP_TRADE_BUDGET} | "
        f"news={'benzinga' if news_source else 'mock'} | social=stocktwits"
    )

    # ── T3.7: Options engine (dedicated Alpaca options account) ───────────────────
    from options.engine import OptionsEngine
    options_engine = OptionsEngine(
        bus=monitor._bus,
        options_key=ALPACA_OPTIONS_KEY,
        options_secret=ALPACA_OPTIONS_SECRET,
        paper=OPTIONS_PAPER_TRADING,
        max_positions=OPTIONS_MAX_POSITIONS,
        trade_budget=float(OPTIONS_TRADE_BUDGET),
        total_budget=float(OPTIONS_TOTAL_BUDGET),
        order_cooldown=OPTIONS_ORDER_COOLDOWN,
        min_dte=OPTIONS_MIN_DTE,
        max_dte=OPTIONS_MAX_DTE,
        leaps_dte=OPTIONS_LEAPS_DTE,
        alert_email=ALERT_EMAIL,
    )
    log.info(
        f"OptionsEngine ready | paper={OPTIONS_PAPER_TRADING} | "
        f"max_positions={OPTIONS_MAX_POSITIONS} | budget=${OPTIONS_TRADE_BUDGET}/trade | "
        f"total=${OPTIONS_TOTAL_BUDGET} | DTE={OPTIONS_MIN_DTE}-{OPTIONS_MAX_DTE}"
    )

    # ── Pre-market connectivity check ─────────────────────────────────────────
    from config import MAX_DAILY_LOSS
    log.info("=" * 60)
    log.info("PRE-MARKET CONNECTIVITY CHECK")
    log.info("=" * 60)
    preflight_ok = True

    # Check 1: Tradier data source
    if DATA_SOURCE == 'tradier' and TRADIER_TOKEN:
        try:
            import requests as _req
            r = _req.get('https://api.tradier.com/v1/markets/quotes',
                         params={'symbols': 'SPY', 'greeks': 'false'},
                         headers={'Authorization': f'Bearer {TRADIER_TOKEN}', 'Accept': 'application/json'},
                         timeout=5)
            if r.status_code == 200:
                log.info("  [OK] Tradier API: connected (status 200)")
            else:
                log.warning(f"  [WARN] Tradier API: status {r.status_code}")
        except Exception as e:
            log.error(f"  [FAIL] Tradier API: {e}")
            preflight_ok = False
    else:
        log.info("  [SKIP] Tradier: not configured as data source")

    # Check 2: Alpaca broker
    if ALPACA_API_KEY and ALPACA_SECRET:
        try:
            import requests as _req
            base = os.getenv('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets')
            r = _req.get(f'{base}/v2/account',
                         headers={'APCA-API-KEY-ID': ALPACA_API_KEY, 'APCA-API-SECRET-KEY': ALPACA_SECRET},
                         timeout=5)
            if r.status_code == 200:
                acct = r.json()
                bp = acct.get('buying_power', '?')
                status = acct.get('status', '?')
                is_paper = 'paper' in base
                log.info(f"  [OK] Alpaca: connected | status={status} | buying_power=${bp} | paper={is_paper}")
            else:
                log.error(f"  [FAIL] Alpaca: status {r.status_code}")
                preflight_ok = False
        except Exception as e:
            log.error(f"  [FAIL] Alpaca API: {e}")
            preflight_ok = False
    else:
        log.error("  [FAIL] Alpaca: API keys not set")
        preflight_ok = False

    # Check 3: Benzinga news
    if BENZINGA_API_KEY:
        try:
            import requests as _req
            r = _req.get('https://api.benzinga.com/api/v2/news',
                         params={'token': BENZINGA_API_KEY, 'tickers': 'SPY', 'pageSize': '1'},
                         headers={'Accept': 'application/json'}, timeout=5)
            log.info(f"  [OK] Benzinga API: status {r.status_code}")
        except Exception as e:
            log.warning(f"  [WARN] Benzinga API: {e} (pop screener will use fallback)")
    else:
        log.info("  [SKIP] Benzinga: no API key (pop screener uses mock news)")

    # Check 4: StockTwits
    try:
        import requests as _req
        r = _req.get('https://api.stocktwits.com/api/2/streams/symbol/SPY.json',
                     headers={'User-Agent': 'TradingHub/1.0'}, timeout=5)
        log.info(f"  [OK] StockTwits API: status {r.status_code}")
    except Exception as e:
        log.warning(f"  [WARN] StockTwits API: {e} (pop screener will use neutral sentiment)")

    # Check 5: Redpanda
    try:
        import socket
        rp_host, rp_port = redpanda_brokers.split(':')
        sock = socket.create_connection((rp_host, int(rp_port)), timeout=3)
        sock.close()
        log.info(f"  [OK] Redpanda: reachable at {redpanda_brokers}")
    except Exception as e:
        log.warning(f"  [WARN] Redpanda: {e} (durable logging unavailable)")

    # Check 6: TimescaleDB
    if DB_ENABLED_RUNTIME:
        log.info("  [OK] TimescaleDB: connected (pool initialized)")
    else:
        log.info("  [SKIP] TimescaleDB: disabled or unreachable")

    # Check 7: Alpaca Options account
    if ALPACA_OPTIONS_KEY and ALPACA_OPTIONS_SECRET:
        try:
            import requests as _req
            base = 'https://paper-api.alpaca.markets' if OPTIONS_PAPER_TRADING else 'https://api.alpaca.markets'
            r = _req.get(f'{base}/v2/account',
                         headers={'APCA-API-KEY-ID': ALPACA_OPTIONS_KEY,
                                  'APCA-API-SECRET-KEY': ALPACA_OPTIONS_SECRET},
                         timeout=5)
            if r.status_code == 200:
                acct = r.json()
                bp = acct.get('buying_power', '?')
                log.info(f"  [OK] Alpaca Options: connected | buying_power=${bp}")
            else:
                log.warning(f"  [WARN] Alpaca Options: status {r.status_code}")
        except Exception as e:
            log.warning(f"  [WARN] Alpaca Options: {e}")
    else:
        log.info("  [SKIP] Alpaca Options: APCA_OPTIONS_KEY not set")

    log.info("=" * 60)
    if preflight_ok:
        log.info("PRE-MARKET CHECK: ALL CRITICAL SYSTEMS OK")
    else:
        log.error("PRE-MARKET CHECK: CRITICAL FAILURES DETECTED — review above")
    log.info("=" * 60)

    # ── Session equity baseline (ground truth from Alpaca) ──────────────────
    session_start_equity = None
    session_prev_close_equity = None
    if ALPACA_API_KEY and ALPACA_SECRET:
        try:
            snap = _fetch_account_snapshot(ALPACA_API_KEY, ALPACA_SECRET, PAPER_TRADING)
            session_start_equity = snap['equity']
            session_prev_close_equity = snap['last_equity']
            log.info("=" * 60)
            log.info("SESSION EQUITY BASELINE (Alpaca Ground Truth)")
            log.info("=" * 60)
            log.info(f"  {'Previous Close Equity':<25} ${session_prev_close_equity:>12,.2f}")
            log.info(f"  {'Current Equity':<25} ${session_start_equity:>12,.2f}")
            log.info(f"  {'Cash':<25} ${snap['cash']:>12,.2f}")
            log.info(f"  {'Buying Power':<25} ${snap['buying_power']:>12,.2f}")
            log.info(f"  {'Portfolio Value':<25} ${snap['portfolio_value']:>12,.2f}")
            log.info("=" * 60)
        except Exception as exc:
            log.warning(f"Could not fetch equity baseline (non-fatal): {exc}")

    # ── Activity Logger (every event → TimescaleDB for analysis) ────────────
    activity_logger = None
    if DB_ENABLED_RUNTIME and db_writer:
        from db.activity_logger import ActivityLogger
        activity_logger = ActivityLogger(bus=monitor._bus, writer=db_writer)
        activity_logger.register()
        log.info("ActivityLogger active → all signals/fills/blocks logged to TimescaleDB")
    else:
        log.warning("ActivityLogger disabled (DB not available) — signals will NOT be logged")

    # ── Initialize canonical RVOL engine ────────────────────────────────────
    try:
        from monitor.rvol import init_global_rvol_engine
        rvol_engine = init_global_rvol_engine()
        # Seed profiles from data client (non-blocking, best-effort)
        try:
            rvol_engine.seed_profiles(monitor.tickers, monitor._data)
        except Exception as seed_err:
            log.warning(f"RVOL profile seeding failed (will lazy-seed): {seed_err}")
    except ImportError:
        log.warning("RVOL engine not available — using legacy calculation")

    # ── Preflight position & order audit ────────────────────────────────
    if ALPACA_API_KEY and ALPACA_SECRET:
        try:
            import requests as _req
            base = os.getenv('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets')
            _headers = {'APCA-API-KEY-ID': ALPACA_API_KEY, 'APCA-API-SECRET-KEY': ALPACA_SECRET}

            # Fetch open positions
            r = _req.get(f'{base}/v2/positions', headers=_headers, timeout=10)
            pf_positions = r.json() if r.status_code == 200 else []

            # Fetch open orders
            r = _req.get(f'{base}/v2/orders', headers=_headers, params={'status': 'open', 'limit': 500}, timeout=10)
            pf_orders = r.json() if r.status_code == 200 else []

            log.info("=" * 60)
            log.info("PREFLIGHT POSITION & ORDER AUDIT")
            log.info("=" * 60)
            log.info(f"  Open positions: {len(pf_positions)}")
            log.info(f"  Open orders:    {len(pf_orders)}")

            if pf_positions:
                pos_summary = [f"{p['symbol']}({p['qty']})" for p in pf_positions]
                log.info(f"  Positions: {', '.join(pos_summary)}")

            if pf_orders:
                for o in pf_orders:
                    log.info(f"  Stale order: {o.get('side','?').upper()} {o.get('symbol','?')}({o.get('qty','?')}) @ ${o.get('limit_price', o.get('stop_price', 'MKT'))}")

                # Cancel all stale orders from previous session
                log.info(f"  Cancelling {len(pf_orders)} stale orders from previous session...")
                try:
                    _req.delete(f'{base}/v2/orders', headers=_headers, timeout=10)
                    log.info("  Stale orders cancelled successfully")
                except Exception as cancel_err:
                    log.warning(f"  Failed to cancel stale orders: {cancel_err}")

            if not pf_positions and not pf_orders:
                log.info("  Clean slate — no positions or orders carried over")
            elif pf_positions and not pf_orders:
                log.info(f"  {len(pf_positions)} swing position(s) carried over — stops will be monitored")

            log.info("=" * 60)
        except Exception as exc:
            log.warning(f"Preflight audit failed (non-fatal): {exc}")

    monitor.start()
    kill_guard = KillSwitchGuard(monitor._bus, MAX_DAILY_LOSS, monitor, log)
    log.info("Monitor running. New entries stop at 3:00 PM ET, exits until 4:00 PM ET.")

    # ── Trading halted flag (daily loss kill switch) ──────────────────────────
    trading_halted = False

    try:
        while True:
            now = datetime.now(ET)
            # Stop at 4:00 PM ET (market close)
            # New entries blocked at 3:30 PM (handled by StrategyEngine._FORCE_CLOSE)
            # Exits still monitored 3:30-4:00 PM so open positions can close
            if now.hour >= 16:
                log.info("4:00 PM ET reached — stopping monitor.")
                break
            # Heartbeat every minute — confirms process is alive
            trades     = monitor.trade_log
            positions  = monitor.positions
            tickers    = monitor.tickers
            wins       = sum(1 for t in trades if t['is_win'])
            losses     = len(trades) - wins
            total_pnl  = sum(t['pnl'] for t in trades)

            # ── DAILY LOSS KILL SWITCH ────────────────────────────────────────
            if not trading_halted and (kill_guard.halted or total_pnl <= MAX_DAILY_LOSS):
                trading_halted = True
                log.error(
                    f"KILL SWITCH ACTIVATED: daily P&L ${total_pnl:+.2f} "
                    f"breached limit ${MAX_DAILY_LOSS:+.2f} | "
                    f"trades={len(trades)} wins={wins} losses={losses}"
                )
                log.error("HALTING ALL TRADING — force-closing open positions")
                # Log kill switch activation to DB
                if activity_logger:
                    activity_logger.log_kill_switch(
                        daily_pnl=total_pnl,
                        threshold=MAX_DAILY_LOSS,
                        trades_count=len(trades),
                        wins=wins,
                        losses=losses,
                        positions_closed=list(positions.keys()),
                    )
                # Force-close all open positions via market sell
                for ticker_name in list(positions.keys()):
                    pos = positions[ticker_name]
                    qty = pos.get('quantity', 0)
                    if qty > 0:
                        from monitor.events import OrderRequestPayload
                        monitor._bus.emit(Event(
                            type=EventType.ORDER_REQ,
                            payload=OrderRequestPayload(
                                ticker=ticker_name, side='SELL', qty=qty,
                                price=pos.get('entry_price', 0),
                                reason='kill_switch_daily_loss',
                            ),
                        ))
                        log.error(f"  KILL SWITCH: force-sell {qty} {ticker_name}")
                # Stop the monitor to prevent new trades
                monitor.stop()
                log.error("Monitor stopped by kill switch. No further trading today.")
                break

            # Activity logging to TimescaleDB
            if DB_ENABLED_RUNTIME and db_writer:
                if activity_logger:
                    try:
                        metrics = monitor._bus.metrics()
                        activity_logger.log_health(
                            tickers_scanned=len(tickers),
                            open_positions=len(positions),
                            trades_today=len(trades),
                            wins_today=wins,
                            losses_today=losses,
                            daily_pnl=total_pnl,
                            system_pressure=metrics.system_pressure,
                            db_rows_written=db_writer.rows_written,
                            db_rows_dropped=db_writer.rows_dropped,
                            db_batches_flushed=db_writer.batches_flushed,
                            registry_count=registry.count(),
                            kill_switch_active=trading_halted,
                            queue_depths=metrics.queue_depths,
                            handler_avg_ms=metrics.handler_avg_ms,
                        )
                    except Exception:
                        pass  # health logging must never crash the monitor
            # Hourly summary (with equity ground truth when available)
            if now.minute == 0 and trades:
                hourly_extra = ""
                if ALPACA_API_KEY and ALPACA_SECRET and session_start_equity is not None:
                    try:
                        hr_snap = _fetch_account_snapshot(ALPACA_API_KEY, ALPACA_SECRET, PAPER_TRADING)
                        equity_pnl = hr_snap['equity'] - session_start_equity
                        hourly_extra = f" | Equity PnL: ${equity_pnl:+.2f} (Alpaca)"
                    except Exception:
                        pass
                log.info(f"Hourly summary: {len(trades)} trades | {wins}W/{losses}L | PnL: ${total_pnl:+.2f}{hourly_extra}")

            # ── Intraday position reconciliation (every hour at :30) ──────
            if now.minute == 30 and ALPACA_API_KEY and ALPACA_SECRET:
                try:
                    import requests as _req
                    base = os.getenv('APCA_API_BASE_URL', 'https://paper-api.alpaca.markets')
                    _headers = {'APCA-API-KEY-ID': ALPACA_API_KEY,
                                'APCA-API-SECRET-KEY': ALPACA_SECRET}

                    r = _req.get(f'{base}/v2/positions', headers=_headers, timeout=10)
                    if r.status_code == 200:
                        broker_positions = {p['symbol']: int(float(p['qty']))
                                            for p in r.json()}
                    else:
                        broker_positions = None

                    if broker_positions is not None:
                        monitor_positions = {tk: pos.get('quantity', 0)
                                             for tk, pos in positions.items()}

                        all_tickers = set(list(broker_positions.keys()) +
                                          list(monitor_positions.keys()))
                        mismatches = []
                        for tk in sorted(all_tickers):
                            b_qty = broker_positions.get(tk, 0)
                            m_qty = monitor_positions.get(tk, 0)
                            if b_qty != m_qty:
                                mismatches.append(
                                    f"{tk}: broker={b_qty} monitor={m_qty}")

                        if mismatches:
                            log.warning(
                                "INTRADAY RECONCILIATION MISMATCH at %s:",
                                now.strftime('%H:%M')
                            )
                            for m in mismatches:
                                log.warning("  %s", m)
                            # TODO(santosh): Investigate mismatches:
                            #   - Missed FILL event (network timeout)
                            #   - Position from another engine on same account
                            #   - Partial fill not fully processed
                            #   - Manual trade on Alpaca dashboard
                        else:
                            log.info(
                                "Intraday reconciliation OK: %d positions match",
                                len(broker_positions)
                            )
                except Exception as exc:
                    log.debug("Intraday reconciliation failed (non-fatal): %s", exc)

            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    finally:
        if not trading_halted:
            monitor.stop()

        # ── Shutdown DB layer gracefully ─────────────────────────────────────
        if DB_ENABLED_RUNTIME and db_loop is not None:
            try:
                if db_session:
                    stop_future = asyncio.run_coroutine_threadsafe(
                        db_session.stop(
                            exit_reason="clean",
                            writer=db_writer,
                        ),
                        db_loop,
                    )
                    stop_future.result(timeout=5)
                if db_writer:
                    flush_future = asyncio.run_coroutine_threadsafe(db_writer.stop(), db_loop)
                    flush_future.result(timeout=5)
                close_future = asyncio.run_coroutine_threadsafe(close_db(), db_loop)
                close_future.result(timeout=5)
                db_loop.call_soon_threadsafe(db_loop.stop)
                log.info(
                    f"DB layer stopped | written={db_writer.rows_written if db_writer else 0} "
                    f"dropped={db_writer.rows_dropped if db_writer else 0}"
                )
            except Exception as exc:
                log.warning(f"DB shutdown error (non-fatal): {exc}")
        # ── EOD POSITION AUDIT (log what's still open, don't force close) ──
        # Pro/Pop positions may be valid swing holds — don't liquidate.
        # Only cancel stale open ORDERS (prevents wash trade issues next morning).
        try:
            from alpaca.trading.client import TradingClient
            from config import APCA_API_KEY_ID, APCA_API_SECRET_KEY
            _paper = PAPER_TRADING  # use module-level import, don't re-import locally
            eod_client = TradingClient(
                api_key=APCA_API_KEY_ID,
                secret_key=APCA_API_SECRET_KEY,
                paper=_paper,
            )
            # Cancel all open orders (prevents wash trade errors on next session)
            try:
                eod_client.cancel_orders()
                log.info("EOD: cancelled all open orders (wash trade prevention)")
            except Exception as cancel_err:
                log.warning(f"EOD: cancel orders failed: {cancel_err}")

            # Log open positions (audit trail, no close)
            open_positions = eod_client.get_all_positions()
            if open_positions:
                tickers_held = [f"{p.symbol}({p.qty})" for p in open_positions]
                log.info(
                    f"EOD: {len(open_positions)} swing position(s) held overnight: "
                    f"{', '.join(tickers_held)}"
                )
            else:
                log.info("EOD: no open positions — clean shutdown")
        except ImportError:
            log.warning("EOD: alpaca-py not available — cannot audit positions")
        except Exception as exc:
            log.warning(f"EOD position audit error (non-fatal): {exc}")

        # ── Post-session analytics (runs in background, non-blocking) ────────
        try:
            import subprocess
            analytics_script = os.path.join(os.path.dirname(__file__), 'scripts', 'post_session_analytics.py')
            if os.path.exists(analytics_script):
                subprocess.Popen(
                    [sys.executable, analytics_script],
                    stdout=open(os.path.join(os.path.dirname(__file__), 'logs', 'post_session.log'), 'a'),
                    stderr=subprocess.STDOUT,
                )
                log.info("Post-session analytics triggered (background)")
        except Exception as exc:
            log.warning(f"Post-session analytics trigger failed (non-fatal): {exc}")

        # ── Close all options positions at EOD ─────────────────────────────
        try:
            if 'options_engine' in dir() and options_engine:
                options_engine.close_all_positions('eod_close')
                log.info("Options positions closed at EOD")
        except Exception as exc:
            log.warning(f"Options EOD close error (non-fatal): {exc}")

        trades = monitor.trade_log
        W = 60
        bar = "=" * W

        log.info(bar)
        log.info(f"  EOD SUMMARY — {datetime.now(ET).strftime('%A, %Y-%m-%d')}")
        log.info(bar)

        if not trades:
            log.info("  No trades executed today.")
            # Still log equity change even with no trades (positions, fees, dividends)
            if ALPACA_API_KEY and ALPACA_SECRET:
                try:
                    eod_snap = _fetch_account_snapshot(ALPACA_API_KEY, ALPACA_SECRET, PAPER_TRADING)
                    baseline = session_prev_close_equity or session_start_equity
                    if baseline:
                        equity_pnl = round(eod_snap['equity'] - baseline, 2)
                        log.info(f"  Equity change (Alpaca): ${equity_pnl:+.2f} (no trades — unrealized/fees/dividends)")
                except Exception:
                    pass
            log.info(bar)
        else:
            wins       = [t for t in trades if t['is_win']]
            losses     = [t for t in trades if not t['is_win']]
            total_pnl  = sum(t['pnl'] for t in trades)
            win_rate   = len(wins) / len(trades) * 100
            avg_win    = sum(t['pnl'] for t in wins)   / len(wins)   if wins   else 0
            avg_loss   = sum(t['pnl'] for t in losses) / len(losses) if losses else 0
            gross_win  = sum(t['pnl'] for t in wins)
            gross_loss = abs(sum(t['pnl'] for t in losses))
            pf         = gross_win / gross_loss if gross_loss > 0 else float('inf')
            best       = max(trades, key=lambda t: t['pnl'])
            worst      = min(trades, key=lambda t: t['pnl'])

            # ── Overall ───────────────────────────────────────────────
            log.info(f"  {'Total Trades':<20} {len(trades)}")
            log.info(f"  {'Wins / Losses':<20} {len(wins)} / {len(losses)}")
            log.info(f"  {'Win Rate':<20} {win_rate:.1f}%")
            log.info(f"  {'Total PnL (trades)':<20} ${total_pnl:+.2f}")
            log.info(f"  {'Avg Win':<20} ${avg_win:+.2f}")
            log.info(f"  {'Avg Loss':<20} ${avg_loss:+.2f}")
            log.info(f"  {'Profit Factor':<20} {pf:.2f}x")
            log.info(f"  {'Best Trade':<20} {best['ticker']} ${best['pnl']:+.2f} ({best['reason']})")
            log.info(f"  {'Worst Trade':<20} {worst['ticker']} ${worst['pnl']:+.2f} ({worst['reason']})")

            # ── P&L RECONCILIATION (Alpaca ground truth) ─────────────
            log.info("")
            log.info("  " + "-" * (W - 2))
            log.info("  P&L RECONCILIATION vs Alpaca Ground Truth")
            log.info("  " + "-" * (W - 2))

            eod_equity = None
            eod_fees = None
            if ALPACA_API_KEY and ALPACA_SECRET:
                # 1. Fetch ending equity snapshot
                try:
                    eod_snap = _fetch_account_snapshot(ALPACA_API_KEY, ALPACA_SECRET, PAPER_TRADING)
                    eod_equity = eod_snap['equity']
                    log.info(f"  {'Ending Equity':<25} ${eod_equity:>12,.2f}")
                    if session_prev_close_equity is not None:
                        log.info(f"  {'Prev Close Equity':<25} ${session_prev_close_equity:>12,.2f}")
                    if session_start_equity is not None:
                        log.info(f"  {'Session Start Equity':<25} ${session_start_equity:>12,.2f}")
                except Exception as exc:
                    log.warning(f"  Could not fetch ending equity: {exc}")

                # 2. Fetch daily fees/commissions
                try:
                    fee_data = _fetch_daily_fees(ALPACA_API_KEY, ALPACA_SECRET, PAPER_TRADING)
                    eod_fees = fee_data['total_fees']
                    log.info(f"  {'Alpaca Fees/Reg':<25} ${eod_fees:>12,.4f}  ({fee_data['fee_count']} charges)")
                except Exception as exc:
                    log.warning(f"  Could not fetch fees: {exc}")
                    eod_fees = 0.0

                # 3. Compute equity-based P&L (ground truth)
                log.info("")
                baseline = session_prev_close_equity or session_start_equity
                if eod_equity is not None and baseline is not None:
                    equity_pnl = round(eod_equity - baseline, 2)
                    fee_adjusted_pnl = round(total_pnl - (eod_fees or 0.0), 2)
                    discrepancy = round(equity_pnl - fee_adjusted_pnl, 2)

                    log.info(f"  {'Trade-log PnL':<25} ${total_pnl:>+12.2f}  (sum of closed trades)")
                    log.info(f"  {'Fees Deducted':<25} ${-(eod_fees or 0.0):>+12.4f}")
                    log.info(f"  {'Fee-Adjusted PnL':<25} ${fee_adjusted_pnl:>+12.2f}  (trades - fees)")
                    log.info(f"  {'Equity-Based PnL':<25} ${equity_pnl:>+12.2f}  (Alpaca ground truth)")
                    log.info(f"  {'Discrepancy':<25} ${discrepancy:>+12.2f}  (equity - fee-adjusted)")

                    if abs(discrepancy) > 0.01:
                        log.warning(
                            f"  ** P&L DISCREPANCY: ${discrepancy:+.2f} **"
                        )
                        log.warning(
                            f"  ** Equity says ${equity_pnl:+.2f} but trades-fees says ${fee_adjusted_pnl:+.2f} **"
                        )
                        # TODO(santosh): Possible causes to validate manually:
                        #   - Unrealized P&L on positions held overnight (open positions change equity)
                        #   - Dividends or corporate actions credited/debited intraday
                        #   - Interest on cash balance (margin accounts)
                        #   - Fees not captured by FEE/CFEE activity types
                        #   - Partial fills where monitor qty != broker qty
                        #   - Rounding differences across many small trades
                        #   - Positions opened by other engines (Pop/Pro/Options) on same account
                        log.warning("  ** Review above causes — see TODO comments in run_monitor.py **")
                    else:
                        log.info("  P&L RECONCILED — trade-log matches Alpaca equity (within $0.01)")
                else:
                    log.warning("  Could not reconcile — equity baseline or ending equity unavailable")

                # 4. Orphaned / missed trade audit (compare Alpaca fills vs trade_log)
                log.info("")
                log.info("  " + "-" * (W - 2))
                log.info("  ORPHANED / MISSED TRADE AUDIT")
                log.info("  " + "-" * (W - 2))
                try:
                    alpaca_fills = _fetch_filled_orders(ALPACA_API_KEY, ALPACA_SECRET, PAPER_TRADING)
                    alpaca_sells = [f for f in alpaca_fills if f['side'] == 'SELL']
                    alpaca_buys  = [f for f in alpaca_fills if f['side'] == 'BUY']

                    log.info(f"  Alpaca filled orders today: {len(alpaca_fills)} ({len(alpaca_buys)} buys, {len(alpaca_sells)} sells)")
                    log.info(f"  Monitor trade_log entries:  {len(trades)}")

                    # Build monitor sell map: ticker -> list of (qty, exit_price)
                    monitor_sells = {}
                    for t in trades:
                        tk = t['ticker']
                        monitor_sells.setdefault(tk, [])
                        monitor_sells[tk].append({
                            'qty': t['qty'],
                            'price': t['exit_price'],
                        })

                    # Build Alpaca sell map: ticker -> list of (qty, fill_price)
                    broker_sells = {}
                    for f in alpaca_sells:
                        tk = f['ticker']
                        broker_sells.setdefault(tk, [])
                        broker_sells[tk].append({
                            'qty': f['qty'],
                            'price': f['fill_price'],
                            'filled_at': f['filled_at'],
                            'order_id': f['id'],
                        })

                    # Compare total sell qty per ticker
                    all_sell_tickers = set(list(monitor_sells.keys()) + list(broker_sells.keys()))
                    orphaned_found = False
                    orphaned_pnl_impact = 0.0

                    for tk in sorted(all_sell_tickers):
                        m_qty = sum(s['qty'] for s in monitor_sells.get(tk, []))
                        b_qty = sum(s['qty'] for s in broker_sells.get(tk, []))

                        if m_qty != b_qty:
                            orphaned_found = True
                            diff = b_qty - m_qty
                            # Estimate missed P&L: find corresponding buy for this ticker
                            buy_prices = [f['fill_price'] for f in alpaca_buys if f['ticker'] == tk]
                            avg_buy = sum(buy_prices) / len(buy_prices) if buy_prices else 0
                            sell_prices = [s['price'] for s in broker_sells.get(tk, [])]
                            avg_sell = sum(sell_prices) / len(sell_prices) if sell_prices else 0
                            est_missed_pnl = round((avg_sell - avg_buy) * abs(diff), 2) if avg_buy else 0

                            if diff > 0:
                                log.warning(
                                    f"  MISSED SELL: {tk} — Alpaca sold {b_qty} shares, "
                                    f"monitor logged {m_qty} (missed {diff} shares, "
                                    f"est. PnL impact: ${est_missed_pnl:+.2f})"
                                )
                            else:
                                log.warning(
                                    f"  PHANTOM SELL: {tk} — monitor logged {m_qty} shares sold, "
                                    f"but Alpaca only filled {b_qty} (phantom {abs(diff)} shares)"
                                )
                            orphaned_pnl_impact += est_missed_pnl

                    # Check for buys at Alpaca with no corresponding monitor entry
                    monitor_buy_tickers = set(t['ticker'] for t in trades)
                    # Also include open positions the monitor knows about
                    monitor_open_tickers = set(monitor.positions.keys()) if hasattr(monitor, 'positions') else set()
                    known_tickers = monitor_buy_tickers | monitor_open_tickers

                    orphan_buys = {}
                    for f in alpaca_buys:
                        tk = f['ticker']
                        orphan_buys.setdefault(tk, [])
                        orphan_buys[tk].append(f)

                    for tk, buys in sorted(orphan_buys.items()):
                        b_buy_qty = sum(b['qty'] for b in buys)
                        # Check if monitor has any record of buying this ticker
                        m_buy_qty = sum(
                            s['qty'] for s in monitor_sells.get(tk, [])
                        )
                        # Also check open positions
                        open_qty = 0
                        if hasattr(monitor, 'positions') and tk in monitor.positions:
                            open_qty = monitor.positions[tk].get('quantity', 0)
                        m_total = m_buy_qty + open_qty

                        if b_buy_qty > m_total and tk not in monitor_sells:
                            orphaned_found = True
                            log.warning(
                                f"  ORPHANED BUY: {tk} — Alpaca bought {b_buy_qty} shares, "
                                f"monitor has no record (not in trade_log or open positions)"
                            )

                    if orphaned_found:
                        if orphaned_pnl_impact != 0:
                            log.warning(f"  Estimated orphaned trade PnL impact: ${orphaned_pnl_impact:+.2f}")
                        # TODO(santosh): Orphaned trades to investigate:
                        #   - Check if broker filled an order but monitor missed the FILL event
                        #   - Check if position was opened by Pop/Pro/Options engine on same account
                        #   - Check for wash-trade cancel/resubmit that created duplicate fills
                        #   - Review logs around the fill time for errors/timeouts
                        log.warning("  ** Orphaned trades detected — review logs for missed fills **")
                    else:
                        log.info("  No orphaned or missed trades — monitor and Alpaca fills match")

                except Exception as exc:
                    log.warning(f"  Orphaned trade audit failed (non-fatal): {exc}")

            # ── Exit reason breakdown ─────────────────────────────────
            log.info("")
            log.info("  Exit Reason Breakdown:")
            reasons = {}
            for t in trades:
                r = t['reason']
                reasons.setdefault(r, {'count': 0, 'pnl': 0.0})
                reasons[r]['count'] += 1
                reasons[r]['pnl']   += t['pnl']
            for reason, stats in sorted(reasons.items(), key=lambda x: -x[1]['pnl']):
                log.info(f"    {reason:<25} {stats['count']:>3} trades  PnL: ${stats['pnl']:+.2f}")

            # ── Per-ticker breakdown ──────────────────────────────────
            log.info("")
            log.info("  Per-Ticker Breakdown:")
            tickers_seen = {}
            for t in trades:
                tk = t['ticker']
                tickers_seen.setdefault(tk, {'count': 0, 'pnl': 0.0, 'wins': 0})
                tickers_seen[tk]['count'] += 1
                tickers_seen[tk]['pnl']   += t['pnl']
                tickers_seen[tk]['wins']  += 1 if t['is_win'] else 0
            for tk, stats in sorted(tickers_seen.items(), key=lambda x: -x[1]['pnl']):
                wr = stats['wins'] / stats['count'] * 100
                log.info(f"    {tk:<6}  {stats['count']:>2} trades  {wr:>5.1f}% win  PnL: ${stats['pnl']:+.2f}")

            # ── Trade-by-trade log ────────────────────────────────────
            log.info("")
            log.info("  Trade Log:")
            log.info(f"  {'Entry':>8}  {'Exit':>8}  {'Ticker':<6}  {'Qty':>3}  {'Entry $':>8}  {'Exit $':>8}  {'PnL':>8}  Reason")
            log.info("  " + "-" * (W - 2))
            for t in trades:
                flag = "✓" if t['is_win'] else "✗"
                log.info(
                    f"  {t.get('entry_time','?'):>8}  {t['time']:>8}  {t['ticker']:<6}  "
                    f"{t['qty']:>3}  ${t['entry_price']:>7.2f}  ${t['exit_price']:>7.2f}  "
                    f"${t['pnl']:>+7.2f}  {flag} {t['reason']}"
                )

        log.info(bar)


if __name__ == '__main__':
    main()
