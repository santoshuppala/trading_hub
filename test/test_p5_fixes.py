#!/usr/bin/env python3
"""
V7 P5 Fixes Test Suite — Operational Hardening.

Tests the 3 P5 fixes from the V7 architectural review:
  P5-1: Backward-compatible migration safety validator
  P5-2: Event payload versioning
  P5-3: Chaos/failure injection tests

Run: python test/test_p5_fixes.py
"""
import os
import sys
import tempfile
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_results = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    _results.append((name, condition))
    msg = f"  [{status}] {name}"
    if detail and not condition:
        msg += f" — {detail}"
    print(msg)


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ══════════════════════════════════════════════════════════════════════
# P5-1: Migration Safety Validator
# ══════════════════════════════════════════════════════════════════════

def test_p5_1_migration_safety():
    section("P5-1: Migration Safety Validator")
    from db.migrations.run import validate_migration

    # Dangerous: NOT NULL without DEFAULT
    w = validate_migration('001.sql',
        'ALTER TABLE t ADD COLUMN x INT NOT NULL;')
    check("P5-1a: NOT NULL without DEFAULT caught",
          len(w) >= 1 and 'NOT NULL' in w[0])

    # Safe: NOT NULL with DEFAULT
    w = validate_migration('002.sql',
        'ALTER TABLE t ADD COLUMN x INT NOT NULL DEFAULT 0;')
    check("P5-1b: NOT NULL with DEFAULT is safe", len(w) == 0)

    # Dangerous: DROP TABLE
    w = validate_migration('003.sql', 'DROP TABLE old_data;')
    check("P5-1c: DROP TABLE caught",
          len(w) >= 1 and 'DROP TABLE' in w[0])

    # Dangerous: DROP COLUMN
    w = validate_migration('004.sql',
        'ALTER TABLE t DROP COLUMN old_col;')
    check("P5-1d: DROP COLUMN caught",
          len(w) >= 1 and 'DROP COLUMN' in w[0])

    # Dangerous: RENAME COLUMN
    w = validate_migration('005.sql',
        'ALTER TABLE t RENAME COLUMN old TO new;')
    check("P5-1e: RENAME COLUMN caught",
          len(w) >= 1 and 'RENAME' in w[0])

    # Override: acknowledged with V7_OVERRIDE comment
    w = validate_migration('006.sql',
        '-- V7_OVERRIDE:DROP TABLE\nDROP TABLE legacy_data;')
    check("P5-1f: V7_OVERRIDE bypasses check", len(w) == 0)

    w = validate_migration('007.sql',
        '-- V7_OVERRIDE:DROP COLUMN\nALTER TABLE t DROP COLUMN old;')
    check("P5-1g: V7_OVERRIDE:DROP COLUMN bypasses", len(w) == 0)

    # Safe: normal CREATE TABLE
    w = validate_migration('008.sql',
        'CREATE TABLE IF NOT EXISTS trading.new_table (id UUID PRIMARY KEY);')
    check("P5-1h: CREATE TABLE is safe", len(w) == 0)

    # Safe: ADD COLUMN with DEFAULT (nullable)
    w = validate_migration('009.sql',
        'ALTER TABLE t ADD COLUMN payload_version INT DEFAULT 1;')
    check("P5-1i: ADD COLUMN with DEFAULT is safe", len(w) == 0)

    # Safe: CREATE INDEX
    w = validate_migration('010.sql',
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_foo ON t(col);')
    check("P5-1j: CREATE INDEX is safe", len(w) == 0)

    # Multiple dangers in one file
    w = validate_migration('011.sql',
        'DROP TABLE a;\nALTER TABLE b DROP COLUMN c;')
    check("P5-1k: Multiple dangers caught",
          len(w) >= 2)


# ══════════════════════════════════════════════════════════════════════
# P5-2: Event Payload Versioning
# ══════════════════════════════════════════════════════════════════════

def test_p5_2_payload_versioning():
    section("P5-2: Event Payload Versioning")
    from monitor.event_bus import Event, EventType
    from monitor.events import HeartbeatPayload

    # Default version = 1
    e1 = Event(type=EventType.HEARTBEAT, payload=None)
    check("P5-2a: Default payload_version is 1",
          e1.payload_version == 1)

    # Custom version
    e2 = Event(type=EventType.HEARTBEAT, payload=None, payload_version=2)
    check("P5-2b: Custom payload_version preserved",
          e2.payload_version == 2)

    # Version survives through EventBus emit
    from monitor.event_bus import EventBus
    bus = EventBus()
    received = []

    def handler(event):
        received.append(event.payload_version)

    bus.subscribe(EventType.HEARTBEAT, handler)
    bus.emit(Event(
        type=EventType.HEARTBEAT,
        payload=HeartbeatPayload(
            n_tickers=1, n_positions=0, open_tickers=(),
            n_trades=0, n_wins=0, total_pnl=0.0),
        payload_version=3,
    ))
    check("P5-2c: payload_version survives EventBus dispatch",
          received == [3])

    # Verify field exists in Event.__repr__
    e3 = Event(type=EventType.HEARTBEAT, payload=None, payload_version=5)
    check("P5-2d: payload_version field accessible",
          hasattr(e3, 'payload_version') and e3.payload_version == 5)

    # Verify source code has payload_version
    import monitor.event_bus as eb
    src = open(eb.__file__).read()
    check("P5-2e: payload_version in Event dataclass",
          'payload_version' in src and 'schema evolution' in src.lower())


# ══════════════════════════════════════════════════════════════════════
# P5-3: Chaos Tests — Failure Injection
# ══════════════════════════════════════════════════════════════════════

def test_p5_3a_state_deletion_recovery():
    """Simulate: ALL state files deleted during operation."""
    section("P5-3a: State File Deletion Recovery")
    from lifecycle.safe_state import SafeStateFile

    with tempfile.TemporaryDirectory() as td:
        sf = SafeStateFile(os.path.join(td, 'state.json'), max_age_seconds=60)

        # Write valid state
        sf.write({'positions': {'AAPL': 100}, 'daily_pnl': -50.0})
        sf.write({'positions': {'AAPL': 100, 'MSFT': 50}})

        # Verify state exists
        data, _ = sf.read()
        check("P5-3a-1: State written successfully", data is not None)

        # DELETE ALL FILES (simulate disk failure / ops error)
        for f in os.listdir(td):
            os.remove(os.path.join(td, f))

        # Read should return None gracefully (no crash)
        data2, fresh = sf.read()
        check("P5-3a-2: Read returns None after deletion (no crash)",
              data2 is None)
        check("P5-3a-3: Not fresh after deletion", not fresh)

        # System can recover by writing fresh state
        ok = sf.write({'positions': {}, 'recovered': True})
        check("P5-3a-4: Can write fresh state after deletion", ok)

        data3, _ = sf.read()
        check("P5-3a-5: Recovered state readable",
              data3 is not None and data3.get('recovered') is True)


def test_p5_3b_corrupt_state_recovery():
    """Simulate: State file corrupted (partial write crash)."""
    section("P5-3b: Corrupt State File Recovery")
    from lifecycle.safe_state import SafeStateFile

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, 'state.json')
        sf = SafeStateFile(path, max_age_seconds=60)

        # Write 3 versions (creates primary + .prev + .prev2)
        sf.write({'v': 1, 'data': 'first'})
        sf.write({'v': 2, 'data': 'second'})
        sf.write({'v': 3, 'data': 'third'})

        # Corrupt primary (simulate crash mid-write)
        with open(path, 'w') as f:
            f.write('{"truncated":')  # incomplete JSON
        with open(path + '.sha256', 'w') as f:
            f.write('bad_checksum')

        # Read should fallback to .prev
        data, _ = sf.read()
        check("P5-3b-1: Falls back to .prev on corruption",
              data is not None)
        check("P5-3b-2: Fallback has previous version data",
              data.get('data') == 'second' if data else False)

        # Corrupt .prev too
        prev = path + '.prev'
        with open(prev, 'w') as f:
            f.write('CORRUPT')
        with open(prev + '.sha256', 'w') as f:
            f.write('wrong')

        # Should fallback to .prev2
        data2, _ = sf.read()
        check("P5-3b-3: Falls back to .prev2 on double corruption",
              data2 is not None)
        check("P5-3b-4: .prev2 has oldest version data",
              data2.get('data') == 'first' if data2 else False)


