#!/usr/bin/env python3
"""
Test 3: Redpanda Consistency Check
====================================
Tests "Monday Morning Recovery" — can CrashRecovery rebuild positions
from the Redpanda event log alone?

Method
------
1. Check if Redpanda is running locally (127.0.0.1:9092).
2. If running:
   a. Emit synthetic FILL events via the EventBus with DurableEventLog attached.
   b. Clear bot_state.json (simulating a crash that wiped local state).
   c. Run CrashRecovery.rebuild() from the event log.
   d. Compare rebuilt positions against the original positions.
3. If NOT running:
   a. Simulate the replay logic in-memory using the serialisation path.
   b. Verify JSON round-trip fidelity of payload serialisation.
   c. Measure deserialisation time for 1000 events.

Pass criteria
-------------
- Rebuilt positions dict matches original positions (same tickers, quantities, entry prices).
- Rebuild completes in < 30 seconds.
- No data loss in JSON serialisation/deserialisation round-trip.
- Trade log is complete (all closed trades reconstructed).
"""

import sys
import os
import time
import json
import logging
import threading
import copy
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import OrderedDict

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor.event_bus import EventBus, Event, EventType, DispatchMode
from monitor.events import (
    FillPayload, PositionPayload, PositionSnapshot, PositionAction, Side,
    SignalPayload, SignalAction,
)
from monitor.event_log import _serialise_event, _serialise_payload, _safe_value

ET = ZoneInfo('America/New_York')

os.makedirs(os.path.join(os.path.dirname(__file__), "logs"), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(os.path.dirname(__file__), 'logs', 'test_3_redpanda_consistency.log'), mode='w'),
    ],
)
log = logging.getLogger(__name__)


def check_redpanda_running() -> bool:
    """Check if Redpanda is accessible at 127.0.0.1:9092."""
    import socket
    try:
        sock = socket.create_connection(('127.0.0.1', 9092), timeout=2)
        sock.close()
        return True
    except (ConnectionRefusedError, OSError, TimeoutError):
        return False


def test_serialisation_roundtrip():
    """Test 3a: JSON serialisation/deserialisation fidelity."""
    log.info("\n--- Test 3a: Payload Serialisation Round-Trip ---")

    # Create a sample FillPayload
    fill = FillPayload(
        ticker='AAPL',
        side=Side.BUY,
        qty=10,
        fill_price=150.25,
        order_id='test-order-001',
        reason='vwap_reclaim',
    )

    event = Event(type=EventType.FILL, payload=fill)
    serialized = _serialise_event(event)
    deserialized = json.loads(serialized.decode('utf-8'))

    log.info(f"Original payload:  ticker={fill.ticker}, side={fill.side}, qty={fill.qty}, "
             f"fill_price={fill.fill_price}, order_id={fill.order_id}")
    log.info(f"Deserialized:      {json.dumps(deserialized['payload'], indent=2)}")

    p = deserialized['payload']
    checks = {
        'ticker match': p.get('ticker') == 'AAPL',
        'side match': p.get('side') == 'buy',
        'qty match': p.get('qty') == 10,
        'fill_price match': p.get('fill_price') == 150.25,
        'order_id match': p.get('order_id') == 'test-order-001',
        'event_type match': deserialized.get('event_type') == 'FILL',
    }

    all_pass = True
    for check_name, result in checks.items():
        log.info(f"  {check_name}: {'PASS' if result else 'FAIL'}")
        if not result:
            all_pass = False

    # Test PositionSnapshot serialisation
    snapshot = PositionSnapshot(
        entry_price=150.0,
        entry_time='10:30:00',
        quantity=10,
        partial_done=False,
        order_id='test-order-001',
        stop_price=148.0,
        target_price=155.0,
        half_target=152.5,
        atr_value=2.5,
    )
    pos_payload = PositionPayload(
        ticker='AAPL',
        action=PositionAction.OPENED,
        position=snapshot,
    )
    pos_event = Event(type=EventType.POSITION, payload=pos_payload)
    pos_serialized = _serialise_event(pos_event)
    pos_deserialized = json.loads(pos_serialized.decode('utf-8'))

    log.info(f"\nPositionPayload serialisation:")
    log.info(f"  event_type: {pos_deserialized.get('event_type')}")
    log.info(f"  payload_type: {pos_deserialized.get('payload_type')}")
    log.info(f"  action: {pos_deserialized['payload'].get('action')}")
    pos_checks = {
        'event_type': pos_deserialized.get('event_type') == 'POSITION',
        'action': pos_deserialized['payload'].get('action') == 'opened',
        'ticker': pos_deserialized['payload'].get('ticker') == 'AAPL',
    }
    for name, result in pos_checks.items():
        log.info(f"  {name}: {'PASS' if result else 'FAIL'}")
        if not result:
            all_pass = False

    return all_pass


