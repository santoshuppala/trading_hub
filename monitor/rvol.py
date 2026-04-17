"""
monitor/rvol.py — Production RVOL (Relative Volume) Engine

Replaces both signals.py:get_rvol() and _compute.py:compute_rvol() with
a single canonical implementation used by ALL strategy engines.

Design:
  - 5-minute bucketed intraday volume profiles per ticker
  - 20-day rolling window with weekday weighting (same-day-of-week counts 2x)
  - Median-based expected volume (robust to outliers like FOMC/OpEx days)
  - Available from bar 1 (no first-hour blackout)
  - EMA-smoothed output to reduce single-bar spikes
  - Percentile ranking vs historical RVOL distribution

Output per ticker per bar:
  RVOLResult(
      rvol=2.35,              # raw ratio: cumulative actual / cumulative expected
      rvol_smooth=2.12,       # EMA-smoothed (5-bar)
      rvol_percentile=87.5,   # where this RVOL sits vs last 20 days (0-100)
      acceleration=1.8,       # short-term volume vs expected slope
      quality='sustained',    # 'sustained' | 'spike' | 'block_trade' | 'normal'
  )

Usage:
    from monitor.rvol import RVOLEngine, RVOLResult

    rvol_engine = RVOLEngine()
    rvol_engine.seed_profiles(tickers, data_client)     # at session start
    result = rvol_engine.update('AAPL', bar_volume=125000, bar_timestamp=ts)
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo('America/New_York')

# ── Constants ────────────────────────────────────────────────────────────────

MARKET_OPEN  = time(9, 30)
MARKET_CLOSE = time(16, 0)
BUCKET_MINUTES = 5          # 5-minute volume buckets (78 per day)
BUCKETS_PER_DAY = 78        # 390 minutes / 5
LOOKBACK_DAYS = 20          # rolling window for profile
MIN_PROFILE_DAYS = 5        # minimum days before profile is usable
WEEKDAY_WEIGHT = 2.0        # same-weekday gets 2x weight in median
EMA_SPAN = 5                # 5-bar EMA for smoothing
RVOL_CLAMP_MAX = 10.0       # cap extreme values
RVOL_CLAMP_MIN = 0.01       # floor
MIN_EXPECTED_VOLUME = 1000  # ignore if expected volume < 1000 shares
ACCELERATION_WINDOW = 3     # bars for acceleration calc


@dataclass(frozen=True)
class RVOLResult:
    """Canonical RVOL output consumed by all strategy engines."""
    rvol: float                # raw ratio: cumulative actual / cumulative expected
    rvol_smooth: float         # EMA-smoothed
    rvol_percentile: float     # 0-100: where this RVOL sits vs recent history
    acceleration: float        # short-term volume slope vs expected slope
    quality: str               # 'sustained' | 'spike' | 'block_trade' | 'normal'

    @staticmethod
    def neutral() -> RVOLResult:
        return RVOLResult(1.0, 1.0, 50.0, 1.0, 'normal')


@dataclass
class _TickerState:
    """Per-ticker mutable state for the current trading session."""
    cumulative_volume: float = 0.0
    current_bucket: int = 0
    bar_count: int = 0
    rvol_history: List[float] = field(default_factory=list)  # today's RVOL readings
    ema_state: float = 1.0    # EMA accumulator
    recent_bar_volumes: List[float] = field(default_factory=list)  # last N bar volumes


@dataclass
class _VolumeProfile:
    """
    Historical intraday volume profile for one ticker.

    Stores cumulative volume at each 5-minute bucket for the last 20 trading days.
    Used to compute "expected cumulative volume at this time of day."
    """
    # bucket_index → list of (weekday, cumulative_volume) per historical day
    buckets: Dict[int, List[Tuple[int, float]]] = field(default_factory=lambda: defaultdict(list))
    trading_dates: List[date] = field(default_factory=list)

    def expected_cumulative_at(self, bucket_idx: int, today_weekday: int) -> float:
        """
        Median expected cumulative volume at this bucket, weekday-weighted.

        Same-weekday observations count twice in the median calculation.
        Falls back to simple median if fewer than MIN_PROFILE_DAYS.
        """
        entries = self.buckets.get(bucket_idx, [])
        if not entries:
            return 0.0

        # Build weighted sample: same weekday counted twice
        values = []
        for weekday, cum_vol in entries:
            values.append(cum_vol)
            if weekday == today_weekday:
                values.append(cum_vol)  # double-count same weekday

        if not values:
            return 0.0

        return median(values)

    def historical_rvol_at(self, bucket_idx: int, today_weekday: int) -> List[float]:
        """
        Compute what RVOL was at this bucket on each historical day.
        Used for percentile ranking.
        """
        expected = self.expected_cumulative_at(bucket_idx, today_weekday)
        if expected <= 0:
            return []

        entries = self.buckets.get(bucket_idx, [])
        return [cum_vol / expected for _, cum_vol in entries if cum_vol > 0]


class RVOLEngine:
    """
    Production RVOL engine for ~200 tickers in real-time.

    Lifecycle:
      1. seed_profiles() — called once at session start, loads 20-day history
      2. update() — called on every BAR event, returns RVOLResult
      3. reset_session() — called at EOD, clears today's state

    Thread-safe: all state mutations under lock.
    """

    def __init__(self) -> None:
        self._profiles: Dict[str, _VolumeProfile] = {}
        self._state: Dict[str, _TickerState] = defaultdict(_TickerState)
        self._lock = threading.Lock()
        self._session_date: Optional[date] = None
        log.info("[RVOLEngine] initialized")

    # ── Profile Seeding (session start) ──────────────────────────────────────

    def seed_profiles(self, tickers: List[str], data_client) -> None:
        """
        Precompute 20-day intraday volume profiles for all tickers.
        Called once at session start. Uses data_client.get_daily_history()
        for daily data, or intraday bars if available.

        Args:
            tickers: list of symbols
            data_client: TradierDataClient or AlpacaDataClient with
                         get_daily_history(ticker, start, end) method
        """
        today = datetime.now(ET).date()
        self._session_date = today
        start = today - timedelta(days=LOOKBACK_DAYS + 10)  # buffer for weekends

        seeded = 0
        for ticker in tickers:
            try:
                self._seed_one(ticker, data_client, start, today)
                seeded += 1
            except Exception as e:
                log.debug("[RVOLEngine] seed failed for %s: %s", ticker, e)

        # V8: Warn if seed rate drops below 80%
        seed_rate = seeded / len(tickers) if tickers else 0
        if seed_rate < 0.80:
            log.warning(
                "[RVOLEngine] LOW SEED RATE: only %d/%d tickers seeded (%.0f%%). "
                "RVOL data may be unreliable for unseeded tickers.",
                seeded, len(tickers), seed_rate * 100,
            )
        else:
            log.info("[RVOLEngine] seeded profiles for %d/%d tickers", seeded, len(tickers))

    def _seed_one(self, ticker: str, data_client, start: date, today: date) -> None:
        """Build volume profile for one ticker from historical data."""
        profile = _VolumeProfile()

        # Try to get intraday bars (minute-level — best quality)
        hist_df = None
        try:
            end_dt = datetime.combine(today, time(0), tzinfo=ET)
            start_dt = datetime.combine(start, time(0), tzinfo=ET)
            hist_df = data_client.get_daily_history(ticker, start_dt, end_dt)
        except Exception:
            pass

        if hist_df is None or hist_df.empty:
            return

        # Check if this is minute data or daily data
        sample_dates = sorted({idx.date() for idx in hist_df.index if idx.date() < today})
        if not sample_dates:
            return

        bars_on_sample = int((hist_df.index.date == sample_dates[0]).sum())
        is_minute_data = bars_on_sample > 1

        if is_minute_data:
            self._seed_from_minute_bars(profile, hist_df, today)
        else:
            self._seed_from_daily_bars(profile, hist_df, today)

        profile.trading_dates = sample_dates[-LOOKBACK_DAYS:]

        with self._lock:
            self._profiles[ticker] = profile

    def _seed_from_minute_bars(
        self, profile: _VolumeProfile, df, today: date,
    ) -> None:
        """Build profile from historical minute-level bars (best quality)."""
        past_dates = sorted({idx.date() for idx in df.index if idx.date() < today})
        for d in past_dates[-LOOKBACK_DAYS:]:
            day_data = df[df.index.date == d]
            if day_data.empty:
                continue

            weekday = d.weekday()
            cum_vol = 0.0
            bucket_vols: Dict[int, float] = {}

            for _, row in day_data.iterrows():
                vol = float(row.get('volume', 0))
                cum_vol += vol
                idx = row.name
                if hasattr(idx, 'hour'):
                    minutes_since_open = (idx.hour * 60 + idx.minute) - (9 * 60 + 30)
                    bucket = max(0, min(minutes_since_open // BUCKET_MINUTES, BUCKETS_PER_DAY - 1))
                    bucket_vols[bucket] = cum_vol

            for bucket, cum in bucket_vols.items():
                profile.buckets[bucket].append((weekday, cum))

    def _seed_from_daily_bars(
        self, profile: _VolumeProfile, df, today: date,
    ) -> None:
        """
        Build approximate profile from daily bars using the canonical
        intraday volume distribution curve.

        U-shaped curve: 15% in first 30min, 45% over mid-session, 40% in last 2 hours.
        """
        past_dates = sorted({idx.date() for idx in df.index if idx.date() < today})

        for d in past_dates[-LOOKBACK_DAYS:]:
            day_rows = df[df.index.date == d]
            if day_rows.empty:
                continue

            daily_vol = float(day_rows['volume'].iloc[0])
            weekday = d.weekday()

            for bucket in range(BUCKETS_PER_DAY):
                pct = _INTRADAY_VOLUME_CURVE[bucket]
                cum_vol = daily_vol * pct
                profile.buckets[bucket].append((weekday, cum_vol))

    # ── Real-Time Update (per bar) ───────────────────────────────────────────

    def update(
        self,
        ticker: str,
        bar_volume: float,
        bar_timestamp: Optional[datetime] = None,
    ) -> RVOLResult:
        """
        Update RVOL for one ticker with new bar data.

        Called on every BAR event. Returns canonical RVOLResult.

        Args:
            ticker: symbol
            bar_volume: volume of this bar
            bar_timestamp: timestamp (uses now if None)

        Returns:
            RVOLResult with rvol, rvol_smooth, percentile, acceleration, quality
        """
        if bar_timestamp is None:
            bar_timestamp = datetime.now(ET)

        with self._lock:
            state = self._state[ticker]
            profile = self._profiles.get(ticker)

            # V8: Dedup by bar timestamp — after merge, both Pro and VWAP
            # call update() for the same ticker on the same BAR event.
            # Without this check, volume is double-counted → RVOL inflated 2x.
            last_ts = getattr(state, '_last_bar_ts', None)
            if last_ts is not None and last_ts == bar_timestamp:
                # Same bar already processed — return cached result
                cached = getattr(state, '_last_result', None)
                if cached is not None:
                    return cached
            state._last_bar_ts = bar_timestamp

        # Accumulate volume
        state.cumulative_volume += bar_volume
        state.bar_count += 1
        state.recent_bar_volumes.append(bar_volume)
        if len(state.recent_bar_volumes) > ACCELERATION_WINDOW * 2:
            state.recent_bar_volumes = state.recent_bar_volumes[-(ACCELERATION_WINDOW * 2):]

        # Compute bucket index
        if hasattr(bar_timestamp, 'hour'):
            minutes_since_open = (bar_timestamp.hour * 60 + bar_timestamp.minute) - (9 * 60 + 30)
        else:
            minutes_since_open = state.bar_count  # fallback
        bucket = max(0, min(minutes_since_open // BUCKET_MINUTES, BUCKETS_PER_DAY - 1))
        state.current_bucket = bucket

        # No profile → return neutral with actual cumulative tracked
        if profile is None or len(profile.trading_dates) < MIN_PROFILE_DAYS:
            return RVOLResult.neutral()

        today_weekday = bar_timestamp.weekday() if hasattr(bar_timestamp, 'weekday') else 0

        # Expected cumulative volume at this time of day
        expected = profile.expected_cumulative_at(bucket, today_weekday)
        if expected < MIN_EXPECTED_VOLUME:
            return RVOLResult.neutral()

        # Raw RVOL
        raw_rvol = state.cumulative_volume / expected
        raw_rvol = max(RVOL_CLAMP_MIN, min(raw_rvol, RVOL_CLAMP_MAX))

        # EMA smoothing
        alpha = 2.0 / (EMA_SPAN + 1)
        state.ema_state = alpha * raw_rvol + (1 - alpha) * state.ema_state
        smooth_rvol = max(RVOL_CLAMP_MIN, min(state.ema_state, RVOL_CLAMP_MAX))

        # Percentile ranking
        hist_rvols = profile.historical_rvol_at(bucket, today_weekday)
        if hist_rvols:
            below = sum(1 for h in hist_rvols if h < raw_rvol)
            percentile = (below / len(hist_rvols)) * 100.0
        else:
            percentile = 50.0

        # Acceleration: recent volume slope vs expected slope
        acceleration = self._compute_acceleration(state, profile, bucket, today_weekday)

        # Quality classification
        quality = self._classify_quality(raw_rvol, smooth_rvol, state)

        # Store for percentile history
        state.rvol_history.append(raw_rvol)

        result = RVOLResult(
            rvol=round(raw_rvol, 4),
            rvol_smooth=round(smooth_rvol, 4),
            rvol_percentile=round(percentile, 1),
            acceleration=round(acceleration, 4),
            quality=quality,
        )
        # V8: Cache result for dedup — second caller gets same result without recomputing
        state._last_result = result
        return result

    def get_rvol(self, ticker: str) -> float:
        """
        Backward-compatible: returns the latest smooth RVOL for a ticker.
        Used as drop-in replacement for signals.py:get_rvol() and
        _compute.py:compute_rvol().
        """
        state = self._state.get(ticker)
        if state is None:
            return 1.0
        return max(RVOL_CLAMP_MIN, min(state.ema_state, RVOL_CLAMP_MAX))

    # ── Acceleration ─────────────────────────────────────────────────────────

    def _compute_acceleration(
        self,
        state: _TickerState,
        profile: _VolumeProfile,
        bucket: int,
        weekday: int,
    ) -> float:
        """
        Compare recent volume slope to expected slope.

        acceleration > 1.0: volume accelerating faster than expected
        acceleration < 1.0: volume decelerating
        """
        vols = state.recent_bar_volumes
        if len(vols) < ACCELERATION_WINDOW * 2:
            return 1.0

        recent = sum(vols[-ACCELERATION_WINDOW:])
        prior = sum(vols[-ACCELERATION_WINDOW * 2:-ACCELERATION_WINDOW])
        if prior <= 0:
            return 1.0

        actual_slope = recent / prior

        # Expected slope: how much volume SHOULD increase between these buckets
        prev_bucket = max(0, bucket - ACCELERATION_WINDOW)
        exp_now = profile.expected_cumulative_at(bucket, weekday)
        exp_prev = profile.expected_cumulative_at(prev_bucket, weekday)
        if exp_prev <= 0:
            return actual_slope

        expected_slope = (exp_now - exp_prev) / exp_prev if exp_prev > 0 else 1.0
        if expected_slope <= 0:
            return actual_slope

        return actual_slope / expected_slope if expected_slope > 0 else 1.0

    # ── Quality Classification ───────────────────────────────────────────────

    @staticmethod
    def _classify_quality(raw: float, smooth: float, state: _TickerState) -> str:
        """
        Classify volume quality:
          sustained    — smooth RVOL > 2.0 for 10+ bars (institutional flow)
          spike        — raw > 3x smooth (single bar burst)
          block_trade  — raw > 5.0 but smooth < 2.0 (one large print)
          normal       — everything else
        """
        if raw > 5.0 and smooth < 2.0:
            return 'block_trade'

        if smooth > 0 and raw > smooth * 3.0:
            return 'spike'

        high_bars = sum(1 for r in state.rvol_history[-15:] if r > 2.0)
        if high_bars >= 10 and smooth > 2.0:
            return 'sustained'

        return 'normal'

    # ── Session Management ───────────────────────────────────────────────────

    def reset_session(self) -> None:
        """Reset all per-ticker state for a new trading day."""
        with self._lock:
            self._state.clear()
            self._session_date = datetime.now(ET).date()
        log.info("[RVOLEngine] session reset")

    def seed_from_bar_payload(self, ticker: str, rvol_df) -> None:
        """
        Convenience: seed profile from a BarPayload's rvol_df if no
        prior seed_profiles() call was made. Graceful fallback.
        """
        if ticker in self._profiles:
            return  # already seeded

        if rvol_df is None or rvol_df.empty:
            return

        profile = _VolumeProfile()
        today = datetime.now(ET).date()

        bars_on_first = 1
        dates = sorted({idx.date() for idx in rvol_df.index if idx.date() < today})
        if dates:
            bars_on_first = int((rvol_df.index.date == dates[0]).sum())

        if bars_on_first > 1:
            self._seed_from_minute_bars(profile, rvol_df, today)
        else:
            self._seed_from_daily_bars(profile, rvol_df, today)

        profile.trading_dates = dates[-LOOKBACK_DAYS:]

        with self._lock:
            self._profiles[ticker] = profile


# ── Canonical Intraday Volume Curve ──────────────────────────────────────────
# Empirical U-shaped cumulative distribution for US equities.
# Source: aggregated from TAQ data across S&P 500 constituents.
# Each entry = cumulative % of daily volume by bucket end.
#
# Bucket 0 = 9:30-9:35 (first 5 min), Bucket 77 = 15:55-16:00 (last 5 min)

def _build_volume_curve() -> List[float]:
    """
    Build empirical intraday cumulative volume curve.

    Shape: heavy at open, light midday, heavy at close.
    First 30 min: ~18% of daily volume
    Mid-session (10:00-14:00): ~42%
    Last 2 hours (14:00-16:00): ~40%
    """
    curve = []
    total_buckets = BUCKETS_PER_DAY

    for i in range(total_buckets):
        # Minutes since open for this bucket's midpoint
        mid_min = i * BUCKET_MINUTES + BUCKET_MINUTES / 2

        # Piecewise volume intensity (relative, will be normalized)
        if mid_min <= 15:       # 9:30-9:45: opening auction, very high
            intensity = 4.0
        elif mid_min <= 30:     # 9:45-10:00: settling, high
            intensity = 2.5
        elif mid_min <= 60:     # 10:00-10:30: morning continuation
            intensity = 1.5
        elif mid_min <= 120:    # 10:30-11:30: mid-morning lull
            intensity = 0.8
        elif mid_min <= 210:    # 11:30-13:00: lunch doldrums
            intensity = 0.6
        elif mid_min <= 270:    # 13:00-14:00: early afternoon pickup
            intensity = 0.8
        elif mid_min <= 330:    # 14:00-15:00: institutional rebalancing
            intensity = 1.3
        elif mid_min <= 360:    # 15:00-15:30: power hour ramp
            intensity = 2.0
        else:                   # 15:30-16:00: closing auction
            intensity = 3.0

        curve.append(intensity)

    # Normalize to cumulative percentages
    total_intensity = sum(curve)
    cumulative = 0.0
    result = []
    for intensity in curve:
        cumulative += intensity / total_intensity
        result.append(cumulative)

    return result


_INTRADAY_VOLUME_CURVE = _build_volume_curve()

# ── Global Singleton ─────────────────────────────────────────────────────────
# Initialized by RealTimeMonitor at startup. Consumed by signals.py and
# _compute.py for backward-compatible RVOL access.
_global_rvol_engine: Optional[RVOLEngine] = None


def init_global_rvol_engine() -> RVOLEngine:
    """Initialize the global RVOL engine. Called once by run_monitor.py."""
    global _global_rvol_engine
    _global_rvol_engine = RVOLEngine()
    log.info("[RVOLEngine] global instance created")
    return _global_rvol_engine
