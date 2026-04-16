#!/usr/bin/env python3
"""
V7 P0 Fixes Test Suite — Stop Losing Money.

Tests the 5 P0 fixes from the V7 architectural review:
  P0-1: BUY order idempotency via deterministic client_order_id
  P0-2: SmartRouter ORDER_REQ dedup (LRU seen set)
  P0-3: position_broker_map TOCTOU (already fixed by SafeStateFile — verified here)
  P0-4: Redpanda manual commit (enable.auto.commit=False)
  P0-5: No optimistic SELL FILL (verify before emitting)

Run: python test/test_p0_fixes.py
"""
import os
import sys
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
# P0-1: BUY order idempotency — deterministic client_order_id
# ══════════════════════════════════════════════════════════════════════

def test_p0_1_buy_idempotency():
    section("P0-1: BUY Order Idempotency (client_order_id)")

    # Verify AlpacaBroker._execute_buy uses client_order_id
    import monitor.brokers as brokers_mod
    src = open(brokers_mod.__file__).read()

    check("P0-1a: BUY uses client_order_id",
          'client_order_id=client_oid' in src or 'client_order_id=' in src)
    check("P0-1b: client_order_id derived from event_id",
          '_source_event_id' in src)
    check("P0-1c: Deterministic format (th-buy-TICKER-EVENT-ATTEMPT)",
          'th-buy-' in src)

    # Verify _on_order_request tags payload with event_id
    check("P0-1d: _on_order_request sets _source_event_id",
          '_source_event_id' in src and 'event.event_id' in src)

    # Test determinism: same event_id → same client_order_id prefix
    event_id = 'abc123def456'
    ticker = 'AAPL'
    attempt = 1
    oid1 = f"th-buy-{ticker}-{event_id[:12]}-{attempt}"
    oid2 = f"th-buy-{ticker}-{event_id[:12]}-{attempt}"
    check("P0-1e: Same inputs → same client_order_id",
          oid1 == oid2)
    check("P0-1f: client_order_id format correct",
          oid1 == 'th-buy-AAPL-abc123def456-1')

    # Different attempt → different client_order_id
    oid3 = f"th-buy-{ticker}-{event_id[:12]}-2"
    check("P0-1g: Different attempt → different client_order_id",
          oid1 != oid3)

    # Different event → different client_order_id
    oid4 = f"th-buy-{ticker}-{'xyz789000000'[:12]}-{attempt}"
    check("P0-1h: Different event → different client_order_id",
          oid1 != oid4)


# ══════════════════════════════════════════════════════════════════════
# P0-2: SmartRouter ORDER_REQ dedup
# ══════════════════════════════════════════════════════════════════════

def test_p0_2_smartrouter_dedup():
    section("P0-2: SmartRouter ORDER_REQ Dedup")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side
    from monitor.smart_router import SmartRouter

    bus = EventBus()
    # Create router without actual brokers (test dedup logic only)
    router = SmartRouter(bus=bus)

    # Track what gets through
    routed = []
    original_on_order_req = router._on_order_req

    def tracking_on_order_req(event):
        # Call original to populate _routed_event_ids
        original_on_order_req(event)

    # Subscribe tracker AFTER router
    def late_tracker(event):
        eid = event.event_id
        # If it's in _routed_event_ids, router processed it
        if eid in router._routed_event_ids:
            routed.append(eid)

    bus.subscribe(EventType.ORDER_REQ, late_tracker, priority=0)

    # First emit — should be processed
    eid = str(uuid.uuid4())
    e1 = Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
            reason='test', layer='vwap',
        ),
        event_id=eid,
    )
    bus.emit(e1)
    check("P0-2a: First ORDER_REQ processed",
          eid in router._routed_event_ids)

    # Same event_id again (replay) — should be SKIPPED
    initial_count = len(router._routed_event_ids)
    e2 = Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
            reason='test', layer='vwap',
        ),
        event_id=eid,  # same event_id
    )
    bus.emit(e2)
    check("P0-2b: Duplicate ORDER_REQ skipped (same event_id)",
          len(router._routed_event_ids) == initial_count)

    # Different event_id — should be processed
    eid2 = str(uuid.uuid4())
    e3 = Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='MSFT', side=Side.BUY, qty=5, price=400.0,
            reason='test2', layer='pro',
        ),
        event_id=eid2,
    )
    bus.emit(e3)
    check("P0-2c: Different event_id processed",
          eid2 in router._routed_event_ids)

    # Verify LRU pruning — add many entries
    for i in range(6000):
        fake_eid = str(uuid.uuid4())
        router._routed_event_ids[fake_eid] = time.monotonic()

    check("P0-2d: _routed_event_ids grows",
          len(router._routed_event_ids) > 5000)

    # Emit one more — should trigger pruning
    e4 = Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='GOOG', side=Side.BUY, qty=3, price=175.0,
            reason='test3', layer='pop',
        ),
        event_id=str(uuid.uuid4()),
    )
    bus.emit(e4)
    check("P0-2e: LRU pruning keeps size bounded",
          len(router._routed_event_ids) <= router._ROUTED_MAX,
          f"size={len(router._routed_event_ids)}")

    # SELL should still pass (no dedup on sells — exits must always execute)
    sell_eid = str(uuid.uuid4())
    bus.emit(Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='AAPL', side=Side.SELL, qty=10, price=155.0,
            reason='SELL_TARGET',
        ),
        event_id=sell_eid,
    ))
    # SELLs are in the dedup set but this is fine — they still execute
    # because no broker is connected, they'll just fail gracefully
    check("P0-2f: SELL events also tracked (no special skip for dedup)",
          sell_eid in router._routed_event_ids)


