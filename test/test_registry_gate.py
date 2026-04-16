#!/usr/bin/env python3
"""
Test suite for V7 centralized RegistryGate.

Validates:
  - Core's RegistryGate acquires/releases registry on ORDER_REQ/POSITION/ORDER_FAIL
  - Satellites only do read-only pre-flight checks
  - Cross-layer dedup works end-to-end through EventBus
  - SmartRouter queries RegistryGate.is_blocked()
  - Layer inference from reason string (backward compat)
  - ORDER_FAIL releases acquired tickers (no leak)
  - SELL orders always pass (never blocked)

Run: python test/test_registry_gate.py
"""
import os
import sys
import tempfile

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
# TEST 1: RegistryGate acquires on BUY, passes SELL
# ══════════════════════════════════════════════════════════════════════

def test_1_acquire_and_pass():
    section("TEST 1: RegistryGate Acquire on BUY, Pass SELL")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side
    from monitor.registry_gate import RegistryGate

    with tempfile.TemporaryDirectory() as td:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=10,
                                          registry_path=os.path.join(td, 'reg.json'))

        bus = EventBus()
        gate = RegistryGate(bus=bus, registry=reg)

        # Track if event was blocked
        blocked_events = []
        passed_events = []

        def _track(event):
            if gate.is_blocked(event.event_id):
                blocked_events.append(event.payload.ticker)
            else:
                passed_events.append(event.payload.ticker)

        bus.subscribe(EventType.ORDER_REQ, _track, priority=0)

        # BUY AAPL with layer='pro'
        buy_event = Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='pro:sr_flip:T1:long', layer='pro',
            ),
        )
        bus.emit(buy_event)

        check("T1a: BUY passed through", 'AAPL' in passed_events)
        check("T1b: Registry acquired AAPL for pro",
              reg.held_by('AAPL') == 'pro')

        # SELL should always pass (even without layer)
        sell_event = Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.SELL, qty=10, price=155.0,
                reason='SELL_TARGET',
            ),
        )
        passed_events.clear()
        bus.emit(sell_event)
        check("T1c: SELL always passes", 'AAPL' in passed_events)


# ══════════════════════════════════════════════════════════════════════
# TEST 2: Cross-layer dedup — second layer blocked
# ══════════════════════════════════════════════════════════════════════

def test_2_cross_layer_dedup():
    section("TEST 2: Cross-Layer Dedup")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side
    from monitor.registry_gate import RegistryGate

    with tempfile.TemporaryDirectory() as td:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=10,
                                          registry_path=os.path.join(td, 'reg.json'))
        bus = EventBus()
        gate = RegistryGate(bus=bus, registry=reg)

        blocked = []
        passed = []

        def _track(event):
            if gate.is_blocked(event.event_id):
                blocked.append(event.payload.layer or 'unknown')
            else:
                passed.append(event.payload.layer or 'unknown')

        bus.subscribe(EventType.ORDER_REQ, _track, priority=0)

        # Pro acquires AAPL
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='pro:sr_flip', layer='pro',
            ),
        ))
        check("T2a: Pro BUY passed", 'pro' in passed)
        check("T2b: AAPL held by pro", reg.held_by('AAPL') == 'pro')

        # Pop tries same ticker — should be blocked
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=5, price=150.0,
                reason='pop:VWAP_RECLAIM', layer='pop',
            ),
        ))
        check("T2c: Pop BUY blocked", 'pop' in blocked)
        check("T2d: AAPL still held by pro", reg.held_by('AAPL') == 'pro')

        # VWAP (core) tries same ticker — blocked
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=8, price=150.0,
                reason='VWAP reclaim', layer='vwap',
            ),
        ))
        check("T2e: VWAP BUY blocked", 'vwap' in blocked)

        # Different ticker — should pass
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='MSFT', side=Side.BUY, qty=5, price=400.0,
                reason='pop:ORB', layer='pop',
            ),
        ))
        check("T2f: Pop BUY MSFT passed", passed.count('pop') == 1)
        check("T2g: MSFT held by pop", reg.held_by('MSFT') == 'pop')

        # Same layer re-acquire (idempotent)
        passed.clear()
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='pro:sr_flip', layer='pro',
            ),
        ))
        check("T2h: Same layer re-acquire passes (idempotent)",
              'pro' in passed)


