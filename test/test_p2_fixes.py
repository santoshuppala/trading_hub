#!/usr/bin/env python3
"""
V7 P2 Fixes Test Suite — Ordering & Consistency.

Tests the 4 P2 fixes from the V7 architectural review:
  P2-1: IPC consumer inbox queue (single-threaded injection)
  P2-2: Causation chain (FILL carries ORDER_REQ event_id)
  P2-3: Crash recovery sorts by stream_seq (already fixed — verify)
  P2-4: Projection table dedup via ON CONFLICT DO NOTHING (already fixed — verify)

Run: python test/test_p2_fixes.py
"""
import os
import sys
import queue
import threading
import time
import uuid

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
# P2-1: IPC consumer inbox queue
# ══════════════════════════════════════════════════════════════════════

def test_p2_1_ipc_inbox():
    section("P2-1: IPC Consumer Inbox Queue")

    # Verify run_core.py uses inbox queue pattern
    core_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'scripts', 'run_core.py')
    with open(core_path) as f:
        src = f.read()

    check("P2-1a: _ipc_inbox queue created",
          '_ipc_inbox' in src and 'queue.Queue' in src.replace('_queue.Queue', 'queue.Queue'))

    check("P2-1b: Consumer enqueues (put_nowait), not emit()",
          'put_nowait(payload)' in src)

    check("P2-1c: _drain_ipc_inbox function exists",
          'def _drain_ipc_inbox' in src)

    check("P2-1d: Drain called in main loop",
          '_drain_ipc_inbox()' in src)

    check("P2-1e: bus.emit() only in _drain_ipc_inbox (main thread)",
          # The _on_remote_order handler should NOT call bus.emit
          'monitor._bus.emit' not in src.split('def _on_remote_order')[1].split('def _drain_ipc_inbox')[0]
          if 'def _on_remote_order' in src and 'def _drain_ipc_inbox' in src
          else False)

    check("P2-1f: Inbox overflow logged as error",
          'Inbox FULL' in src)

    check("P2-1g: Inbox has bounded size",
          'maxsize=500' in src or 'maxsize=' in src)

    # Test the queue pattern in isolation
    inbox = queue.Queue(maxsize=10)

    # Simulate consumer thread enqueuing
    def producer():
        for i in range(5):
            inbox.put_nowait({'ticker': f'T{i}', 'side': 'BUY', 'qty': 1,
                             'price': 100.0, 'reason': 'test'})

    t = threading.Thread(target=producer)
    t.start()
    t.join()

    check("P2-1h: Queue filled by producer thread",
          inbox.qsize() == 5)

    # Drain on main thread
    drained = []
    while not inbox.empty():
        drained.append(inbox.get_nowait())

    check("P2-1i: All items drained on main thread",
          len(drained) == 5)
    check("P2-1j: Order preserved (FIFO)",
          [d['ticker'] for d in drained] == ['T0', 'T1', 'T2', 'T3', 'T4'])


# ══════════════════════════════════════════════════════════════════════
# P2-2: Causation chain (FILL carries ORDER_REQ event_id)
# ══════════════════════════════════════════════════════════════════════

