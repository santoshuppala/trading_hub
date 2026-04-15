"""
Shared DB initialization helper for satellite processes.

Each satellite process needs its own async DB loop + EventSourcingSubscriber
to persist events (PRO_STRATEGY_SIGNAL, POP_SIGNAL, NEWS_DATA, OPTIONS_SIGNAL, etc.)
to the same event_store that the core process uses.

Usage in satellite process:
    from scripts._db_helper import init_satellite_db

    db_cleanup = init_satellite_db(bus, process_name='pro')
    # ... run engine ...
    # On shutdown:
    if db_cleanup:
        db_cleanup()
"""
import asyncio
import logging
import os
import sys
import threading

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


def init_satellite_db(bus, process_name: str = 'satellite'):
    """Initialize DB layer for a satellite process.

    Sets up:
      - Async event loop in a daemon thread
      - asyncpg connection pool
      - DBWriter (batch writer)
      - EventSourcingSubscriber (persists events from the local bus)

    Returns a cleanup function to call on shutdown, or None if DB init failed.
    """
    try:
        from config import DB_ENABLED, DATABASE_URL
    except ImportError:
        log.warning("[%s] Cannot import DB config — events won't be persisted", process_name)
        return None

    if not DB_ENABLED:
        log.info("[%s] DB disabled (DB_ENABLED=false) — events won't be persisted", process_name)
        return None

    try:
        from db import init_db, close_db
        from db.writer import DBWriter, init_writer
        from db.event_sourcing_subscriber import EventSourcingSubscriber

        # Dedicated async event loop for this process
        db_loop = asyncio.new_event_loop()

        def _run_loop():
            asyncio.set_event_loop(db_loop)
            db_loop.run_forever()

        db_thread = threading.Thread(target=_run_loop, name=f"db-{process_name}", daemon=True)
        db_thread.start()

        # Initialize pool
        pool_future = asyncio.run_coroutine_threadsafe(init_db(DATABASE_URL), db_loop)
        pool_future.result(timeout=15)

        # Initialize writer
        db_writer = init_writer(db_loop)
        start_future = asyncio.run_coroutine_threadsafe(db_writer.start(), db_loop)
        start_future.result(timeout=5)

        # Register event sourcing subscriber on the local bus
        db_sub = EventSourcingSubscriber(
            bus=bus,
            writer=db_writer,
            session_id=str(__import__('uuid').uuid4()),
        )
        db_sub.register()

        log.info("[%s] DB layer ready — events will be persisted to event_store", process_name)

        # Return cleanup function
        def _cleanup():
            try:
                flush_future = asyncio.run_coroutine_threadsafe(db_writer.stop(), db_loop)
                flush_future.result(timeout=5)
                close_future = asyncio.run_coroutine_threadsafe(close_db(), db_loop)
                close_future.result(timeout=5)
                db_loop.call_soon_threadsafe(db_loop.stop)
                log.info("[%s] DB layer stopped (written=%d dropped=%d)",
                         process_name, db_writer.rows_written, db_writer.rows_dropped)
            except Exception as exc:
                log.warning("[%s] DB cleanup error: %s", process_name, exc)

        return _cleanup

    except Exception as exc:
        log.warning("[%s] DB initialization failed — events won't be persisted: %s",
                    process_name, exc)
        return None