def test_p5_3c_broker_timeout_handling():
    """Simulate: Broker API hangs during order execution."""
    section("P5-3c: Broker API Timeout Handling")
    from monitor.event_bus import EventBus, EventType, Event, HANDLER_TIMEOUT_SEC
    from monitor.events import OrderRequestPayload, Side
    import monitor.event_bus as eb

    bus = EventBus()
    results = {'timeout_caught': False}

    def hanging_broker(event):
        """Simulates a broker that hangs forever."""
        time.sleep(100)  # way longer than timeout

    # Set short timeout for test
    original = eb.HANDLER_TIMEOUT_SEC
    eb.HANDLER_TIMEOUT_SEC = 0.5

    bus.subscribe(EventType.ORDER_REQ, hanging_broker)

    # Emit ORDER_REQ — should timeout, not hang
    start = time.monotonic()
    bus.emit(Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='TEST', side=Side.BUY, qty=1, price=100.0,
            reason='chaos_test'),
    ))
    elapsed = time.monotonic() - start

    eb.HANDLER_TIMEOUT_SEC = original

    check("P5-3c-1: Handler timed out (didn't hang forever)",
          elapsed < 5.0,
          f"elapsed={elapsed:.1f}s")
    check("P5-3c-2: Completed within 2x timeout",
          elapsed < 1.5)


