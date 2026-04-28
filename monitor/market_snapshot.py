"""
Market Snapshot — persists previous day's bars + indicators for cold start.

Saves at EOD, loads at startup. Eliminates REST dependency for first 60 seconds.
If snapshot is missing/corrupt, system falls back to REST (current behavior, no regression).

Saved data per ticker:
  - Last 40 bars (OHLCV) — enough for RSI(14), ATR(14), VWAP
  - Last computed indicators (RSI, ATR, RVOL, VWAP)
  - S/R levels, ORB range from previous session
  - Timestamp of snapshot

File: data/market_snapshot.json (~2MB for 250 tickers × 40 bars)
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ET = ZoneInfo('America/New_York')

_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'market_snapshot.json')

_MAX_BARS = 40  # enough for RSI(14) + ATR(14) + warmup


def save_snapshot(bars_cache: dict, indicators_cache: dict = None,
                  levels_cache: dict = None) -> bool:
    """Save end-of-day snapshot for tomorrow's cold start.

    Args:
        bars_cache: {ticker: DataFrame} from monitor._bars_cache
        indicators_cache: {ticker: {rsi, atr, rvol, vwap}} optional
        levels_cache: {ticker: {sr_levels, orb_high, ...}} optional

    Returns True if saved successfully.
    """
    try:
        snapshot = {
            '_timestamp': datetime.now(ET).isoformat(),
            '_date': datetime.now(ET).strftime('%Y-%m-%d'),
            '_tickers': 0,
            'bars': {},
            'indicators': indicators_cache or {},
            'levels': levels_cache or {},
        }

        for ticker, df in bars_cache.items():
            if df is None or len(df) == 0:
                continue
            try:
                # Take last N bars, convert to list of dicts
                tail = df.tail(_MAX_BARS)
                bars = []
                for _, row in tail.iterrows():
                    bars.append({
                        'o': float(row.get('open', row.get('o', 0))),
                        'h': float(row.get('high', row.get('h', 0))),
                        'l': float(row.get('low', row.get('l', 0))),
                        'c': float(row.get('close', row.get('c', 0))),
                        'v': float(row.get('volume', row.get('v', 0))),
                    })
                if bars:
                    snapshot['bars'][ticker] = bars
            except Exception:
                continue

        snapshot['_tickers'] = len(snapshot['bars'])

        # Atomic write
        tmp = _SNAPSHOT_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(snapshot, f, separators=(',', ':'))
        os.replace(tmp, _SNAPSHOT_PATH)

        log.info("[MarketSnapshot] Saved %d tickers (%d bytes)",
                 snapshot['_tickers'], os.path.getsize(_SNAPSHOT_PATH))
        return True

    except Exception as e:
        log.warning("[MarketSnapshot] Save failed: %s", e)
        return False


def load_snapshot() -> Optional[dict]:
    """Load previous day's snapshot for cold start.

    Returns dict with keys: bars, indicators, levels, _date
    Returns None if no snapshot, corrupt, or too old (>3 days).
    """
    try:
        if not os.path.exists(_SNAPSHOT_PATH):
            log.info("[MarketSnapshot] No snapshot file — using REST cold start")
            return None

        with open(_SNAPSHOT_PATH) as f:
            snapshot = json.load(f)

        # Validate
        snap_date = snapshot.get('_date', '')
        if not snap_date:
            log.warning("[MarketSnapshot] Snapshot has no date — ignoring")
            return None

        # Check age (allow up to 3 days for weekends)
        from datetime import date
        try:
            snap_d = date.fromisoformat(snap_date)
            age_days = (date.today() - snap_d).days
            if age_days > 4:  # Mon snapshot used on Tue (1 day), Fri→Mon (3 days)
                log.warning("[MarketSnapshot] Snapshot too old (%d days) — ignoring", age_days)
                return None
        except (ValueError, TypeError):
            pass

        n_tickers = len(snapshot.get('bars', {}))
        log.info("[MarketSnapshot] Loaded snapshot from %s: %d tickers", snap_date, n_tickers)
        return snapshot

    except (json.JSONDecodeError, OSError) as e:
        log.warning("[MarketSnapshot] Load failed (corrupt?): %s — using REST", e)
        return None


def seed_from_snapshot(bar_builder, tick_detector, snapshot: dict) -> int:
    """Seed BarBuilder and TickDetector from a loaded snapshot.

    Args:
        bar_builder: BarBuilder instance (or None)
        tick_detector: TickSignalDetector instance (or None)
        snapshot: dict from load_snapshot()

    Returns number of tickers seeded.
    """
    import pandas as pd

    bars_data = snapshot.get('bars', {})
    indicators = snapshot.get('indicators', {})
    levels = snapshot.get('levels', {})
    seeded = 0

    for ticker, bar_list in bars_data.items():
        try:
            if not bar_list or len(bar_list) < 15:
                continue

            # Rebuild DataFrame from snapshot
            df = pd.DataFrame(bar_list)
            df.columns = ['open', 'high', 'low', 'close', 'volume']

            # Seed BarBuilder with historical bars
            if bar_builder and hasattr(bar_builder, 'seed'):
                bar_builder.seed(ticker, df)

            # Seed TickDetector with levels + indicators
            if tick_detector:
                if ticker in levels:
                    tick_detector.update_levels(ticker, levels[ticker],
                                                from_snapshot=True)
                if ticker in indicators:
                    tick_detector.update_indicators(ticker, indicators[ticker],
                                                    from_snapshot=True)
                elif len(df) >= 15:
                    # Compute basic indicators from snapshot bars
                    try:
                        close = df['close'].values
                        high = df['high'].values
                        low = df['low'].values
                        vol = df['volume'].values
                        import numpy as np

                        # RSI (SMA, last 14)
                        delta = np.diff(close)
                        gains = np.where(delta > 0, delta, 0)
                        losses = np.where(delta < 0, -delta, 0)
                        avg_gain = float(gains[-14:].mean())
                        avg_loss = float(losses[-14:].mean())
                        if avg_gain < 1e-10 and avg_loss < 1e-10:
                            rsi = 50.0
                        elif avg_loss < 1e-10:
                            rsi = 100.0
                        else:
                            rsi = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

                        # ATR (SMA, last 14)
                        prev_c = np.empty_like(close)
                        prev_c[0] = close[0]
                        prev_c[1:] = close[:-1]
                        tr = np.maximum(high - low,
                                        np.maximum(np.abs(high - prev_c),
                                                   np.abs(low - prev_c)))
                        atr = float(tr[-14:].mean())

                        # VWAP
                        tp = (high + low + close) / 3.0
                        cum_tpv = np.cumsum(tp * vol)
                        cum_v = np.cumsum(vol)
                        vwap = float(cum_tpv[-1] / cum_v[-1]) if cum_v[-1] > 0 else float(close[-1])

                        tick_detector.update_indicators(ticker, {
                            'rsi': rsi, 'atr': atr, 'rvol': 1.0, 'vwap': vwap,
                        }, from_snapshot=True)
                    except Exception:
                        pass

            seeded += 1
        except Exception:
            continue

    # Seed prev_close for gap detection at market open
    if tick_detector and hasattr(tick_detector, 'set_prev_closes'):
        prev_closes = {}
        for ticker, bar_list in bars_data.items():
            if bar_list and len(bar_list) > 0:
                prev_closes[ticker] = bar_list[-1].get('c', 0)
        if prev_closes:
            tick_detector.set_prev_closes(prev_closes)
            log.info("[MarketSnapshot] Seeded %d prev_close prices for gap detection",
                     len(prev_closes))

    log.info("[MarketSnapshot] Seeded %d tickers from snapshot", seeded)
    return seeded