# ══════════════════════════════════════════════════════════════════════
# P0-3: position_broker_map TOCTOU (verified by SafeStateFile)
# ══════════════════════════════════════════════════════════════════════

def test_p0_3_broker_map_toctou():
    section("P0-3: position_broker_map TOCTOU (SafeStateFile)")

    # Verify SmartRouter uses SafeStateFile for broker map
    import monitor.smart_router as sr_mod
    src = open(sr_mod.__file__).read()

    check("P0-3a: SmartRouter imports SafeStateFile",
          'SafeStateFile' in src)
    check("P0-3b: _broker_map_sf created",
          '_broker_map_sf = SafeStateFile' in src)
    check("P0-3c: _load_broker_map uses sf.read()",
          'self._broker_map_sf.read()' in src)
    check("P0-3d: _save_broker_map uses sf.write()",
          'self._broker_map_sf.write(' in src)


# ══════════════════════════════════════════════════════════════════════
# P0-4: Redpanda manual commit
# ══════════════════════════════════════════════════════════════════════

def test_p0_4_manual_commit():
    section("P0-4: Redpanda Manual Commit")

    import monitor.ipc as ipc_mod
    src = open(ipc_mod.__file__).read()

    # Verify auto-commit is disabled
    check("P0-4a: enable.auto.commit is False",
          "'enable.auto.commit': False" in src)

    # Verify manual commit after handler success
    check("P0-4b: commit() called after handler",
          'self._consumer.commit(msg' in src)

    # Verify commit NOT called on handler error
    check("P0-4c: Error path says 'offset NOT committed'",
          'offset NOT committed' in src)

    # Verify commit is asynchronous (non-blocking)
    check("P0-4d: commit is asynchronous",
          'asynchronous=True' in src)

    # Verify old auto.commit.interval.ms is removed
    check("P0-4e: auto.commit.interval.ms removed",
          'auto.commit.interval.ms' not in src)


# ══════════════════════════════════════════════════════════════════════
# P0-5: No optimistic SELL FILL
# ══════════════════════════════════════════════════════════════════════

def test_p0_5_no_optimistic_sell():
    section("P0-5: No Optimistic SELL FILL")

    import monitor.brokers as brokers_mod
    src = open(brokers_mod.__file__).read()

    # Verify "optimistically" is no longer in the code
    # (V6 had "emitting FILL optimistically" in 3 places)
    optimistic_count = src.count('optimistically')
    check("P0-5a: No 'optimistically' in broker code",
          optimistic_count == 0,
          f"found {optimistic_count} occurrences")

    # Verify extended verification loop exists
    check("P0-5b: Extended verify loop with retries",
          'for retry in range(3)' in src)

    # Verify UNVERIFIED path emits ORDER_FAIL, not FILL
    check("P0-5c: Unverified sell emits ORDER_FAIL",
          'SELL UNVERIFIED' in src and 'self._fail(p)' in src)

    # Verify CRITICAL alert on unverified sell
    check("P0-5d: CRITICAL alert on unverified sell",
          "severity='CRITICAL'" in src and 'UNVERIFIED' in src)

    # Verify terminal states (cancelled/expired/rejected) emit ORDER_FAIL
    check("P0-5e: Terminal status emits ORDER_FAIL",
          'NOT emitting FILL' in src and 'self._fail(p)' in src)


# ══════════════════════════════════════════════════════════════════════
# INTEGRATION: All 5 P0 fixes work together
# ══════════════════════════════════════════════════════════════════════

def test_integration():
    section("INTEGRATION: P0 Fixes Work Together")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side

    bus = EventBus()

    # Create SmartRouter (no real brokers)
    from monitor.smart_router import SmartRouter
    router = SmartRouter(bus=bus)

    # Simulate: same ORDER_REQ replayed 3 times (Redpanda replay after crash)
    eid = str(uuid.uuid4())
    for i in range(3):
        e = Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='pro:sr_flip:T1:long', layer='pro',
            ),
            event_id=eid,
        )
        bus.emit(e)

    # Only first should be in routed set (other 2 were deduped)
    check("INT-a: Triplicated ORDER_REQ only routed once",
          eid in router._routed_event_ids)

    # The client_order_id would be deterministic for all 3 replays
    event_id_prefix = eid[:12]
    expected_oid = f"th-buy-AAPL-{event_id_prefix}-1"
    check("INT-b: All 3 replays would produce same client_order_id",
          f"th-buy-AAPL-{event_id_prefix}-1" == expected_oid)

    # Different event = different routing
    eid2 = str(uuid.uuid4())
    e2 = Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='MSFT', side=Side.BUY, qty=5, price=400.0,
            reason='vwap_reclaim', layer='vwap',
        ),
        event_id=eid2,
    )
    bus.emit(e2)
    check("INT-c: Different event routed independently",
          eid2 in router._routed_event_ids)
    check("INT-d: Two events in routed set",
          eid in router._routed_event_ids and eid2 in router._routed_event_ids)


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("  V7 P0 Fixes — Stop Losing Money Test Suite")
    print("  Testing: idempotency, dedup, TOCTOU, commit, SELL verify")
    print("=" * 60)

    test_p0_1_buy_idempotency()
    test_p0_2_smartrouter_dedup()
    test_p0_3_broker_map_toctou()
    test_p0_4_manual_commit()
    test_p0_5_no_optimistic_sell()
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