# ══════════════════════════════════════════════════════════════════════
# TEST 3: POSITION CLOSED releases registry
# ══════════════════════════════════════════════════════════════════════

def test_3_position_closed_releases():
    section("TEST 3: POSITION CLOSED Releases Registry")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import (OrderRequestPayload, PositionPayload,
                                 Side, PositionAction)
    from monitor.registry_gate import RegistryGate

    with tempfile.TemporaryDirectory() as td:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=10,
                                          registry_path=os.path.join(td, 'reg.json'))
        bus = EventBus()
        gate = RegistryGate(bus=bus, registry=reg)

        # Acquire AAPL
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='pro:sr_flip', layer='pro',
            ),
        ))
        check("T3a: AAPL acquired", reg.is_held('AAPL'))

        # Emit POSITION CLOSED
        bus.emit(Event(
            type=EventType.POSITION,
            payload=PositionPayload(
                ticker='AAPL',
                action=PositionAction.CLOSED,
                position=None,
                pnl=25.0,
            ),
        ))
        check("T3b: AAPL released after CLOSED", not reg.is_held('AAPL'))

        # Now another layer can acquire it
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=5, price=151.0,
                reason='pop:momentum', layer='pop',
            ),
        ))
        check("T3c: Pop can now acquire AAPL", reg.held_by('AAPL') == 'pop')


# ══════════════════════════════════════════════════════════════════════
# TEST 4: ORDER_FAIL releases acquired ticker (no leak)
# ══════════════════════════════════════════════════════════════════════

def test_4_order_fail_releases():
    section("TEST 4: ORDER_FAIL Releases Acquired Ticker")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import (OrderRequestPayload, OrderFailPayload,
                                 Side)
    from monitor.registry_gate import RegistryGate

    with tempfile.TemporaryDirectory() as td:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=10,
                                          registry_path=os.path.join(td, 'reg.json'))
        bus = EventBus()
        gate = RegistryGate(bus=bus, registry=reg)

        # Acquire AAPL via BUY
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='vwap_reclaim', layer='vwap',
            ),
        ))
        check("T4a: AAPL acquired", reg.is_held('AAPL'))

        # Simulate ORDER_FAIL (broker rejected)
        bus.emit(Event(
            type=EventType.ORDER_FAIL,
            payload=OrderFailPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='broker timeout',
            ),
        ))
        check("T4b: AAPL released after ORDER_FAIL",
              not reg.is_held('AAPL'))

        # No leak — ticker available for other layers
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=5, price=150.0,
                reason='pro:trend_pullback', layer='pro',
            ),
        ))
        check("T4c: Pro can acquire AAPL after fail release",
              reg.held_by('AAPL') == 'pro')


# ══════════════════════════════════════════════════════════════════════
# TEST 5: Layer inference from reason string (backward compat)
# ══════════════════════════════════════════════════════════════════════

def test_5_layer_inference():
    section("TEST 5: Layer Inference from Reason String")
    from monitor.registry_gate import RegistryGate

    check("T5a: 'pro:sr_flip:T1:long' → pro",
          RegistryGate._infer_layer('pro:sr_flip:T1:long') == 'pro')
    check("T5b: 'pop:VWAP_RECLAIM:pop' → pop",
          RegistryGate._infer_layer('pop:VWAP_RECLAIM:pop') == 'pop')
    check("T5c: 'options:iron_condor' → options",
          RegistryGate._infer_layer('options:iron_condor') == 'options')
    check("T5d: 'VWAP reclaim' → vwap",
          RegistryGate._infer_layer('VWAP reclaim') == 'vwap')
    check("T5e: '' → vwap (default)",
          RegistryGate._infer_layer('') == 'vwap')
    check("T5f: None → vwap",
          RegistryGate._infer_layer(None) == 'vwap')

    # Test with actual ORDER_REQ (no layer field — infer from reason)
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side

    with tempfile.TemporaryDirectory() as td:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=10,
                                          registry_path=os.path.join(td, 'reg.json'))
        bus = EventBus()
        gate = RegistryGate(bus=bus, registry=reg)

        # ORDER_REQ without layer field — should infer from reason
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='GOOG', side=Side.BUY, qty=5, price=175.0,
                reason='pro:fib_confluence:T3:long',
                # no layer= field
            ),
        ))
        check("T5g: Inferred layer='pro' from reason",
              reg.held_by('GOOG') == 'pro')


