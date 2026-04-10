import backtrader as bt
from collections import deque
from .base import BaseTradeStrategy
from vwap_utils import detect_vwap_reclaim as _detect_vwap_reclaim
from vwap_utils import detect_vwap_breakdown as _detect_vwap_breakdown


class VWAPReclaimStrategy(BaseTradeStrategy):
    """
    Backtest implementation of the live VWAP Reclaim strategy.

    Entry — ALL conditions must be true:
      1. Flexible VWAP reclaim: uptrend context → dip (1–N bars) → 2-bar reclaim
      2. Opened above VWAP (bullish day bias)
      3. RSI 50–70

    Exit:
      - Trailing stop at 1×ATR below price
      - Target at 2×ATR above entry
      - RSI overbought (> rsi_overbought)
      - VWAP breakdown (price crosses below VWAP)
      - EOD force-close when date changes (intraday only)

    Timeframe notes:
      - On 1m/5m/1h data: VWAP resets each day, mirrors the live strategy exactly.
        Trading hours guard: skip first 15 bars (≈ 9:30–9:45 AM).
      - On 1d data: uses a rolling N-bar VWAP (vwap_period param, default 20).
        Each day's "opened above VWAP" is checked against the current rolling VWAP.
        This turns the strategy into a swing-trade variant.
    """

    params = (
        ('rsi_period', 14),
        ('atr_period', 14),
        ('rsi_overbought', 70),
        ('atr_multiplier', 2.0),
        ('min_stop_pct', 0.005),
        ('vwap_period', 20),        # rolling window for daily VWAP; ignored on intraday
        ('reclaim_lookback', 6),    # bars to search for dip before the 2-bar confirmation
        ('max_dip_age', 4),         # max bars between end of dip and first confirm bar
        ('open_cost', 0.0),
        ('close_cost', 0.0),
        ('interval', '1d'),
        ('trade_log', None),
    )

    def __init__(self):
        super().__init__()
        self.rsi = bt.indicators.RSI(self.data.close, period=self.params.rsi_period)
        self.atr = bt.indicators.ATR(self.data, period=self.params.atr_period)

        self._intraday = self.params.interval not in ('1d', '1wk', '1mo')

        # Intraday VWAP state (resets each day)
        self._vwap_num = 0.0
        self._vwap_den = 0.0
        self._last_date = None
        self._bars_today = 0
        self._opened_above_vwap = False

        # Rolling VWAP state for daily (sliding window)
        w = self.params.vwap_period
        self._roll_tp  = deque(maxlen=w)
        self._roll_vol = deque(maxlen=w)

        # History buffers — sized to cover full lookback + 2 confirm bars
        buf = self.params.reclaim_lookback + 4
        self._vwap_history  = deque(maxlen=buf)
        self._close_history = deque(maxlen=buf)

        # Position tracking
        self.stop_price   = None
        self.target_price = None

    # ------------------------------------------------------------------

    def _reset_intraday(self):
        self._vwap_num = 0.0
        self._vwap_den = 0.0
        self._vwap_history.clear()
        self._close_history.clear()
        self._opened_above_vwap = False
        self._bars_today = 0

    def _rolling_vwap(self):
        tot_vol = sum(self._roll_vol)
        if tot_vol == 0:
            return None
        return sum(tp * v for tp, v in zip(self._roll_tp, self._roll_vol)) / tot_vol

    # ------------------------------------------------------------------

    def next(self):
        current_date = self.data.datetime.date(0)

        # ── Intraday: reset VWAP each day and force-close EOD ─────────
        if self._intraday:
            if current_date != self._last_date:
                if self._last_date is not None and self.position:
                    self.close()
                    self.stop_price   = None
                    self.target_price = None
                self._reset_intraday()
            self._last_date = current_date

        # ── Compute VWAP for this bar ──────────────────────────────────
        typical = (self.data.high[0] + self.data.low[0] + self.data.close[0]) / 3

        if self._intraday:
            self._vwap_num += typical * self.data.volume[0]
            self._vwap_den += self.data.volume[0]
            self._bars_today += 1
            if self._vwap_den == 0:
                return
            current_vwap = self._vwap_num / self._vwap_den

            if self._bars_today == 1:
                self._opened_above_vwap = self.data.open[0] > current_vwap
        else:
            self._roll_tp.append(typical)
            self._roll_vol.append(self.data.volume[0])
            current_vwap = self._rolling_vwap()
            if current_vwap is None:
                return
            self._opened_above_vwap = self.data.open[0] > current_vwap

        self._vwap_history.append(current_vwap)
        self._close_history.append(self.data.close[0])

        # ── Need enough history + warm indicators ──────────────────────
        min_bars = self.params.reclaim_lookback + 2
        if len(self._vwap_history) < min_bars:
            return
        if self.rsi[0] != self.rsi[0] or self.atr[0] != self.atr[0]:  # NaN
            return

        closes = list(self._close_history)
        vwaps  = list(self._vwap_history)

        # ── Pattern detection ──────────────────────────────────────────
        vwap_reclaim = _detect_vwap_reclaim(
            closes, vwaps,
            lookback=self.params.reclaim_lookback,
            max_dip_age=self.params.max_dip_age,
        )
        rsi            = self.rsi[0]
        atr            = self.atr[0]
        price_curr     = self.data.close[0]
        vwap_breakdown = _detect_vwap_breakdown(closes, vwaps, rsi)

        rsi_bullish    = 50 <= rsi <= self.params.rsi_overbought
        rsi_overbought = rsi > self.params.rsi_overbought

        # ── Entry ──────────────────────────────────────────────────────
        if not self.position:
            if self._intraday and self._bars_today < 15:
                return

            if vwap_reclaim and self._opened_above_vwap and rsi_bullish:
                self.buy()
                reclaim_low       = self.data.low[-1]
                candle_stop       = reclaim_low - (atr * 0.5)
                pct_stop          = price_curr * (1 - self.params.min_stop_pct)
                self.stop_price   = min(candle_stop, pct_stop)
                self.target_price = price_curr + (atr * self.params.atr_multiplier)

        # ── Exit ───────────────────────────────────────────────────────
        else:
            trail = price_curr - (atr * 1.0)
            if self.stop_price is None or trail > self.stop_price:
                self.stop_price = trail

            hit_stop   = price_curr <= self.stop_price
            hit_target = self.target_price is not None and price_curr >= self.target_price
            hit_rsi    = rsi_overbought
            hit_vwap   = vwap_breakdown

            if hit_stop or hit_target or hit_rsi or hit_vwap:
                self.close()
                self.stop_price   = None
                self.target_price = None
