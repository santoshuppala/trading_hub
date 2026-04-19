import logging
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

    # LRU indicator cache: (ticker, df_id) → computed indicator dict
    # df_id is id(df) — unique per DataFrame object, so a new slice creates a new entry.
    # Cache is bounded to 500 entries to avoid unbounded growth.
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
        # {(ticker, df_id): indicator_dict} — avoids recomputing for identical DataFrame objects
        self._indicator_cache: dict = {}
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

        # Check indicator cache first — avoids recomputing for same DataFrame object
        _cache_key = (ticker, id(data), rsi_period, atr_period)
        _cached = self._indicator_cache.get(_cache_key)
        if _cached is not None:
            vwap          = _cached['vwap_series']
            current_price = _cached['current_price']
            prev_price    = _cached['prev_price']
            current_vwap  = _cached['current_vwap']
            prev_vwap     = _cached['prev_vwap']
            rsi_value     = _cached['rsi_value']
            atr_value     = _cached['atr_value']
        else:
            # VWAP
            vwap = compute_vwap(high, low, close, volume)
            current_price = ts(close.iloc[-1])
            prev_price    = ts(close.iloc[-2])
            current_vwap  = ts(vwap.iloc[-1])
            prev_vwap     = ts(vwap.iloc[-2])

            # RSI — returns 50.0 (neutral) if insufficient bars for the rolling window
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(window=rsi_period).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_period).mean()
            rsi_raw = ts((100 - (100 / (1 + gain / loss))).iloc[-1])
            rsi_value = rsi_raw if (rsi_raw is not None and rsi_raw == rsi_raw) else 50.0

            # ATR
            tr = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low - close.shift()).abs(),
            ], axis=1).max(axis=1)
            atr_value = ts(tr.rolling(window=atr_period).mean().iloc[-1])

            # Store in cache (evict oldest entry if full)
            if len(self._indicator_cache) >= self._MAX_CACHE:
                try:
                    self._indicator_cache.pop(next(iter(self._indicator_cache)))
                except StopIteration:
                    pass
            self._indicator_cache[_cache_key] = {
                'vwap_series': vwap,
                'current_price': current_price, 'prev_price': prev_price,
                'current_vwap': current_vwap,   'prev_vwap': prev_vwap,
                'rsi_value': rsi_value,          'atr_value': atr_value,
            }

        # Opening range bias
        open_price = ts(data['open'].iloc[0])
        open_vwap  = ts(vwap.iloc[0])
        opened_above_vwap = (
            open_price is not None and open_vwap is not None and open_price > open_vwap
        )

        if any(
            v is None or (isinstance(v, float) and v != v)
            for v in [current_price, current_vwap, prev_price, prev_vwap, rsi_value, atr_value]
        ):
            return None

        # Flexible VWAP reclaim: dip can span 1–N bars within lookback window
        closes_list = close.tolist()
        vwaps_list  = vwap.tolist()
        vwap_reclaim = _detect_vwap_reclaim(closes_list, vwaps_list, lookback=reclaim_lookback, max_dip_age=max_dip_age)

        vwap_breakdown = _detect_vwap_breakdown(closes_list, vwaps_list, rsi_value)
        reclaim_candle_low = ts(low.iloc[-2])

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
