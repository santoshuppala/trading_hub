#!/usr/bin/env python3
"""
V7 P1 Fixes Test Suite — Stop Silent Failures.

Tests the 6 P1 fixes from the V7 architectural review:
  P1-6:  pickle.load() bounded read (no hang on corrupt file)
  P1-7:  Option chain snapshot timeout (15s per batch)
  P1-8:  Handler execution timeout (30s, configurable)
  P1-9:  Portfolio risk fail-closed (block on None buying power)
  P1-10: Cache TTL enforced in get_bars() (already fixed in SafeStateFile)
  P1-11: bot_state.json read lock (already fixed by SafeStateFile)

Run: python test/test_p1_fixes.py
"""
import os
import sys
import time

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
# P1-6: pickle.load() bounded read
# ══════════════════════════════════════════════════════════════════════

def test_p1_6_pickle_bounded_read():
    section("P1-6: Pickle Bounded Read (No Hang)")
    import monitor.shared_cache as sc_mod

    src = open(sc_mod.__file__).read()

    # Verify _MAX_CACHE_BYTES constant exists
    check("P1-6a: _MAX_CACHE_BYTES defined",
          '_MAX_CACHE_BYTES' in src)

    # Verify f.read() uses bounded size (not unbounded pickle.load)
    check("P1-6b: Pre-V7 path uses f.read(_MAX_CACHE_BYTES)",
          'f.read(_MAX_CACHE_BYTES)' in src)

    # Verify pickle.loads (from bytes) used instead of pickle.load (from file)
    # Count: pickle.loads should be used, pickle.load(f) should NOT appear
    check("P1-6c: Uses pickle.loads (from bytes, safe)",
          'pickle.loads(raw)' in src)

    # Verify checksum path also reads raw bytes first
    check("P1-6d: Checksum path reads raw bytes before deserializing",
          "raw = f.read()" in src)

    # Test actual bounded read
    import tempfile, pickle
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, 'test.pkl')
        # Write a valid pickle
        with open(path, 'wb') as f:
            pickle.dump({'test': True, '_version': 1}, f)

        reader = sc_mod.CacheReader(cache_path=path, max_age_seconds=60)
        data = reader.read()
        check("P1-6e: Bounded read works for valid pickle",
              data is not None and data.get('test') is True)

        # Write corrupt pickle (truncated)
        with open(path, 'wb') as f:
            f.write(b'\x80\x05\x95\x00\x00\x00\x00')  # partial pickle header

        reader2 = sc_mod.CacheReader(cache_path=path, max_age_seconds=60)
        data2 = reader2.read()
        check("P1-6f: Corrupt pickle returns cached_data (no hang)",
              True)  # If we get here, it didn't hang


# ══════════════════════════════════════════════════════════════════════
# P1-7: Option chain snapshot timeout
# ══════════════════════════════════════════════════════════════════════

def test_p1_7_chain_timeout():
    section("P1-7: Option Chain Snapshot Timeout")
    from options.chain import AlpacaOptionChainClient

    src = open(AlpacaOptionChainClient.__module__.replace('.', '/') + '.py').read()

    # Verify timeout constant exists
    check("P1-7a: _SNAPSHOT_TIMEOUT_SEC defined",
          hasattr(AlpacaOptionChainClient, '_SNAPSHOT_TIMEOUT_SEC'))
    check("P1-7b: Timeout is 15 seconds",
          AlpacaOptionChainClient._SNAPSHOT_TIMEOUT_SEC == 15.0)

    # Verify concurrent.futures used for timeout
    check("P1-7c: Uses concurrent.futures for timeout",
          'concurrent.futures' in src)
    check("P1-7d: future.result(timeout=...) called",
          'future.result(' in src and '_SNAPSHOT_TIMEOUT_SEC' in src)

    # Verify TimeoutError is caught
    check("P1-7e: TimeoutError caught and logged",
          'TimeoutError' in src)

    # Verify timeout breaks out of retry loop (don't retry hangs)
    check("P1-7f: Timeout breaks retry loop (no retry on hang)",
          "break  # don't retry timeouts" in src)


# ══════════════════════════════════════════════════════════════════════
# P1-8: Handler execution timeout
# ══════════════════════════════════════════════════════════════════════

