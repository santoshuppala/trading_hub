"""
db/reader.py — Async read queries for replay and analysis.

All methods are coroutines; call them from an async context or via
asyncio.run().  The module uses the shared pool from connection.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .connection import get_pool

log = logging.getLogger(__name__)


class DBReader:
    """
    Read-side API for the TimescaleDB event store.

    Typical usage
    -------------
        reader = DBReader()
        fills  = await reader.get_fills(since=datetime(2026, 4, 1, tzinfo=timezone.utc))
        bars   = await reader.get_bars("AAPL", limit=200)
    """

    # ── Bar data ──────────────────────────────────────────────────────────────

    async def get_bars(
        self,
        ticker: str,
        limit: int = 200,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Return the most recent ``limit`` bars for ``ticker``."""
        pool = get_pool()
        async with pool.acquire() as conn:
            if since:
                rows = await conn.fetch(
                    """
                    SELECT ts, open, high, low, close, volume, vwap, rvol, atr, rsi
                    FROM trading.bar_events
                    WHERE ticker = $1 AND ts >= $2
                    ORDER BY ts DESC
                    LIMIT $3
                    """,
                    ticker, since, limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT ts, open, high, low, close, volume, vwap, rvol, atr, rsi
                    FROM trading.bar_events
                    WHERE ticker = $1
                    ORDER BY ts DESC
                    LIMIT $2
                    """,
                    ticker, limit,
                )
        return [dict(r) for r in rows]

    # ── Fills (for replay / crash recovery supplement) ────────────────────────

    async def get_fills(
        self,
        since: Optional[datetime] = None,
        ticker: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return fills, optionally filtered by time and ticker."""
        pool = get_pool()
        where_clauses = []
        args: List[Any] = []

        if since:
            args.append(since)
            where_clauses.append(f"ts >= ${len(args)}")
        if ticker:
            args.append(ticker)
            where_clauses.append(f"ticker = ${len(args)}")

        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        sql = f"""
            SELECT ts, ticker, side, qty, fill_price, order_id, reason,
                   stop_price, target_price, atr_value, event_id, correlation_id
            FROM trading.fill_events
            {where}
            ORDER BY ts ASC
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]

    # ── Open-day replay ───────────────────────────────────────────────────────

    async def get_todays_fills(self) -> List[Dict[str, Any]]:
        """Return all fills from today (ET date) — used for intraday replay."""
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return await self.get_fills(since=today_start)

    # ── Signal history ────────────────────────────────────────────────────────

    async def get_signals(
        self,
        ticker: Optional[str] = None,
        action: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """Return recent signals, optionally filtered."""
        pool = get_pool()
        where_clauses = []
        args: List[Any] = []

        if ticker:
            args.append(ticker)
            where_clauses.append(f"ticker = ${len(args)}")
        if action:
            args.append(action)
            where_clauses.append(f"action = ${len(args)}")

        args.append(limit)
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        sql = f"""
            SELECT ts, ticker, action, current_price, atr_value, rsi_value, rvol,
                   vwap, stop_price, target_price, event_id
            FROM trading.signal_events
            {where}
            ORDER BY ts DESC
            LIMIT ${len(args)}
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]

    # ── Pro-strategy signals ──────────────────────────────────────────────────

    async def get_pro_signals(
        self,
        tier: Optional[int] = None,
        strategy_name: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        pool = get_pool()
        where_clauses = []
        args: List[Any] = []

        if tier:
            args.append(tier)
            where_clauses.append(f"tier = ${len(args)}")
        if strategy_name:
            args.append(strategy_name)
            where_clauses.append(f"strategy_name = ${len(args)}")

        args.append(limit)
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        sql = f"""
            SELECT ts, ticker, strategy_name, tier, direction, entry_price,
                   stop_price, target_1, target_2, confidence, detector_signals
            FROM trading.pro_strategy_signal_events
            {where}
            ORDER BY ts DESC
            LIMIT ${len(args)}
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]

    # ── Risk blocks ───────────────────────────────────────────────────────────

    async def get_risk_blocks(
        self,
        ticker: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        pool = get_pool()
        args: List[Any] = []
        where = ""
        if ticker:
            args.append(ticker)
            where = "WHERE ticker = $1"
        args.append(limit)
        sql = f"""
            SELECT ts, ticker, reason, signal_action
            FROM trading.risk_block_events
            {where}
            ORDER BY ts DESC
            LIMIT ${len(args)}
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]

    # ── Daily summary ─────────────────────────────────────────────────────────

    async def get_daily_summary(
        self, trade_date: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        pool = get_pool()
        if trade_date:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM trading.daily_trade_summary WHERE trade_date = $1",
                    trade_date,
                )
        else:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM trading.daily_trade_summary ORDER BY trade_date DESC, ticker"
                )
        return [dict(r) for r in rows]

    # ── Session log ────────────────────────────────────────────────────────────

    async def get_sessions(
        self, limit: int = 20, running_only: bool = False
    ) -> List[Dict[str, Any]]:
        """Return recent sessions.  ``running_only=True`` returns sessions without a stop_ts (crashed or active)."""
        pool = get_pool()
        where = "WHERE stop_ts IS NULL" if running_only else ""
        sql = f"""
            SELECT session_id, start_ts, stop_ts, mode, broker,
                   events_emitted, events_persisted, rows_dropped,
                   exit_reason, error_message
            FROM trading.session_log
            {where}
            ORDER BY start_ts DESC
            LIMIT $1
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, limit)
        return [dict(r) for r in rows]

    async def get_session_events(
        self, session_start: datetime, session_stop: Optional[datetime] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Return all events between session start and stop for deterministic replay."""
        pool = get_pool()
        stop = session_stop or datetime.now(timezone.utc)
        tables = [
            "bar_events", "signal_events", "order_req_events",
            "fill_events", "position_events", "pop_signal_events",
            "pro_strategy_signal_events",
        ]
        result: Dict[str, List[Dict[str, Any]]] = {}
        async with pool.acquire() as conn:
            for table in tables:
                rows = await conn.fetch(
                    f"SELECT * FROM trading.{table} WHERE ts >= $1 AND ts <= $2 ORDER BY ts, ingested_at",
                    session_start, stop,
                )
                result[table] = [dict(r) for r in rows]
        return result

    # ── Writer stats ──────────────────────────────────────────────────────────

    async def get_table_row_counts(self) -> Dict[str, int]:
        """Return approximate row counts for all trading tables."""
        pool = get_pool()
        sql = """
            SELECT relname AS table_name,
                   n_live_tup AS row_count
            FROM pg_stat_user_tables
            WHERE schemaname = 'trading'
            ORDER BY relname
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql)
        return {r["table_name"]: r["row_count"] for r in rows}