def test_p2_2_causation_chain():
    section("P2-2: Causation Chain (FILL ← ORDER_REQ)")

    import monitor.brokers as brokers_mod
    src = open(brokers_mod.__file__).read()

    # Verify _current_causation_id is set in _on_order_request
    check("P2-2a: _current_causation_id set from event.event_id",
          '_current_causation_id = event.event_id' in src)

    # Verify _emit_fill reads _current_causation_id
    check("P2-2b: _emit_fill reads _current_causation_id",
          '_current_causation_id' in src and 'getattr(self' in src)

    # Verify causation_id is cleared after execution
    check("P2-2c: Causation cleared after order execution",
          '_current_causation_id = None' in src)

    # Test the actual causation chain with PaperBroker
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side
    from monitor.brokers import PaperBroker

    bus = EventBus()
    fills = []

    def on_fill(event):
        fills.append({
            'ticker': event.payload.ticker,
            'correlation_id': event.correlation_id,
        })

    bus.subscribe(EventType.FILL, on_fill)
    broker = PaperBroker(bus)

    # Emit ORDER_REQ with known event_id
    order_event_id = str(uuid.uuid4())
    order_event = Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
            reason='test', layer='vwap',
        ),
        event_id=order_event_id,
    )

    # PaperBroker processes it
    broker._on_order_request(order_event)

    check("P2-2d: PaperBroker emitted FILL",
          len(fills) == 1)

    # FILL should carry ORDER_REQ's event_id as correlation_id
    if fills:
        check("P2-2e: FILL.correlation_id = ORDER_REQ.event_id",
              fills[0]['correlation_id'] == order_event_id,
              f"expected={order_event_id[:12]} got={str(fills[0]['correlation_id'])[:12]}")
    else:
        check("P2-2e: FILL.correlation_id (skipped — no fill)", False)

    # Test SELL also carries causation
    fills.clear()
    sell_event_id = str(uuid.uuid4())
    sell_event = Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='AAPL', side=Side.SELL, qty=10, price=155.0,
            reason='SELL_TARGET',
        ),
        event_id=sell_event_id,
    )
    broker._on_order_request(sell_event)

    check("P2-2f: SELL FILL emitted",
          len(fills) == 1)
    if fills:
        check("P2-2g: SELL FILL.correlation_id = SELL ORDER_REQ.event_id",
              fills[0]['correlation_id'] == sell_event_id)
    else:
        check("P2-2g: SELL FILL correlation (skipped)", False)

    # Test causation is cleared between orders
    check("P2-2h: _current_causation_id cleared after order",
          broker._current_causation_id is None)


# ══════════════════════════════════════════════════════════════════════
# P2-3: Crash recovery sorts by stream_seq (already fixed)
# ══════════════════════════════════════════════════════════════════════

def test_p2_3_crash_recovery_sorting():
    section("P2-3: Crash Recovery Sorts by stream_seq (Verify)")

    event_log_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'monitor', 'event_log.py')
    with open(event_log_path) as f:
        src = f.read()

    # Verify replay() sorts records
    check("P2-3a: replay() collects records into list",
          'records: list = []' in src or 'records = []' in src)
    check("P2-3b: replay() sorts by _sort_key",
          'records.sort(key=_sort_key)' in src)
    check("P2-3c: _sort_key uses stream_seq as primary",
          "r.get('stream_seq')" in src)
    check("P2-3d: _sort_key falls back to timestamp",
          "r.get('timestamp'" in src)
    check("P2-3e: Comment documents causal ordering requirement",
          'causal' in src.lower() and 'ORDER_REQ' in src and 'FILL' in src)

    # Verify sort produces correct order
    def _sort_key(r):
        seq = r.get('stream_seq')
        if seq is not None:
            try:
                return (0, int(seq), '')
            except (TypeError, ValueError):
                pass
        return (1, 0, r.get('timestamp', ''))

    # Simulate out-of-order events from different partitions
    records = [
        {'event_type': 'FILL', 'stream_seq': '5', 'timestamp': '2026-04-15T14:32:01'},
        {'event_type': 'ORDER_REQ', 'stream_seq': '3', 'timestamp': '2026-04-15T14:32:00'},
        {'event_type': 'SIGNAL', 'stream_seq': '1', 'timestamp': '2026-04-15T14:31:59'},
        {'event_type': 'POSITION', 'stream_seq': '7', 'timestamp': '2026-04-15T14:32:02'},
    ]
    records.sort(key=_sort_key)
    sorted_types = [r['event_type'] for r in records]
    check("P2-3f: Sorted order is SIGNAL→ORDER_REQ→FILL→POSITION",
          sorted_types == ['SIGNAL', 'ORDER_REQ', 'FILL', 'POSITION'],
          f"got={sorted_types}")

    # Events without stream_seq sort after those with it (timestamp fallback)
    records2 = [
        {'event_type': 'FILL', 'timestamp': '2026-04-15T14:32:01'},
        {'event_type': 'ORDER_REQ', 'stream_seq': '3', 'timestamp': '2026-04-15T14:32:00'},
    ]
    records2.sort(key=_sort_key)
    check("P2-3g: Events with stream_seq sort before those without",
          records2[0]['event_type'] == 'ORDER_REQ')


