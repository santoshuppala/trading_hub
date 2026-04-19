#!/usr/bin/env python
"""
run_backtest.py — CLI for running backtests.

Usage:
    # Pro strategies on 5 tickers (yfinance, default)
    python backtests/run_backtest.py --engine pro --tickers AAPL NVDA TSLA --start 2025-03-01 --end 2025-03-31

    # All engines on all tickers (Tradier data)
    python backtests/run_backtest.py --engine all --tickers all --data tradier --budget 50000

    # Tradier fetch + save to DB (Option C)
    python backtests/run_backtest.py --engine pro --tickers all --data tradier --save-to-db

    # Load from DB (fast — no API calls)
    python backtests/run_backtest.py --engine pro --tickers all --data db --start 2026-03-01 --end 2026-04-18

    # Backfill only (no backtest run)
    python scripts/backfill_bars.py
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtests.engine import BacktestEngine
from backtests.reporters.console import ConsoleReporter

try:
    from backtests.reporters.csv_reporter import CsvReporter
    HAS_CSV = True
except ImportError:
    HAS_CSV = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description='Run backtests for Pro/Pop/Options strategies.',
    )
    parser.add_argument(
        '--engine', type=str, default='pro',
        help="Engines: 'pro', 'pop', 'options', or 'all' (default: pro)",
    )
    parser.add_argument(
        '--tickers', nargs='+', required=True,
        help="Tickers to backtest. Use 'all' for full universe (183 tickers)",
    )
    parser.add_argument(
        '--start', type=str, default=None,
        help='Start date YYYY-MM-DD (default: 56 days ago for tradier, 30 days for others)',
    )
    parser.add_argument(
        '--end', type=str, default=None,
        help='End date YYYY-MM-DD (default: today)',
    )
    parser.add_argument(
        '--budget', type=float, default=1000.0,
        help='Per-trade budget (default: 1000.0)',
    )
    parser.add_argument(
        '--data', type=str, default='yfinance',
        help="Data source: 'yfinance', 'tradier', 'db' (default: yfinance)",
    )
    parser.add_argument(
        '--save-to-db', action='store_true',
        help='Persist fetched bars to market_bars table (Option C)',
    )
    parser.add_argument(
        '--csv', action='store_true',
        help='Export results to CSV files',
    )
    parser.add_argument(
        '--no-risk-sizing', action='store_true',
        help='Disable beta/correlation risk sizing',
    )

    args = parser.parse_args()

    # Resolve tickers
    if args.tickers == ['all']:
        from config import TICKERS
        tickers = TICKERS
        log.info("Using full ticker universe: %d tickers", len(tickers))
    else:
        tickers = args.tickers

    # Parse engines
    if args.engine == 'all':
        engines = ['pro', 'pop', 'options']
    else:
        engines = [e.strip() for e in args.engine.split(',')]

    # Default date range based on data source
    if args.end:
        end_date = args.end
    else:
        end_date = datetime.now().strftime('%Y-%m-%d')

    if args.start:
        start_date = args.start
    else:
        # Default lookback: 56 days for tradier (40 trading days), 30 for others
        lookback = 56 if args.data == 'tradier' else 30
        start_dt = datetime.now() - timedelta(days=lookback)
        start_date = start_dt.strftime('%Y-%m-%d')

    log.info("Starting backtest: tickers=%d, engines=%s, %s → %s, source=%s",
             len(tickers), engines, start_date, end_date, args.data)

    try:
        engine = BacktestEngine(
            tickers=tickers,
            start_date=start_date,
            end_date=end_date,
            engines=engines,
            data_source=args.data,
            trade_budget=args.budget,
            use_risk_sizing=not args.no_risk_sizing,
            save_to_db=args.save_to_db,
        )

        result = engine.run()

        # Print results
        ConsoleReporter.report(result)

        # CSV export
        if args.csv and HAS_CSV:
            CsvReporter.export(result)
            log.info("CSV results exported")

        return 0

    except Exception as e:
        log.error("Backtest failed: %s", e, exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
