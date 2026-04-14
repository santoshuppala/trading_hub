"""
db/projection_builder.py — Rebuild Read Models from Event Store

In event sourcing, projections are ALWAYS built FROM events, never the other way around.
This module provides functions to rebuild all projection tables from the event_store.

Usage:
    # Rebuild all projections
    await ProjectionBuilder.rebuild_all()

    # Rebuild specific projections
    await ProjectionBuilder.rebuild_position_state('ARM')
    await ProjectionBuilder.rebuild_completed_trades(date='2026-04-13')
    await ProjectionBuilder.rebuild_daily_metrics('2026-04-13')
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date
from typing import Optional, List, Dict, Any

from .connection import get_pool

log = logging.getLogger(__name__)


class ProjectionBuilder:
    """Rebuild read models from the event store."""

    @staticmethod
    async def rebuild_all() -> None:
        """Rebuild all projections from scratch."""
        log.info("Starting complete projection rebuild...")
        await ProjectionBuilder.rebuild_all_position_states()
        await ProjectionBuilder.rebuild_all_completed_trades()
        await ProjectionBuilder.rebuild_all_daily_metrics()
        await ProjectionBuilder.rebuild_all_signal_history()
        await ProjectionBuilder.rebuild_all_fill_history()
        log.info("Complete projection rebuild finished")

    # ── Position State Projection ──────────────────────────────────────────

    @staticmethod
    async def rebuild_position_state(ticker: str) -> None:
        """Rebuild position state for a specific ticker by replaying events."""
        pool = get_pool()
        position_state = {}

        async with pool.acquire() as conn:
            # Get all position events for this ticker in order
            events = await conn.fetch(
                """
                SELECT
                    event_id,
                    event_type,
                    event_payload,
                    event_time
                FROM event_store
                WHERE aggregate_id = $1
                ORDER BY event_sequence ASC
                """,
                f"position_{ticker}",
            )

            # Replay events to reconstruct current state
            for event in events:
                payload = json.loads(event["event_payload"])
                event_type = event["event_type"]

                if event_type == "PositionOpened":
                    position_state = {
                        "ticker":           ticker,
                        "position_id":      event["event_id"],
                        "action":           payload.get("action"),
                        "quantity":         payload.get("qty"),
                        "entry_price":      payload.get("entry_price"),
                        "entry_time":       payload.get("entry_time"),
                        "current_price":    payload.get("current_price"),
                        "stop_price":       payload.get("stop_price"),
                        "target_1_price":   payload.get("target_price"),
                        "target_2_price":   payload.get("half_target"),
                        "unrealised_pnl":   None,
                        "realised_pnl":     None,
                        "opened_event_id":  event["event_id"],
                        "last_modified_event_id": event["event_id"],
                        "last_modified_time": event["event_time"],
                    }

                elif event_type == "PartialExited":
                    # Update remaining quantity and add to PnL
                    if position_state:
                        position_state["quantity"] = payload.get("qty")
                        position_state["unrealised_pnl"] = payload.get("pnl")
                        position_state["last_modified_event_id"] = event["event_id"]
                        position_state["last_modified_time"] = event["event_time"]

                elif event_type == "PositionClosed":
                    # Record final PnL and clear position state
                    if position_state:
                        position_state["realised_pnl"] = payload.get("pnl")
                        position_state["action"] = "CLOSED"
                        position_state["last_modified_event_id"] = event["event_id"]
                        position_state["last_modified_time"] = event["event_time"]

            # Update database with final state
            if position_state and position_state.get("action") != "CLOSED":
                # Position still open
                await conn.execute(
                    """
                    INSERT INTO position_state
                    (ticker, position_id, action, quantity, entry_price, entry_time,
                     current_price, stop_price, target_1_price, target_2_price,
                     unrealised_pnl, opened_event_id, last_modified_event_id, last_modified_time)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                    ON CONFLICT (ticker) DO UPDATE SET
                        action = EXCLUDED.action,
                        quantity = EXCLUDED.quantity,
                        entry_price = EXCLUDED.entry_price,
                        entry_time = EXCLUDED.entry_time,
                        current_price = EXCLUDED.current_price,
                        stop_price = EXCLUDED.stop_price,
                        target_1_price = EXCLUDED.target_1_price,
                        target_2_price = EXCLUDED.target_2_price,
                        unrealised_pnl = EXCLUDED.unrealised_pnl,
                        last_modified_event_id = EXCLUDED.last_modified_event_id,
                        last_modified_time = EXCLUDED.last_modified_time
                    """,
                    position_state["ticker"],
                    position_state["position_id"],
                    position_state["action"],
                    position_state["quantity"],
                    position_state["entry_price"],
                    position_state["entry_time"],
                    position_state["current_price"],
                    position_state["stop_price"],
                    position_state["target_1_price"],
                    position_state["target_2_price"],
                    position_state["unrealised_pnl"],
                    position_state["opened_event_id"],
                    position_state["last_modified_event_id"],
                    position_state["last_modified_time"],
                )
            elif position_state and position_state.get("action") == "CLOSED":
                # Position closed - remove from active positions
                await conn.execute(
                    "DELETE FROM position_state WHERE ticker = $1",
                    ticker,
                )

        log.debug(f"Rebuilt position state for {ticker}")

    @staticmethod
    async def rebuild_all_position_states() -> None:
        """Rebuild all position states."""
        pool = get_pool()

        async with pool.acquire() as conn:
            # Get all unique tickers
            tickers = await conn.fetch(
                """
                SELECT DISTINCT
                    (event_payload->>'ticker') as ticker
                FROM event_store
                WHERE aggregate_type = 'Position'
                """
            )

        for row in tickers:
            await ProjectionBuilder.rebuild_position_state(row["ticker"])

        log.info(f"Rebuilt {len(tickers)} position states")

    # ── Completed Trades Projection ────────────────────────────────────────

    @staticmethod
    async def rebuild_completed_trades(since: Optional[date] = None) -> None:
        """
        Rebuild completed trades from position events.
        Links OPENED and CLOSED events to create trade records.
        """
        pool = get_pool()

        async with pool.acquire() as conn:
            # Find all position pairs (OPENED → CLOSED)
            query = """
            WITH position_events AS (
                SELECT
                    event_id,
                    event_type,
                    event_payload,
                    event_time,
                    aggregate_id,
                    LAG(event_id) OVER (
                        PARTITION BY aggregate_id ORDER BY event_sequence
                    ) as prev_event_id
                FROM event_store
                WHERE aggregate_type = 'Position'
                  AND event_type IN ('PositionOpened', 'PositionClosed')
            )
            SELECT
                open_events.event_id as opened_event_id,
                open_events.event_payload as open_payload,
                close_events.event_id as closed_event_id,
                close_events.event_payload as close_payload,
                open_events.event_time as open_time,
                close_events.event_time as close_time,
                close_events.aggregate_id
            FROM position_events open_events
            JOIN position_events close_events
                ON open_events.aggregate_id = close_events.aggregate_id
                AND open_events.event_type = 'PositionOpened'
                AND close_events.event_type = 'PositionClosed'
                AND close_events.prev_event_id = open_events.event_id
            """
            if since:
                query += f" WHERE close_events.event_time >= '{since}'"

            query += " ORDER BY close_events.event_time DESC"

            trades = await conn.fetch(query)

            # Insert completed trades
            for trade in trades:
                open_payload = json.loads(trade["open_payload"])
                close_payload = json.loads(trade["close_payload"])

                duration_seconds = int(
                    (trade["close_time"] - trade["open_time"]).total_seconds()
                )

                pnl = close_payload.get("pnl", 0)
                is_win = pnl >= 0

                await conn.execute(
                    """
                    INSERT INTO completed_trades
                    (trade_id, ticker, quantity, entry_price, entry_time,
                     exit_price, exit_time, gross_pnl, realised_pnl, is_win,
                     duration_seconds, opened_event_id, closed_event_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                    ON CONFLICT (trade_id) DO NOTHING
                    """,
                    trade["closed_event_id"],
                    open_payload.get("ticker"),
                    open_payload.get("qty"),
                    open_payload.get("entry_price"),
                    open_payload.get("entry_time"),
                    close_payload.get("exit_price"),
                    trade["close_time"],
                    pnl,
                    pnl,
                    is_win,
                    duration_seconds,
                    trade["opened_event_id"],
                    trade["closed_event_id"],
                )

        log.info(f"Rebuilt completed trades since {since}")

    @staticmethod
    async def rebuild_all_completed_trades() -> None:
        """Rebuild all completed trades."""
        await ProjectionBuilder.rebuild_completed_trades()

    # ── Daily Metrics Projection ────────────────────────────────────────────

    @staticmethod
    async def rebuild_daily_metrics(metric_date: Optional[str] = None) -> None:
        """Rebuild daily metrics for a specific date."""
        pool = get_pool()

        if metric_date is None:
            metric_date = datetime.now().date().isoformat()

        async with pool.acquire() as conn:
            metrics = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN is_win THEN 1 ELSE 0 END) as winning_trades,
                    SUM(CASE WHEN is_win = false THEN 1 ELSE 0 END) as losing_trades,
                    SUM(realised_pnl) as total_pnl,
                    MAX(realised_pnl) as max_win,
                    MIN(realised_pnl) as max_loss,
                    MIN(exit_time) as first_trade,
                    MAX(exit_time) as last_trade
                FROM completed_trades
                WHERE DATE(exit_time) = $1
                """,
                metric_date,
            )

            if metrics and metrics["total_trades"] > 0:
                win_rate = (
                    metrics["winning_trades"] * 100 / metrics["total_trades"]
                )

                await conn.execute(
                    """
                    INSERT INTO daily_metrics
                    (metric_date, total_trades, winning_trades, losing_trades,
                     total_pnl, max_win, max_loss, win_rate, first_trade, last_trade)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (metric_date) DO UPDATE SET
                        total_trades = EXCLUDED.total_trades,
                        winning_trades = EXCLUDED.winning_trades,
                        losing_trades = EXCLUDED.losing_trades,
                        total_pnl = EXCLUDED.total_pnl,
                        max_win = EXCLUDED.max_win,
                        max_loss = EXCLUDED.max_loss,
                        win_rate = EXCLUDED.win_rate,
                        first_trade = EXCLUDED.first_trade,
                        last_trade = EXCLUDED.last_trade,
                        last_updated = now()
                    """,
                    metric_date,
                    metrics["total_trades"],
                    metrics["winning_trades"],
                    metrics["losing_trades"],
                    metrics["total_pnl"],
                    metrics["max_win"],
                    metrics["max_loss"],
                    win_rate,
                    metrics["first_trade"],
                    metrics["last_trade"],
                )

        log.debug(f"Rebuilt daily metrics for {metric_date}")

    @staticmethod
    async def rebuild_all_daily_metrics(days: int = 30) -> None:
        """Rebuild daily metrics for the last N days."""
        from datetime import timedelta

        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)

        for i in range(days):
            current_date = start_date + timedelta(days=i)
            await ProjectionBuilder.rebuild_daily_metrics(current_date.isoformat())

        log.info(f"Rebuilt daily metrics for {days} days")

    # ── Signal History Projection ──────────────────────────────────────────

    @staticmethod
    async def rebuild_all_signal_history() -> None:
        """Rebuild signal history from signal events."""
        pool = get_pool()

        async with pool.acquire() as conn:
            signals = await conn.fetch(
                """
                SELECT
                    event_id,
                    event_payload,
                    event_time
                FROM event_store
                WHERE event_type IN ('ProStrategySignal', 'PopStrategySignal', 'StrategySignal')
                ORDER BY event_time DESC
                """
            )

            for signal in signals:
                payload = json.loads(signal["event_payload"])

                await conn.execute(
                    """
                    INSERT INTO signal_history
                    (signal_id, signal_type, ticker, action, entry_price,
                     stop_price, target_1, target_2, confidence, signal_time, signal_event_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (signal_id) DO NOTHING
                    """,
                    signal["event_id"],
                    signal["event_type"],
                    payload.get("ticker") or payload.get("symbol"),
                    payload.get("action"),
                    payload.get("entry_price"),
                    payload.get("stop_price"),
                    payload.get("target_1") or payload.get("target_price"),
                    payload.get("target_2") or payload.get("half_target"),
                    payload.get("confidence"),
                    signal["event_time"],
                    signal["event_id"],
                )

        log.info("Rebuilt signal history")

    # ── Fill History Projection ────────────────────────────────────────────

    @staticmethod
    async def rebuild_all_fill_history() -> None:
        """Rebuild fill history from fill events."""
        pool = get_pool()

        async with pool.acquire() as conn:
            fills = await conn.fetch(
                """
                SELECT
                    event_id,
                    event_payload,
                    event_time
                FROM event_store
                WHERE event_type = 'FillExecuted'
                ORDER BY event_time DESC
                """
            )

            for fill in fills:
                payload = json.loads(fill["event_payload"])

                await conn.execute(
                    """
                    INSERT INTO fill_history
                    (fill_id, ticker, side, quantity, fill_price, fill_time,
                     order_id, reason, fill_event_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (fill_id) DO NOTHING
                    """,
                    fill["event_id"],
                    payload.get("ticker"),
                    payload.get("side"),
                    payload.get("qty"),
                    payload.get("fill_price"),
                    fill["event_time"],
                    payload.get("order_id"),
                    payload.get("reason"),
                    fill["event_id"],
                )

        log.info("Rebuilt fill history")


