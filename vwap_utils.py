"""
Shared VWAP utilities.
Used by monitor/signals.py, monitor/data_client.py, and strategies/vwap_reclaim.py.
"""


def compute_vwap(high, low, close, volume):
    """
    Cumulative intraday VWAP from a pandas Series/DataFrame slice.

    Parameters
    ----------
    high, low, close, volume : array-like (pandas Series)

    Returns
    -------
    pandas Series of cumulative VWAP values, same index as inputs.
    """
    typical = (high + low + close) / 3
    return (typical * volume).cumsum() / volume.cumsum()


def detect_vwap_reclaim(closes, vwaps, lookback=6, max_dip_age=4):
    """
    Flexible VWAP reclaim detector.

    Pattern (oldest → newest):
      1. Uptrend context  — at least 1 bar above VWAP before the dip
      2. Dip              — at least 1 bar below VWAP (can span 1–N bars)
      3. Recency check    — end of dip must be within max_dip_age bars of confirmation
      4. 2-bar reclaim    — last 2 bars both above VWAP

    Parameters
    ----------
    closes, vwaps  : list-like, oldest first, length >= 4
    lookback       : int  — how far back to search for the dip (default 6)
    max_dip_age    : int  — max bars between end of dip and first confirm bar (default 4)
                           Prevents stale dips from triggering when price has been
                           below VWAP for a long time since the dip ended.

    Returns
    -------
    bool
    """
    n = len(closes)
    if n < 4:   # minimum: 2 confirm + 1 dip + 1 context
        return False

    # 2-bar confirmation: last 2 bars must be above VWAP
    if closes[-1] <= vwaps[-1] or closes[-2] <= vwaps[-2]:
        return False

    # Search window: everything before the 2 confirmation bars
    start = max(0, n - lookback - 2)
    win_c = closes[start: n - 2]
    win_v = vwaps[start: n - 2]

    # Walk backwards to find the rightmost bar below VWAP (end of dip)
    dip_idx = None
    for i in range(len(win_c) - 1, -1, -1):
        if win_c[i] < win_v[i]:
            dip_idx = i
            break

    if dip_idx is None:
        return False  # no dip found in window

    # Recency check: dip must end within max_dip_age bars of the first confirmation bar.
    # len(win_c) - 1 - dip_idx = number of bars between dip end and win_c[-1],
    # plus 1 more bar to reach the first confirmation bar (closes[-2]).
    dip_age = (len(win_c) - 1 - dip_idx) + 1
    if dip_age > max_dip_age:
        return False  # dip is too old; trend context no longer intact

    # Uptrend context: at least 1 bar above VWAP before the dip
    for i in range(dip_idx):
        if win_c[i] > win_v[i]:
            return True

    return False


def detect_vwap_breakdown(closes, vwaps, rsi):
    """
    VWAP breakdown exit detector.

    Requires genuine weakness — not just a single noisy bar crossing below VWAP.
    Current close must be below VWAP, confirmed by EITHER:
      (a) 3 consecutive bars below VWAP  — strong structural breakdown
      (b) 2 consecutive bars + RSI < 45  — momentum confirmation

    Parameters
    ----------
    closes, vwaps : list-like, oldest first, length >= 3
    rsi           : float — current RSI value

    Returns
    -------
    bool
    """
    if len(closes) < 3 or len(vwaps) < 3:
        return False
    cur_below  = closes[-1] < vwaps[-1]
    prev_below = closes[-2] < vwaps[-2]
    prev2_below = closes[-3] < vwaps[-3]

    if not cur_below:
        return False

    # 3-bar confirmation: strong structural breakdown
    if prev_below and prev2_below:
        return True

    # 2-bar + weak momentum: RSI confirms selling pressure
    if prev_below and rsi < 45:
        return True

    return False
