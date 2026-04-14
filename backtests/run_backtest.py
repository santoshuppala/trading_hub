#!/usr/bin/env python
"""
run_backtest.py — CLI for running backtests.

Usage:
    python backtests/run_backtest.py --engine pro --tickers AAPL NVDA --start 2025-03-01 --end 2025-03-31
    python backtests/run_backtest.py --engine all --tickers AAPL NVDA TSLA --start 2025-03-01 --end 2025-06-01
"""
import argparse
import logging
import sys
from datetime import datetime

from backtests.engine import BacktestEngine
from backtests.reporters.console import ConsoleReporter

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
)
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description='Run backtests for Pop/Pro/Options strategies.')
    parser.add_argument(
        '--engine',
        type=str,
        default='pro',
        help="Engines to run: 'pro', 'pop', 'options', or 'all' (default: pro)"
    )
    parser.add_argument(
        '--tickers',
        nargs='+',
        required=True,
        help='Tickers to backtest (e.g., AAPL NVDA TSLA)'
    )
    parser.add_argument(
        '--start',
        type=str,
        required=True,
        help='Start date (YYYY-MM-DD)'
    )
    parser.add_argument(
        '--end',
        type=str,
        required=True,
        help='End date (YYYY-MM-DD)'
    )
    parser.add_argument(
        '--budget',
        type=float,
        default=1000.0,
        help='Starting capital (default: 1000.0)'
    )
    parser.add_argument(
        '--data',
        type=str,
        default='yfinance',
        help="Data source: 'yfinance', 'tradier', 'alpaca' (default: yfinance)"
    )

    args = parser.parse_args()

    # Parse engines
    if args.engine == 'all':
        engines = ['pro', 'pop', 'options']
    else:
        engines = [e.strip() for e in args.engine.split(',')]

    log.info(f"Starting backtest: tickers={args.tickers}, engines={engines}")

    try:
        # Create and run engine
        engine = BacktestEngine(
            tickers=args.tickers,
            start_date=args.start,
            end_date=args.end,
            engines=engines,
            data_source=args.data,
            trade_budget=args.budget,
        )

        result = engine.run()

        # Print results
        ConsoleReporter.report(result)

        return 0

    except Exception as e:
        log.error(f"Backtest failed: {e}", exc_info=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
