"""
db/feature_store.py — ML feature extraction from TimescaleDB.

Provides ready-to-use feature vectors for downstream ML models.
All features are derived from stored bar and signal data — no
live market data dependency.

Typical usage
-------------
    fs = FeatureStore()
    df = await fs.get_bar_features("AAPL", lookback=50)
    label_df = await fs.get_signal_outcome_labels(days=30)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    import pandas as pd
    _PANDAS = True
except ImportError:
    _PANDAS = False

from .connection import get_pool

log = logging.getLogger(__name__)


class FeatureStore:
    """
    ML feature extraction and labelling queries.

    All methods return either ``list[dict]`` (when pandas unavailable)
    or ``pd.DataFrame`` (when pandas is installed and ``as_df=True``).
    """

    # ── Bar features ──────────────────────────────────────────────────────────

    async def get_bar_features(
        self,
        ticker: str,
        lookback: int = 50,
        as_df: bool = True,
    ):
        """
        Return the last ``lookback`` bars with derived ML features.

        Features included
        -----------------
        - Raw OHLCV + VWAP, RVOL, ATR, RSI
        - bar_return        : (close-open)/open
        - bar_range_pct     : (high-low)/open
        - close_position    : 0=bottom wick, 1=top (close position in bar)
        - vol_ratio_20      : volume / 20-bar avg volume
        - price_vs_ma10     : close / 10-bar close MA - 1
        """
        pool = get_pool()
        sql = """
            SELECT ts, ticker, open, high, low, close, volume,
                   vwap, rvol, atr, rsi,
                   bar_return, bar_range_pct, close_position,
                   vol_ratio_20, price_vs_ma10
            FROM trading.ml_bar_features
            WHERE ticker = $1
            ORDER BY ts DESC
            LIMIT $2
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, ticker, lookback)

        records = [dict(r) for r in rows]
        if as_df and _PANDAS and records:
            import pandas as pd
            df = pd.DataFrame(records)
            df = df.sort_values("ts").reset_index(drop=True)
            return df
        return records

    # ── Signal outcome labels ─────────────────────────────────────────────────

    async def get_signal_outcome_labels(
        self,
        days: int = 30,
        as_df: bool = True,
    ):
        """
        Join BUY signals with subsequent fills to produce outcome labels.

        Outcome columns
        ---------------
        - executed      : 1 if a BUY fill followed within 5 min
        - fill_price    : actual fill price (NaN if not executed)
        - outcome_return: (fill_price - current_price) / current_price
        """
        pool = get_pool()
        since = datetime.now(timezone.utc) - timedelta(days=days)
        sql = """
            SELECT
                s.ts            AS signal_ts,
                s.ticker,
                s.action,
                s.current_price,
                s.atr_value,
                s.rsi_value,
                s.rvol,
                s.vwap,
                s.stop_price,
                s.target_price,
                f.fill_price,
                CASE WHEN f.fill_price IS NOT NULL THEN 1 ELSE 0 END AS executed,
                CASE
                    WHEN f.fill_price IS NOT NULL
                    THEN (f.fill_price - s.current_price) / NULLIF(s.current_price, 0)
                    ELSE NULL
                END AS outcome_return
            FROM trading.signal_events s
            LEFT JOIN LATERAL (
                SELECT fill_price
                FROM trading.fill_events f2
                WHERE f2.ticker = s.ticker
                  AND f2.side   = 'BUY'
                  AND f2.ts BETWEEN s.ts AND s.ts + INTERVAL '5 minutes'
                ORDER BY f2.ts
                LIMIT 1
            ) f ON TRUE
            WHERE s.ts >= $1
              AND s.action = 'BUY'
            ORDER BY s.ts
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, since)

        records = [dict(r) for r in rows]
        if as_df and _PANDAS and records:
            import pandas as pd
            return pd.DataFrame(records)
        return records

    # ── Pro-strategy outcome labels ───────────────────────────────────────────

    async def get_pro_signal_outcomes(
        self,
        days: int = 30,
        tier: Optional[int] = None,
        as_df: bool = True,
    ):
        """
        Join PRO_STRATEGY_SIGNAL events with subsequent fills.

        Outcome columns
        ---------------
        - executed           : 1 if fill found within 5 min
        - rr_achieved        : (fill_price - stop_price) / (target_1 - stop_price)
        - hit_target_1       : 1 if max subsequent price within 30 min >= target_1
        """
        pool = get_pool()
        since = datetime.now(timezone.utc) - timedelta(days=days)
        args: List[Any] = [since]
        tier_clause = ""
        if tier is not None:
            args.append(tier)
            tier_clause = f"AND p.tier = ${len(args)}"

        sql = f"""
            SELECT
                p.ts, p.ticker, p.strategy_name, p.tier, p.direction,
                p.entry_price, p.stop_price, p.target_1, p.target_2,
                p.confidence, p.atr_value, p.rvol, p.rsi_value,
                f.fill_price,
                CASE WHEN f.fill_price IS NOT NULL THEN 1 ELSE 0 END AS executed,
                CASE
                    WHEN f.fill_price IS NOT NULL AND (p.target_1 - p.stop_price) > 0
                    THEN (f.fill_price - p.stop_price) / (p.target_1 - p.stop_price)
                    ELSE NULL
                END AS rr_achieved,
                h.max_price,
                CASE
                    WHEN h.max_price IS NOT NULL AND h.max_price >= p.target_1 THEN 1 ELSE 0
                END AS hit_target_1
            FROM trading.pro_strategy_signal_events p
            LEFT JOIN LATERAL (
                SELECT fill_price
                FROM trading.fill_events f2
                WHERE f2.ticker = p.ticker
                  AND f2.side   = 'BUY'
                  AND f2.ts BETWEEN p.ts AND p.ts + INTERVAL '5 minutes'
                ORDER BY f2.ts
                LIMIT 1
            ) f ON TRUE
            LEFT JOIN LATERAL (
                SELECT max(high) AS max_price
                FROM trading.bar_events b
                WHERE b.ticker = p.ticker
                  AND b.ts BETWEEN p.ts AND p.ts + INTERVAL '30 minutes'
            ) h ON TRUE
            WHERE p.ts >= $1 {tier_clause}
            ORDER BY p.ts
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)

        records = [dict(r) for r in rows]
        if as_df and _PANDAS and records:
            import pandas as pd
            return pd.DataFrame(records)
        return records

    # ── Detector signal effectiveness ─────────────────────────────────────────

    async def get_detector_effectiveness(
        self, days: int = 30, as_df: bool = True
    ):
        """
        Aggregate: for each detector, how often does its firing correlate
        with a BUY signal being executed?
        """
        pool = get_pool()
        since = datetime.now(timezone.utc) - timedelta(days=days)
        sql = """
            SELECT
                p.strategy_name,
                p.tier,
                count(*) AS total,
                count(f.fill_price) AS executed,
                round(count(f.fill_price)::numeric / nullif(count(*),0) * 100, 1) AS exec_pct,
                avg(p.confidence) AS avg_confidence
            FROM trading.pro_strategy_signal_events p
            LEFT JOIN LATERAL (
                SELECT fill_price
                FROM trading.fill_events f2
                WHERE f2.ticker = p.ticker AND f2.side = 'BUY'
                  AND f2.ts BETWEEN p.ts AND p.ts + INTERVAL '5 minutes'
                ORDER BY f2.ts LIMIT 1
            ) f ON TRUE
            WHERE p.ts >= $1
            GROUP BY p.strategy_name, p.tier
            ORDER BY exec_pct DESC
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, since)
        records = [dict(r) for r in rows]
        if as_df and _PANDAS and records:
            import pandas as pd
            return pd.DataFrame(records)
        return records
