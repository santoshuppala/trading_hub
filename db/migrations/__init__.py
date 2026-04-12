"""db.migrations — SQL migration runner for TimescaleDB."""
from .run import run_migrations

__all__ = ["run_migrations"]