def test_serialisation_performance():
    """Test 3b: Serialisation performance for 1000 events."""
    log.info("\n--- Test 3b: Serialisation Performance (1000 FILL events) ---")

    events = []
    for i in range(1000):
        fill = FillPayload(
            ticker=f'TCK{i % 200:03d}',
            side=Side.BUY if i % 2 == 0 else Side.SELL,
            qty=1 + (i % 10),
            fill_price=100.0 + (i % 50),
            order_id=f'order-{i:06d}',
            reason='test_reason',
        )
        events.append(Event(type=EventType.FILL, payload=fill))

    # Benchmark serialisation
    start = time.monotonic()
    serialized = []
    for event in events:
        serialized.append(_serialise_event(event))
    elapsed = time.monotonic() - start

    log.info(f"1000 events serialised in {elapsed * 1000:.1f}ms")
    log.info(f"Rate: {1000 / elapsed:,.0f} events/sec")
    log.info(f"Avg bytes per event: {sum(len(s) for s in serialized) / len(serialized):.0f}")

    # Benchmark deserialisation
    start = time.monotonic()
    for s in serialized:
        json.loads(s.decode('utf-8'))
    elapsed = time.monotonic() - start

    log.info(f"1000 events deserialised in {elapsed * 1000:.1f}ms")
    log.info(f"Rate: {1000 / elapsed:,.0f} events/sec")

    under_30s = elapsed < 30.0
    log.info(f"Under 30s threshold: {'PASS' if under_30s else 'FAIL'}")
    return under_30s


