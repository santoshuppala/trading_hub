import atexit
import logging
import os
import subprocess
import sys
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

LOCK_FILE = os.path.join(os.path.dirname(__file__), '..', '.monitor.lock')


def _pid_is_monitor(pid):
    """Return True if the given PID is actually a Python process (not a reused PID)."""
    try:
        result = subprocess.run(
            ['ps', '-p', str(pid), '-o', 'comm='],
            capture_output=True, text=True, timeout=2,
        )
        return 'python' in result.stdout.lower()
    except Exception:
        return True  # can't verify — assume it's valid


def _is_running_elsewhere():
    """Return (True, pid) if another monitor process holds the lock, else (False, None)."""
    try:
        with open(LOCK_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)   # raises OSError if PID is gone
        if not _pid_is_monitor(pid):
            # PID was reused by a non-Python process — stale lock
            log.warning(f"Stale lock (PID {pid} is not a Python process). Removing.")
            os.remove(LOCK_FILE)
            return False, None
        return True, pid
    except (FileNotFoundError, ValueError):
        return False, None
    except OSError:
        # PID is gone — stale lock
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

from alpaca.trading.client import TradingClient

from .alerts import send_alert
from .data_client import make_data_client
from .screener import MomentumScreener
from .signals import SignalAnalyzer
from .orders import OrderManager

ET = ZoneInfo('America/New_York')


