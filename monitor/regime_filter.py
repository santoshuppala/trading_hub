"""
Market Regime Filter — Continuous Orthogonal Scoring.

Classifies market conditions in real-time using 3 independent dimensions:
  1. Trend persistence (variance ratio — are moves persistent or reverting?)
  2. Volatility risk premium (VRP — implied vol vs realized vol)
  3. Participation (EWM breadth — is the move broad or narrow?)

Each strategy has a per-strategy score derived from these dimensions.
Strategies are blocked when their score falls below a calibrated threshold,
and position size is modulated by the score.

Key properties:
  - Continuous 0-1 scores (no cliff edges, smooth transitions)
  - Percentile-based normalization (self-adapting, works at any VIX level)
  - Per-strategy weights (momentum needs different conditions than mean-reversion)
  - Uncertainty detection (conflicting dimensions → reduce size)
  - Crash-safe (state persisted every 60s, restores in <10ms)
  - Multi-timeframe (session gate + intraday precision)
  - 30-minute ramp at open (insufficient data → neutral scores)
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from datetime import datetime
from enum import Enum
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import numpy as np

log = logging.getLogger(__name__)
ET = ZoneInfo('America/New_York')

# State file for crash recovery
_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'regime_state.json')

# ETFs to exclude from breadth (they follow market, don't measure participation)
_ETFS = {
    'SPY', 'QQQ', 'IWM', 'DIA',
    'XLK', 'XLF', 'XLE', 'XLV', 'XLY', 'XLI', 'XLC', 'XLRE', 'XLB', 'XLU',
    'ARKK', 'SOXS', 'SOXL', 'TQQQ', 'SQQQ',
}

# ── Per-strategy weights across 3 dimensions ────────────────────────────
# Positive weight = higher dimension score helps the strategy
# Negative weight = LOWER dimension score helps (mean-reversion benefits from low trend)
STRATEGY_WEIGHTS: Dict[str, Dict[str, float]] = {
    # MOMENTUM: need trending + broad participation
    'trend_pullback':      {'trend': 0.50, 'vrp': 0.20, 'participation': 0.30},
    'momentum_ignition':   {'trend': 0.60, 'vrp': 0.15, 'participation': 0.25},
    'gap_and_go':          {'trend': 0.45, 'vrp': 0.25, 'participation': 0.30},
    'flag_pennant':        {'trend': 0.55, 'vrp': 0.20, 'participation': 0.25},

    # BREAKOUT: need participation + calm vol (breakouts stick in low vol)
    'orb':                 {'trend': 0.30, 'vrp': 0.35, 'participation': 0.35},
    'inside_bar':          {'trend': 0.25, 'vrp': 0.40, 'participation': 0.35},
    'bollinger_squeeze':   {'trend': 0.20, 'vrp': 0.45, 'participation': 0.35},
    'fib_confluence':      {'trend': 0.35, 'vrp': 0.30, 'participation': 0.35},

    # MEAN-REVERSION: benefit from LOW trend + high VRP (overpriced fear)
    'sr_flip':             {'trend': -0.20, 'vrp': 0.40, 'participation': 0.40},
    'liquidity_sweep':     {'trend': -0.15, 'vrp': 0.45, 'participation': 0.40},
    'vwap_reclaim':        {'trend': 0.10,  'vrp': 0.40, 'participation': 0.50},
}

# Minimum score to trade (conservative defaults — calibrate from data after 2 weeks)
STRATEGY_MIN_SCORE: Dict[str, float] = {
    'trend_pullback':      0.45,
    'momentum_ignition':   0.50,
    'gap_and_go':          0.45,
    'flag_pennant':        0.50,
    'orb':                 0.35,
    'inside_bar':          0.35,
    'bollinger_squeeze':   0.35,
    'fib_confluence':      0.40,
    'sr_flip':             0.35,
    'liquidity_sweep':     0.40,
    'vwap_reclaim':        0.35,
}

# If ALL strategy scores below this → stop all trading
_KILL_THRESHOLD = 0.25

# Update intervals
_INTRADAY_UPDATE_SEC = 60
_SESSION_UPDATE_SEC = 300
_STATE_SAVE_SEC = 60


class RegimeFilter:
    """Real-time market regime classification with per-strategy scoring.

    Lifecycle:
      1. Created once at startup (run_core.py)
      2. update() called every bar cycle (~60s) from monitor._run_one_cycle()
      3. is_strategy_allowed() + get_size_multiplier() called by RiskAdapter/RiskEngine
      4. State persisted every 60s for crash recovery
    """

    def __init__(self, bars_cache_ref: dict, alt_data_fn=None):
        """
        Args:
            bars_cache_ref: shared dict from monitor (bars_cache)
            alt_data_fn: callable returning VIX float (e.g., alt_data_reader.vix)
        """
        self._bars_cache = bars_cache_ref
        self._get_vix = alt_data_fn

        # Current dimension scores (0.0 to 1.0)
        self._trend_score = 0.5
        self._vrp_score = 0.5
        self._participation_score = 0.5
        self._uncertainty = 0.0

        # Session-level scores (slow, stable)
        self._session_trend = 0.5
        self._session_vrp = 0.5
        self._session_participation = 0.5
        self._last_session_update = 0.0

        # Per-strategy final scores
        self._strategy_scores: Dict[str, float] = {}
        self._strategy_multipliers: Dict[str, float] = {}

        # Score history for velocity/transition detection
        self._score_history: deque = deque(maxlen=30)
        self._breadth_history: deque = deque(maxlen=10)

        # Percentile calibration (rolling 20 trading days)
        self._trend_raw_history: deque = deque(maxlen=100)  # intraday readings
        self._vrp_raw_history: deque = deque(maxlen=100)
        self._trend_daily: list = []  # daily summary values (up to 20 days)
        self._vrp_daily: list = []

        # Timing
        self._last_update = 0.0
        self._last_state_save = 0.0
        self._confidence_penalty = 0.0  # reduced after stale state restore

        # Try to restore from saved state
        self._load_state()

        log.info("[RegimeFilter] Initialized | trend=%.2f vrp=%.2f participation=%.2f "
                 "uncertainty=%.2f",
                 self._trend_score, self._vrp_score,
                 self._participation_score, self._uncertainty)

    # ── Public API ───────────────────────────────────────────────────────

    def update(self) -> None:
        """Recompute regime from current data. Called every bar cycle (~60s)."""
        now = time.monotonic()
        if now - self._last_update < _INTRADAY_UPDATE_SEC * 0.8:
            return  # too soon
        self._last_update = now

        # Compute 3 dimensions from live data
        self._trend_score = self._compute_trend()
        self._vrp_score = self._compute_vrp()
        self._participation_score = self._compute_breadth()

        # Session-level (every 5 min)
        if now - self._last_session_update > _SESSION_UPDATE_SEC:
            self._last_session_update = now
            self._session_trend = self._trend_score
            self._session_vrp = self._vrp_score
            self._session_participation = self._participation_score

        # 30-minute ramp at open
        now_et = datetime.now(ET)
        mins_since_open = (now_et.hour - 9) * 60 + (now_et.minute - 30)
        if 0 < mins_since_open < 30:
            ramp = mins_since_open / 30.0
            self._trend_score = 0.5 + (self._trend_score - 0.5) * ramp
            self._vrp_score = 0.5 + (self._vrp_score - 0.5) * ramp
            self._participation_score = 0.5 + (self._participation_score - 0.5) * ramp

        # Confidence penalty from stale state restore (decays over 5 min)
        if self._confidence_penalty > 0:
            self._confidence_penalty = max(0, self._confidence_penalty - 0.01)

        # Uncertainty
        self._uncertainty = self._compute_uncertainty()

        # Score history for velocity detection
        avg_score = (self._trend_score + self._vrp_score + self._participation_score) / 3
        self._score_history.append(avg_score)

        # Per-strategy scores
        self._update_strategy_scores()

        # Persist state
        if now - self._last_state_save > _STATE_SAVE_SEC:
            self._last_state_save = now
            self._save_state()

    def is_strategy_allowed(self, strategy_name: str) -> bool:
        """Check if strategy is allowed in current regime."""
        # Kill check: if ALL strategies below kill threshold → block everything
        if self._strategy_scores and all(
            s < _KILL_THRESHOLD for s in self._strategy_scores.values()
        ):
            return False  # market is hostile to all strategies

        score = self._strategy_scores.get(strategy_name)
        if score is None:
            return True  # unknown strategy → allow (conservative)
        threshold = STRATEGY_MIN_SCORE.get(strategy_name, 0.35)
        return score >= threshold

    def get_size_multiplier(self, strategy_name: str) -> float:
        """Position size multiplier for strategy in current regime. 0.25-1.0."""
        score = self._strategy_scores.get(strategy_name, 0.5)
        threshold = STRATEGY_MIN_SCORE.get(strategy_name, 0.35)

        if score < threshold:
            return 0.0  # blocked

        # Scale: at threshold → 0.5x, at 1.0 → 1.0x
        denom = 1.0 - threshold
        if denom <= 0:
            return 1.0
        base = 0.5 + 0.5 * (score - threshold) / denom

        # Uncertainty penalty
        if self._uncertainty > 0.25:
            base *= (1.0 - self._uncertainty * 0.5)

        # Transition penalty (velocity)
        velocity = self._score_velocity()
        if velocity < -0.03:
            base *= 0.6

        return min(1.0, max(0.25, base))

    def get_strategy_score(self, strategy_name: str) -> float:
        """Raw regime score for a strategy. 0.0-1.0."""
        return self._strategy_scores.get(strategy_name, 0.5)

    @property
    def trend_score(self) -> float:
        return self._trend_score

    @property
    def vrp_score(self) -> float:
        return self._vrp_score

    @property
    def participation_score(self) -> float:
        return self._participation_score

    @property
    def uncertainty(self) -> float:
        return self._uncertainty

    def stats(self) -> dict:
        return {
            'trend': round(self._trend_score, 3),
            'vrp': round(self._vrp_score, 3),
            'participation': round(self._participation_score, 3),
            'uncertainty': round(self._uncertainty, 3),
            'velocity': round(self._score_velocity(), 4),
            'strategies_allowed': sum(1 for s in STRATEGY_WEIGHTS
                                      if self.is_strategy_allowed(s)),
            'strategies_total': len(STRATEGY_WEIGHTS),
            'strategy_scores': {s: round(v, 3) for s, v in self._strategy_scores.items()},
        }

    # ── Dimension Computations ───────────────────────────────────────────

    def _compute_trend(self) -> float:
        """Variance ratio from SPY 1-min bars. >1 = trending, <1 = mean-reverting."""
        try:
            spy_df = self._bars_cache.get('SPY')
            if spy_df is None or len(spy_df) < 35:
                return 0.5  # not enough data

            closes = spy_df['close'].values[-60:]  # last 60 bars max
            returns = np.diff(closes) / closes[:-1]

            if len(returns) < 30:
                return 0.5

            # Variance ratio: var(5-bar returns) / (5 × var(1-bar returns))
            short_window = 5
            # Build 5-bar cumulative returns
            long_returns = np.array([
                returns[i:i + short_window].sum()
                for i in range(len(returns) - short_window + 1)
            ])

            var_long = np.var(long_returns[-30:]) if len(long_returns) >= 30 else np.var(long_returns)
            var_short = np.var(returns[-30:]) * short_window

            if var_short < 1e-12:
                return 0.5

            vr = var_long / var_short
            self._trend_raw_history.append(vr)

            # Convert to 0-1 percentile using history
            return self._to_percentile(vr, self._trend_raw_history)
        except Exception:
            return 0.5

    def _compute_vrp(self) -> float:
        """VRP = VIX / realized_vol. >1 = fearful, <1 = complacent."""
        try:
            # Get VIX
            vix = None
            if self._get_vix:
                try:
                    vix = self._get_vix()
                except Exception:
                    pass
            if vix is None or vix <= 0:
                return 0.5  # no VIX → neutral

            # Compute realized vol from SPY
            spy_df = self._bars_cache.get('SPY')
            if spy_df is None or len(spy_df) < 20:
                return 0.5

            closes = spy_df['close'].values[-60:]
            returns = np.diff(closes) / closes[:-1]
            if len(returns) < 10:
                return 0.5

            realized_vol_1min = np.std(returns)
            # Annualize: sqrt(252 days × 390 min/day) × 100 for percentage
            realized_vol_annual = realized_vol_1min * np.sqrt(252 * 390) * 100

            if realized_vol_annual < 1e-6:
                return 0.5

            vrp = vix / realized_vol_annual
            self._vrp_raw_history.append(vrp)

            return self._to_percentile(vrp, self._vrp_raw_history)
        except Exception:
            return 0.5

    def _compute_breadth(self) -> float:
        """EWM breadth: fraction of tickers advancing, recent-weighted."""
        try:
            advancing = 0
            total = 0
            halflife = 10

            for ticker, df in self._bars_cache.items():
                if ticker in _ETFS:
                    continue
                if df is None or len(df) < 3:
                    continue
                try:
                    closes = df['close'].values[-min(10, len(df)):]
                    if len(closes) < 2:
                        continue
                    changes = np.diff(closes) / closes[:-1]
                    if len(changes) == 0:
                        continue
                    # EWM: recent changes weighted higher
                    weights = np.exp(-np.arange(len(changes))[::-1] / halflife)
                    weighted = np.sum(changes * weights) / np.sum(weights)
                    if weighted > 0:
                        advancing += 1
                    total += 1
                except Exception:
                    continue

            if total == 0:
                return 0.5

            raw = advancing / total
            self._breadth_history.append(raw)

            # EWM smooth the breadth readings
            if len(self._breadth_history) >= 2:
                arr = np.array(self._breadth_history)
                weights = np.exp(-np.arange(len(arr))[::-1] / 5)
                return float(np.sum(arr * weights) / np.sum(weights))
            return raw
        except Exception:
            return 0.5

    def _compute_uncertainty(self) -> float:
        """How much do the 3 dimensions disagree?"""
        scores = [self._trend_score, self._vrp_score, self._participation_score]
        unc = float(np.std(scores))

        # Extra penalty: high trend + low participation = narrow rally trap
        if self._trend_score > 0.7 and self._participation_score < 0.4:
            unc += 0.15

        return min(1.0, unc)

    # ── Strategy Scoring ─────────────────────────────────────────────────

    def _update_strategy_scores(self) -> None:
        """Compute per-strategy scores from dimension scores."""
        for strategy, weights in STRATEGY_WEIGHTS.items():
            # Blend session (60%) + intraday (40%)
            t = self._session_trend * 0.6 + self._trend_score * 0.4
            v = self._session_vrp * 0.6 + self._vrp_score * 0.4
            p = self._session_participation * 0.6 + self._participation_score * 0.4

            score = 0.0
            for dim, w in weights.items():
                dim_val = {'trend': t, 'vrp': v, 'participation': p}[dim]
                if w < 0:
                    score += abs(w) * (1.0 - dim_val)
                else:
                    score += w * dim_val

            # Apply confidence penalty (from stale state restore)
            if self._confidence_penalty > 0:
                score = 0.5 + (score - 0.5) * (1.0 - self._confidence_penalty)

            self._strategy_scores[strategy] = score
            self._strategy_multipliers[strategy] = self.get_size_multiplier(strategy)

    def _score_velocity(self) -> float:
        """Rate of change of average score. Negative = regime degrading."""
        if len(self._score_history) < 5:
            return 0.0
        recent = list(self._score_history)[-5:]
        return (recent[-1] - recent[0]) / 5.0

    # ── Percentile Normalization ─────────────────────────────────────────

    @staticmethod
    def _to_percentile(value: float, history: deque) -> float:
        """Convert raw value to 0-1 percentile using EWM history."""
        if len(history) < 5:
            # Not enough history — use simple normalization around 1.0
            # (variance ratio: 1.0 = random walk, >1 = trending)
            return min(1.0, max(0.0, value / 2.0))  # crude but safe

        arr = np.array(history)
        # EWM weights (recent values matter more)
        n = len(arr)
        weights = np.exp(-np.arange(n)[::-1] / max(n * 0.3, 5))
        # Weighted percentile: fraction of weighted history below current value
        below = np.sum(weights[arr <= value])
        total = np.sum(weights)
        return float(below / total) if total > 0 else 0.5

    # ── State Persistence (crash recovery) ───────────────────────────────

    def _save_state(self) -> None:
        """Persist regime state to disk. Atomic write."""
        try:
            state = {
                'timestamp': datetime.now(ET).isoformat(),
                'trend_score': self._trend_score,
                'vrp_score': self._vrp_score,
                'participation_score': self._participation_score,
                'uncertainty': self._uncertainty,
                'score_history': list(self._score_history),
                'breadth_history': list(self._breadth_history),
                'session_scores': {
                    'trend': self._session_trend,
                    'vrp': self._session_vrp,
                    'participation': self._session_participation,
                },
                'per_strategy_scores': dict(self._strategy_scores),
                'percentile_calibration': {
                    'trend_raw': list(self._trend_raw_history)[-50:],
                    'vrp_raw': list(self._vrp_raw_history)[-50:],
                    'trend_daily': self._trend_daily[-20:],
                    'vrp_daily': self._vrp_daily[-20:],
                },
            }
            tmp = _STATE_PATH + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(state, f, separators=(',', ':'))
            os.replace(tmp, _STATE_PATH)
        except Exception as exc:
            log.debug("[RegimeFilter] State save failed: %s", exc)

    def _load_state(self) -> None:
        """Restore regime state from disk on startup."""
        try:
            if not os.path.exists(_STATE_PATH):
                self._cold_start()
                return

            with open(_STATE_PATH) as f:
                state = json.load(f)

            ts_str = state.get('timestamp', '')
            if not ts_str:
                self._cold_start()
                return

            from dateutil.parser import parse as _parse_dt
            saved_time = _parse_dt(ts_str)
            now = datetime.now(ET)
            age_sec = (now - saved_time).total_seconds()

            # Restore percentile calibration (always useful regardless of age)
            cal = state.get('percentile_calibration', {})
            self._trend_raw_history = deque(cal.get('trend_raw', []), maxlen=100)
            self._vrp_raw_history = deque(cal.get('vrp_raw', []), maxlen=100)
            self._trend_daily = cal.get('trend_daily', [])
            self._vrp_daily = cal.get('vrp_daily', [])

            if age_sec < 300:  # < 5 min — fresh, use as-is
                self._trend_score = state.get('trend_score', 0.5)
                self._vrp_score = state.get('vrp_score', 0.5)
                self._participation_score = state.get('participation_score', 0.5)
                self._uncertainty = state.get('uncertainty', 0.0)
                self._score_history = deque(state.get('score_history', []), maxlen=30)
                self._breadth_history = deque(state.get('breadth_history', []), maxlen=10)
                sess = state.get('session_scores', {})
                self._session_trend = sess.get('trend', 0.5)
                self._session_vrp = sess.get('vrp', 0.5)
                self._session_participation = sess.get('participation', 0.5)
                self._strategy_scores = state.get('per_strategy_scores', {})
                log.info("[RegimeFilter] Restored from state (age=%ds) — fully operational",
                         int(age_sec))

            elif age_sec < 3600:  # 5-60 min — stale but usable
                self._trend_score = state.get('trend_score', 0.5)
                self._vrp_score = state.get('vrp_score', 0.5)
                self._participation_score = state.get('participation_score', 0.5)
                self._score_history = deque(state.get('score_history', []), maxlen=30)
                self._breadth_history = deque(state.get('breadth_history', []), maxlen=10)
                self._confidence_penalty = 0.3
                log.info("[RegimeFilter] Restored stale state (age=%dm) — reduced confidence",
                         int(age_sec // 60))

            else:  # > 60 min — too old, cold start with calibration
                log.info("[RegimeFilter] State too old (%dm) — cold start with "
                         "percentile history", int(age_sec // 60))
                # percentile calibration already restored above

        except Exception as exc:
            log.warning("[RegimeFilter] State load failed: %s — cold start", exc)
            self._cold_start()

    def _cold_start(self) -> None:
        """First-ever startup or unrecoverable state. Try DB for calibration."""
        log.info("[RegimeFilter] Cold start — scores neutral until calibrated")
        try:
            import psycopg2
            from config import DATABASE_URL
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute(
                "SELECT spy_return_pct, vix_close, avg_rvol_universe "
                "FROM ml_daily_regime ORDER BY regime_date DESC LIMIT 20")
            rows = cur.fetchall()
            if rows:
                self._trend_daily = [float(r[0]) for r in rows if r[0] is not None]
                self._vrp_daily = [float(r[1]) for r in rows if r[1] is not None]
                log.info("[RegimeFilter] Seeded percentile calibration from DB: "
                         "%d trend days, %d vrp days",
                         len(self._trend_daily), len(self._vrp_daily))
            conn.close()
        except Exception:
            pass  # no DB → pure cold start
