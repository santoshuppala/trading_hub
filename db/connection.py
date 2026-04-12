"""
db/connection.py — asyncpg connection pool lifecycle.

The pool is a module-level singleton so that writer, reader,
and feature_store all share the same connections without needing
dependency injection beyond an import.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def init_db(dsn: str | None = None) -> asyncpg.Pool:
    """
    Create the asyncpg connection pool and return it.

    Parameters
    ----------
    dsn : str, optional
        asyncpg connection DSN.  Defaults to the DATABASE_URL env var.
        Format: ``postgresql://user:password@host:port/dbname``

    Returns
    -------
    asyncpg.Pool
    """
    global _pool
    if _pool is not None:
        return _pool

    url = dsn or os.getenv("DATABASE_URL", "postgresql://trading:trading_secret@localhost:5432/trading_hub")

    log.info("Connecting to TimescaleDB: %s", _mask_dsn(url))
    _pool = await asyncpg.create_pool(
        url,
        min_size=2,
        max_size=10,
        command_timeout=30,
        # Ensure UTC timestamps are returned as aware datetimes
        init=_init_connection,
    )
    log.info("TimescaleDB pool ready (min=2 max=10)")
    return _pool


async def close_db() -> None:
    """Gracefully close the pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("TimescaleDB pool closed")


def get_pool() -> asyncpg.Pool:
    """Return the open pool.  Raises RuntimeError if init_db() was not called."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call await init_db() first")
    return _pool


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _init_connection(conn: asyncpg.Connection) -> None:
    """Per-connection initialisation: set search_path and UTC timezone."""
    await conn.execute("SET search_path TO trading, public")
    await conn.execute("SET timezone TO 'UTC'")


def _mask_dsn(dsn: str) -> str:
    """Replace password in DSN with *** for safe logging."""
    import re
    return re.sub(r'(:)([^:@]+)(@)', r'\1***\3', dsn)