# ══════════════════════════════════════════════════════════════════════
# TEST 6: Global max enforced at Core
# ══════════════════════════════════════════════════════════════════════

def test_6_global_max():
    section("TEST 6: Global Max Enforced at Core")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side
    from monitor.registry_gate import RegistryGate

    with tempfile.TemporaryDirectory() as td:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=3,
                                          registry_path=os.path.join(td, 'reg.json'))
        bus = EventBus()
        gate = RegistryGate(bus=bus, registry=reg)

        blocked = []
        passed = []

        def _track(event):
            if gate.is_blocked(event.event_id):
                blocked.append(event.payload.ticker)
            else:
                passed.append(event.payload.ticker)

        bus.subscribe(EventType.ORDER_REQ, _track, priority=0)

        # Fill up to global max
        for i, (ticker, layer) in enumerate([('A', 'vwap'), ('B', 'pro'), ('C', 'pop')]):
            bus.emit(Event(
                type=EventType.ORDER_REQ,
                payload=OrderRequestPayload(
                    ticker=ticker, side=Side.BUY, qty=1, price=100.0,
                    reason=f'{layer}:test', layer=layer,
                ),
            ))

        check("T6a: 3 tickers acquired", reg.count() == 3)
        check("T6b: All 3 passed", len(passed) == 3)

        # 4th should be blocked (global max = 3)
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='D', side=Side.BUY, qty=1, price=100.0,
                reason='vwap_reclaim', layer='vwap',
            ),
        ))
        check("T6c: 4th ticker blocked at global max",
              'D' in blocked)
        check("T6d: Count still 3", reg.count() == 3)


# ══════════════════════════════════════════════════════════════════════
# TEST 7: RISK_BLOCK emitted on registry rejection
# ══════════════════════════════════════════════════════════════════════

def test_7_risk_block_emitted():
    section("TEST 7: RISK_BLOCK Emitted on Rejection")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side
    from monitor.registry_gate import RegistryGate

    with tempfile.TemporaryDirectory() as td:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=10,
                                          registry_path=os.path.join(td, 'reg.json'))
        bus = EventBus()
        gate = RegistryGate(bus=bus, registry=reg)

        risk_blocks = []

        def _on_block(event):
            risk_blocks.append(event.payload)

        bus.subscribe(EventType.RISK_BLOCK, _on_block)

        # Acquire AAPL for pro
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='pro:sr_flip', layer='pro',
            ),
        ))

        # Pop tries — should emit RISK_BLOCK
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=5, price=150.0,
                reason='pop:momentum', layer='pop',
            ),
        ))

        check("T7a: RISK_BLOCK emitted", len(risk_blocks) == 1)
        check("T7b: RISK_BLOCK mentions 'registry'",
              'registry' in risk_blocks[0].reason if risk_blocks else False)
        check("T7c: RISK_BLOCK mentions holder 'pro'",
              'pro' in risk_blocks[0].reason if risk_blocks else False)


# ══════════════════════════════════════════════════════════════════════
# TEST 8: Satellite read-only pre-flight pattern
# ══════════════════════════════════════════════════════════════════════

def test_8_satellite_readonly():
    section("TEST 8: Satellite Read-Only Pre-Flight")

    with tempfile.TemporaryDirectory() as td:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=10,
                                          registry_path=os.path.join(td, 'reg.json'))

        # Simulate Core acquiring AAPL for 'pro'
        reg.try_acquire('AAPL', 'pro')

        # Satellite pre-flight check (read-only)
        # This is what Pro/Pop/Options RiskAdapters now do:
        holder = reg.held_by('AAPL')

        # Pro should see itself as holder — allow
        check("T8a: Pro sees itself as holder",
              holder == 'pro')
        pro_allowed = (holder is None or holder == 'pro')
        check("T8b: Pro pre-flight allows", pro_allowed)

        # Pop should see 'pro' as holder — block
        pop_allowed = (holder is None or holder == 'pop')
        check("T8c: Pop pre-flight blocks", not pop_allowed)

        # Unacquired ticker — anyone can proceed
        holder2 = reg.held_by('MSFT')
        check("T8d: Unacquired ticker returns None", holder2 is None)
        any_allowed = (holder2 is None or holder2 == 'pop')
        check("T8e: Any layer allowed on free ticker", any_allowed)

        # Read-only: registry not modified by held_by()
        check("T8f: Registry unchanged (count=1)", reg.count() == 1)


