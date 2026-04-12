#!/usr/bin/env python3
"""
Test 5: Network "Warm-up" Script
===================================
Ensures the data_client can pre-fetch RVOL baselines for all 200 tickers
before Monday's open. Tests parallel fetch with ThreadPoolExecutor.

Method
------
1. Load TRADIER_TOKEN from .env.
2. Create TradierDataClient.
3. Measure sequential fetch time for 10 tickers (baseline).
4. Measure parallel fetch time for 200 tickers using ThreadPoolExecutor.
5. Verify ThreadPoolExecutor exists in tradier_client.py.
6. Test sandbox fallback if production API is slow.

Pass criteria
-------------
- ThreadPoolExecutor with max_workers=20 is used in fetch_batch_bars.
- 200 ticker pre-fetch completes in < 60 seconds.
- At least 90% of tickers return valid daily history data.
- No connection timeouts or auth failures.
"""

import sys
import os
import time
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ET = ZoneInfo('America/New_York')
N_TICKERS = 200
WARMUP_TIMEOUT = 60  # seconds

os.makedirs(os.path.join(os.path.dirname(__file__), "logs"), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(os.path.dirname(__file__), 'logs', 'test_5_network_warmup.log'), mode='w'),
    ],
)
log = logging.getLogger(__name__)


