"""
db/writer.py — Async batch DBWriter with circuit breaker.

Design
------
- Events are pushed into an in-memory queue (asyncio.Queue) by the
  EventBus subscriber thread (sync → async bridge via run_coroutine_threadsafe).
- A background asyncio task drains the queue in batches using
  asyncpg's executemany() for high throughput.
- A circuit breaker disables writes after DB_CIRCUIT_BREAKER_FAILURES
  consecutive failures and re-tries after DB_CIRCUIT_BREAKER_RESET seconds.
  This prevents DB issues from blocking the hot trading path.

Thread safety
-------------
All public methods are safe to call from sync threads.  Internal state
mutations happen only inside the asyncio event loop task.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import os

from .connection import get_pool

log = logging.getLogger(__name__)

# ── Tunables ──────────────────────────────────────────────────────────────────
_BATCH_MAX      = int(os.getenv("DB_BATCH_MAX",              "200"))
_FLUSH_INTERVAL = float(os.getenv("DB_FLUSH_INTERVAL",       "0.5"))   # seconds
_QUEUE_MAXSIZE  = int(os.getenv("DB_QUEUE_MAXSIZE",          "10000"))
_CB_FAILURES    = int(os.getenv("DB_CIRCUIT_BREAKER_FAILURES", "5"))
_CB_RESET       = int(os.getenv("DB_CIRCUIT_BREAKER_RESET",   "30"))   # seconds


@dataclass
class _WriteRecord:
    table:  str
    row:    Dict[str, Any]


class DBWriter:
    """
    Async batch writer for all trading event tables.

    Usage (async context)
    ---------------------
        writer = DBWriter(loop=asyncio.get_event_loop())
        await writer.start()
        writer.enqueue("bar_events", {...})   # called from any thread
        await writer.stop()

    The ``enqueue`` method is the only public API needed by the subscriber.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop   = loop
        self._queue: asyncio.Queue[_WriteRecord] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._task:  Optional[asyncio.Task] = None

        # Circuit-breaker state
        self._cb_failures     = 0
        self._cb_open         = False
        self._cb_open_since   = 0.0

        # Metrics
        self.rows_written     = 0
        self.rows_dropped     = 0
        self.batches_flushed  = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def enqueue(self, table: str, row: Dict[str, Any]) -> None:
        """
        Thread-safe enqueue.  Call from any thread (including EventBus workers).
        Drops the row and increments ``rows_dropped`` if the queue is full or
        the circuit breaker is open.
        """
        if self._cb_open:
            elapsed = time.monotonic() - self._cb_open_since
            if elapsed < _CB_RESET:
                self.rows_dropped += 1
                return
            # Reset circuit breaker
            self._cb_open     = False
            self._cb_failures = 0
            log.info("DBWriter circuit breaker reset after %.0fs", elapsed)

        record = _WriteRecord(table=table, row=row)
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, record)
        except asyncio.QueueFull:
            self.rows_dropped += 1
            if self.rows_dropped % 500 == 1:
                log.warning("DBWriter queue full — %d rows dropped so far", self.rows_dropped)

    async def start(self) -> None:
        """Start the background flush task."""
        self._task = self._loop.create_task(self._flush_loop(), name="db-writer-flush")
        log.info("DBWriter started (batch=%d flush=%.1fs queue=%d)",
                 _BATCH_MAX, _FLUSH_INTERVAL, _QUEUE_MAXSIZE)

    async def stop(self) -> None:
        """Flush remaining rows, then cancel the task."""
        if self._task:
            await self._drain()             # flush whatever remains
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("DBWriter stopped | written=%d dropped=%d batches=%d",
                 self.rows_written, self.rows_dropped, self.batches_flushed)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(_FLUSH_INTERVAL)
            await self._drain()

    async def _drain(self) -> None:
        """Pull up to _BATCH_MAX records from the queue and write them."""
        if self._queue.empty():
            return

        batch: List[_WriteRecord] = []
        while len(batch) < _BATCH_MAX:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not batch:
            return

        # Group by table for bulk insert
        by_table: Dict[str, List[Dict[str, Any]]] = {}
        for rec in batch:
            by_table.setdefault(rec.table, []).append(rec.row)

        pool = get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for table, rows in by_table.items():
                        await self._insert_batch(conn, table, rows)
            self.rows_written    += len(batch)
            self.batches_flushed += 1
            self._cb_failures     = 0
        except Exception as exc:
            self._cb_failures += 1
            log.error("DBWriter flush failed (failure %d/%d): %s",
                      self._cb_failures, _CB_FAILURES, exc)
            if self._cb_failures >= _CB_FAILURES:
                self._cb_open       = True
                self._cb_open_since = time.monotonic()
                log.error("DBWriter circuit breaker OPEN — writes suspended for %ds", _CB_RESET)
            # Re-queue dropped rows (best-effort, may overflow)
            for rec in batch:
                try:
                    self._queue.put_nowait(rec)
                except asyncio.QueueFull:
                    self.rows_dropped += 1

    @staticmethod
    async def _insert_batch(
        conn: Any,
        table: str,
        rows: List[Dict[str, Any]],
    ) -> None:
        if not rows:
            return
        cols = list(rows[0].keys())
        col_list  = ", ".join(cols)
        val_refs  = ", ".join(f"${i+1}" for i in range(len(cols)))
        sql = f"INSERT INTO trading.{table} ({col_list}) VALUES ({val_refs}) ON CONFLICT DO NOTHING"
        records = [tuple(r[c] for c in cols) for r in rows]
        await conn.executemany(sql, records)