def test_p5_3d_concurrent_registry_access():
    """Simulate: Multiple threads accessing registry simultaneously."""
    section("P5-3d: Concurrent Registry Access")
    from monitor.distributed_registry import DistributedPositionRegistry

    with tempfile.TemporaryDirectory() as td:
        reg = DistributedPositionRegistry(
            global_max=50,
            registry_path=os.path.join(td, 'reg.json'))

        errors = []
        acquired = {'count': 0}
        lock = threading.Lock()

        def worker(layer, n_tickers):
            for i in range(n_tickers):
                ticker = f"{layer}_{i}"
                try:
                    if reg.try_acquire(ticker, layer):
                        with lock:
                            acquired['count'] += 1
                except Exception as exc:
                    errors.append(f"{layer}_{i}: {exc}")

        # 4 threads, each trying to acquire 10 tickers
        threads = []
        for layer in ['core', 'pro', 'pop', 'options']:
            t = threading.Thread(target=worker, args=(layer, 10))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        check("P5-3d-1: No errors during concurrent access",
              len(errors) == 0,
              f"{len(errors)} errors: {errors[:3]}")
        check("P5-3d-2: All 40 tickers acquired",
              acquired['count'] == 40,
              f"acquired={acquired['count']}")
        check("P5-3d-3: Registry count matches",
              reg.count() == 40,
              f"count={reg.count()}")

        # Verify no duplicates
        all_pos = reg.all_positions()
        check("P5-3d-4: No duplicate tickers",
              len(all_pos) == 40)


def test_p5_3e_stale_cache_trading_prevention():
    """Simulate: Core crashes, cache goes stale, satellites stop trading."""
    section("P5-3e: Stale Cache Trading Prevention")
    import pandas as pd
    from monitor.shared_cache import CacheWriter, CacheReader

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, 'cache.pkl')
        writer = CacheWriter(cache_path=path)
        reader = CacheReader(cache_path=path, max_age_seconds=0.5)

        # Core writes bars
        bars = {'AAPL': pd.DataFrame({'close': [150.0], 'volume': [1000]})}
        writer.write(bars, {})

        # Satellite reads fresh data — should get bars
        b1, _ = reader.get_bars()
        check("P5-3e-1: Fresh cache returns bars", 'AAPL' in b1)

        # Simulate: Core crashes (stops writing)
        time.sleep(0.7)

        # Satellite reads stale data — should get EMPTY (fail-safe)
        b2, _ = reader.get_bars()
        check("P5-3e-2: Stale cache returns EMPTY (prevents stale trading)",
              len(b2) == 0)

        # Core recovers and writes again
        writer.write(bars, {})
        b3, _ = reader.get_bars()
        check("P5-3e-3: Recovered cache returns bars",
              'AAPL' in b3)


def test_p5_3f_fill_dedup_on_replay():
    """Simulate: Same FILL event replayed after crash (Redpanda replay)."""
    section("P5-3f: FILL Dedup on Replay")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import FillPayload

    bus = EventBus()
    fills_processed = []

    def on_fill(event):
        fills_processed.append(event.event_id)

    bus.subscribe(EventType.FILL, on_fill)

    # Emit same FILL 3 times (simulating Redpanda replay)
    fill_eid = 'replay-test-fill-id-12345'
    for _ in range(3):
        bus.emit(Event(
            type=EventType.FILL,
            payload=FillPayload(
                ticker='AAPL', side='BUY', qty=10,
                fill_price=150.0, order_id='order123',
                reason='test'),
            event_id=fill_eid,
        ))

    # EventBus idempotency window should dedup
    check("P5-3f-1: Only 1 FILL processed (dedup caught replays)",
          fills_processed.count(fill_eid) == 1,
          f"count={fills_processed.count(fill_eid)}")


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("  V7 P5 Fixes — Operational Hardening Test Suite")
    print("  Testing: migration safety, payload versioning, chaos tests")
    print("=" * 60)

    test_p5_1_migration_safety()
    test_p5_2_payload_versioning()
    test_p5_3a_state_deletion_recovery()
    test_p5_3b_corrupt_state_recovery()
    test_p5_3c_broker_timeout_handling()
    test_p5_3d_concurrent_registry_access()
    test_p5_3e_stale_cache_trading_prevention()
    test_p5_3f_fill_dedup_on_replay()

    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in _results if ok)
    failed = sum(1 for _, ok in _results if not ok)
    total = len(_results)
    print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
    if failed:
        print("\n  FAILURES:")
        for name, ok in _results:
            if not ok:
                print(f"    - {name}")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)
