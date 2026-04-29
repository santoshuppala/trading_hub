"""
TickSignalDetector — sub-second entry detection from WebSocket trade ticks.

Runs 5 Pro-style detectors on each tick instead of waiting for 60s bar close.
Uses pre-computed levels (from BAR-based detectors) + real-time tick price.

Detectors (tick-level):
  1. sr_flip:            price crosses S/R level
  2. orb:                price breaks opening range
  3. momentum_ignition:  volume spike + range expansion
  4. gap_and_go:         continuation in gap direction
  5. liquidity_sweep:    sweep below support then reverse

What comes from bars (updated every 60s):
  - S/R levels, ORB range, gap direction, nearest support/resistance
  - RSI, ATR, RVOL, VWAP (from last completed bar)

What comes from ticks (real-time):
  - Current price, volume, bid/ask spread
  - Rolling volume/price accumulators for momentum detection

Performance: <10μs per tick, 2000 ticks/sec = 20ms/sec total CPU.
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Optional

log = logging.getLogger(__name__)

# Feature flag
_FF_ENABLED = os.getenv('FF_TICK_ENTRIES', 'true').lower() not in ('false', '0', 'no')

# Minimum seconds between signals for same ticker (prevents rapid-fire)
_SIGNAL_COOLDOWN = 60.0

# Rolling window for momentum detection (seconds)
_MOMENTUM_WINDOW = 30.0


@dataclass
class TickSignal:
    """Signal produced by a tick-level detector."""
    ticker: str
    strategy: str        # sr_flip, orb, momentum_ignition, gap_and_go, liquidity_sweep, trend_pullback
    direction: str       # 'long' or 'short'
    entry_price: float   # limit price (for stop-limit: the max fill price)
    stop_price: float
    target_1: float
    target_2: float
    atr: float
    rsi: float
    rvol: float
    confidence: float    # 0-1
    activation_price: float = 0.0  # for stop-limit orders: price that activates the order


@dataclass
class PendingSetup:
    """Setup registered by bar-level detection, awaiting tick-level trigger.

    Two-stage system: ProSetupEngine detects "setup exists" on bars,
    TickDetector triggers entry when live tick confirms the bounce.
    Expires after 3 minutes if no tick confirmation.
    """
    ticker: str
    strategy: str           # 'trend_pullback' (extensible)
    direction: str          # 'long'
    ema9: float             # 1-min EMA9 at detection time
    ema20: float            # 1-min EMA20 at detection time
    stop_price: float
    target_1: float
    target_2: float
    atr: float
    rsi: float
    rvol: float
    confidence: float
    created_at: float       # monotonic time
    expires_at: float       # created_at + 180s (3 minutes)


class TickSignalDetector:
    """Detects entry signals from WebSocket trade ticks.

    Lifecycle:
      1. Created once during monitor startup
      2. update_levels() called by ProSetupEngine on each BAR (every 60s)
      3. on_tick() called by TradierStream on each trade tick (~10/sec/ticker)
      4. Emits PRO_STRATEGY_SIGNAL events when detectors fire

    Data-readiness model (not wall-clock):
      - Detects overnight gaps via prev_close (regime: none/small/medium/large)
      - Large gaps invalidate all snapshot S/R levels
      - Detectors gated by data readiness (has live BAR levels), not time
      - Mid-session restart: ready in ~12-72s, not artificially blind for 5 min
      - Session high/low/VWAP updated from ticks in real-time
    """

    # Gap thresholds (ATR multiples) for regime classification
    _GAP_SMALL = 0.5    # <0.5 ATR = no gap, reuse all levels
    _GAP_MEDIUM = 2.0   # 0.5-2.0 ATR = moderate gap, degrade confidence
    _GAP_LARGE = 4.0    # >4.0 ATR = earnings/news gap, invalidate all levels
    # Indicator freshness: max seconds before indicators are stale
    _INDICATOR_MAX_AGE = 1800.0  # 30 minutes
    # Per-level cooldown: seconds between signals from same level
    _LEVEL_COOLDOWN_SEC = 10.0
    # Tick burst: max ticks per 100ms window before adaptive sampling
    _BURST_THRESHOLD = 50
    # Level must exist for N bar cycles before trusted
    _LEVEL_MIN_AGE_BARS = 2
    # Bar interval (seconds) — used to compute level min age
    _BAR_INTERVAL = 60.0
    # Minimum live BAR count before a ticker is "data ready"
    _MIN_LIVE_BARS = 2
    # ORB time cutoff: minutes after market open after which ORB is disabled
    _ORB_CUTOFF_MINUTES = 30.0
    # Confidence weights (additive, not multiplicative)
    _W_SIGNAL = 0.50    # base signal strength
    _W_REGIME = 0.20    # gap regime quality
    _W_FRESH = 0.20     # indicator freshness
    _W_STABLE = 0.10    # level stability
    # Trade freeze: observe signals but don't execute until data is trustworthy
    _FREEZE_LOG_INTERVAL = 60.0  # log suppressed signals every 60s, not every tick

    def __init__(self, bus, positions: dict):
        """
        Args:
            bus: EventBus for emitting signals
            positions: shared positions dict (read-only, for dedup)
        """
        self._bus = bus
        self._positions = positions

        # Pre-computed levels from BAR detectors (updated every 60s)
        # {ticker: {sr_levels, orb_high, orb_low, gap_direction, ...}}
        self._levels: Dict[str, dict] = {}

        # Indicators from last completed bar
        # {ticker: {rsi, atr, rvol, vwap}}
        self._indicators: Dict[str, dict] = {}

        # Data-readiness model (replaces wall-clock warm-up)
        # A ticker is "ready" when it has >= _MIN_LIVE_BARS + FRESH indicators.
        # Not time-based — a restart at 11:15 with fresh data is ready after 2 BARs.
        self._levels_from_live: set = set()  # tickers that got fresh BAR-computed levels
        self._live_bar_count: Dict[str, int] = defaultdict(int)  # live BARs per ticker
        self._prev_close: Dict[str, float] = {}  # {ticker: previous day close} for gap detection

        # Gap regime per ticker: 'none', 'small', 'medium', 'large'
        # Classified on FIRST TICK (not first bar) for immediate context.
        self._gap_regime: Dict[str, str] = {}  # {ticker: regime}
        self._gap_atr: Dict[str, float] = {}   # {ticker: gap_size_in_atr}

        # Market open anchor: for ORB cutoff. Set to actual 9:30 ET on first tick,
        # NOT restart time. On restart at 11:15, ORB cutoff correctly uses 9:30.
        self._market_open_mono: float = 0.0  # monotonic time equivalent of 9:30 ET

        # Indicator freshness: monotonic time when indicators were last updated from LIVE bar
        self._indicator_updated_at: Dict[str, float] = {}

        # Per-tick rolling state (includes running VWAP for intra-bar tracking)
        self._tick_state: Dict[str, dict] = defaultdict(lambda: {
            'prev_price': 0.0,
            'ticks': [],           # [(time, price, volume)] rolling window
            'swept': False,
            'sweep_low': 0.0,
            'sweep_time': 0.0,
            'vwap_pv': 0.0,        # cumulative price × volume (for tick VWAP)
            'vwap_v': 0,           # cumulative volume
        })

        # Dedup: last signal time per ticker
        self._last_signal_time: Dict[str, float] = {}
        # Per-level cooldown: {(ticker, level_rounded): monotonic_time}
        self._level_cooldown: Dict[tuple, float] = {}

        # Tick burst tracking + adaptive sampling
        self._tick_counts: Dict[str, int] = defaultdict(int)
        self._tick_window_start: float = 0.0
        self._sample_counter: Dict[str, int] = defaultdict(int)  # per-ticker tick counter
        self._sample_rate: int = 1  # process every Nth tick (1=all, 2=half, etc.)

        # Level stability: {(ticker, level_rounded): first_seen_monotonic}
        self._level_first_seen: Dict[tuple, float] = {}

        # Two-stage pending setups: {ticker: PendingSetup}
        # Registered by ProSetupEngine for strategies that need tick confirmation.
        # Ephemeral: expire after 3 min. One per ticker (latest wins).
        self._pending_setups: Dict[str, PendingSetup] = {}
        self._setups_registered = 0
        self._setups_triggered = 0
        self._setups_expired = 0

        # Level versioning: monotonic counter per ticker, incremented on each
        # update_levels call. Ticks read (levels, version) as a snapshot to
        # detect stale reads when a BAR update races with tick processing.
        self._level_version: Dict[str, int] = defaultdict(int)

        # Trade freeze: signals generate and log, but DON'T emit ORDER_REQ
        # until the system has trustworthy data.
        # Freeze lifts when: REST reseed complete OR 2+ tickers data-ready.
        # This prevents real money loss from early garbage signals after restart.
        self._order_freeze = True
        self._freeze_lifted_at: float = 0.0
        self._signals_frozen = 0  # signals observed but not emitted during freeze
        self._last_freeze_log: float = 0.0

        self._signals_emitted = 0
        self._ticks_processed = 0
        self._ticks_skipped_burst = 0

        if _FF_ENABLED:
            log.info("[TickDetector] Initialized — 5 tick-level detectors active")
        else:
            log.info("[TickDetector] DISABLED via FF_TICK_ENTRIES=false")

    # ── Public API ───────────────────────────────────────────────────

    def update_levels(self, ticker: str, levels: dict,
                      from_snapshot: bool = False) -> None:
        """Called by ProSetupEngine on each BAR to push pre-computed levels.

        Args:
            from_snapshot: True if seeded from market snapshot (stale).
                           Snapshot levels are stored but don't count toward
                           live bar count or data readiness.
        """
        now = time.monotonic()
        self._levels[ticker] = levels
        self._level_version[ticker] += 1
        if not from_snapshot:
            self._levels_from_live.add(ticker)
            self._live_bar_count[ticker] += 1

        # ── Level stability tracking ─────────────────────────────────
        # Track when each S/R level was first seen — new levels can't be trusted yet.
        sr_levels = levels.get('sr_levels', [])
        for lv in sr_levels:
            key = (ticker, round(lv, 2))
            if key not in self._level_first_seen:
                self._level_first_seen[key] = now

        # ── Gap regime refinement from BAR data ──────────────────────
        # Primary gap classification happens on first TICK (in on_tick).
        # Here we refine with official session_open from BAR if tick-based
        # classification hasn't happened yet (e.g., no prev_close available).
        if ticker not in self._gap_regime:
            prev_close = self._prev_close.get(ticker, 0)
            ind = self._indicators.get(ticker, {})
            atr = ind.get('atr', 0)
            open_price = levels.get('session_open', 0)
            if prev_close > 0 and atr > 0 and open_price > 0:
                gap_atr = abs(open_price - prev_close) / atr
                self._gap_atr[ticker] = gap_atr
                if gap_atr < self._GAP_SMALL:
                    self._gap_regime[ticker] = 'none'
                elif gap_atr < self._GAP_MEDIUM:
                    self._gap_regime[ticker] = 'small'
                elif gap_atr < self._GAP_LARGE:
                    self._gap_regime[ticker] = 'medium'
                else:
                    self._gap_regime[ticker] = 'large'
                    levels['sr_levels'] = []
                    levels['nearest_support'] = 0
                    levels['nearest_resistance'] = 0
                    log.warning("[TickDetector] LARGE GAP %s: %.1f ATR "
                                "(bar open=$%.2f prev=$%.2f) — S/R INVALIDATED",
                                ticker, gap_atr, open_price, prev_close)

        # ── Trade freeze: auto-lift when enough tickers are data-ready ──
        # Freeze lifts when 10+ tickers have 2+ live BARs + fresh indicators.
        # This means REST reseed happened AND ProSetupEngine processed real data.
        if self._order_freeze:
            _ready_count = sum(
                1 for t in self._live_bar_count
                if self._live_bar_count[t] >= self._MIN_LIVE_BARS
                and t in self._indicator_updated_at
            )
            if _ready_count >= 10:
                self._order_freeze = False
                self._freeze_lifted_at = time.monotonic()
                log.info("[TickDetector] FREEZE LIFTED — %d tickers data-ready, "
                         "%d signals were held during freeze",
                         _ready_count, self._signals_frozen)

        # ── Periodic memory cleanup (every 100 update_levels calls) ───
        _total_calls = sum(self._live_bar_count.values())
        if _total_calls > 0 and _total_calls % 100 == 0:
            _cutoff = now - 600  # remove entries older than 10 minutes
            self._level_first_seen = {
                k: v for k, v in self._level_first_seen.items() if v > _cutoff
            }
            self._level_cooldown = {
                k: v for k, v in self._level_cooldown.items() if v > _cutoff
            }
            # Prune expired pending setups
            _expired = [k for k, v in self._pending_setups.items() if v.expires_at < now]
            for k in _expired:
                del self._pending_setups[k]
                self._setups_expired += 1

        # ── Store today's close for tomorrow's gap detection ─────────
        close = levels.get('close', levels.get('session_close', 0))
        if close > 0:
            self._prev_close[ticker] = close

    def update_indicators(self, ticker: str, indicators: dict,
                          from_snapshot: bool = False) -> None:
        """Called by ProSetupEngine on each BAR to push last-bar indicators.

        Args:
            from_snapshot: True if seeded from market snapshot (stale).
                           Stale indicators are usable but with freshness penalty.
        """
        self._indicators[ticker] = indicators
        if not from_snapshot:
            self._indicator_updated_at[ticker] = time.monotonic()

    def _is_data_ready(self, ticker: str) -> bool:
        """True if ticker has enough LIVE data for reliable signals.

        Requires BOTH:
          - At least _MIN_LIVE_BARS (2) live BAR cycles processed
          - Indicators refreshed from live computation (not snapshot)

        This prevents acting on hybrid snapshot+1bar data where S/R levels
        are computed from yesterday's bars + one partial live bar.

        Timeline:
          - 9:30 open: ready after 2nd BAR flush (~120s)
          - 11:15 restart: ready after 2nd BAR cycle (~120s from restart)
            BUT: snapshot data is from today (accurate), so the wait is safe —
            broker stops protect positions, and we avoid false signals from
            hybrid data.
        """
        if self._live_bar_count.get(ticker, 0) < self._MIN_LIVE_BARS:
            return False
        # Also need fresh indicators (not just snapshot)
        if ticker not in self._indicator_updated_at:
            return False
        return True

    def _compute_confidence(self, ticker: str, base_signal: float,
                             indicators_fresh: bool) -> float:
        """Compute signal confidence using weighted additive model.

        Additive prevents the multiplicative collapse problem where
        small penalties kill legitimate signals:
          Multiplicative: 0.8 × 0.6 × 0.3 = 0.144 (dead)
          Additive:       0.5×0.8 + 0.2×0.6 + 0.2×1.0 + 0.1×0.3 = 0.75 (alive)

        Components:
          _W_SIGNAL (0.50): base signal strength from detector
          _W_REGIME (0.20): gap regime quality (1.0=none, 0.0=large)
          _W_FRESH  (0.20): indicator freshness (1.0=fresh, 0.3=stale)
          _W_STABLE (0.10): level stability (1.0=mature, 0.3=new)
        """
        # Regime score
        regime = self._gap_regime.get(ticker, 'none')
        regime_score = {'none': 1.0, 'small': 0.8,
                        'medium': 0.4, 'large': 0.0}.get(regime, 0.5)

        # Freshness score
        fresh_score = 1.0 if indicators_fresh else 0.3

        # Stability score (use live bar count as proxy)
        bars = self._live_bar_count.get(ticker, 0)
        stability_score = min(1.0, bars / max(self._MIN_LIVE_BARS, 1))

        confidence = (self._W_SIGNAL * base_signal
                      + self._W_REGIME * regime_score
                      + self._W_FRESH * fresh_score
                      + self._W_STABLE * stability_score)
        return confidence

    def on_tick(self, ticker: str, price: float, size: int,
                cvol: int, timestamp: float) -> None:
        """Called by TradierStream on each trade tick.

        Must complete in <10μs for 2000 ticks/sec budget.
        """
        if not _FF_ENABLED:
            return

        now = time.monotonic()

        # Compute market open anchor (once) — ORB cutoff relative to 9:30 ET, not restart
        if self._market_open_mono <= 0:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            _now_et = datetime.now(ZoneInfo('America/New_York'))
            _open_930 = _now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            _secs_since_open = (_now_et - _open_930).total_seconds()
            # monotonic "9:30" = now - seconds_since_930
            self._market_open_mono = now - max(0, _secs_since_open)

        # ── Fix #3: Tick-level gap detection (before any BAR) ────────
        # Classify gap on FIRST TICK, not first bar. First tick price
        # vs prev_close gives immediate gap context.
        if ticker not in self._gap_regime and ticker in self._prev_close:
            ind = self._indicators.get(ticker, {})
            atr = ind.get('atr', 0)
            prev_close = self._prev_close[ticker]
            if atr > 0 and prev_close > 0:
                gap_atr = abs(price - prev_close) / atr
                self._gap_atr[ticker] = gap_atr
                if gap_atr < self._GAP_SMALL:
                    self._gap_regime[ticker] = 'none'
                elif gap_atr < self._GAP_MEDIUM:
                    self._gap_regime[ticker] = 'small'
                    log.info("[TickDetector] SMALL GAP %s: %.1f ATR "
                             "(tick=$%.2f prev=$%.2f)",
                             ticker, gap_atr, price, prev_close)
                elif gap_atr < self._GAP_LARGE:
                    self._gap_regime[ticker] = 'medium'
                    log.info("[TickDetector] MEDIUM GAP %s: %.1f ATR "
                             "(tick=$%.2f prev=$%.2f)",
                             ticker, gap_atr, price, prev_close)
                else:
                    self._gap_regime[ticker] = 'large'
                    # Invalidate snapshot S/R immediately (don't wait for BAR)
                    lvls = self._levels.get(ticker)
                    if lvls:
                        lvls['sr_levels'] = []
                        lvls['nearest_support'] = 0
                        lvls['nearest_resistance'] = 0
                    log.warning("[TickDetector] LARGE GAP %s: %.1f ATR "
                                "(tick=$%.2f prev=$%.2f) — S/R INVALIDATED",
                                ticker, gap_atr, price, prev_close)

        # ── Fix #2: Adaptive sampling under load ─���───────────────────
        # At overload: don't drop ticks blindly. Sample every Nth tick
        # per ticker, BUT always process price-crossing ticks (large moves).
        if now - self._tick_window_start > 0.1:
            total = sum(self._tick_counts.values())
            # Adaptive: increase sample rate as load grows
            if total > self._BURST_THRESHOLD * 3:
                self._sample_rate = 4   # process 1 in 4
            elif total > self._BURST_THRESHOLD * 2:
                self._sample_rate = 3
            elif total > self._BURST_THRESHOLD:
                self._sample_rate = 2   # process 1 in 2
            else:
                self._sample_rate = 1   # process all
            self._tick_counts.clear()
            self._tick_window_start = now
        self._tick_counts[ticker] += 1
        _overloaded = self._sample_rate > 1

        # Adaptive sampling: skip this tick unless it's the Nth
        # Exceptions (NEVER sample out):
        #   - Large price moves (potential crossings)
        #   - Tickers with pending setups (trigger tick is critical)
        _has_pending_setup = ticker in self._pending_setups
        if _overloaded and not _has_pending_setup:
            self._sample_counter[ticker] += 1
            prev = self._tick_state[ticker]['prev_price']
            ind_check = self._indicators.get(ticker, {})
            atr_check = ind_check.get('atr', 0)
            _is_crossing = (prev > 0 and atr_check > 0
                            and abs(price - prev) > atr_check * 0.05)
            if not _is_crossing and self._sample_counter[ticker] % self._sample_rate != 0:
                self._ticks_skipped_burst += 1
                return  # sampled out — skip
            # Large move → always process (potential crossing event)

        self._ticks_processed += 1

        # ── Guard 1: Must have levels (at least one BAR processed) ───
        levels = self._levels.get(ticker)
        if not levels:
            return
        # Snapshot level version — detect if BAR update races with this tick
        _level_ver = self._level_version.get(ticker, 0)

        # ── Guard 2: Already in position → skip (exits via QUOTE) ────
        if ticker in self._positions:
            return

        # ── Guard 3: Signal cooldown (60s per ticker) ────────────────
        if now - self._last_signal_time.get(ticker, 0) < _SIGNAL_COOLDOWN:
            return

        # ── Guard 4: Need indicators ─────────────────────────────────
        ind = self._indicators.get(ticker)
        if not ind:
            return

        rsi = ind.get('rsi', 50)
        atr = ind.get('atr', 0)

        # ── Guard 5: Indicator freshness — stale indicators weaken gating ─
        # If indicators haven't been refreshed in >30min, skip RSI/RVOL
        # gating entirely (they're unreliable). ATR is more stable, keep it.
        _ind_age = now - self._indicator_updated_at.get(ticker, 0)
        _indicators_fresh = _ind_age < self._INDICATOR_MAX_AGE

        if _indicators_fresh:
            # RSI must be in entry range (only trust if fresh)
            if not (40 <= rsi <= 75):
                return

        # ── Guard 6: Need minimum ATR ────────────────────────────────
        if atr <= 0 or atr < price * 0.001:
            return

        # ── Update tick state ────────────────────────────────────────
        state = self._tick_state[ticker]
        prev_price = state['prev_price']
        state['prev_price'] = price

        # Rolling tick-VWAP (cumulative, resets each bar via update_levels)
        state['vwap_pv'] += price * size
        state['vwap_v'] += size

        # Rolling tick window (last 30 seconds)
        state['ticks'].append((now, price, size))
        cutoff = now - _MOMENTUM_WINDOW
        state['ticks'] = [(t, p, v) for t, p, v in state['ticks'] if t > cutoff]

        if prev_price <= 0:
            return  # first tick, no crossing detection possible

        # ── Fix #5: Tick flood gate — skip if price hasn't moved ─────
        # Most ticks are at the same price (trade prints at bid/ask).
        # Skip detection unless price actually changed meaningfully.
        _min_move = atr * 0.01  # 1% of ATR = minimum meaningful move
        if abs(price - prev_price) < _min_move:
            return

        # ── Capture pre-update session high/low for gap detector ────
        # Bug fix: session_high must be read BEFORE we update it,
        # otherwise gap_and_go checks "prev <= new_high < price" which
        # is always False when price IS the new high.
        _session_high_pre = levels.get('session_high', 0)
        _session_low_pre = levels.get('session_low', 0)

        # Update session high/low for next tick's reference
        if price > _session_high_pre:
            levels['session_high'] = price
        if _session_low_pre <= 0 or price < _session_low_pre:
            levels['session_low'] = price

        # ── Distance-to-level gate — skip if far from all levels ─────
        sr_levels = levels.get('sr_levels', [])
        orb_high = levels.get('orb_high', 0)
        orb_low = levels.get('orb_low', 0)
        support = levels.get('nearest_support', 0)
        _all_levels = [l for l in sr_levels if l > 0]
        if orb_high > 0:
            _all_levels.append(orb_high)
        if orb_low > 0:
            _all_levels.append(orb_low)
        if support > 0:
            _all_levels.append(support)
        # Include session_high for gap_and_go distance check
        if _session_high_pre > 0:
            _all_levels.append(_session_high_pre)
        if _all_levels:
            _min_dist = min(abs(price - l) for l in _all_levels)
            if _min_dist > atr * 0.5:  # more than 0.5 ATR from any level
                # Bypass: tickers with pending setups must always reach setup check
                # (EMA9/EMA20 triggers are not in the S/R levels list)
                if ticker not in self._pending_setups:
                    return  # too far — no crossing possible this tick

        # ── Data-readiness gating ────��───────────────────────────────
        # Requires 2+ live BARs + fresh indicators (not just 1 hybrid bar).
        _data_ready = self._is_data_ready(ticker)
        _gap_regime = self._gap_regime.get(ticker, 'none')

        # ── Fix #4: ORB time cutoff — useless after 30 min ──────────
        _orb_allowed = True
        if self._market_open_mono > 0:
            _mins_since_930 = (now - self._market_open_mono) / 60.0
            if _mins_since_930 > self._ORB_CUTOFF_MINUTES:
                _orb_allowed = False

        # ── Run detectors (gated by data readiness + gap regime) ─────
        signal = None

        # ORB: allowed during first 30 min of session, independent of levels
        if _orb_allowed:
            signal = signal or self._check_orb(ticker, price, prev_price, levels, ind)

        # S/R flip: needs trusted levels — skip if data not ready or large gap
        if _data_ready and _gap_regime != 'large':
            signal = signal or self._check_sr_flip(ticker, price, prev_price, levels, ind)

        # Momentum: expensive + needs volume context — skip under overload
        if _data_ready and not _overloaded:
            signal = signal or self._check_momentum(ticker, price, state, levels, ind)

        # Gap continuation: only meaningful when gap was detected
        # Pass pre-update session_high so crossing detection works correctly
        if _data_ready and _gap_regime in ('small', 'medium'):
            signal = signal or self._check_gap(ticker, price, prev_price, levels, ind, _session_high_pre)

        # Sweep: needs trusted support levels — skip if large gap
        if _data_ready and _gap_regime != 'large' and not _overloaded:
            signal = signal or self._check_sweep(ticker, price, prev_price, state, levels, ind, now)

        # ── Two-stage setup trigger (trend_pullback) ─────────────────
        # Check pending setups AFTER existing detectors (don't override)
        if not signal and ticker in self._pending_setups:
            setup = self._pending_setups[ticker]
            signal = self._check_trend_setup(ticker, price, prev_price, setup, ind, now)

        if signal:
            # Context-aware smart stop: scale buffer by current volatility + time of day
            from monitor.smart_stop import compute_stop_buffer, should_reject_wide_stop
            # Use tick window range for volatility (more current than bar-level)
            _tick_prices = [p for _, p, _ in state['ticks']] if state['ticks'] else []
            _tick_range = (max(_tick_prices) - min(_tick_prices)) if len(_tick_prices) >= 2 else atr
            _buffer = compute_stop_buffer(atr, tick_range=_tick_range)

            _stop_dist = signal.entry_price - signal.stop_price
            if should_reject_wide_stop(_stop_dist, atr):
                return  # stop too far, R:R will be bad
            if _stop_dist < _buffer:
                signal.stop_price = signal.entry_price - _buffer

            # Per-level cooldown — prevent oscillation signals
            _level_key = (ticker, round(signal.entry_price, 2))
            _last_at = self._level_cooldown.get(_level_key, 0)
            if now - _last_at < self._LEVEL_COOLDOWN_SEC:
                return  # same level fired recently — skip

            # Level version check: if levels changed mid-tick, discard
            if self._level_version.get(ticker, 0) != _level_ver:
                return  # levels updated mid-tick — discard

            # Weighted additive confidence
            signal.confidence = self._compute_confidence(
                ticker, signal.confidence, _indicators_fresh)

            if signal.confidence < 0.3:
                return  # too low confidence — skip

            # ── Trade freeze: observe signals but don't execute ──────
            # After startup/restart, signals generate and log but don't
            # emit ORDER_REQ until data is trustworthy. This prevents
            # real money loss from early garbage signals.
            if self._order_freeze:
                self._signals_frozen += 1
                if now - self._last_freeze_log > self._FREEZE_LOG_INTERVAL:
                    self._last_freeze_log = now
                    log.info("[TickDetector] FREEZE: %d signals observed but held "
                             "(waiting for data readiness)", self._signals_frozen)
                return  # signal observed but not emitted

            self._level_cooldown[_level_key] = now
            self._last_signal_time[ticker] = now
            self._signals_emitted += 1
            self._emit_signal(signal)

    # ── Detectors ────────────────────────────────────────────────────

    def _check_sr_flip(self, ticker, price, prev_price, levels, ind):
        """Price crosses above a resistance level → long signal."""
        sr_levels = levels.get('sr_levels', [])
        atr = ind.get('atr', 0)
        rvol = ind.get('rvol', 0)

        for level in sr_levels:
            # Price just crossed ABOVE this level
            if prev_price < level <= price:
                if rvol >= 1.5:  # need some volume confirmation
                    stop = level - atr * 0.5
                    return TickSignal(
                        ticker=ticker, strategy='sr_flip', direction='long',
                        entry_price=price, stop_price=stop,
                        target_1=price + atr * 1.5, target_2=price + atr * 3.0,
                        atr=atr, rsi=ind.get('rsi', 50), rvol=rvol,
                        confidence=0.8,
                    )
        return None

    def _check_orb(self, ticker, price, prev_price, levels, ind):
        """Price breaks above opening range high → long signal."""
        orb_high = levels.get('orb_high', 0)
        atr = ind.get('atr', 0)

        if orb_high > 0 and prev_price < orb_high <= price:
            # Volume in last 30s should be building
            ticks = self._tick_state[ticker]['ticks']
            recent_vol = sum(v for _, _, v in ticks)
            avg_bar_vol = levels.get('avg_bar_volume', 1)
            if avg_bar_vol > 0 and recent_vol > avg_bar_vol * 0.3:
                stop = orb_high - atr
                orb_low = levels.get('orb_low', orb_high - atr * 2)
                return TickSignal(
                    ticker=ticker, strategy='orb', direction='long',
                    entry_price=price, stop_price=stop,
                    target_1=price + (orb_high - orb_low),
                    target_2=price + (orb_high - orb_low) * 2,
                    atr=atr, rsi=ind.get('rsi', 50), rvol=ind.get('rvol', 0),
                    confidence=0.85,
                )
        return None

    def _check_momentum(self, ticker, price, state, levels, ind):
        """Volume spike + range expansion in last 30 seconds."""
        ticks = state['ticks']
        if len(ticks) < 10:
            return None

        atr = ind.get('atr', 0)
        avg_bar_vol = levels.get('avg_bar_volume', 1)

        # Volume in window
        window_vol = sum(v for _, _, v in ticks)
        # Range in window
        prices = [p for _, p, _ in ticks]
        window_high = max(prices)
        window_low = min(prices)
        window_range = window_high - window_low

        # Volume must be 2x a normal minute's volume (in 30s = 4x rate)
        if avg_bar_vol > 0 and window_vol < avg_bar_vol * 2:
            return None

        # Range must be expanding (>0.7 ATR in 30 seconds)
        if atr > 0 and window_range < atr * 0.7:
            return None

        # Direction: where did price end up relative to window open
        window_open = ticks[0][1]
        if price > window_open:
            direction = 'long'
            stop = window_low - atr * 0.3
            t1 = price + atr * 2
            t2 = price + atr * 4
        else:
            return None  # only long for now

        return TickSignal(
            ticker=ticker, strategy='momentum_ignition', direction=direction,
            entry_price=price, stop_price=stop, target_1=t1, target_2=t2,
            atr=atr, rsi=ind.get('rsi', 50), rvol=ind.get('rvol', 0),
            confidence=0.75,
        )

    def _check_gap(self, ticker, price, prev_price, levels, ind,
                   session_high_pre: float = 0):
        """Price continuing in gap direction, making new session high."""
        gap_dir = levels.get('gap_direction')
        # Use pre-update session_high (before on_tick mutated it to current price).
        # If caller didn't pass it, fall back to levels dict (already mutated).
        session_high = session_high_pre or levels.get('session_high', 0)
        atr = ind.get('atr', 0)

        if gap_dir == 'up' and session_high > 0:
            # Price just crossed above previous session high
            if prev_price <= session_high < price:
                stop = session_high - atr * 0.5
                return TickSignal(
                    ticker=ticker, strategy='gap_and_go', direction='long',
                    entry_price=price, stop_price=stop,
                    target_1=price + atr * 2, target_2=price + atr * 4,
                    atr=atr, rsi=ind.get('rsi', 50), rvol=ind.get('rvol', 0),
                    confidence=0.7,
                )
        return None

    def _check_sweep(self, ticker, price, prev_price, state, levels, ind, now):
        """Price sweeps below support then reverses back above."""
        support = levels.get('nearest_support', 0)
        atr = ind.get('atr', 0)
        if support <= 0:
            return None

        # Phase 1: price crosses below support (sweep begins)
        if prev_price >= support > price and not state.get('swept'):
            state['swept'] = True
            state['sweep_low'] = price
            state['sweep_time'] = now
            return None

        # Phase 2: price reverses back above support (within 30s)
        if state.get('swept') and prev_price <= support < price:
            elapsed = now - state.get('sweep_time', 0)
            if elapsed < 30:  # must reverse within 30 seconds
                sweep_low = state['sweep_low']
                state['swept'] = False
                stop = sweep_low - atr * 0.3
                return TickSignal(
                    ticker=ticker, strategy='liquidity_sweep', direction='long',
                    entry_price=price, stop_price=stop,
                    target_1=price + atr * 2, target_2=price + atr * 4,
                    atr=atr, rsi=ind.get('rsi', 50), rvol=ind.get('rvol', 0),
                    confidence=0.8,
                )

        # Reset sweep if too old (>30s)
        if state.get('swept') and now - state.get('sweep_time', 0) > 30:
            state['swept'] = False

        return None

    # ── Two-Stage Setup Detector ────────────────────────────────────

    def _check_trend_setup(self, ticker, price, prev_price, setup, ind, now):
        """Tick-level trigger for trend_pullback pending setups.

        Confirms the pullback bounce is REAL using live tick data:
          - Price must be above EMA20 (thesis still valid)
          - Price must not be too far above EMA9 (not chasing)
          - EMA9 reclaim OR EMA20 bounce with momentum
          - Volume confirmation from recent ticks (live, not stale RVOL)
        """
        # Expiry check
        if now > setup.expires_at:
            del self._pending_setups[ticker]
            self._setups_expired += 1
            log.debug("[TickDetector] Setup expired: %s (180s, no tick confirmation)",
                      ticker)
            return None

        # Thesis validity: price must be ABOVE EMA20
        if price < setup.ema20:
            return None  # pullback broke EMA20 → thesis dead

        # Gap-through guard: price not too far above EMA9 (< 0.5 ATR)
        if price > setup.ema9 + setup.atr * 0.5:
            return None  # too extended above EMA9, missed the entry

        # ── Trigger condition 1: EMA9 reclaim ────────────────────────
        # Price crosses above EMA9 from below
        _ema9_reclaim = (prev_price < setup.ema9 <= price)

        # ── Trigger condition 2: EMA20 bounce with momentum ──────────
        # Previous tick was near EMA20, this tick shows upward move
        _near_ema20 = (abs(prev_price - setup.ema20) / setup.ema20 < 0.002
                       if setup.ema20 > 0 else False)
        _momentum = (price - prev_price) > setup.atr * 0.01
        _ema20_bounce = _near_ema20 and _momentum

        if not (_ema9_reclaim or _ema20_bounce):
            return None  # no trigger this tick

        # ── Volume confirmation (live tick data) ─────────────────────
        # Recent 30s tick volume must show buying participation
        state = self._tick_state[ticker]
        recent_ticks = state['ticks']
        recent_vol = sum(v for _, _, v in recent_ticks) if recent_ticks else 0
        avg_bar_vol = self._levels.get(ticker, {}).get('avg_bar_volume', 0)
        if avg_bar_vol > 0 and recent_vol < avg_bar_vol * 0.3:
            return None  # no volume participation → fake bounce

        # ── ALL CHECKS PASSED — build signal at LIVE tick price ──────
        # Recalculate stop/targets from live entry (not stale bar entry)
        from monitor.smart_stop import compute_stop_buffer
        _tick_prices = [p for _, p, _ in recent_ticks] if recent_ticks else [price]
        _tick_range = (max(_tick_prices) - min(_tick_prices)) if len(_tick_prices) >= 2 else setup.atr
        _buffer = compute_stop_buffer(setup.atr, tick_range=_tick_range)

        # Stop: use structural stop from bar-level, but ensure minimum buffer
        stop = min(setup.stop_price, price - _buffer)
        stop = min(stop, price - setup.atr * 0.4)  # absolute minimum

        # Targets from live entry
        risk = price - stop
        target_1 = price + risk * 1.0  # 1R
        target_2 = price + risk * 2.0  # 2R

        # Limit price for stop-limit order: tick_price + 0.1% buffer
        limit_price = round(price * 1.001, 4)
        activation_price = round(price, 4)

        # Consume setup
        del self._pending_setups[ticker]
        self._setups_triggered += 1

        trigger_type = 'ema9_reclaim' if _ema9_reclaim else 'ema20_bounce'
        log.info("[TickDetector] SETUP TRIGGERED: %s %s | trigger=%s "
                 "tick=$%.2f ema9=$%.2f ema20=$%.2f vol=%d/%d",
                 ticker, setup.strategy, trigger_type,
                 price, setup.ema9, setup.ema20,
                 recent_vol, int(avg_bar_vol * 0.3))

        return TickSignal(
            ticker=ticker,
            strategy='trend_pullback',
            direction='long',
            entry_price=limit_price,
            stop_price=round(stop, 4),
            target_1=round(target_1, 4),
            target_2=round(target_2, 4),
            atr=setup.atr,
            rsi=ind.get('rsi', 50),
            rvol=ind.get('rvol', 0),
            confidence=setup.confidence,
            activation_price=activation_price,
        )

    # ── Signal Emission ──────────────────────────────────────────────

    def _emit_signal(self, sig: TickSignal) -> None:
        """Emit a PRO_STRATEGY_SIGNAL event, same as ProSetupEngine."""
        try:
            from monitor.event_bus import Event, EventType
            from monitor.events import ProStrategySignalPayload

            _tier = 2 if sig.strategy in ('orb', 'gap_and_go') else (
                1 if sig.strategy in ('sr_flip', 'trend_pullback') else 3)
            # For stop-limit orders, include activation_price in detector_signals JSON
            _det_json = '{"source": "tick_detector"}'
            if sig.activation_price > 0:
                _det_json = (f'{{"source": "tick_detector", '
                             f'"order_type": "stop_limit", '
                             f'"activation_price": {sig.activation_price}}}')
            payload = ProStrategySignalPayload(
                ticker=sig.ticker,
                strategy_name=sig.strategy,
                direction=sig.direction,
                tier=_tier,
                entry_price=round(sig.entry_price, 4),
                stop_price=round(sig.stop_price, 4),
                target_1=round(sig.target_1, 4),
                target_2=round(sig.target_2, 4),
                atr_value=round(sig.atr, 4),
                rsi_value=round(sig.rsi, 1),
                rvol=round(sig.rvol, 2),
                vwap=round(sig.entry_price, 4),  # tick price ≈ VWAP proxy
                confidence=round(sig.confidence, 2),
                detector_signals=_det_json,
            )

            self._bus.emit(Event(
                type=EventType.PRO_STRATEGY_SIGNAL,
                payload=payload,
            ))

            log.info(
                "[TickDetector] SIGNAL %s %s %s | price=$%.2f stop=$%.2f "
                "atr=%.4f rsi=%.1f rvol=%.2f conf=%.0f%%",
                sig.ticker, sig.strategy, sig.direction,
                sig.entry_price, sig.stop_price,
                sig.atr, sig.rsi, sig.rvol, sig.confidence * 100,
            )
        except Exception as exc:
            log.warning("[TickDetector] Signal emit failed for %s: %s", sig.ticker, exc)

    # ── Diagnostics ──────────────────────────────────────────────────

    def set_prev_closes(self, prev_closes: Dict[str, float]) -> None:
        """Seed previous-day closes from snapshot for gap detection at open."""
        self._prev_close.update(prev_closes)

    def lift_freeze(self) -> None:
        """Manually lift order freeze (e.g., after REST reseed validates data)."""
        if self._order_freeze:
            self._order_freeze = False
            self._freeze_lifted_at = time.monotonic()
            log.info("[TickDetector] FREEZE LIFTED (manual) — %d signals were held",
                     self._signals_frozen)

    def register_setup(self, setup: PendingSetup) -> None:
        """Register a pending setup for tick-level trigger confirmation.

        Called by ProSetupEngine for two-stage strategies (trend_pullback).
        No filtering here — all validation happens at trigger time with live data.
        """
        self._pending_setups[setup.ticker] = setup
        self._setups_registered += 1
        log.info("[TickDetector] SETUP registered: %s %s ema9=$%.2f ema20=$%.2f "
                 "atr=$%.4f conf=%.0f%% | expires in 180s",
                 setup.ticker, setup.strategy, setup.ema9, setup.ema20,
                 setup.atr, setup.confidence * 100)

    def stats(self) -> dict:
        regime_counts = defaultdict(int)
        for r in self._gap_regime.values():
            regime_counts[r] += 1
        _ready = sum(
            1 for t in self._live_bar_count
            if self._live_bar_count[t] >= self._MIN_LIVE_BARS
            and t in self._indicator_updated_at
        )
        return {
            'tickers_with_levels': len(self._levels),
            'tickers_with_indicators': len(self._indicators),
            'tickers_data_ready': _ready,
            'signals_emitted': self._signals_emitted,
            'signals_frozen': self._signals_frozen,
            'order_freeze': self._order_freeze,
            'ticks_processed': self._ticks_processed,
            'ticks_sampled_out': self._ticks_skipped_burst,
            'sample_rate': self._sample_rate,
            'gap_regimes': dict(regime_counts),
            'stale_indicators': sum(
                1 for t in self._indicators
                if (time.monotonic() - self._indicator_updated_at.get(t, 0))
                > self._INDICATOR_MAX_AGE
            ),
            'pending_setups': len(self._pending_setups),
            'setups_registered': self._setups_registered,
            'setups_triggered': self._setups_triggered,
            'setups_expired': self._setups_expired,
            'enabled': _FF_ENABLED,
        }
