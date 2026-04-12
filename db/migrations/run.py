#!/usr/bin/env python3
"""
db/migrations/run.py — Idempotent SQL migration runner.

Executes every *.sql file in migrations/sql/ in filename order.
Uses an advisory lock so concurrent runners don't collide.

Usage
-----
    # From project root:
    python -m db.migrations.run

    # Or directly:
    python db/migrations/run.py

    # Custom DSN:
    DATABASE_URL=postgresql://... python -m db.migrations.run

Each migration is tracked in ``trading.schema_migrations``.
Already-applied files are skipped.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

_SQL_DIR = Path(__file__).parent / "sql"
_ADVISORY_LOCK_ID = 8675309  # arbitrary, unique to this app

_BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS trading;
CREATE TABLE IF NOT EXISTS trading.schema_migrations (
    filename    TEXT        PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    checksum    TEXT        NOT NULL
);
"""


async def run_migrations(dsn: str | None = None) -> int:
    """
    Apply all pending SQL migrations.

    Returns the number of migrations applied.
    """
    url = dsn or os.getenv(
        "DATABASE_URL",
        "postgresql://trading:trading_secret@localhost:5432/trading_hub",
    )

    conn: asyncpg.Connection = await asyncpg.connect(url)
    applied = 0

    try:
        # Advisory lock — blocks other runners
        await conn.execute(f"SELECT pg_advisory_lock({_ADVISORY_LOCK_ID})")

        # Bootstrap tracking table
        await conn.execute(_BOOTSTRAP)

        # Discover already-applied migrations
        rows = await conn.fetch("SELECT filename FROM trading.schema_migrations")
        done = {r["filename"] for r in rows}

        # Collect and sort SQL files
        sql_files = sorted(_SQL_DIR.glob("*.sql"))
        if not sql_files:
            log.info("No SQL files found in %s", _SQL_DIR)
            return 0

        for sql_file in sql_files:
            if sql_file.name in done:
                log.debug("skip (already applied): %s", sql_file.name)
                continue

            content = sql_file.read_text(encoding="utf-8")
            checksum = hashlib.sha256(content.encode()).hexdigest()[:16]

            log.info("Applying migration: %s", sql_file.name)
            try:
                await conn.execute(content)
                await conn.execute(
                    "INSERT INTO trading.schema_migrations (filename, checksum) VALUES ($1, $2)",
                    sql_file.name,
                    checksum,
                )
                applied += 1
                log.info("  OK: %s", sql_file.name)
            except Exception as exc:
                log.error("  FAILED: %s — %s", sql_file.name, exc)
                raise

        log.info("Migrations complete: %d applied, %d skipped",
                 applied, len(done))
    finally:
        await conn.execute(f"SELECT pg_advisory_unlock({_ADVISORY_LOCK_ID})")
        await conn.close()

    return applied


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    try:
        count = asyncio.run(run_migrations())
        print(f"\n{count} migration(s) applied.")
    except Exception as exc:
        print(f"\nMigration failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
