"""
Shared logging setup — all processes use ET (Eastern Time) timestamps.

Trading logs in ET match market hours (9:30-16:00 ET). Using a consistent
format across all 6 processes prevents timezone confusion when correlating
events across core.log, options.log, supervisor.log, data_collector.log,
watchdog.log, and outer_watchdog.log.

Usage:
    from scripts._log_setup import setup_logging
    log = setup_logging('core')        # → logs/YYYYMMDD/core.log
    log = setup_logging('supervisor')  # → logs/YYYYMMDD/supervisor.log
"""
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ETFormatter(logging.Formatter):
    """Force all log timestamps to ET regardless of system timezone."""

    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, tz=ET)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime('%Y-%m-%d %H:%M:%S') + f',{int(record.msecs):03d}'


def setup_logging(
    process_name: str,
    log_filename: str = None,
    extra_handlers: list = None,
) -> logging.Logger:
    """Configure logging with ET timestamps for a trading process.

    Parameters
    ----------
    process_name : str
        Process label shown in log lines (e.g., 'core', 'options').
    log_filename : str, optional
        Log file name. Defaults to '{process_name}.log'.
    extra_handlers : list, optional
        Additional handlers (e.g., StreamHandler for outer_watchdog).

    Returns
    -------
    logging.Logger
    """
    if log_filename is None:
        log_filename = f'{process_name}.log'

    log_dir = os.path.join(PROJECT_ROOT, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    date_dir = os.path.join(log_dir, datetime.now().strftime('%Y%m%d'))
    os.makedirs(date_dir, exist_ok=True)
    log_file = os.path.join(date_dir, log_filename)

    fmt = f'%(asctime)s %(levelname)s [{process_name}] %(message)s'
    formatter = ETFormatter(fmt)

    logging.root.handlers = []
    handlers = [logging.FileHandler(log_file)]
    if extra_handlers:
        handlers.extend(extra_handlers)

    for h in handlers:
        h.setFormatter(formatter)

    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    return logging.getLogger(process_name)
