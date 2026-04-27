import logging
import math
import threading
from collections import OrderedDict

import numpy as np
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)

from vwap_utils import compute_vwap, detect_vwap_reclaim as _detect_vwap_reclaim, detect_vwap_breakdown as _detect_vwap_breakdown  # noqa: E402

# Trading window: 9:45 AM – 3:00 PM ET
TRADE_START_HOUR = 9
TRADE_START_MIN = 45
FORCE_CLOSE_HOUR = 15


def get_rvol(hist_df, current_volume_sum, current_bar_index):
    """
    Relative Volume ratio (current vs historical average at this time of day).

    Supports two hist_df formats:
      - Minute bars (multiple rows per date): precise bar-aligned comparison.
      - Daily bars (one row per date, from Tradier): time-fraction approximation
        using the fraction of the trading day elapsed so far.

    Returns RVOL ratio (2.0 = twice the usual volume at this time of day).
    Falls back to 1.0 if historical data is unavailable.
    """
    try:
        if hist_df is None or hist_df.empty:
            return 1.0
        today = datetime.now(ET).date()
        past_dates = sorted({idx.date() for idx in hist_df.index if idx.date() < today})[-5:]
        if not past_dates:
            return 1.0

        # Detect whether this is minute-level or daily data
        sample_date = past_dates[0]
        bars_on_sample = int((hist_df.index.date == sample_date).sum())
        is_minute_data = bars_on_sample > 1

        if is_minute_data:
            # Precise: compare cumulative volume at the same bar index
            avg_cum_vol = []
            for d in past_dates:
                day_vol = hist_df.loc[hist_df.index.date == d, 'volume']
                if len(day_vol) >= current_bar_index:
                    avg_cum_vol.append(day_vol.iloc[:current_bar_index].sum())
            if not avg_cum_vol:
                return 1.0
            avg = sum(avg_cum_vol) / len(avg_cum_vol)
            return current_volume_sum / avg if avg > 0 else 1.0
        else:
            # Daily bars: estimate expected volume using time-fraction of trading day.
            # In the first 60 minutes of trading the time-fraction denominator is
            # tiny and RVOL is artificially inflated (e.g. normal opening volume
            # appears as 10-25× RVOL).  Return 1.0 (neutral) to avoid false
            # momentum signals during the volatile open.
            now = datetime.now(ET)
            market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
            elapsed = max(0.0, (now - market_open).total_seconds())
            if elapsed < 60 * 60:          # first hour — RVOL unreliable
                return 1.0

            daily_vols = [
                float(hist_df.loc[hist_df.index.date == d, 'volume'].iloc[0])
                for d in past_dates
                if (hist_df.index.date == d).any()
            ]
            if not daily_vols:
                return 1.0
            avg_daily_vol = sum(daily_vols) / len(daily_vols)
            market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
            total   = (market_close - market_open).total_seconds()
            time_frac = min(elapsed / total, 1.0) if total > 0 else 0.5
            expected = avg_daily_vol * time_frac
            return current_volume_sum / expected if expected > 0 else 1.0
    except Exception:
        return 1.0