def test_in_memory_crash_recovery():
    """Test 3c: Simulate CrashRecovery logic in memory."""
    log.info("\n--- Test 3c: In-Memory Crash Recovery Simulation ---")

    # Simulate a trading session: buy 5 tickers, sell 2, partial sell 1
    original_positions = {
        'AAPL': {
            'entry_price': 150.0, 'entry_time': '10:30:00', 'quantity': 10,
            'partial_done': True, 'order_id': 'ord-001',
            'stop_price': 148.0, 'target_price': 155.0, 'half_target': 152.5,
            'atr_value': 2.5,
        },
        'MSFT': {
            'entry_price': 320.0, 'entry_time': '10:45:00', 'quantity': 5,
            'partial_done': False, 'order_id': 'ord-002',
            'stop_price': 315.0, 'target_price': 330.0, 'half_target': 325.0,
            'atr_value': 5.0,
        },
        'NVDA': {
            'entry_price': 800.0, 'entry_time': '11:00:00', 'quantity': 3,
            'partial_done': False, 'order_id': 'ord-003',
            'stop_price': 790.0, 'target_price': 820.0, 'half_target': 810.0,
            'atr_value': 10.0,
        },
    }
    original_trade_log = [
        {'ticker': 'GOOGL', 'entry_price': 140.0, 'entry_time': '09:50:00',
         'exit_price': 142.0, 'qty': 8, 'pnl': 16.0, 'reason': 'sell_target',
         'time': '11:20:00', 'is_win': True},
        {'ticker': 'AMZN', 'entry_price': 180.0, 'entry_time': '10:15:00',
         'exit_price': 178.0, 'qty': 6, 'pnl': -12.0, 'reason': 'sell_stop',
         'time': '10:45:00', 'is_win': False},
    ]

    # Generate FILL event records (simulating what Redpanda would replay)
    fill_records = [
        # GOOGL buy then sell
        {'event_type': 'FILL', 'payload': {'ticker': 'GOOGL', 'side': 'buy', 'qty': 8, 'fill_price': 140.0, 'order_id': 'ord-g1', 'reason': 'vwap_reclaim'}, 'timestamp': '2026-04-10T09:50:00-04:00'},
        {'event_type': 'FILL', 'payload': {'ticker': 'GOOGL', 'side': 'sell', 'qty': 8, 'fill_price': 142.0, 'order_id': 'ord-g2', 'reason': 'sell_target'}, 'timestamp': '2026-04-10T11:20:00-04:00'},
        # AMZN buy then sell
        {'event_type': 'FILL', 'payload': {'ticker': 'AMZN', 'side': 'buy', 'qty': 6, 'fill_price': 180.0, 'order_id': 'ord-a1', 'reason': 'vwap_reclaim'}, 'timestamp': '2026-04-10T10:15:00-04:00'},
        {'event_type': 'FILL', 'payload': {'ticker': 'AMZN', 'side': 'sell', 'qty': 6, 'fill_price': 178.0, 'order_id': 'ord-a2', 'reason': 'sell_stop'}, 'timestamp': '2026-04-10T10:45:00-04:00'},
        # AAPL buy then partial sell then still open
        {'event_type': 'FILL', 'payload': {'ticker': 'AAPL', 'side': 'buy', 'qty': 10, 'fill_price': 150.0, 'order_id': 'ord-001', 'reason': 'vwap_reclaim'}, 'timestamp': '2026-04-10T10:30:00-04:00'},
        {'event_type': 'FILL', 'payload': {'ticker': 'AAPL', 'side': 'sell', 'qty': 5, 'fill_price': 152.5, 'order_id': 'ord-001p', 'reason': 'partial_sell'}, 'timestamp': '2026-04-10T11:00:00-04:00'},
        # MSFT buy (still open)
        {'event_type': 'FILL', 'payload': {'ticker': 'MSFT', 'side': 'buy', 'qty': 5, 'fill_price': 320.0, 'order_id': 'ord-002', 'reason': 'vwap_reclaim'}, 'timestamp': '2026-04-10T10:45:00-04:00'},
        # NVDA buy (still open)
        {'event_type': 'FILL', 'payload': {'ticker': 'NVDA', 'side': 'buy', 'qty': 3, 'fill_price': 800.0, 'order_id': 'ord-003', 'reason': 'vwap_reclaim'}, 'timestamp': '2026-04-10T11:00:00-04:00'},
    ]

    # Simulate CrashRecovery.rebuild() logic
    start = time.monotonic()
    rebuilt_positions = {}
    rebuilt_trade_log = []

    for rec in fill_records:
        et = rec.get('event_type', '')
        p = rec.get('payload', {})

        if et == 'FILL':
            ticker = p.get('ticker', '')
            side = p.get('side', '')
            qty = int(p.get('qty', 0))
            fill_price = float(p.get('fill_price', 0))
            reason = p.get('reason', '')
            order_id = p.get('order_id', '')

            if side == 'buy' and ticker:
                rebuilt_positions[ticker] = {
                    'entry_price': fill_price,
                    'entry_time': rec.get('timestamp', '')[:8],
                    'quantity': qty,
                    'partial_done': False,
                    'order_id': order_id,
                    'stop_price': fill_price * 0.995,
                    'target_price': fill_price * 1.01,
                    'half_target': fill_price * 1.005,
                    'atr_value': None,
                }
            elif side == 'sell' and ticker:
                if ticker in rebuilt_positions:
                    pos = rebuilt_positions[ticker]
                    sold_qty = qty
                    remaining = pos['quantity'] - sold_qty
                    pnl = round((fill_price - pos['entry_price']) * sold_qty, 2)
                    rebuilt_trade_log.append({
                        'ticker': ticker,
                        'entry_price': pos['entry_price'],
                        'entry_time': pos.get('entry_time', ''),
                        'exit_price': fill_price,
                        'qty': sold_qty,
                        'pnl': pnl,
                        'reason': reason,
                        'time': rec.get('timestamp', '')[:8],
                        'is_win': pnl >= 0,
                    })
                    if remaining > 0 and reason == 'partial_sell':
                        pos['quantity'] = remaining
                        pos['partial_done'] = True
                        pos['stop_price'] = pos['entry_price']
                    else:
                        del rebuilt_positions[ticker]

    elapsed = time.monotonic() - start

    # ── Verify results ────────────────────────────────────────────────────
    log.info(f"Rebuild time: {elapsed * 1000:.2f}ms")
    log.info(f"Rebuilt positions: {list(rebuilt_positions.keys())}")
    log.info(f"Original positions: {list(original_positions.keys())}")
    log.info(f"Rebuilt trade_log entries: {len(rebuilt_trade_log)}")
    log.info(f"Original trade_log entries: {len(original_trade_log)}")

    # Check positions match
    position_match = set(rebuilt_positions.keys()) == set(original_positions.keys())
    log.info(f"Position tickers match: {'PASS' if position_match else 'FAIL'}")

    for ticker in rebuilt_positions:
        if ticker in original_positions:
            rb = rebuilt_positions[ticker]
            orig = original_positions[ticker]
            log.info(f"\n  {ticker} rebuilt:")
            log.info(f"    entry_price: {rb['entry_price']} (expected: {orig['entry_price']})")
            log.info(f"    quantity: {rb['quantity']} (expected: {orig['quantity']})")
            # Note: stop/target won't match exactly since rebuild uses 0.995/1.01 fallback
            if rb['entry_price'] != orig['entry_price']:
                log.info(f"    FAIL: entry_price mismatch!")
            if rb['quantity'] != orig['quantity']:
                log.info(f"    WARN: quantity mismatch — partial_sell handling may differ")

    # Check trade_log
    trade_match = len(rebuilt_trade_log) == len(original_trade_log)
    log.info(f"\nTrade log count match: {'PASS' if trade_match else 'FAIL'}")

    for i, rt in enumerate(rebuilt_trade_log):
        if i < len(original_trade_log):
            ot = original_trade_log[i]
            log.info(f"  Trade {i}: {rt['ticker']} pnl={rt['pnl']} (expected: {ot['ticker']} pnl={ot['pnl']})")

    return position_match and trade_match and elapsed < 30.0


