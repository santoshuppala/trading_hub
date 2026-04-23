"""
Edge context helpers — capture market context at signal time for the edge model.

Provides:
    categorize_regime()      — quantize continuous regime score to categorical bucket
    compute_time_bucket()    — bucket current ET time into trading session windows
    compute_confluence_score() — aggregate detector agreement from JSON signals
    SignalContextCache       — cache signal context for propagation to FillLot

Zero behaviour change: these helpers only *record* context, they don't gate trades.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)


# ── Regime categorization ─────────────────────────────────────────────────

def categorize_regime(score: float, vix: Optional[float] = None) -> str:
    """Quantize continuous regime score [-1.0, +1.0] to categorical bucket.

    Uses VIX as override for VOLATILE detection (score alone misses sudden
    spikes where F&G hasn't updated yet).

    Returns: 'BULL_TREND', 'RANGE_BOUND', 'BEAR_TREND', or 'VOLATILE'
    """
    if vix is not None and vix > 25:
        return 'VOLATILE'
    if score > 0.3:
        return 'BULL_TREND'
    if score < -0.3:
        return 'BEAR_TREND'
    return 'RANGE_BOUND'


# ── Time bucket ───────────────────────────────────────────────────────────

# Buckets aligned with known session dynamics (opening range, lunch lull, etc.)
_TIME_BUCKETS = [
    ((9, 30), (9, 45),  '0930_0945'),   # opening range
    ((9, 45), (10, 30), '0945_1030'),   # morning momentum
    ((10, 30), (11, 30), '1030_1130'),  # mid-morning
    ((11, 30), (13, 0),  '1130_1300'),  # lunch lull
    ((13, 0), (15, 0),  '1300_1500'),   # afternoon
    ((15, 0), (15, 45), '1500_1545'),   # late day
    ((15, 45), (16, 0), '1545_1600'),   # close
]


def compute_time_bucket(now_et: Optional[datetime] = None) -> str:
    """Return the session time bucket for the current ET time.

    Returns bucket string like '0930_0945' or 'PRE_MARKET' / 'AFTER_HOURS'.
    """
    if now_et is None:
        now_et = datetime.now(ET)

    h, m = now_et.hour, now_et.minute
    t = (h, m)

    for (sh, sm), (eh, em), label in _TIME_BUCKETS:
        if (sh, sm) <= t < (eh, em):
            return label

    if t < (9, 30):
        return 'PRE_MARKET'
    return 'AFTER_HOURS'


# ── Confluence score ──────────────────────────────────────────────────────

def compute_confluence_score(detector_signals_json: str) -> float:
    """Compute aggregate detector agreement from JSON detector signals.

    Input: JSON string like:
        {"trend": {"fired": true, "strength": 0.72},
         "vwap":  {"fired": true, "strength": 0.65},
         "sr":    {"fired": false, "strength": 0.0}, ...}

    Returns: mean strength of fired detectors, or 0.0 if none fired.
    """
    try:
        signals = json.loads(detector_signals_json) if detector_signals_json else {}
    except (json.JSONDecodeError, TypeError):
        return 0.0

    fired_strengths = []
    for _name, info in signals.items():
        if isinstance(info, dict) and info.get('fired'):
            strength = info.get('strength', 0.0)
            if isinstance(strength, (int, float)):
                fired_strengths.append(float(strength))

    if not fired_strengths:
        return 0.0
    return sum(fired_strengths) / len(fired_strengths)


# ── Signal context cache ──────────────────────────────────────────────────

class SignalContextCache:
    """Caches edge context from SIGNAL / PRO_STRATEGY_SIGNAL events.

    Keyed by event_id (which becomes correlation_id downstream in FILL).
    PositionManager looks up context when creating FillLot.

    Thread-safe, bounded (LRU eviction at max_size).
    """

    def __init__(self, max_size: int = 1000):
        self._cache: OrderedDict[str, Dict] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def store(self, event_id: str, context: Dict) -> None:
        """Store edge context for a signal event."""
        with self._lock:
            self._cache[event_id] = context
            self._cache.move_to_end(event_id)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def get(self, correlation_id: Optional[str]) -> Dict:
        """Retrieve and remove cached context by correlation_id.

        Returns empty dict if not found (graceful degradation).
        """
        if not correlation_id:
            return {}
        with self._lock:
            return self._cache.pop(correlation_id, {})

    def peek(self, correlation_id: Optional[str]) -> Dict:
        """Retrieve without removing (for partial fills)."""
        if not correlation_id:
            return {}
        with self._lock:
            return self._cache.get(correlation_id, {})

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)
