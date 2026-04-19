#!/usr/bin/env python
"""
backfill_bars.py — Fetch historical bars from Tradier and persist to DB.

One-time data pull for backtesting (Option C: Tradier → DB → future backtests).

Fetches:
  - 1-min bars: 20 trading days (~28 calendar days)
  - 5-min bars: 40 trading days (~56 calendar days)
  - Daily bars: 60 days (for RVOL baseline)

For ALL tickers in config.TICKERS (183 symbols).

Rate limit: Tradier allows 120 req/min. This script batches with pauses
to stay well under. Total time: ~8-12 minutes for all data.

Usage:
    python scripts/backfill_bars.py
    python scripts/backfill_bars.py --tickers AAPL NVDA TSLA
    python scripts/backfill_bars.py --interval 5m --days 40
    python scripts/backfill_bars.py --skip-db  # fetch only, don't save to DB
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TICKERS
from backtests.data_loader import BarDataLoader

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description='Backfill historical bars from Tradier → DB',
    )
    parser.add_argument(
        '--tickers', nargs='+', default=None,
        help='Tickers to fetch (default: all from config.TICKERS)',
    )
    parser.add_argument(
        '--interval', type=str, default='all',
        help="Interval: '1m', '5m', '1d', or 'all' (default: all)",
    )
    parser.add_argument(
        '--days', type=int, default=None,
        help='Override lookback days (default: 28 for 1m, 56 for 5m)',
    )
    parser.add_argument(
        '--skip-db', action='store_true',
        help='Fetch data but do not save to DB',
    )
    parser.add_argument(
        '--tradier-token', type=str, default=None,
        help='Tradier API token (default: TRADIER_TOKEN env var)',
    )
    parser.add_argument(
        '--db-dsn', type=str, default=None,
        help='PostgreSQL DSN (default: DATABASE_URL env var)',
    )
    args = parser.parse_args()

    tickers = args.tickers or TICKERS
    token = args.tradier_token or os.getenv('TRADIER_TOKEN', '')
    if not token:
        log.error("TRADIER_TOKEN not set. Pass --tradier-token or set env var.")
        return 1

    now = datetime.now(ET)
    loader = BarDataLoader(source='tradier', tradier_token=token, db_dsn=args.db_dsn)

    intervals_to_fetch = []
    if args.interval == 'all':
        intervals_to_fetch = [('1m', args.days or 28), ('5m', args.days or 56)]
    elif args.interval == '1m':
        intervals_to_fetch = [('1m', args.days or 28)]
    elif args.interval == '5m':
        intervals_to_fetch = [('5m', args.days or 56)]
    elif args.interval == '1d':
        intervals_to_fetch = [('1d', args.days or 90)]
    else:
        log.error("Invalid interval: %s. Use '1m', '5m', '1d', or 'all'", args.interval)
        return 1

    log.info("=" * 70)
    log.info("BACKFILL: %d tickers, intervals=%s",
             len(tickers), [f"{i}({d}d)" for i, d in intervals_to_fetch])
    log.info("=" * 70)

    total_rows = 0

    for interval, lookback_days in intervals_to_fetch:
        start = now - timedelta(days=lookback_days)
        end = now

        log.info("")
        log.info("─── Fetching %s bars (%d calendar days) ───", interval, lookback_days)

        if interval == '1d':
            data = loader.load_rvol_baseline(tickers, now, lookback_days=lookback_days)
        else:
            data = loader.load_intraday(tickers, start, end, interval=interval)

        if not data:
            log.warning("No data returned for %s", interval)
            continue

        # Summary
        total_bars = sum(len(df) for df in data.values())
        dates = set()
        for df in data.values():
            dates.update(df.index.date)

        log.info(
            "Fetched %s: %d tickers, %d bars, %d trading days",
            interval, len(data), total_bars, len(dates),
        )

        # Save to DB
        if not args.skip_db:
            log.info("Saving %s bars to DB...", interval)
            n = loader.save_to_db(data, interval)
            total_rows += n
            log.info("Saved %d rows to market_bars", n)
        else:
            log.info("Skipping DB save (--skip-db)")

    log.info("")
    log.info("=" * 70)
    log.info("BACKFILL COMPLETE: %d total rows saved to DB", total_rows)
    log.info("=" * 70)

    return 0


if __name__ == '__main__':
    sys.exit(main())