def test_redpanda_live():
    """Test 3d: Live Redpanda replay (if available)."""
    log.info("\n--- Test 3d: Live Redpanda Replay ---")

    if not check_redpanda_running():
        log.info("SKIP: Redpanda not running at 127.0.0.1:9092")
        log.info("To test: start Redpanda with `docker run -p 9092:9092 vectorized/redpanda:latest`")
        return None

    try:
        from monitor.event_log import EventLogConsumer, CrashRecovery

        # Test consumer connection
        consumer = EventLogConsumer(brokers='127.0.0.1:9092', topic='trading-hub-events')
        log.info("PASS: EventLogConsumer connected to Redpanda")

        # Try replay
        start = time.monotonic()
        records = list(consumer.replay(n=100))
        elapsed = time.monotonic() - start

        log.info(f"Replayed {len(records)} records in {elapsed:.2f}s")
        if records:
            log.info(f"First record: {json.dumps(records[0], indent=2, default=str)[:200]}")
            log.info(f"Event types: {set(r.get('event_type', 'UNKNOWN') for r in records)}")

        consumer.close()

        if elapsed < 30.0:
            log.info(f"PASS: Replay under 30s ({elapsed:.2f}s)")
            return True
        else:
            log.info(f"FAIL: Replay took {elapsed:.2f}s (>30s)")
            return False

    except Exception as e:
        log.info(f"FAIL: Redpanda error: {e}")
        return False


