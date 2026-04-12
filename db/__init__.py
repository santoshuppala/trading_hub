"""
db — Async TimescaleDB/PostgreSQL event store for TradingHub.

Usage
-----
    from db import init_db, close_db, get_writer

    # At startup (after monitor is created, before monitor.start()):
    pool = await init_db()          # creates asyncpg pool
    writer = get_writer()           # singleton DBWriter
    subscriber = DBSubscriber(bus=monitor._bus, writer=writer)
    subscriber.register()           # wire all EventBus subscriptions

    # At shutdown (in finally block):
    await close_db()

Public API
----------
    init_db()        → asyncpg.Pool
    close_db()       → None
    get_pool()       → asyncpg.Pool
    get_writer()     → DBWriter
"""

from .connection import init_db, close_db, get_pool
from .writer import DBWriter, get_writer, SessionManager
from .subscriber import DBSubscriber
from .reader import DBReader
from .feature_store import FeatureStore
from .migrations import run_migrations

__all__ = [
    "init_db", "close_db", "get_pool",
    "DBWriter", "get_writer",
    "SessionManager",
    "DBSubscriber",
    "DBReader",
    "FeatureStore",
    "run_migrations",
]