# ══════════════════════════════════════════════════════════════════════
# TEST 9: OrderRequestPayload layer field
# ══════════════════════════════════════════════════════════════════════

def test_9_payload_layer_field():
    section("TEST 9: OrderRequestPayload Layer Field")
    from monitor.events import OrderRequestPayload, Side

    # With layer
    p1 = OrderRequestPayload(
        ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
        reason='test', layer='pro',
    )
    check("T9a: layer='pro' preserved", p1.layer == 'pro')

    # Without layer (backward compat)
    p2 = OrderRequestPayload(
        ticker='MSFT', side=Side.BUY, qty=5, price=400.0,
        reason='test',
    )
    check("T9b: layer defaults to None", p2.layer is None)

    # Frozen — can't mutate
    try:
        p1.layer = 'pop'
        check("T9c: Frozen — mutation blocked", False, "should have raised")
    except AttributeError:
        check("T9c: Frozen — mutation blocked", True)


# ══════════════════════════════════════════════════════════════════════
# TEST 10: Full lifecycle — acquire → fill → close → re-acquire
# ══════════════════════════════════════════════════════════════════════

def test_10_full_lifecycle():
    section("TEST 10: Full Lifecycle (Acquire → Close → Re-acquire)")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import (OrderRequestPayload, PositionPayload,
                                 Side, PositionAction)
    from monitor.registry_gate import RegistryGate

    with tempfile.TemporaryDirectory() as td:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=10,
                                          registry_path=os.path.join(td, 'reg.json'))
        bus = EventBus()
        gate = RegistryGate(bus=bus, registry=reg)

        # Step 1: Pro acquires AAPL
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='pro:sr_flip:T1:long', layer='pro',
            ),
        ))
        check("T10a: AAPL held by pro", reg.held_by('AAPL') == 'pro')

        # Step 2: Pop blocked
        blocked = []

        def _track(event):
            if gate.is_blocked(event.event_id):
                blocked.append(True)

        bus.subscribe(EventType.ORDER_REQ, _track, priority=0)

        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=5, price=150.0,
                reason='pop:momentum', layer='pop',
            ),
        ))
        check("T10b: Pop blocked while pro holds", len(blocked) == 1)

        # Step 3: Position closes
        bus.emit(Event(
            type=EventType.POSITION,
            payload=PositionPayload(
                ticker='AAPL', action=PositionAction.CLOSED,
                position=None, pnl=50.0,
            ),
        ))
        check("T10c: AAPL released after close", not reg.is_held('AAPL'))

        # Step 4: Pop can now acquire
        blocked.clear()
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=5, price=151.0,
                reason='pop:momentum', layer='pop',
            ),
        ))
        check("T10d: Pop acquires AAPL after close",
              reg.held_by('AAPL') == 'pop')
        check("T10e: Pop not blocked this time", len(blocked) == 0)

        # Step 5: PARTIAL_EXIT does NOT release
        from monitor.events import PositionSnapshot
        bus.emit(Event(
            type=EventType.POSITION,
            payload=PositionPayload(
                ticker='AAPL', action=PositionAction.PARTIAL_EXIT,
                position=PositionSnapshot(
                    entry_price=151.0, entry_time='14:00',
                    quantity=3, partial_done=True,
                    order_id='test', stop_price=149.0,
                    target_price=155.0, half_target=153.0,
                ),
                pnl=10.0,
            ),
        ))
        check("T10f: AAPL still held after PARTIAL_EXIT",
              reg.is_held('AAPL'))


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("  V7 RegistryGate — Centralized Registry Test Suite")
    print("  Testing: acquire, dedup, release, inference, lifecycle")
    print("=" * 60)

    test_1_acquire_and_pass()
    test_2_cross_layer_dedup()
    test_3_position_closed_releases()
    test_4_order_fail_releases()
    test_5_layer_inference()
    test_6_global_max()
    test_7_risk_block_emitted()
    test_8_satellite_readonly()
    test_9_payload_layer_field()
    test_10_full_lifecycle()

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