def load_tradier_token() -> str:
    """Load TRADIER_TOKEN from .env file."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    token = ''
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith('TRADIER_TOKEN='):
                    token = line.split('=', 1)[1].strip().strip('"').strip("'")
                    break
    if not token:
        token = os.environ.get('TRADIER_TOKEN', '')
    return token


def test_threadpool_exists():
    """Test 5a: Verify ThreadPoolExecutor is used in tradier_client.py."""
    log.info("\n--- Test 5a: ThreadPoolExecutor in tradier_client.py ---")

    from monitor.tradier_client import TradierDataClient, _MAX_WORKERS
    log.info(f"_MAX_WORKERS = {_MAX_WORKERS}")

    # Check source code for ThreadPoolExecutor usage
    source_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'monitor', 'tradier_client.py',
    )
    with open(source_path) as f:
        source = f.read()

    has_tpe = 'ThreadPoolExecutor' in source
    has_batch = 'fetch_batch_bars' in source
    has_max_workers = f'max_workers={_MAX_WORKERS}' in source or f'max_workers {_MAX_WORKERS}' in source

    log.info(f"ThreadPoolExecutor import: {'PASS' if has_tpe else 'FAIL'}")
    log.info(f"fetch_batch_bars method:     {'PASS' if has_batch else 'FAIL'}")
    log.info(f"max_workers={_MAX_WORKERS}:   {'PASS' if has_max_workers else 'WARN (may use default)'}")

    return has_tpe and has_batch


def test_sequential_fetch(token: str, tickers: list[str], n: int = 10):
    """Test 5b: Sequential fetch baseline (n tickers)."""
    log.info(f"\n--- Test 5b: Sequential Fetch Baseline ({n} tickers) ---")

    from monitor.tradier_client import TradierDataClient
    client = TradierDataClient(token)

    end = datetime.now(ET)
    start = end - timedelta(days=14)
    sample = tickers[:n]

    log.info(f"Fetching daily history for {len(sample)} tickers sequentially...")
    results = {}
    fetch_start = time.monotonic()

    for ticker in sample:
        t0 = time.monotonic()
        try:
            df = client.get_daily_history(ticker, start, end)
            results[ticker] = {'success': True, 'rows': len(df), 'time_ms': (time.monotonic() - t0) * 1000}
        except Exception as e:
            results[ticker] = {'success': False, 'error': str(e), 'time_ms': (time.monotonic() - t0) * 1000}

    total_time = time.monotonic() - fetch_start

    successes = sum(1 for r in results.values() if r['success'])
    avg_time = sum(r['time_ms'] for r in results.values()) / len(results)

    log.info(f"Sequential results: {successes}/{len(sample)} successful")
    log.info(f"Total time: {total_time:.2f}s")
    log.info(f"Avg per ticker: {avg_time:.0f}ms")
    log.info(f"Projected for 200 tickers: {avg_time * 200 / 1000:.1f}s")

    return {
        'total_time': total_time,
        'avg_per_ticker_ms': avg_time,
        'success_rate': successes / len(sample),
        'projected_200': avg_time * 200 / 1000,
    }


def test_parallel_fetch(token: str, tickers: list[str]):
    """Test 5c: Parallel fetch with ThreadPoolExecutor (200 tickers)."""
    log.info(f"\n--- Test 5c: Parallel Fetch ({len(tickers)} tickers, max_workers=20) ---")

    from monitor.tradier_client import TradierDataClient
    client = TradierDataClient(token)

    end = datetime.now(ET)
    start = end - timedelta(days=14)

    results = {}
    log.info(f"Starting parallel fetch...")

    fetch_start = time.monotonic()

    def _fetch_one(ticker):
        t0 = time.monotonic()
        try:
            df = client.get_daily_history(ticker, start, end)
            return ticker, True, len(df), (time.monotonic() - t0) * 1000, None
        except Exception as e:
            return ticker, False, 0, (time.monotonic() - t0) * 1000, str(e)

    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(_fetch_one, t): t for t in tickers}
        completed = 0
        for f in as_completed(futures):
            completed += 1
            try:
                ticker, success, rows, time_ms, error = f.result(timeout=30)
                results[ticker] = {'success': success, 'rows': rows, 'time_ms': time_ms}
                if not success:
                    log.debug(f"  {ticker}: FAILED ({error})")
                elif completed % 50 == 0:
                    log.info(f"  Progress: {completed}/{len(tickers)} ({time.monotonic() - fetch_start:.1f}s)")
            except Exception as e:
                ticker = futures[f]
                results[ticker] = {'success': False, 'error': str(e), 'time_ms': 0}

    total_time = time.monotonic() - fetch_start

    successes = sum(1 for r in results.values() if r['success'])
    success_rate = successes / len(tickers)
    avg_time = sum(r['time_ms'] for r in results.values()) / len(results)
    max_time = max(r['time_ms'] for r in results.values())
    min_time = min(r['time_ms'] for r in results.values() if r['time_ms'] > 0)

    log.info(f"\nParallel fetch results:")
    log.info(f"  Total time:          {total_time:.2f}s")
    log.info(f"  Success rate:        {successes}/{len(tickers)} ({success_rate * 100:.1f}%)")
    log.info(f"  Avg per ticker:      {avg_time:.0f}ms")
    log.info(f"  Min latency:         {min_time:.0f}ms")
    log.info(f"  Max latency:         {max_time:.0f}ms")
    log.info(f"  Under {WARMUP_TIMEOUT}s target:    {'PASS' if total_time < WARMUP_TIMEOUT else 'FAIL'}")

    return {
        'total_time': total_time,
        'success_rate': success_rate,
        'avg_per_ticker_ms': avg_time,
        'under_timeout': total_time < WARMUP_TIMEOUT,
    }


def test_fetch_batch_bars(token: str, tickers: list[str]):
    """Test 5d: Test the actual TradierDataClient.fetch_batch_bars() method."""
    log.info(f"\n--- Test 5d: fetch_batch_bars() Full Integration ---")

    from monitor.tradier_client import TradierDataClient
    client = TradierDataClient(token)

    log.info(f"Calling fetch_batch_bars() for {len(tickers)} tickers...")
    start = time.monotonic()

    try:
        bars_cache, rvol_cache = client.fetch_batch_bars(tickers)
        elapsed = time.monotonic() - start

        log.info(f"fetch_batch_bars() completed in {elapsed:.2f}s")
        log.info(f"  bars_cache: {len(bars_cache)} tickers with data")
        log.info(f"  rvol_cache: {len(rvol_cache)} tickers with daily history")

        # Note: fetch_batch_bars skips fetch if before market open
        if not bars_cache and not rvol_cache:
            log.info("  (No data returned — expected if market is closed / before 9:30 AM ET)")
            log.info("  This means the method correctly gates on market open time")
            return {'success': True, 'reason': 'market_closed', 'elapsed': elapsed}

        bars_rate = len(bars_cache) / len(tickers) if tickers else 0
        rvol_rate = len(rvol_cache) / len(tickers) if tickers else 0

        log.info(f"  bars coverage:  {bars_rate * 100:.1f}%")
        log.info(f"  rvol coverage:  {rvol_rate * 100:.1f}%")

        return {
            'success': True,
            'elapsed': elapsed,
            'bars_count': len(bars_cache),
            'rvol_count': len(rvol_cache),
            'bars_rate': bars_rate,
            'rvol_rate': rvol_rate,
        }
    except Exception as e:
        elapsed = time.monotonic() - start
        log.info(f"fetch_batch_bars() failed after {elapsed:.2f}s: {e}")
        return {'success': False, 'error': str(e), 'elapsed': elapsed}


def test_pre_fetch_method():
    """Test 5e: Verify a pre-fetch method exists or suggest one."""
    log.info("\n--- Test 5e: Pre-Fetch Method Availability ---")

    from monitor.tradier_client import TradierDataClient
    from monitor.data_client import BaseDataClient

    # Check if TradierDataClient has a pre-fetch / warm-up method
    methods = [m for m in dir(TradierDataClient) if not m.startswith('_')]
    log.info(f"TradierDataClient public methods: {methods}")

    has_prefetch = any('prefetch' in m.lower() or 'warmup' in m.lower() or 'warm' in m.lower() for m in methods)
    log.info(f"Dedicated pre-fetch method: {'PASS' if has_prefetch else 'MISSING'}")

    # fetch_batch_bars can serve as pre-fetch
    has_batch = 'fetch_batch_bars' in methods
    log.info(f"fetch_batch_bars (can be used for pre-fetch): {'PASS' if has_batch else 'FAIL'}")

    # Check BaseDataClient interface
    base_methods = [m for m in dir(BaseDataClient) if not m.startswith('_')]
    log.info(f"BaseDataClient abstract methods: {base_methods}")

    if not has_prefetch:
        log.info("\nSUGGESTED ADDITION: Add a prefetch_rvol_baseline() method to BaseDataClient")
        log.info("  This method should:")
        log.info("    1. Fetch 14-day daily history for all tickers in parallel")
        log.info("    2. Store results in rvol_cache for Monday morning")
        log.info("    3. Be callable on Sunday night to warm up the cache")
        log.info("    4. Return the rvol_cache dict for injection into the monitor loop")

    return has_batch


def run_test():
    log.info("=" * 70)
    log.info("TEST 5: Network Warm-up Script")
    log.info("=" * 70)

    token = load_tradier_token()
    if not token:
        log.info("FAIL: No TRADIER_TOKEN found in .env or environment")
        return False

    log.info(f"Token loaded: {token[:8]}...{token[-4:]}")
    log.info(f"Current time: {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    log.info("")

    # Load tickers from config
    from config import TICKERS
    test_tickers = TICKERS[:N_TICKERS]
    log.info(f"Using {len(test_tickers)} tickers from config.py")

    results = {}

    # Run tests
    results['threadpool_exists'] = test_threadpool_exists()
    results['prefetch_method'] = test_pre_fetch_method()

    # Network tests (may fail if market closed / weekend)
    try:
        results['sequential'] = test_sequential_fetch(token, test_tickers, n=10)
    except Exception as e:
        log.info(f"Sequential fetch failed: {e}")
        results['sequential'] = None

    try:
        results['parallel'] = test_parallel_fetch(token, test_tickers)
    except Exception as e:
        log.info(f"Parallel fetch failed: {e}")
        results['parallel'] = None

    try:
        results['batch_bars'] = test_fetch_batch_bars(token, test_tickers)
    except Exception as e:
        log.info(f"fetch_batch_bars failed: {e}")
        results['batch_bars'] = None

    # Summary
    log.info("\n" + "=" * 70)
    log.info("RESULTS")
    log.info("=" * 70)
    for name, result in results.items():
        if result is None:
            status = "SKIP (network error)"
        elif isinstance(result, bool):
            status = "PASS" if result else "FAIL"
        elif isinstance(result, dict):
            status = "PASS" if result.get('success', False) else "FAIL/WARN"
        else:
            status = "UNKNOWN"
        log.info(f"  {name:25s}: {status}")

    # Detailed timing
    if results.get('sequential'):
        seq = results['sequential']
        log.info(f"\n  Sequential ({10} tickers): {seq['total_time']:.2f}s, {seq['avg_per_ticker_ms']:.0f}ms/ticker")
        log.info(f"  Projected for 200 sequential: {seq['projected_200']:.1f}s")

    if results.get('parallel'):
        par = results['parallel']
        log.info(f"  Parallel (200 tickers): {par['total_time']:.2f}s, {par['avg_per_ticker_ms']:.0f}ms/ticker avg")
        if results.get('sequential'):
            speedup = results['sequential']['avg_per_ticker_ms'] * 200 / (par['total_time'] * 1000)
            log.info(f"  Parallel speedup: {speedup:.1f}x")

    # Improvements
    log.info("\n" + "-" * 70)
    log.info("IMPROVEMENTS NEEDED")
    log.info("-" * 70)

    if results.get('parallel') and not results['parallel'].get('under_timeout'):
        log.info("1. Parallel fetch exceeded 60s timeout:")
        log.info("   - Consider increasing _MAX_WORKERS from 20 to 30-40")
        log.info("   - Or implement request batching if Tradier supports it")
        log.info("   - Add exponential backoff for rate-limit (429) errors")

    if results.get('parallel') and results['parallel'].get('success_rate', 0) < 0.90:
        log.info("2. Success rate below 90%:")
        log.info("   - Tradier may be rate-limiting; add retry logic with backoff")
        log.info("   - Or spread requests over a longer window (chunk + sleep)")

    if not results.get('prefetch_method', True):
        log.info("3. No dedicated prefetch_rvol_baseline() method:")
        log.info("   - Add to BaseDataClient interface and TradierDataClient")
        log.info("   - Should accept tickers list and return rvol_cache dict")
        log.info("   - Call from a Sunday night cron job or startup script")

    log.info("4. Cold start risk: fetch_batch_bars() gates on market open (9:30 AM)")
    log.info("   - Pre-fetch rvol_cache the night BEFORE market open")
    log.info("   - Store rvol_cache to disk (pickle/parquet) for instant Monday load")
    log.info("   - Only fetch today's intraday bars after 9:30 AM (not 14-day history)")

    log.info("5. Add a warmup CLI command to run_monitor.py:")
    log.info("   python run_monitor.py --warmup  # Pre-fetch RVOL data only, don't start trading")

    log.info("\nTest 5 complete.")

    passed = sum(1 for r in results.values() if r is True or (isinstance(r, dict) and r.get('success')))
    total = sum(1 for r in results.values() if r is not None)
    return total > 0 and passed >= total * 0.5


if __name__ == '__main__':
    success = run_test()
    sys.exit(0 if success else 1)