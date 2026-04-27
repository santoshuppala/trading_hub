"""
Trade Quality Scorer V1 — Real-time trade state assessment.

Pure function. No state, no side effects, no external dependencies.
Computes trend_score (state) and failure_score (active failure detection)
from position microstructure data already available in lifecycle evaluation.

V1 is LOGGING ONLY — scores are recorded but do NOT affect exit decisions.
After 2 days of data (target: Apr 28 EOD), failure_score threshold will be
calibrated from actual distribution and wired as first production use.

Signals (5 for trend, 7 for failure):
    trend:   structure(0.30), volume(0.25), vwap(0.20), pullback(0.15), momentum(0.10)
    failure: vwap_breakdown, early_break, lower_highs, sell_pressure,
             deep_retracement, stall, compression

Design principles:
    - All signals normalized 0.0-1.0
    - ATR-relative where applicable (no raw price comparisons)
    - Volume is directional (buying pressure, not activity)
    - Structure uses consecutive sequences (not scattered counts)
    - Failure score separate from trend score (independent systems)
    - EMA smoothing on composites (prevents bar-to-bar thrash)
"""
from __future__ import annotations

from typing import Optional


def score_trade(
    # Current bar
    bar_open: float, bar_high: float, bar_low: float,
    bar_close: float, bar_volume: float,
    # Position context
    entry_price: float, atr: float, R: float,
    running_high: float, running_low: float,
    vwap: float, rsi: float,
    # Rolling buffers (maintained by lifecycle, last 10 bars)
    bar_opens: list[float],
    bar_highs: list[float],
    bar_lows: list[float],
    bar_closes: list[float],
    bar_volumes: list[float],
    # VWAP history for slope calculation
    vwap_history: list[float],
    # Previous scores (for EMA smoothing)
    prev_trend_score: Optional[float] = None,
    prev_failure_score: Optional[float] = None,
) -> dict:
    """Score current trade quality. Returns trend + failure scores with features."""

    structure = _structure_score(bar_highs, bar_lows, atr, vwap, bar_close)
    volume = _volume_score(bar_opens, bar_closes, bar_volumes)
    vwap_s = _vwap_score(bar_close, vwap, atr, vwap_history)
    momentum = _momentum_score(bar_open, bar_high, bar_low, bar_close)
    pullback = _pullback_score(bar_close, entry_price, running_high)

    # Composite trend score
    trend_raw = (
        0.30 * structure
        + 0.25 * volume
        + 0.20 * vwap_s
        + 0.10 * momentum
        + 0.15 * pullback
    )

    # R-context decay: exhaustion bias at extended levels
    # Tied to pullback depth, not just R level
    unrealized_r = (bar_close - entry_price) / R if R > 0 else 0
    r_decay = 1.0
    if unrealized_r > 1.5 and running_high > entry_price:
        pullback_depth = (running_high - bar_close) / (running_high - entry_price)
        if pullback_depth < 0.2:
            r_decay = 0.95      # shallow pullback at high R — slight caution
        elif pullback_depth < 0.4:
            r_decay = 0.90      # moderate pullback — real caution
        else:
            r_decay = 0.80      # deep pullback at high R — likely failing

    trend_raw *= r_decay

    # EMA smoothing
    if prev_trend_score is not None:
        trend_smooth = 0.7 * trend_raw + 0.3 * prev_trend_score
    else:
        trend_smooth = trend_raw

    # Failure score (independent system)
    failure_raw, failure_features = _failure_score(
        bar_open, bar_close, bar_volume, vwap, atr,
        entry_price, running_high, R,
        bar_highs, bar_lows, bar_closes, bar_opens, bar_volumes,
        bars_held=len(bar_closes),
    )

    if prev_failure_score is not None:
        failure_smooth = 0.7 * failure_raw + 0.3 * prev_failure_score
    else:
        failure_smooth = failure_raw

    # Hysteresis state classification (logged, not acted on in V1)
    # Wide bands for data collection — tighten from distribution analysis
    trend_state = _classify_state(trend_smooth, None)

    return {
        'trend_score': round(trend_raw, 4),
        'trend_score_smooth': round(trend_smooth, 4),
        'trend_state': trend_state,
        'failure_score': round(failure_raw, 4),
        'failure_score_smooth': round(failure_smooth, 4),
        'r_decay': round(r_decay, 3),
        'features': {
            'structure': round(structure, 4),
            'volume': round(volume, 4),
            'vwap': round(vwap_s, 4),
            'momentum': round(momentum, 4),
            'pullback': round(pullback, 4),
        },
        'failure_features': {k: round(v, 4) for k, v in failure_features.items()},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# TREND SIGNALS (each returns 0.0 - 1.0)
# ═══════════════════════════════════════════════════════════════════════════════

def _structure_score(bar_highs, bar_lows, atr, vwap, bar_close):
    """Consecutive HH/HL sequence quality, multi-horizon.

    HH weighted 0.7 (trend), HL weighted 0.3 (support).
    HL-only accumulation capped if no energy (low vwap/momentum).
    """
    if len(bar_highs) < 4:
        return 0.5

    min_hh = atr * 0.2   # magnitude threshold for higher high
    min_hl = atr * 0.1   # lower threshold for higher low (tighter bases valid)

    def _longest_run(highs, lows, threshold_hh, threshold_hl):
        hh_run = 0
        hl_run = 0
        max_hh = 0
        max_hl = 0
        for i in range(1, len(highs)):
            if highs[i] > highs[i - 1] + threshold_hh:
                hh_run += 1
                max_hh = max(max_hh, hh_run)
            else:
                hh_run = 0
            if lows[i] > lows[i - 1] + threshold_hl:
                hl_run += 1
                max_hl = max(max_hl, hl_run)
            else:
                hl_run = 0
        return max_hh, max_hl

    # Multi-horizon: short (5 bar) + mid (full buffer)
    if len(bar_highs) >= 6:
        short_hh, short_hl = _longest_run(bar_highs[-5:], bar_lows[-5:], min_hh, min_hl)
        mid_hh, mid_hl = _longest_run(bar_highs, bar_lows, min_hh, min_hl)
    else:
        short_hh, short_hl = _longest_run(bar_highs, bar_lows, min_hh, min_hl)
        mid_hh, mid_hl = short_hh, short_hl

    # Normalize: 4 consecutive = 1.0
    hh_score_s = min(1.0, short_hh / 4)
    hl_score_s = min(1.0, short_hl / 4)
    hh_score_m = min(1.0, mid_hh / 6)
    hl_score_m = min(1.0, mid_hl / 6)

    # Accumulation cap: HL without HH is support, not trend
    # Gate with energy: if VWAP/momentum are weak, it's dead compression
    if short_hh == 0 and short_hl > 2:
        # Check if this accumulation has energy
        vwap_above = bar_close > vwap if vwap > 0 else True
        if not vwap_above:
            hl_score_s *= 0.3   # dead compression below VWAP
        else:
            hl_score_s *= 0.5   # valid accumulation but incomplete

    # Weighted: HH is trend confirmation, HL is support
    short_score = 0.7 * hh_score_s + 0.3 * hl_score_s
    mid_score = 0.7 * hh_score_m + 0.3 * hl_score_m

    return 0.6 * short_score + 0.4 * mid_score


def _volume_score(bar_opens, bar_closes, bar_volumes):
    """Directional volume: buying pressure vs selling pressure × activity.

    Not direction-agnostic — heavy selling scores low even if total volume is high.
    Activity factor prevents low-liquidity names from scoring high on tiny volume.
    """
    if len(bar_volumes) < 5 or len(bar_opens) < 5 or len(bar_closes) < 5:
        return 0.5

    lookback = min(5, len(bar_volumes))
    up_vol = 0.0
    down_vol = 0.0

    for i in range(-lookback, 0):
        v = bar_volumes[i]
        if bar_closes[i] >= bar_opens[i]:
            up_vol += v
        else:
            down_vol += v

    total = up_vol + down_vol
    if total <= 0:
        return 0.5

    directional = up_vol / total

    # Activity factor: recent volume vs earlier volume
    # Prevents dead stocks from scoring high on tiny directional volume
    recent_total = sum(bar_volumes[-5:]) if len(bar_volumes) >= 5 else total
    earlier_total = sum(bar_volumes[-10:-5]) if len(bar_volumes) >= 10 else recent_total

    if earlier_total > 0:
        activity = min(1.0, (recent_total / earlier_total) * 0.7)
        # 0.7 scaling: need 1.4x recent/earlier to reach 1.0 activity
    else:
        activity = 0.5

    return directional * max(0.3, activity)  # floor at 0.3 — don't zero out on low activity


def _vwap_score(bar_close, vwap, atr, vwap_history):
    """Price vs VWAP with exhaustion penalty and slope adjustment.

    Bell curve: best near VWAP, decays past 2 ATR (exhaustion).
    Slope: rising VWAP = trend, flat VWAP = range.
    """
    if vwap <= 0 or atr <= 0:
        return 0.5

    distance = (bar_close - vwap) / atr

    # Base score: distance from VWAP
    if distance >= 0:
        if distance <= 2.0:
            base = min(1.0, 0.5 + distance * 0.25)
        else:
            # Exhaustion: decays past 2 ATR
            base = max(0.5, 1.0 - (distance - 2.0) * 0.15)
    else:
        base = max(0.0, 0.5 + distance * 0.25)

    # VWAP slope adjustment (4-bar window for smoothing)
    slope_adj = 1.0
    if len(vwap_history) >= 4 and atr > 0:
        vwap_slope = (vwap_history[-1] - vwap_history[-4]) / (3 * atr)
        # Rising VWAP → boost (max 1.3x), falling → penalize (min 0.7x)
        slope_adj = max(0.7, min(1.3, 1.0 + vwap_slope))

    return max(0.0, min(1.0, base * slope_adj))


def _momentum_score(bar_open, bar_high, bar_low, bar_close):
    """Closing strength: where in the bar range did price close?

    Orthogonal to structure/pullback (doesn't use HH/HL or retracement).
    Close at high = strong buying, close at low = selling pressure.
    """
    if bar_open <= 0 or bar_high <= bar_low:
        return 0.5

    bar_range = bar_high - bar_low
    if bar_range <= 0:
        return 0.5

    # 1.0 = closed at high, 0.0 = closed at low
    return (bar_close - bar_low) / bar_range


def _pullback_score(bar_close, entry_price, running_high):
    """How shallow is the current pullback from running high?

    0% retracement = 1.0, 100% = 0.0.
    Most independent signal — low correlation with structure/volume.
    """
    if running_high <= entry_price:
        return 0.3  # hasn't moved yet

    move_size = running_high - entry_price
    if move_size <= 0:
        return 0.5

    retracement = (running_high - bar_close) / move_size
    return max(0.0, 1.0 - retracement)


# ═══════════════════════════════════════════════════════════════════════════════
# FAILURE SIGNALS (independent from trend)
# ═══════════════════════════════════════════════════════════════════════════════

def _failure_score(bar_open, bar_close, bar_volume, vwap, atr,
                   entry_price, running_high, R,
                   bar_highs, bar_lows, bar_closes, bar_opens, bar_volumes,
                   bars_held):
    """Detect active trade failure. Separate from trend_score.

    Weighted signals with early trigger for fast reversals
    and stall detection for silent failures.
    """
    signals = {}

    # 1. VWAP breakdown persistence (weight: 1.5 — strongest failure signal)
    #    2+ bars below VWAP = confirmed breakdown
    if vwap > 0 and len(bar_closes) >= 3:
        bars_below = sum(1 for c in bar_closes[-3:] if c < vwap)
        signals['vwap_breakdown'] = 1.0 if bars_below >= 2 else (0.5 if bars_below == 1 else 0.0)
    else:
        signals['vwap_breakdown'] = 0.0

    # 2. Early VWAP break (weight: 0.8 — fast reaction, single bar)
    #    Fires before multi-bar confirmation. Catches fast reversals.
    if vwap > 0 and atr > 0:
        break_depth = (bar_close - vwap) / atr
        if break_depth < -0.3:
            signals['early_break'] = min(1.0, -break_depth / 0.5)
        else:
            signals['early_break'] = 0.0
    else:
        signals['early_break'] = 0.0

    # 3. Lower highs forming (weight: 1.2 — structural deterioration)
    if len(bar_highs) >= 3:
        lh = (bar_highs[-1] < bar_highs[-2]) and (bar_highs[-2] < bar_highs[-3])
        signals['lower_highs'] = 1.0 if lh else 0.0
    else:
        signals['lower_highs'] = 0.0

    # 4. Selling pressure: volume spike on down bar (weight: 1.0)
    if len(bar_closes) >= 2 and len(bar_volumes) >= 5 and len(bar_opens) >= 1:
        avg_vol = sum(bar_volumes[-5:]) / len(bar_volumes[-5:])
        down_bar = bar_close < bar_opens[-1] if bar_opens else bar_close < bar_closes[-2]
        vol_spike = bar_volume > 1.5 * avg_vol if avg_vol > 0 else False
        signals['sell_pressure'] = 1.0 if (down_bar and vol_spike) else 0.0
    else:
        signals['sell_pressure'] = 0.0

    # 5. Deep retracement (weight: 1.0)
    #    Scales from 0 at 40% retrace to 1.0 at 80%+
    if running_high > entry_price:
        retrace = (running_high - bar_close) / (running_high - entry_price)
        signals['deep_retracement'] = min(1.0, max(0.0, (retrace - 0.4) / 0.4))
    else:
        signals['deep_retracement'] = 0.0

    # 6. Stall detection (weight: 1.0)
    #    "Worked then died" (had momentum, lost it) vs "never worked"
    if bars_held > 5:
        unrealized_r = (bar_close - entry_price) / R if R > 0 else 0
        max_r = (running_high - entry_price) / R if R > 0 else 0
        if unrealized_r < 0.5:
            if max_r > 0.8:
                signals['stall'] = 1.0   # had momentum, lost it — dangerous
            else:
                signals['stall'] = 0.5   # never worked — mediocre
        else:
            signals['stall'] = 0.0
    else:
        signals['stall'] = 0.0

    # 7. Compression: range dying (weight: 0.8)
    #    Market losing interest in the stock
    if len(bar_highs) >= 5 and len(bar_lows) >= 5:
        ranges = [bar_highs[i] - bar_lows[i] for i in range(-5, 0)]
        avg_range = sum(ranges) / len(ranges) if ranges else 0
        current_range = bar_highs[-1] - bar_lows[-1] if bar_highs and bar_lows else 0
        if avg_range > 0 and current_range < 0.5 * avg_range:
            signals['compression'] = 0.5
        else:
            signals['compression'] = 0.0
    else:
        signals['compression'] = 0.0

    # Weighted composite
    weights = {
        'vwap_breakdown': 1.5,
        'early_break': 0.8,
        'lower_highs': 1.2,
        'sell_pressure': 1.0,
        'deep_retracement': 1.0,
        'stall': 1.0,
        'compression': 0.8,
    }

    weighted_sum = sum(signals[k] * weights[k] for k in signals)
    total_weight = sum(weights.values())

    return weighted_sum / total_weight, signals


# ═══════════════════════════════════════════════════════════════════════════════
# STATE CLASSIFICATION (logged, not acted on in V1)
# ═══════════════════════════════════════════════════════════════════════════════

def _classify_state(trend_smooth, prev_state):
    """Hysteresis-based state classification.

    Wide bands for V1 data collection. Will tighten from distribution analysis.
    Entry/exit asymmetry prevents thrashing.
    """
    if prev_state == 'STRONG':
        if trend_smooth < 0.50:
            return 'WEAK'
        return 'STRONG'
    elif prev_state == 'WEAK':
        if trend_smooth > 0.75:
            return 'STRONG'
        if trend_smooth < 0.35:
            return 'FAILING'
        return 'WEAK'
    elif prev_state == 'FAILING':
        if trend_smooth > 0.75:
            return 'STRONG'
        if trend_smooth > 0.50:
            return 'WEAK'
        return 'FAILING'
    else:
        # Initial classification (no previous state)
        if trend_smooth > 0.70:
            return 'STRONG'
        elif trend_smooth > 0.45:
            return 'WEAK'
        else:
            return 'FAILING'