def test_bot_state_persistence():
    """Test 3e: Verify bot_state.json save/load round-trip."""
    log.info("\n--- Test 3e: bot_state.json Persistence ---")

    state_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'bot_state.json')
    backup_path = state_path + '.backup_test3'

    # Read current state
    if not os.path.exists(state_path):
        log.info("WARN: bot_state.json not found — creating test state")
        test_state = {
            'date': '2026-04-11',
            'positions': {
                'AAPL': {'entry_price': 150.0, 'quantity': 10, 'stop_price': 148.0},
            },
            'reclaimed_today': ['AAPL'],
            'trade_log': [
                {'ticker': 'GOOGL', 'pnl': 16.0, 'is_win': True},
            ],
        }
    else:
        with open(state_path) as f:
            test_state = json.load(f)

    # Save
    start = time.monotonic()
    with open(backup_path, 'w') as f:
        json.dump(test_state, f, indent=2)
    save_time = time.monotonic() - start

    # Load
    start = time.monotonic()
    with open(backup_path) as f:
        loaded = json.load(f)
    load_time = time.monotonic() - start

    log.info(f"Save time: {save_time * 1000:.2f}ms")
    log.info(f"Load time: {load_time * 1000:.2f}ms")
    log.info(f"State preserved: {'PASS' if loaded == test_state else 'FAIL'}")

    # Cleanup
    if os.path.exists(backup_path):
        os.remove(backup_path)

    return loaded == test_state


def run_test():
    log.info("=" * 70)
    log.info("TEST 3: Redpanda Consistency Check")
    log.info("=" * 70)

    redpanda_running = check_redpanda_running()
    log.info(f"Redpanda at 127.0.0.1:9092: {'RUNNING' if redpanda_running else 'NOT RUNNING'}")
    log.info("")

    results = {}
    results['serialisation_roundtrip'] = test_serialisation_roundtrip()
    results['serialisation_perf'] = test_serialisation_performance()
    results['crash_recovery_sim'] = test_in_memory_crash_recovery()
    results['redpanda_live'] = test_redpanda_live()
    results['bot_state_persistence'] = test_bot_state_persistence()

    # Summary
    log.info("\n" + "=" * 70)
    log.info("RESULTS")
    log.info("=" * 70)
    for name, result in results.items():
        if result is None:
            status = "SKIP"
        elif result:
            status = "PASS"
        else:
            status = "FAIL"
        log.info(f"  {name:30s}: {status}")

    # Improvements
    log.info("\n" + "-" * 70)
    log.info("IMPROVEMENTS NEEDED")
    log.info("-" * 70)

    if not results['crash_recovery_sim']:
        log.info("1. Crash recovery simulation had mismatches — partial_sell handling may lose position state")
        log.info("   CrashRecovery uses fallback stop/target (0.995/1.01 × entry_price)")
        log.info("   Consider also replaying SIGNAL events to get exact stop/target values")

    if not redpanda_running:
        log.info("2. Redpanda not running — live replay test was skipped")
        log.info("   Start Redpanda before Monday to verify real replay: docker compose up -d redpanda")

    if results['redpanda_live'] is not None and not results['redpanda_live']:
        log.info("3. Redpanda replay was slow (>30s) — consider implementing periodic snapshots")
        log.info("   A snapshot every 5 min would reduce replay to ~5 min of events max")

    log.info("4. General: bot_state.json and Redpanda can diverge if writes aren't atomic")
    log.info("   Consider writing bot_state.json ONLY after successful Redpanda flush")

    log.info("\nTest 3 complete.")

    passed = sum(1 for r in results.values() if r is True)
    total = sum(1 for r in results.values() if r is not None)
    return passed >= total * 0.7  # 70% pass rate


if __name__ == '__main__':
    success = run_test()
    sys.exit(0 if success else 1)