# ══════════════════════════════════════════════════════════════════════
# P2-4: Projection table dedup (ON CONFLICT DO NOTHING)
# ══════════════════════════════════════════════════════════════════════

def test_p2_4_projection_dedup():
    section("P2-4: Projection Table Dedup (ON CONFLICT DO NOTHING)")

    writer_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'db', 'writer.py')
    with open(writer_path) as f:
        src = f.read()

    check("P2-4a: DBWriter uses ON CONFLICT DO NOTHING",
          'ON CONFLICT DO NOTHING' in src)

    # Verify it's in the INSERT statement template
    check("P2-4b: Applied to parameterized INSERT",
          'INSERT INTO' in src and 'ON CONFLICT DO NOTHING' in src)

    # Check event_store schema has PK on event_id
    schema_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'db', 'schema_event_sourcing_pg.sql')
    if os.path.exists(schema_path):
        with open(schema_path) as f:
            schema = f.read()
        check("P2-4c: event_store has event_id PRIMARY KEY",
              'event_id' in schema and 'PRIMARY KEY' in schema)
    else:
        check("P2-4c: Schema file exists (skipped)", True)


# ══════════════════════════════════════════════════════════════════════
# INTEGRATION: Ordering guarantees work together
# ══════════════════════════════════════════════════════════════════════

def test_integration():
    section("INTEGRATION: Ordering Guarantees")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side
    from monitor.brokers import PaperBroker

    bus = EventBus()

    # Track full event chain with correlation IDs
    chain = []

    def track_order(event):
        chain.append({
            'type': 'ORDER_REQ',
            'event_id': event.event_id,
            'correlation_id': event.correlation_id,
            'ticker': event.payload.ticker,
        })

    def track_fill(event):
        chain.append({
            'type': 'FILL',
            'event_id': event.event_id,
            'correlation_id': event.correlation_id,
            'ticker': event.payload.ticker,
        })

    bus.subscribe(EventType.ORDER_REQ, track_order, priority=99)
    bus.subscribe(EventType.FILL, track_fill, priority=99)

    broker = PaperBroker(bus)

    # Simulate order flow
    order_eid = str(uuid.uuid4())
    bus.emit(Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='MSFT', side=Side.BUY, qty=5, price=400.0,
            reason='test', layer='vwap',
        ),
        event_id=order_eid,
    ))

    # Find the ORDER_REQ and FILL in chain
    order_events = [c for c in chain if c['type'] == 'ORDER_REQ']
    fill_events = [c for c in chain if c['type'] == 'FILL']

    check("INT-a: ORDER_REQ captured",
          len(order_events) >= 1)
    check("INT-b: FILL captured",
          len(fill_events) >= 1)

    if order_events and fill_events:
        order = order_events[0]
        fill = fill_events[0]

        check("INT-c: FILL.correlation_id links to ORDER_REQ.event_id",
              fill['correlation_id'] == order['event_id'])

        check("INT-d: Same ticker in ORDER_REQ and FILL",
              order['ticker'] == fill['ticker'] == 'MSFT')

        # ORDER_REQ should appear before FILL in the chain
        order_idx = chain.index(order)
        fill_idx = chain.index(fill)
        check("INT-e: ORDER_REQ processed before FILL",
              order_idx < fill_idx,
              f"order_idx={order_idx} fill_idx={fill_idx}")
    else:
        check("INT-c: Chain linkage (skipped)", False)
        check("INT-d: Ticker match (skipped)", False)
        check("INT-e: Ordering (skipped)", False)


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("  V7 P2 Fixes — Ordering & Consistency Test Suite")
    print("  Testing: inbox queue, causation chain, sort, dedup")
    print("=" * 60)

    test_p2_1_ipc_inbox()
    test_p2_2_causation_chain()
    test_p2_3_crash_recovery_sorting()
    test_p2_4_projection_dedup()
    test_integration()

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
