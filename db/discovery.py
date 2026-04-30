"""
db/discovery.py — Discovered tickers persistence (DB as single source of truth).

Replaces the Kafka + JSON file discovery pipeline. Both data_collector and
Core use this module:
  - data_collector: record_discovery() on each source scan
  - Core: get_today_tickers() on startup, get_new_since() every 60s

All methods are coroutines using the shared pool from connection.py.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set
from zoneinfo import ZoneInfo

from .connection import get_pool

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)


async def record_discovery(
    ticker: str,
    source: str,
    metadata: Optional[Dict[str, Any]] = None,
    session_date: Optional[str] = None,
) -> None:
    """
    Record a discovered ticker to the database.

    Uses UPSERT: if (ticker, session_date, source) already exists,
    updates last_seen and merges metadata.

    Parameters
    ----------
    ticker : str
        The ticker symbol (e.g., 'INTC')
    source : str
        Discovery source (e.g., 'stocktwits', 'benzinga_news', 'finviz_intraday',
        'polygon_movers', 'yahoo_earnings', 'yahoo_intraday', 'polygon_intraday',
        'momentum_screener', 'unusual_options_flow')
    metadata : dict, optional
        Source-specific context (reason, change_pct, volume, headline, etc.)
    session_date : str, optional
        Trading date as 'YYYY-MM-DD'. Defaults to today ET.
    """
    pool = get_pool()
    if not pool:
        return

    if session_date is None:
        session_date = datetime.now(ET).strftime('%Y-%m-%d')

    now = datetime.now(ET)
    meta_json = json.dumps(metadata or {}, default=str)

    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO trading.discovered_tickers
                    (ts, ticker, source, discovery_data, metadata, session_date, last_seen, ingested_at)
                VALUES ($1, $2, $3, $4, $4::jsonb, $5, $1, $1)
                ON CONFLICT (ticker, session_date, source)
                DO UPDATE SET
                    last_seen = $1,
                    metadata = COALESCE(
                        trading.discovered_tickers.metadata, '{}'::jsonb
                    ) || $4::jsonb
            """, now, ticker, source, meta_json, session_date)
    except Exception as exc:
        log.warning("[Discovery DB] record_discovery(%s, %s) failed: %s",
                    ticker, source, exc)


async def record_discoveries_batch(
    discoveries: List[Dict[str, Any]],
    session_date: Optional[str] = None,
) -> int:
    """
    Batch insert multiple discoveries efficiently.

    Parameters
    ----------
    discoveries : list of dict
        Each dict must have 'ticker' and 'source', optionally 'metadata'.
    session_date : str, optional
        Defaults to today ET.

    Returns
    -------
    int : number of rows written
    """
    pool = get_pool()
    if not pool or not discoveries:
        return 0

    if session_date is None:
        session_date = datetime.now(ET).strftime('%Y-%m-%d')

    now = datetime.now(ET)
    written = 0

    try:
        async with pool.acquire() as conn:
            for d in discoveries:
                ticker = d.get('ticker', '')
                source = d.get('source', 'unknown')
                meta_json = json.dumps(d.get('metadata', {}), default=str)
                if not ticker:
                    continue
                try:
                    await conn.execute("""
                        INSERT INTO trading.discovered_tickers
                            (ts, ticker, source, discovery_data, metadata,
                             session_date, last_seen, ingested_at)
                        VALUES ($1, $2, $3, $4, $4::jsonb, $5, $1, $1)
                        ON CONFLICT (ticker, session_date, source)
                        DO UPDATE SET
                            last_seen = $1,
                            metadata = COALESCE(
                                trading.discovered_tickers.metadata, '{}'::jsonb
                            ) || $4::jsonb
                    """, now, ticker, source, meta_json, session_date)
                    written += 1
                except Exception as exc:
                    log.debug("[Discovery DB] batch insert %s failed: %s",
                              ticker, exc)
    except Exception as exc:
        log.warning("[Discovery DB] batch insert failed: %s", exc)

    return written


async def get_today_tickers(
    session_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Get all discovered tickers for today.

    Returns list of dicts with keys: ticker, source, discovered_at, metadata.
    Used by Core on startup for full restoration.
    """
    pool = get_pool()
    if not pool:
        return []

    if session_date is None:
        session_date = datetime.now(ET).strftime('%Y-%m-%d')

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT ON (ticker)
                    ticker, source, ts as discovered_at, metadata
                FROM trading.discovered_tickers
                WHERE session_date = $1
                ORDER BY ticker, ts ASC
            """, session_date)
            return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("[Discovery DB] get_today_tickers failed: %s", exc)
        return []


async def get_new_since(
    since: datetime,
    session_date: Optional[str] = None,
) -> List[str]:
    """
    Get tickers discovered after a given timestamp (for periodic polling).

    Returns list of ticker strings (deduplicated).
    Used by Core every 60s to pick up new discoveries.
    """
    pool = get_pool()
    if not pool:
        return []

    if session_date is None:
        session_date = datetime.now(ET).strftime('%Y-%m-%d')

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT ticker
                FROM trading.discovered_tickers
                WHERE session_date = $1 AND ts > $2
                ORDER BY ticker
            """, session_date, since)
            return [r['ticker'] for r in rows]
    except Exception as exc:
        log.warning("[Discovery DB] get_new_since failed: %s", exc)
        return []
