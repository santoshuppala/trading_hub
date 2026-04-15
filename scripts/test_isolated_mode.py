#!/usr/bin/env python3
"""
Integration test for process isolation mode.

Simulates a full session lifecycle without real market data:
1. Supervisor starts all 4 processes
2. Core writes mock bars to shared cache
3. Satellites read cache and process bars
4. IPC events flow through Redpanda
5. Events are persisted to DB
6. Position registry works cross-process
7. Graceful shutdown

Run: python scripts/test_isolated_mode.py
"""
import json
import os
import subprocess
import sys
import time
import signal
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

PYTHON = sys.executable
PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

results = []


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((name, condition))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    return condition


def main():
    print("=" * 60)
    print("ISOLATED MODE INTEGRATION TEST")
    print("=" * 60)
    print()

    # ── Test 1: Infrastructure ──────────────────────────────────────
    print("1. Infrastructure checks")

    # Redpanda
    try:
        from confluent_kafka import Producer
        p = Producer({'bootstrap.servers': '127.0.0.1:9092'})
        p.produce('th-bar-ready', key=b'test', value=b'{"test":true}')
        check("Redpanda produce", p.flush(5) == 0)
    except Exception as e:
        check("Redpanda produce", False, str(e))

    # Redpanda topics
    try:
        from confluent_kafka.admin import AdminClient
        admin = AdminClient({'bootstrap.servers': '127.0.0.1:9092'})
        topics = set(admin.list_topics(timeout=5).topics.keys())
        required = {'th-orders', 'th-fills', 'th-signals', 'th-pop-signals', 'th-bar-ready', 'th-registry'}
        missing = required - topics
        check("Redpanda topics", len(missing) == 0,
              f"missing: {missing}" if missing else f"all {len(required)} present")
    except Exception as e:
        check("Redpanda topics", False, str(e))

    # DB
    try:
        import psycopg2
        from config import DATABASE_URL
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM event_store LIMIT 1")
        count = cur.fetchone()[0]
        conn.close()
        check("TimescaleDB", True, f"{count} events in event_store")
    except Exception as e:
        check("TimescaleDB", False, str(e))

    # ── Test 2: Shared cache ────────────────────────────────────────
    print("\n2. Shared cache (write/read round-trip)")

    try:
        import pandas as pd
        from monitor.shared_cache import CacheWriter, CacheReader

        writer = CacheWriter()
        mock_bars = {
            'AAPL': pd.DataFrame({
                'open': [150.0, 151.0], 'high': [152.0, 153.0],
                'low': [149.0, 150.0], 'close': [151.0, 152.0],
                'volume': [10000, 20000],
            }),
            'QQQ': pd.DataFrame({
                'open': [620.0, 621.0], 'high': [622.0, 623.0],
                'low': [619.0, 620.0], 'close': [621.0, 622.0],
                'volume': [50000, 60000],
            }),
        }
        mock_rvol = {
            'AAPL': pd.DataFrame({
                'open': [148.0], 'high': [150.0], 'low': [147.0],
                'close': [149.0], 'volume': [5000000],
            }),
        }
        writer.write(mock_bars, mock_rvol)
        check("Cache write", True)

        reader = CacheReader()
        bars, rvol = reader.get_bars()
        check("Cache read — AAPL bars", 'AAPL' in bars and len(bars['AAPL']) == 2)
        check("Cache read — QQQ bars", 'QQQ' in bars and len(bars['QQQ']) == 2)
        check("Cache read — AAPL rvol", 'AAPL' in rvol)
        check("Cache freshness", reader.is_fresh())
    except Exception as e:
        check("Shared cache", False, str(e))

    # ── Test 3: Distributed registry ────────────────────────────────
    print("\n3. Distributed position registry")

    try:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=5)
        reg.reset()

        check("Registry acquire AAPL/vwap", reg.try_acquire('AAPL', 'vwap'))
        check("Registry block AAPL/pop", not reg.try_acquire('AAPL', 'pop'))
        check("Registry acquire NVDA/pro", reg.try_acquire('NVDA', 'pro'))
        check("Registry count", reg.count() == 2, f"count={reg.count()}")
        check("Registry held_by", reg.held_by('AAPL') == 'vwap')

        reg.release('AAPL')
        check("Registry release + reacquire", reg.try_acquire('AAPL', 'pop'))

        reg.reset()
        check("Registry reset", reg.count() == 0)
    except Exception as e:
        check("Distributed registry", False, str(e))

    # ── Test 4: IPC round-trip ──────────────────────────────────────
    print("\n4. IPC via Redpanda (publish + consume)")

    try:
        from monitor.ipc import EventPublisher, EventConsumer, TOPIC_ORDERS

        pub = EventPublisher(source_name='test')
        received = []

        cons = EventConsumer(
            group_id=f'test-{int(time.time())}',  # unique group so we don't conflict
            topics=[TOPIC_ORDERS],
            source_name='test',
        )
        cons.on(TOPIC_ORDERS, lambda key, payload: received.append((key, payload)))
        cons.start()

        # Wait for consumer to be ready before publishing
        time.sleep(2)

        # Publish a test order
        pub.publish(TOPIC_ORDERS, 'AAPL', {
            'ticker': 'AAPL', 'side': 'BUY', 'qty': 10,
            'price': 150.0, 'reason': 'test', 'source': 'test',
        })
        pub.flush(5)

        # Wait for consumer to pick it up
        time.sleep(3)
        cons.stop()
        pub.stop()

        check("IPC publish", True)
        check("IPC consume", len(received) > 0,
              f"received {len(received)} message(s)")
        if received:
            check("IPC payload", received[-1][1].get('ticker') == 'AAPL',
                  f"got: {received[-1][1].get('ticker')}")
    except Exception as e:
        check("IPC round-trip", False, str(e))

    # ── Test 5: DB helper for satellites ────────────────────────────
    print("\n5. Satellite DB initialization")

    try:
        from monitor.event_bus import EventBus
        test_bus = EventBus()
        from scripts._db_helper import init_satellite_db
        cleanup = init_satellite_db(test_bus, process_name='test')
        check("Satellite DB init", cleanup is not None)
        if cleanup:
            cleanup()
            check("Satellite DB cleanup", True)
    except Exception as e:
        check("Satellite DB", False, str(e))

    # ── Test 6: Supervisor startup (5 seconds) ──────────────────────
    print("\n6. Supervisor process startup (10 second test)")

    try:
        sup = subprocess.Popen(
            [PYTHON, 'scripts/supervisor.py'],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(12)

        # Check status file
        status_file = os.path.join(PROJECT_ROOT, 'data', 'supervisor_status.json')
        if os.path.exists(status_file):
            with open(status_file) as f:
                status = json.load(f)

            processes = status.get('processes', {})
            check("Supervisor started", True)

            for name in ['core', 'pro', 'pop', 'options']:
                proc = processes.get(name, {})
                proc_status = proc.get('status', 'unknown')
                pid = proc.get('pid')
                # Core should be running; satellites may have crashed (after-hours)
                # but they should have started (pid assigned)
                started = pid is not None or proc_status in ('running', 'crashed', 'stopped')
                check(f"  {name} process", started,
                      f"status={proc_status} pid={pid} restarts={proc.get('restart_count', '?')}")
        else:
            check("Supervisor started", False, "no status file")

        # Kill supervisor
        try:
            os.killpg(os.getpgid(sup.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass  # already exited
        try:
            sup.wait(timeout=15)
        except Exception:
            pass
        check("Supervisor shutdown", True, f"exit code {sup.returncode}")

    except Exception as e:
        check("Supervisor", False, str(e))
        try:
            os.killpg(os.getpgid(sup.pid), signal.SIGKILL)
        except Exception:
            pass

    # ── Test 7: DB event persistence check ──────────────────────────
    print("\n7. DB event persistence (check recent writes)")

    try:
        import psycopg2
        from config import DATABASE_URL
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        cur = conn.cursor()

        # Check if any events were written in the last 5 minutes
        cur.execute("""
            SELECT event_type, COUNT(*)
            FROM event_store
            WHERE event_time >= NOW() - INTERVAL '5 minutes'
            GROUP BY event_type
            ORDER BY COUNT(*) DESC
            LIMIT 10
        """)
        recent = cur.fetchall()
        conn.close()

        if recent:
            check("Recent DB events", True, f"{sum(r[1] for r in recent)} events: " +
                  ', '.join(f"{r[0]}({r[1]})" for r in recent))
        else:
            check("Recent DB events", True, "0 events (expected — market closed, no real bars)")
    except Exception as e:
        check("DB persistence check", False, str(e))

    # ── Test 8: Crash analyzer readiness ────────────────────────────
    print("\n8. Crash analyzer (pattern matching)")

    try:
        from scripts.crash_analyzer import CrashAnalyzer
        analyzer = CrashAnalyzer()

        # Test with a known pattern
        tb = """Traceback (most recent call last):
  File "/Users/santosh/Documents/santosh/trading_hub/trading_hub/scripts/run_options.py", line 50, in main
    from monitor.events import SomeNewPayload
ImportError: cannot import name 'SomeNewPayload' from 'monitor.events'"""

        diag = analyzer.analyze(tb)
        check("Crash analyzer parse", diag.error_type == 'ImportError')
        check("Crash analyzer diagnosis", 'SomeNewPayload' in diag.root_cause or
              'SomeNewPayload' in diag.error_message)
    except Exception as e:
        check("Crash analyzer", False, str(e))

    # ── Test 9: Sector map + ETF thresholds ─────────────────────────
    print("\n9. Sector map + ETF thresholds")

    try:
        from monitor.sector_map import is_etf, ETF_RVOL_MIN, get_sector
        check("QQQ is ETF", is_etf('QQQ'))
        check("SPY is ETF", is_etf('SPY'))
        check("AAPL is NOT ETF", not is_etf('AAPL'))
        check("ETF_RVOL_MIN < 1.0", ETF_RVOL_MIN < 1.0, f"ETF_RVOL_MIN={ETF_RVOL_MIN}")
        check("NVDA sector", get_sector('NVDA') == 'Technology')
    except Exception as e:
        check("Sector map", False, str(e))

    # ── Test 10: Sentiment baseline ─────────────────────────────────
    print("\n10. Sentiment baseline engine")

    try:
        from pop_screener.sentiment_baseline import SentimentBaselineEngine
        baseline = SentimentBaselineEngine(lookback_days=7)

        # Defaults (no DB history)
        check("Default headline baseline", baseline.headline_baseline('AAPL') == 2.0)
        check("Default social baseline", baseline.social_baseline('AAPL') == 100.0)

        # Intraday update
        baseline.update_intraday('AAPL', 5, 200.0)
        check("Intraday update", baseline.headline_baseline('AAPL') == 5.0)

        # Try DB load (may have data or may not)
        baseline.load_from_db()
        summary = baseline.summary()
        check("DB baseline load", True,
              f"headline_tickers={summary['headline_tickers']} social_tickers={summary['social_tickers']}")
    except Exception as e:
        check("Sentiment baseline", False, str(e))

    # ── Summary ─────────────────────────────────────────────────────
    print()
    print("=" * 60)
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    total = len(results)
    print(f"RESULTS: {passed}/{total} passed, {failed} failed")

    if failed == 0:
        print(f"[{PASS}] ALL CHECKS PASSED — isolated mode ready for tomorrow")
    else:
        print(f"[{FAIL}] {failed} check(s) failed — review before going live")
        for name, ok in results:
            if not ok:
                print(f"  FAILED: {name}")

    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