async def audit_event_store() -> Dict[str, Any]:
    """
    Audit the event store for completeness and latencies.

    Returns statistics about event processing pipeline.
    """
    pool = get_pool()

    async with pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) as total_events,
                COUNT(DISTINCT event_type) as unique_event_types,
                COUNT(DISTINCT aggregate_type) as unique_aggregate_types,
                MIN(event_time) as earliest_event,
                MAX(event_time) as latest_event,
                AVG(EXTRACT(EPOCH FROM (persisted_time - event_time))) * 1000 as avg_latency_ms,
                MAX(EXTRACT(EPOCH FROM (persisted_time - event_time))) * 1000 as max_latency_ms,
                PERCENTILE_CONT(0.95) WITHIN GROUP (
                    ORDER BY EXTRACT(EPOCH FROM (persisted_time - event_time)) * 1000
                ) as p95_latency_ms
            FROM event_store
            """
        )

    return {
        "total_events": stats["total_events"],
        "unique_event_types": stats["unique_event_types"],
        "unique_aggregates": stats["unique_aggregate_types"],
        "earliest_event": stats["earliest_event"],
        "latest_event": stats["latest_event"],
        "avg_latency_ms": stats["avg_latency_ms"],
        "max_latency_ms": stats["max_latency_ms"],
        "p95_latency_ms": stats["p95_latency_ms"],
    }
