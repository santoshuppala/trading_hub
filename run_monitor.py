"""
Standalone monitor launcher — runs without Streamlit UI.
Scheduled daily at 6:00 AM PST (9:00 AM ET, 30 min before open).
Stops automatically at 3:15 PM ET after all positions are force-closed.
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
    GLOBAL_MAX_POSITIONS,
    DB_ENABLED, DATABASE_URL,
)
from monitor.position_registry import registry

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
            from db.subscriber import DBSubscriber
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
        from db.subscriber import DBSubscriber
        db_sub = DBSubscriber(bus=monitor._bus, writer=db_writer)
        db_sub.register()
        log.info("DBSubscriber registered — all events will be persisted to TimescaleDB")

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

    log.info("=" * 60)
    if preflight_ok:
        log.info("PRE-MARKET CHECK: ALL CRITICAL SYSTEMS OK")
    else:
        log.error("PRE-MARKET CHECK: CRITICAL FAILURES DETECTED — review above")
    log.info("=" * 60)

    # ── Activity Logger (every event → TimescaleDB for analysis) ────────────
    activity_logger = None
    if DB_ENABLED_RUNTIME and db_writer:
        from db.activity_logger import ActivityLogger
        activity_logger = ActivityLogger(bus=monitor._bus, writer=db_writer)
        activity_logger.register()
        log.info("ActivityLogger active → all signals/fills/blocks logged to TimescaleDB")
    else:
        log.warning("ActivityLogger disabled (DB not available) — signals will NOT be logged")

    monitor.start()
    log.info("Monitor running. Will stop at 3:15 PM ET.")

    # ── Trading halted flag (daily loss kill switch) ──────────────────────────
    trading_halted = False

    try:
        while True:
            now = datetime.now(ET)
            # Stop at 3:15 PM ET (after 3 PM force-close has fired)
            if now.hour == 15 and now.minute >= 15:
                log.info("3:15 PM ET reached — stopping monitor.")
                break
            # Heartbeat every minute — confirms process is alive
            trades     = monitor.trade_log
            positions  = monitor.positions
            tickers    = monitor.tickers
            wins       = sum(1 for t in trades if t['is_win'])
            losses     = len(trades) - wins
            total_pnl  = sum(t['pnl'] for t in trades)

            # ── DAILY LOSS KILL SWITCH ────────────────────────────────────────
            if not trading_halted and total_pnl <= MAX_DAILY_LOSS:
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
                                ticker=ticker_name, side='sell', qty=qty,
                                price=pos.get('entry_price', 0),
                                reason='kill_switch_daily_loss',
                            ),
                        ))
                        log.error(f"  KILL SWITCH: force-sell {qty} {ticker_name}")
                # Stop the monitor to prevent new trades
                monitor.stop()
                log.error("Monitor stopped by kill switch. No further trading today.")
                break

            log.info(
                f"[heartbeat] scanning {len(tickers)} tickers | "
                f"open positions: {len(positions)} {list(positions.keys()) or 'none'} | "
                f"trades today: {len(trades)} ({wins}W/{losses}L) | "
                f"PnL: ${total_pnl:+.2f} | kill_switch: ${MAX_DAILY_LOSS:+.2f}"
            )
            # DB health + activity logging
            if DB_ENABLED_RUNTIME and db_writer:
                log.info(
                    f"[heartbeat] DB: written={db_writer.rows_written} "
                    f"dropped={db_writer.rows_dropped} batches={db_writer.batches_flushed}"
                )
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
            # Hourly summary
            if now.minute == 0 and trades:
                log.info(f"Hourly summary: {len(trades)} trades | {wins}W/{losses}L | PnL: ${total_pnl:+.2f}")
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
        trades = monitor.trade_log
        W = 60
        bar = "=" * W

        log.info(bar)
        log.info(f"  EOD SUMMARY — {datetime.now(ET).strftime('%A, %Y-%m-%d')}")
        log.info(bar)

        if not trades:
            log.info("  No trades executed today.")
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
            log.info(f"  {'Total PnL':<20} ${total_pnl:+.2f}")
            log.info(f"  {'Avg Win':<20} ${avg_win:+.2f}")
            log.info(f"  {'Avg Loss':<20} ${avg_loss:+.2f}")
            log.info(f"  {'Profit Factor':<20} {pf:.2f}x")
            log.info(f"  {'Best Trade':<20} {best['ticker']} ${best['pnl']:+.2f} ({best['reason']})")
            log.info(f"  {'Worst Trade':<20} {worst['ticker']} ${worst['pnl']:+.2f} ({worst['reason']})")

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