# ── Session lifecycle ────────────────────────────────────────────────────────

class SessionManager:
    """
    Manages session_log rows.  Call ``start()`` at monitor boot and
    ``stop()`` in the finally block.  Crashed sessions have stop_ts=NULL.
    """

    def __init__(self) -> None:
        self.session_id: Optional[str] = None

    async def start(
        self,
        mode: str = "live",
        broker: Optional[str] = None,
        tickers: Optional[List[str]] = None,
        config_hash: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Insert a new session row and return the session_id (UUID string)."""
        import json as _json
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO trading.session_log
                    (mode, broker, tickers, config_hash, metadata)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                RETURNING session_id::text
                """,
                mode,
                broker,
                tickers,
                config_hash,
                _json.dumps(metadata) if metadata else None,
            )
        self.session_id = row["session_id"]
        log.info("Session started: %s (mode=%s)", self.session_id, mode)
        return self.session_id

    async def stop(
        self,
        exit_reason: str = "clean",
        error_message: Optional[str] = None,
        writer: Optional["DBWriter"] = None,
    ) -> None:
        """Update the session row with stop_ts and writer metrics."""
        if not self.session_id:
            return
        pool = get_pool()
        emitted = 0
        persisted = 0
        dropped = 0
        if writer:
            persisted = writer.rows_written
            dropped = writer.rows_dropped
            emitted = persisted + dropped
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE trading.session_log
                SET stop_ts          = now(),
                    events_emitted   = $2,
                    events_persisted = $3,
                    rows_dropped     = $4,
                    exit_reason      = $5,
                    error_message    = $6
                WHERE session_id = $1::uuid
                """,
                self.session_id,
                emitted,
                persisted,
                dropped,
                exit_reason,
                error_message,
            )
        log.info("Session stopped: %s (reason=%s, persisted=%d, dropped=%d)",
                 self.session_id, exit_reason, persisted, dropped)


# ── Module-level singleton ────────────────────────────────────────────────────

_writer: Optional[DBWriter] = None


def get_writer() -> DBWriter:
    """
    Return the module-level DBWriter singleton.
    Must call init_writer() first (done inside init_db flow via DBSubscriber).
    """
    if _writer is None:
        raise RuntimeError("DBWriter not initialised — call init_writer() first")
    return _writer


def init_writer(loop: asyncio.AbstractEventLoop) -> DBWriter:
    """Create and register the module-level singleton."""
    global _writer
    _writer = DBWriter(loop=loop)
    return _writer