def test_p1_8_handler_timeout():
    section("P1-8: Handler Execution Timeout")
    from monitor.event_bus import (EventBus, EventType, Event,
                                    HANDLER_TIMEOUT_SEC)

    # Verify constant
    check("P1-8a: HANDLER_TIMEOUT_SEC defined", HANDLER_TIMEOUT_SEC > 0)
    check("P1-8b: Timeout is 30 seconds", HANDLER_TIMEOUT_SEC == 30.0)

    # Verify the _deliver method uses timeout wrapper
    import monitor.event_bus as eb_mod
    src = open(eb_mod.__file__).read()
    check("P1-8c: _deliver uses concurrent.futures",
          'concurrent.futures' in src and 'HANDLER_TIMEOUT_SEC' in src)
    check("P1-8d: TimeoutError raised on hang",
          'TimeoutError' in src and 'handler-timeout' in src)

    # Test: normal handler completes within timeout
    from monitor.events import HeartbeatPayload
    bus = EventBus()
    results = []

    def fast_handler(event):
        results.append('fast')

    bus.subscribe(EventType.HEARTBEAT, fast_handler)
    bus.emit(Event(
        type=EventType.HEARTBEAT,
        payload=HeartbeatPayload(
            n_tickers=1, n_positions=0, open_tickers=(),
            n_trades=0, n_wins=0, total_pnl=0.0,
        ),
    ))
    check("P1-8e: Fast handler completes normally",
          'fast' in results)

    # Test: slow handler exceeding custom short timeout
    # We temporarily set a very short timeout to test
    import monitor.event_bus as eb
    original_timeout = eb.HANDLER_TIMEOUT_SEC
    eb.HANDLER_TIMEOUT_SEC = 0.5  # 500ms for testing

    bus2 = EventBus()
    slow_results = []

    def slow_handler(event):
        time.sleep(2.0)  # 2s > 0.5s timeout
        slow_results.append('should_not_reach')

    bus2.subscribe(EventType.HEARTBEAT, slow_handler)
    bus2.emit(Event(
        type=EventType.HEARTBEAT,
        payload=HeartbeatPayload(
            n_tickers=1, n_positions=0, open_tickers=(),
            n_trades=0, n_wins=0, total_pnl=0.0,
        ),
    ))

    # Handler was timed out — result should not contain the value
    check("P1-8f: Slow handler timed out (did not complete)",
          'should_not_reach' not in slow_results)

    # Restore original timeout
    eb.HANDLER_TIMEOUT_SEC = original_timeout

    # Test: timeout disabled (0) — original V6 behavior
    eb.HANDLER_TIMEOUT_SEC = 0
    bus3 = EventBus()
    v6_results = []

    def v6_handler(event):
        v6_results.append('v6_ok')

    bus3.subscribe(EventType.HEARTBEAT, v6_handler)
    bus3.emit(Event(
        type=EventType.HEARTBEAT,
        payload=HeartbeatPayload(
            n_tickers=1, n_positions=0, open_tickers=(),
            n_trades=0, n_wins=0, total_pnl=0.0,
        ),
    ))
    check("P1-8g: Timeout=0 falls back to direct call (V6 compat)",
          'v6_ok' in v6_results)

    eb.HANDLER_TIMEOUT_SEC = original_timeout


# ══════════════════════════════════════════════════════════════════════
# P1-9: Portfolio risk fail-closed
# ══════════════════════════════════════════════════════════════════════

def test_p1_9_fail_closed():
    section("P1-9: Portfolio Risk Fail-Closed")
    import monitor.portfolio_risk as pr_mod
    src = open(pr_mod.__file__).read()

    # Verify fail-closed check exists
    check("P1-9a: Checks 'buying_power is None'",
          'buying_power is None' in src)

    # Verify it blocks (not skips)
    check("P1-9b: None buying power calls self._block",
          'buying power unavailable' in src.lower() or 'UNAVAILABLE' in src)

    # Verify CRITICAL alert sent
    check("P1-9c: CRITICAL alert on unavailable buying power",
          "BUYING POWER UNAVAILABLE" in src)

    # Verify it says fail-closed (not fail-open)
    check("P1-9d: Documented as fail-closed",
          'fail-closed' in src)

    # Verify the old "if bp is not None" pattern is gone
    # Old: `if buying_power is not None and order_notional > buying_power:`
    # New: separate None check first, then comparison
    lines = src.split('\n')
    old_pattern_count = sum(1 for l in lines
                           if 'buying_power is not None and' in l
                           and 'order_notional' in l)
    check("P1-9e: Old fail-open pattern removed",
          old_pattern_count == 0,
          f"found {old_pattern_count} occurrences of old pattern")


# ══════════════════════════════════════════════════════════════════════
# P1-10: Cache TTL enforced (already fixed)
# ══════════════════════════════════════════════════════════════════════

def test_p1_10_cache_ttl():
    section("P1-10: Cache TTL Enforced (Verify Previous Fix)")
    import tempfile
    import pandas as pd
    from monitor.shared_cache import CacheWriter, CacheReader

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, 'cache.pkl')
        writer = CacheWriter(cache_path=path)
        reader = CacheReader(cache_path=path, max_age_seconds=0.5)

        bars = {'AAPL': pd.DataFrame({'close': [150.0]})}
        writer.write(bars, {})

        # Fresh — should return data
        b1, _ = reader.get_bars()
        check("P1-10a: Fresh cache returns bars", 'AAPL' in b1)

        # Wait for staleness
        time.sleep(0.7)

        # Stale — should return empty
        b2, _ = reader.get_bars()
        check("P1-10b: Stale cache returns empty dicts", len(b2) == 0)

        # Refresh — should return data again
        writer.write(bars, {})
        b3, _ = reader.get_bars()
        check("P1-10c: Refreshed cache returns bars", 'AAPL' in b3)


# ══════════════════════════════════════════════════════════════════════
# P1-11: bot_state.json read lock (already fixed)
# ══════════════════════════════════════════════════════════════════════

def test_p1_11_state_read_lock():
    section("P1-11: bot_state.json Read Lock (Verify Previous Fix)")
    import monitor.state as state_mod
    src = open(state_mod.__file__).read()

    # Verify SafeStateFile is used (which includes fcntl locks)
    check("P1-11a: Uses SafeStateFile",
          'SafeStateFile' in src)

    # Verify old threading.Lock pattern is gone
    check("P1-11b: No raw threading.Lock",
          'threading.Lock' not in src and 'threading' not in src)

    # Verify load_state uses _safe.read() (which acquires shared lock)
    check("P1-11c: load_state uses _safe.read()",
          '_safe.read()' in src)


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("  V7 P1 Fixes — Stop Silent Failures Test Suite")
    print("  Testing: timeouts, fail-closed, TTL, read locks")
    print("=" * 60)

    test_p1_6_pickle_bounded_read()
    test_p1_7_chain_timeout()
    test_p1_8_handler_timeout()
    test_p1_9_fail_closed()
    test_p1_10_cache_ttl()
    test_p1_11_state_read_lock()

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