class RealTimeMonitor:
    """
    Orchestrates real-time monitoring, signal analysis, and order execution
    using the VWAP reclaim strategy.

    Public interface (backward-compatible with realtime_main.RealTimeMonitor):
      Attributes: tickers, positions, trade_log, running
      Methods:    start(), stop()
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
    ):
        self.base_tickers = list(tickers)    # fixed watchlist
        self.tickers = list(tickers)         # active list (base + dynamic momentum)
        self.strategy_name = strategy_name
        self.strategy_params = strategy_params
        self.per_ticker_params = per_ticker_params or {}
        self.open_cost = open_cost           # kept for backtest compatibility
        self.close_cost = close_cost
        self.alert_email = alert_email
        self.positions = {}                  # ticker -> position dict
        self.trade_log = []                  # completed trades for today's summary
        self.reclaimed_today = set()         # tickers that already had a reclaim today
        self._bars_cache = {}                # today's bars: {ticker: DataFrame}
        self._rvol_cache = {}                # historical bars for RVOL: {ticker: DataFrame}
        self._last_momentum_refresh = None   # datetime of last screener refresh
        self.running = False
        self.thread = None
        self.max_positions = max_positions
        self.order_cooldown = order_cooldown
        self.trade_budget = trade_budget         # dollars per trade
        self.last_order_time = {}
        self._last_reset_date = datetime.now(ET).date()

        # ── Alpaca: order execution only ───────────────────────────────
        api_key    = alpaca_api_key    or os.getenv('APCA_API_KEY_ID', '')
        api_secret = alpaca_secret_key or os.getenv('APCA_API_SECRET_KEY', '')

        if api_key and api_secret:
            trading_client = TradingClient(api_key, api_secret, paper=paper)
            log.info(f"Alpaca trading client initialized (paper={paper}).")
        else:
            trading_client = None
            log.warning("Alpaca keys not provided — orders will be skipped.")

        # ── Market data client (provider selected by data_source) ─────
        token = tradier_token or os.getenv('TRADIER_TOKEN', '')
        data_client = make_data_client(
            data_source,
            tradier_token=token,
            alpaca_api_key=api_key,
            alpaca_secret=api_secret,
        )
        log.info(f"Data client initialized: {data_source}.")

        self._data = data_client
        self._screener = MomentumScreener(data_client)
        self._signal_analyzer = SignalAnalyzer(strategy_params, self.per_ticker_params)
        self._order_manager = OrderManager(trading_client, alert_email)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _send_alert(self, message):
        send_alert(self.alert_email, message)

    def _reset_daily_state(self):
        """Reset per-day tracking at the start of each new trading day."""
        today = datetime.now(ET).date()
        if today != self._last_reset_date:
            self.reclaimed_today.clear()
            self._last_reset_date = today

    # ------------------------------------------------------------------
    # Core loop methods
    # ------------------------------------------------------------------

    def analyze_ticker(self, ticker):
        """Analyze a single ticker and place orders if conditions are met."""
        try:
            self._reset_daily_state()

            now = datetime.now(ET)
            hour, minute = now.hour, now.minute

            # Force-close all positions by 3:00 PM ET
            force_close = hour == 15 and minute >= 0
            if force_close and ticker in self.positions:
                data = self._data.get_bars(ticker, self._bars_cache, self._rvol_cache, calendar_days=2)
                today = now.date()
                data = data[data.index.date == today] if not data.empty else data
                if not data.empty:
                    current_price = float(data['close'].iloc[-1])
                    pos = self.positions[ticker]
                    qty = pos['quantity']
                    if qty > 0:
                        order_id = self._order_manager.place_sell(ticker, qty)
                        if order_id:
                            pnl = (current_price - pos['entry_price']) * qty
                            self._send_alert(
                                f"SELL (EOD close): {ticker} @ ${current_price:.2f} | PnL: ${pnl:.2f}"
                            )
                            self.trade_log.append({
                                'ticker': ticker,
                                'entry_price': pos['entry_price'],
                                'entry_time': pos.get('entry_time', ''),
                                'exit_price': current_price,
                                'qty': qty,
                                'pnl': round(pnl, 2),
                                'reason': 'EOD force close',
                                'time': now.strftime('%H:%M:%S'),
                                'is_win': pnl >= 0,
                            })
                            del self.positions[ticker]
                return

            # Only trade 9:45 AM – 3:00 PM ET
            market_open = (hour == 9 and minute >= 45) or (10 <= hour <= 14)
            if not market_open:
                return

            data = self._data.get_bars(ticker, self._bars_cache, self._rvol_cache, calendar_days=2)
            if data.empty:
                return
            today = now.date()
            data = data[data.index.date == today]
            if len(data) < 30:
                return

            result = self._signal_analyzer.analyze(ticker, data, self._rvol_cache)
            if result is None:
                return

            current_price = result['current_price']
            atr_value = result['atr_value']
            rsi_value = result['rsi_value']
            rvol = result['rvol']
            current_vwap = result['vwap']
            reclaim_candle_low = result['reclaim_candle_low']
            vwap_reclaim = result['_vwap_reclaim']
            opened_above_vwap = result['_opened_above_vwap']
            rsi_overbought = result['_rsi_overbought']
            atr_mult = result['_atr_mult']
            min_stop_pct = result['_min_stop_pct']

            if ticker not in self.positions:
                # Position sizing / cooldown guards
                if len(self.positions) >= self.max_positions:
                    return
                if time.time() - self.last_order_time.get(ticker, 0) < self.order_cooldown:
                    return

                # Skip if this ticker already had a reclaim today
                if ticker in self.reclaimed_today:
                    return

                rvol_confirmed = rvol >= 2.0
                rsi_bullish = 50 <= rsi_value <= 70
                spy_bullish = self._data.get_spy_vwap_bias(self._bars_cache)

                if vwap_reclaim and opened_above_vwap and rsi_bullish and rvol_confirmed and spy_bullish:
                    # Level 1 quote: check bid/ask spread before entering
                    spread_pct, ask_price = self._data.check_spread(ticker)
                    if spread_pct is None:
                        ask_price = current_price
                        spread_pct = 0.0
                    if spread_pct > self._order_manager.MAX_SPREAD_PCT:
                        log.warning(f"Skipping {ticker}: spread too wide ({spread_pct:.3%})")
                        return

                    # Effective entry = ask + slippage
                    effective_entry = ask_price * (1 + self._order_manager.SLIPPAGE_PCT)

                    # Position size: allocate trade_budget dollars
                    qty = max(1, int(self.trade_budget / effective_entry))
                    dollar_value = round(qty * effective_entry, 2)

                    order_id = self._order_manager.place_buy(ticker, qty, ask_price)
                    if order_id:
                        candle_stop = reclaim_candle_low - (atr_value * 0.5)
                        pct_stop = effective_entry * (1 - min_stop_pct)
                        stop_price = min(candle_stop, pct_stop)
                        target_price = effective_entry + (atr_value * atr_mult)
                        half_target = effective_entry + (atr_value * 1.0)
                        self.positions[ticker] = {
                            'entry_price': effective_entry,
                            'entry_time': now.strftime('%H:%M:%S'),
                            'quantity': qty,
                            'partial_done': False,
                            'order_id': order_id,
                            'stop_price': stop_price,
                            'target_price': target_price,
                            'half_target': half_target,
                        }
                        self.reclaimed_today.add(ticker)
                        self.last_order_time[ticker] = time.time()
                        self._send_alert(
                            f"BUY (VWAP Reclaim): {ticker} {qty} shares @ ${effective_entry:.2f} "
                            f"(${dollar_value:.0f} deployed) | "
                            f"Spread: {spread_pct:.3%} | VWAP: ${current_vwap:.2f} | "
                            f"Stop: ${stop_price:.2f} | Target: ${target_price:.2f} | "
                            f"Partial: ${half_target:.2f} | RSI: {rsi_value:.1f} | RVOL: {rvol:.1f}x"
                        )
            else:
                pos = self.positions[ticker]
                qty = pos['quantity']

                # ── Exit 1: sell 50% at 1x ATR, move stop to breakeven ────────
                if not pos['partial_done'] and current_price >= pos['half_target'] and qty >= 2:
                    partial_qty = qty // 2
                    order_id = self._order_manager.place_sell(ticker, partial_qty)
                    if order_id:
                        partial_pnl = round((current_price - pos['entry_price']) * partial_qty, 2)
                        pos['quantity'] -= partial_qty
                        pos['partial_done'] = True
                        pos['stop_price'] = pos['entry_price']  # breakeven stop
                        self.trade_log.append({
                            'ticker':      ticker,
                            'entry_price': pos['entry_price'],
                            'entry_time':  pos.get('entry_time', ''),
                            'exit_price':  current_price,
                            'qty':         partial_qty,
                            'pnl':         partial_pnl,
                            'reason':      'partial exit (1x ATR)',
                            'time':        now.strftime('%H:%M:%S'),
                            'is_win':      partial_pnl >= 0,
                        })
                        self._send_alert(
                            f"SELL partial: {ticker} {partial_qty} shares @ ${current_price:.2f} | "
                            f"PnL: ${partial_pnl:+.2f} | "
                            f"Stop moved to breakeven ${pos['entry_price']:.2f} | "
                            f"Holding {pos['quantity']} shares"
                        )
                    return

                # Trailing stop update
                trail_stop = current_price - (atr_value * 1.0)
                if trail_stop > pos['stop_price']:
                    pos['stop_price'] = trail_stop

                hit_stop   = current_price <= pos['stop_price']
                hit_target = current_price >= pos['target_price']
                rsi_exit   = rsi_value > rsi_overbought
                vwap_exit  = result['_vwap_breakdown']

                # ── Exit 2: sell remaining shares ─────────────────────────────
                if hit_stop or hit_target or rsi_exit or vwap_exit:
                    remaining_qty = pos['quantity']
                    order_id = self._order_manager.place_sell(ticker, remaining_qty)
                    if order_id:
                        pnl = round((current_price - pos['entry_price']) * remaining_qty, 2)
                        reason = (
                            'target reached' if hit_target else
                            'trailing stop'  if hit_stop  else
                            'RSI overbought' if rsi_exit  else
                            'VWAP breakdown'
                        )
                        self._send_alert(
                            f"SELL final ({reason}): {ticker} {remaining_qty} shares @ "
                            f"${current_price:.2f} | PnL: ${pnl:+.2f}"
                        )
                        self.trade_log.append({
                            'ticker':      ticker,
                            'entry_price': pos['entry_price'],
                            'entry_time':  pos.get('entry_time', ''),
                            'exit_price':  current_price,
                            'qty':         remaining_qty,
                            'pnl':         pnl,
                            'reason':      reason,
                            'time':        now.strftime('%H:%M:%S'),
                            'is_win':      pnl >= 0,
                        })
                        del self.positions[ticker]
                        self.last_order_time[ticker] = time.time()

        except Exception as e:
            log.error(f"Error analyzing {ticker}: {e}")

    def run(self):
        """Main monitoring loop. Called in a background thread by start()."""
        self.running = True
        while self.running:
            # Refresh momentum stocks from Alpaca screener (every 30 min)
            self.tickers, self._last_momentum_refresh = self._screener.refresh(
                self.base_tickers,
                self.tickers,
                self._last_momentum_refresh,
            )

            # Single batch API call — fetches all tickers at once
            log.info(
                f"[{datetime.now(ET).strftime('%H:%M:%S')}] "
                f"Fetching bars for {len(self.tickers)} tickers..."
            )
            self._bars_cache, self._rvol_cache = self._data.fetch_batch_bars(self.tickers)

            # Analyze all tickers in parallel using cached data
            with ThreadPoolExecutor(max_workers=min(len(self.tickers), 50)) as ex:
                futures = {ex.submit(self.analyze_ticker, t): t for t in self.tickers}
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Exception as e:
                        log.error(f"Error in thread for {futures[f]}: {e}")

            now = datetime.now(ET)
            market_open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
            if now < market_open_time:
                # Sleep until 30 seconds before open instead of spinning every minute
                wait = max(10, (market_open_time - now).total_seconds() - 30)
                log.info(f"Pre-market: sleeping {wait / 60:.1f} min until near open.")
                time.sleep(wait)
            else:
                time.sleep(60)  # During market: re-fetch every minute

    def start(self):
        """Start the monitoring loop in a background thread."""
        already_running, pid = _is_running_elsewhere()
        if already_running:
            msg = (
                f"ERROR: Monitor is already running (PID {pid}).\n"
                f"Stop it first before starting a new instance.\n"
                f"Lock file: {os.path.abspath(LOCK_FILE)}"
            )
            log.error(msg)
            raise RuntimeError(msg)

        if not self.running:
            _write_lock()
            atexit.register(_remove_lock)   # cleans up on any normal exit or SIGTERM
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()
            log.info(f"Real-time monitoring started (PID {os.getpid()}).")

    def stop(self):
        """Stop the monitoring loop and wait for the thread to finish."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=10)
        _remove_lock()
        log.info("Real-time monitoring stopped.")