class SignalAnalyzer:
    """
    Implements the VWAP reclaim entry/exit strategy.

    Entry conditions (ALL must be true):
      1. 2-bar VWAP reclaim confirmation
      2. Stock opened above VWAP (bullish day bias)
      3. RSI 50–70
      4. RVOL >= 2x (institutional participation)
      5. SPY above its VWAP (market tailwind)

    Exit conditions:
      - Trailing stop (ATR-based)
      - Target reached (2x ATR)
      - RSI overbought (> rsi_overbought)
      - VWAP breakdown
      - Partial exit at 1x ATR (if qty >= 2)
      - Force close at 3:00 PM ET
    """

    # Content-based LRU indicator cache: (ticker, n, last_close, first_close, last_vol, rsi_p, atr_p)
    # Survives DataFrame recreation (new object same data = cache hit).
    # Bounded to 500 entries with true LRU eviction via OrderedDict.
    _MAX_CACHE = 500

    # V8: PARTIAL_SELL dedup window (seconds)
    _PARTIAL_SELL_DEDUP_SEC = 60.0

    def __init__(self, strategy_params, per_ticker_params):
        """
        Parameters
        ----------
        strategy_params : dict
            Default strategy parameters (rsi_period, atr_period, etc.).
        per_ticker_params : dict
            Per-ticker overrides: {ticker: {param: value, ...}}.
        """
        self.strategy_params = strategy_params
        self.per_ticker_params = per_ticker_params or {}
        # Content-based LRU cache: OrderedDict for true LRU eviction
        self._indicator_cache: OrderedDict = OrderedDict()
        self._cache_lock = threading.Lock()  # defensive — production is SYNC, but safe for future ASYNC
        # V8: PARTIAL_SELL dedup tracker {ticker: monotonic_time}
        self._last_partial_time: dict = {}

    def _to_scalar(self, val):
        try:
            if hasattr(val, 'item'):
                return float(val.item())
            elif hasattr(val, 'iloc'):
                v = val.iloc[0] if len(val) > 0 else None
                return None if v is None else (float(v.item()) if hasattr(v, 'item') else float(v))
            return float(val)
        except (ValueError, TypeError, AttributeError):
            return None

    def _compute_indicators_np(self, c, h, l, v, rsi_period, atr_period):
        """
        Pure numpy full-recompute of RSI (SMA), ATR (SMA), VWAP.
        Returns dict on success, None on any data quality issue (caller falls back to pandas).
        """
        n = len(c)
        if n < max(rsi_period, atr_period) + 2:
            return None

        # ── Validate ALL arrays including volume ─────────────────
        if not (np.all(np.isfinite(c)) and np.all(np.isfinite(h))
                and np.all(np.isfinite(l)) and np.all(np.isfinite(v))):
            return None
        if np.any(c <= 0):
            return None

        # ── VWAP (full cumulative array) ─────────────────────────
        tp = (h + l + c) / 3.0
        cum_tpv = np.cumsum(tp * v)
        cum_v = np.cumsum(v)
        if cum_v[-1] <= 0:
            return None  # all-zero volume — match pandas NaN→None behavior
        safe_cv = np.where(cum_v > 0, cum_v, np.nan)
        vwap_all = cum_tpv / safe_cv
        # Forward-fill NaN positions (zero-volume bars)
        for i in range(n):
            if np.isnan(vwap_all[i]):
                vwap_all[i] = vwap_all[i - 1] if i > 0 else c[i]

        # ── RSI (SMA method — exact match to pandas rolling().mean()) ──
        delta = np.empty(n)
        delta[0] = 0.0
        delta[1:] = c[1:] - c[:-1]
        gains = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)
        avg_gain = float(gains[n - rsi_period:n].mean())
        avg_loss = float(losses[n - rsi_period:n].mean())
        if avg_gain < 1e-10 and avg_loss < 1e-10:
            rsi = 50.0  # no movement = neutral (pandas returns NaN→50.0)
        elif avg_loss < 1e-10:
            rsi = 100.0
        else:
            rsi = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
        if not np.isfinite(rsi):
            rsi = 50.0

        # ── ATR (SMA of last atr_period true ranges) ────────────
        prev_c = np.empty_like(c)
        prev_c[0] = c[0]
        prev_c[1:] = c[:-1]
        tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
        atr = float(tr[n - atr_period:n].mean())
        if not (np.isfinite(atr) and atr > 0):
            atr = float(c[-1]) * 0.005  # fallback: 0.5% of price

        return {
            'current_price': float(c[-1]),
            'prev_price': float(c[-2]),
            'current_vwap': float(vwap_all[-1]),
            'prev_vwap': float(vwap_all[-2]),
            'rsi': rsi,
            'atr': atr,
            'open_vwap': float(vwap_all[0]),
            'vwaps_list': vwap_all.tolist(),
            'closes_list': c.tolist(),
            'reclaim_candle_low': float(l[-2]),
        }

    def analyze(self, ticker, data, rvol_cache):
        """
        Compute all indicators and determine the signal for a ticker.

        Parameters
        ----------
        ticker : str
        data : pd.DataFrame
            Today's 1-minute bars for this ticker (already filtered to today).
        rvol_cache : dict
            Mapping {ticker: DataFrame} of historical bars for RVOL.

        Returns
        -------
        dict with keys:
            action          : str or None — 'buy', 'sell_stop', 'sell_target',
                              'sell_rsi', 'sell_vwap', 'sell_eod', 'PARTIAL_SELL', None
            current_price   : float or None
            ask_price       : float or None  (populated by caller after spread check)
            spread_pct      : float or None  (populated by caller after spread check)
            atr_value       : float or None
            rsi_value       : float or None
            rvol            : float
            vwap            : float or None  (current VWAP)
            stop_price      : float or None  (computed entry stop)
            target_price    : float or None
            half_target     : float or None
            reclaim_candle_low : float or None
        Returns None if data is insufficient.
        """
        if data is None or len(data) < 30:
            return None

        params = {**self.strategy_params, **self.per_ticker_params.get(ticker, {})}
        rsi_period = params.get('rsi_period', 14)
        atr_period = params.get('atr_period', 14)
        rsi_overbought = params.get('rsi_overbought', 70)
        atr_mult = params.get('atr_multiplier', 2.0)
        min_stop_pct = params.get('min_stop_pct', 0.05)
        reclaim_lookback = params.get('reclaim_lookback', 6)
        max_dip_age      = params.get('max_dip_age', 4)

        ts = self._to_scalar

        close = data['close']
        high = data['high']
        low = data['low']
        volume = data['volume']

        # ── Extract numpy arrays ONCE (often zero-copy) ──────────
        try:
            c_arr = np.asarray(close, dtype=np.float64)
            h_arr = np.asarray(high, dtype=np.float64)
            l_arr = np.asarray(low, dtype=np.float64)
            v_arr = np.asarray(volume, dtype=np.float64)
            o_arr = np.asarray(data['open'], dtype=np.float64)
        except (ValueError, TypeError, KeyError):
            c_arr = h_arr = l_arr = v_arr = o_arr = None

        # ── Content-based cache key ─────────────────────────────────
        # Volume sum catches mid-bar backfill corrections (same first/last bar
        # but corrected middle bars → different volume sum → cache miss).
        n = len(data)
        if c_arr is not None:
            _cache_key = (ticker, n, float(c_arr[-1]), float(c_arr[0]),
                          float(v_arr.sum()), rsi_period, atr_period)
        else:
            _cache_key = (ticker, n, id(data))  # fallback for bad data

        with self._cache_lock:
            if _cache_key in self._indicator_cache:
                self._indicator_cache.move_to_end(_cache_key)  # LRU touch
                _cached = self._indicator_cache[_cache_key]
            else:
                _cached = None

        if _cached is not None:
            current_price      = _cached['current_price']
            prev_price         = _cached['prev_price']
            current_vwap       = _cached['current_vwap']
            prev_vwap          = _cached['prev_vwap']
            rsi_value          = _cached['rsi_value']
            atr_value          = _cached['atr_value']
            open_vwap          = _cached['open_vwap']
            vwaps_list         = _cached['vwaps_list']
            closes_list        = _cached['closes_list']
            reclaim_candle_low = _cached['reclaim_candle_low']
        else:
            # ── Try numpy fast path (~12x faster than pandas) ────
            np_result = None
            if c_arr is not None:
                try:
                    np_result = self._compute_indicators_np(
                        c_arr, h_arr, l_arr, v_arr, rsi_period, atr_period)
                except Exception:
                    np_result = None

            if np_result is not None:
                current_price      = np_result['current_price']
                prev_price         = np_result['prev_price']
                current_vwap       = np_result['current_vwap']
                prev_vwap          = np_result['prev_vwap']
                rsi_value          = np_result['rsi']
                atr_value          = np_result['atr']
                open_vwap          = np_result['open_vwap']
                vwaps_list         = np_result['vwaps_list']
                closes_list        = np_result['closes_list']
                reclaim_candle_low = np_result['reclaim_candle_low']
            else:
                # ── FALLBACK: existing pandas code (unchanged) ───
                vwap = compute_vwap(high, low, close, volume)
                current_price = ts(close.iloc[-1])
                prev_price    = ts(close.iloc[-2])
                current_vwap  = ts(vwap.iloc[-1])
                prev_vwap     = ts(vwap.iloc[-2])

                delta = close.diff()
                gain = delta.where(delta > 0, 0).rolling(window=rsi_period).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_period).mean()
                rsi_raw = ts((100 - (100 / (1 + gain / loss))).iloc[-1])
                rsi_value = rsi_raw if (rsi_raw is not None and rsi_raw == rsi_raw) else 50.0

                tr = pd.concat([
                    high - low,
                    (high - close.shift()).abs(),
                    (low - close.shift()).abs(),
                ], axis=1).max(axis=1)
                atr_value = ts(tr.rolling(window=atr_period).mean().iloc[-1])

                open_vwap          = ts(vwap.iloc[0])
                vwaps_list         = vwap.tolist()
                closes_list        = close.tolist()
                reclaim_candle_low = ts(low.iloc[-2])

            # ── Store in LRU cache ───────────────────────────────
            with self._cache_lock:
                if len(self._indicator_cache) >= self._MAX_CACHE:
                    self._indicator_cache.popitem(last=False)  # evict LRU
                self._indicator_cache[_cache_key] = {
                    'current_price': current_price, 'prev_price': prev_price,
                    'current_vwap': current_vwap,   'prev_vwap': prev_vwap,
                    'rsi_value': rsi_value,          'atr_value': atr_value,
                    'open_vwap': open_vwap,
                    'vwaps_list': vwaps_list,
                    'closes_list': closes_list,
                    'reclaim_candle_low': reclaim_candle_low,
                }

        # Opening range bias
        open_price = float(o_arr[0]) if o_arr is not None else ts(data['open'].iloc[0])
        opened_above_vwap = (
            open_price is not None and open_vwap is not None and open_price > open_vwap
        )

        if any(
            v is None or (isinstance(v, float) and (v != v or not math.isfinite(v)))
            for v in [current_price, current_vwap, prev_price, prev_vwap, rsi_value, atr_value]
        ):
            return None

        # Flexible VWAP reclaim: dip can span 1–N bars within lookback window
        vwap_reclaim = _detect_vwap_reclaim(closes_list, vwaps_list, lookback=reclaim_lookback, max_dip_age=max_dip_age)

        vwap_breakdown = _detect_vwap_breakdown(closes_list, vwaps_list, rsi_value)

        # RVOL — use canonical RVOLEngine if available, else fallback
        try:
            from monitor.rvol import _global_rvol_engine
            if _global_rvol_engine is not None:
                # V9: Try streaming cvol first (real-time, survives restart)
                rt_result = _global_rvol_engine.get_realtime_rvol(ticker)
                if rt_result is not None:
                    rvol = rt_result.rvol_smooth
                else:
                    # Fallback: BAR-based accumulation (original path)
                    hist_df = rvol_cache.get(ticker) if rvol_cache else None
                    _global_rvol_engine.seed_from_bar_payload(ticker, hist_df)
                    last_vol = float(volume.iloc[-1]) if len(volume) > 0 else 0
                    result = _global_rvol_engine.update(ticker, last_vol)
                    rvol = result.rvol_smooth
            else:
                raise ImportError
        except (ImportError, Exception):
            # Fallback to old calculation
            hist_df = rvol_cache.get(ticker) if rvol_cache else None
            cum_volume = float(volume.sum())
            rvol = get_rvol(hist_df, cum_volume, len(volume))

        return {
            'action': None,
            'current_price': current_price,
            'ask_price': None,
            'spread_pct': None,
            'atr_value': atr_value,
            'rsi_value': rsi_value,
            'rvol': rvol,
            'vwap': current_vwap,
            'stop_price': None,
            'target_price': None,
            'half_target': None,
            'reclaim_candle_low': reclaim_candle_low,
            # Internal flags used by monitor
            '_vwap_reclaim': vwap_reclaim,
            '_vwap_breakdown': vwap_breakdown,
            '_opened_above_vwap': opened_above_vwap,
            '_rsi_overbought': rsi_overbought,
            '_atr_mult': atr_mult,
            '_min_stop_pct': min_stop_pct,
        }

    def check_position_exit(self, ticker, pos, data, rvol_cache):
        """
        Determine if an open position should be exited.

        Parameters
        ----------
        ticker : str
        pos : dict
            Position dict with keys: entry_price, stop_price, target_price,
            half_target, partial_done, quantity.
        data : pd.DataFrame
            Today's bars for ticker.
        rvol_cache : dict

        Returns
        -------
        str or None
            One of: 'sell_stop', 'sell_target', 'sell_rsi', 'sell_vwap',
            'PARTIAL_SELL', None
        """
        result = self.analyze(ticker, data, rvol_cache)
        if result is None:
            return None

        current_price = result['current_price']
        atr_value = result['atr_value']
        rsi_value = result['rsi_value']
        rsi_overbought = result['_rsi_overbought']
        vwap_breakdown = result['_vwap_breakdown']

        # V8: Use .get() with defaults to prevent KeyError on malformed positions
        # (corrupt state, reconciliation edge cases, test data)
        entry_price_val = pos.get('entry_price', current_price)
        stop_price = pos.get('stop_price', entry_price_val * 0.97)
        target_price = pos.get('target_price', entry_price_val * 1.05)
        half_target = pos.get('half_target', (entry_price_val + target_price) / 2)
        partial_done = pos.get('partial_done', False)
        qty = pos.get('quantity', pos.get('qty', 1))

        # Partial exit — sell 50% at 1x ATR, move stop to breakeven
        if not partial_done and current_price >= half_target and qty >= 2:
            # V8: Dedup — skip if PARTIAL_SELL was emitted within 60s
            import time as _time
            now_mono = _time.monotonic()
            last_partial = self._last_partial_time.get(ticker, 0)
            if now_mono - last_partial < self._PARTIAL_SELL_DEDUP_SEC:
                pass  # suppress duplicate
            else:
                self._last_partial_time[ticker] = now_mono
                return 'PARTIAL_SELL'

        # V8: Strategy-specific trailing stop logic
        strategy = pos.get('strategy', 'vwap_reclaim')
        entry_price_val = pos.get('entry_price', current_price)

        if strategy.startswith('pro:'):
            # V8: Pro tier-based trailing — breakeven at +1R, lock +1R at +2R
            risk = entry_price_val - stop_price
            if risk > 0:
                current_r = (current_price - entry_price_val) / risk

                # Determine tier from strategy name
                _tier_map = {
                    'sr_flip': 1, 'trend_pullback': 1, 'vwap_reclaim': 1,
                    'orb': 2, 'gap_and_go': 2, 'inside_bar': 2, 'flag_pennant': 2,
                    'momentum_ignition': 3, 'fib_confluence': 3,
                    'bollinger_squeeze': 3, 'liquidity_sweep': 3,
                }
                sub = strategy.split(':')[1] if ':' in strategy else ''
                tier = _tier_map.get(sub, 2)

                if tier >= 2:
                    old_stop = stop_price
                    if current_r >= 2.0:
                        lock_r = 1.0 if tier == 3 else 0.5
                        new_stop = entry_price_val + risk * lock_r
                        if new_stop > stop_price:
                            pos['stop_price'] = new_stop
                            stop_price = new_stop
                            log.info("[Trail] %s tier=%d R=%.1f stop=$%.2f→$%.2f (locked +%.1fR)",
                                     pos.get('_ticker', ''), tier, current_r, old_stop, new_stop, lock_r)
                    elif current_r >= 1.0:
                        new_stop = entry_price_val  # breakeven
                        if new_stop > stop_price:
                            pos['stop_price'] = new_stop
                            stop_price = new_stop
                            log.info("[Trail] %s tier=%d R=%.1f stop=$%.2f→$%.2f (breakeven)",
                                     pos.get('_ticker', ''), tier, current_r, old_stop, new_stop)
        else:
            # VWAP: ATR-based trailing stop (existing behavior)
            trail_stop = current_price - (atr_value * 1.0)
            if trail_stop > stop_price:
                pos['stop_price'] = trail_stop
                stop_price = trail_stop

        if current_price <= pos.get('stop_price', stop_price):
            return 'SELL_STOP'
        if current_price >= target_price:
            return 'SELL_TARGET'
        if rsi_value > rsi_overbought:
            return 'SELL_RSI'
        if vwap_breakdown:
            return 'SELL_VWAP'

        return None
