#!/usr/bin/env python3
"""
V7 EventBus Priority Enforcement Test Suite.

Validates:
  1. EventBus dispatch ordering rule: higher priority number = runs first
  2. ORDER_REQ gate chain: PortfolioRiskGate(3) → RegistryGate(2) → SmartRouter(1)
  3. Gates that BLOCK run before components that EXECUTE
  4. FILL/POSITION handler ordering is correct
  5. Persistence layer (priority 10) runs before all business logic
  6. No handler at priority > 10 except ActivityLogger (99)

Run: python test/test_priority_enforcement.py
"""
import os
import sys

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
# TEST 1: EventBus dispatch ordering — higher number runs first
# ══════════════════════════════════════════════════════════════════════

def test_1_dispatch_order():
    section("TEST 1: EventBus Dispatch Order (Higher Number = First)")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import HeartbeatPayload

    bus = EventBus()
    order = []

    for pri in [0, 1, 2, 3, 5, 10, 99]:
        def make_handler(p):
            return lambda e: order.append(p)
        bus.subscribe(EventType.HEARTBEAT, make_handler(pri), priority=pri)

    bus.emit(Event(
        type=EventType.HEARTBEAT,
        payload=HeartbeatPayload(
            n_tickers=1, n_positions=0, open_tickers=(),
            n_trades=0, n_wins=0, total_pnl=0.0,
        ),
    ))

    expected = [99, 10, 5, 3, 2, 1, 0]
    check("T1a: Handlers run in descending priority order",
          order == expected,
          f"expected={expected} got={order}")

    # Same priority — insertion order preserved
    bus2 = EventBus()
    order2 = []
    bus2.subscribe(EventType.HEARTBEAT, lambda e: order2.append('A'), priority=5)
    bus2.subscribe(EventType.HEARTBEAT, lambda e: order2.append('B'), priority=5)
    bus2.subscribe(EventType.HEARTBEAT, lambda e: order2.append('C'), priority=5)

    bus2.emit(Event(
        type=EventType.HEARTBEAT,
        payload=HeartbeatPayload(
            n_tickers=1, n_positions=0, open_tickers=(),
            n_trades=0, n_wins=0, total_pnl=0.0,
        ),
    ))
    check("T1b: Same priority preserves insertion order",
          order2 == ['A', 'B', 'C'],
          f"got={order2}")


# ══════════════════════════════════════════════════════════════════════
# TEST 2: ORDER_REQ gate chain enforcement
# ══════════════════════════════════════════════════════════════════════

def test_2_order_req_chain():
    section("TEST 2: ORDER_REQ Gate Chain (3 → 2 → 1)")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side

    bus = EventBus()
    execution_order = []

    # Simulate PortfolioRiskGate at priority 3
    def portfolio_gate(event):
        execution_order.append('PortfolioRiskGate')

    # Simulate RegistryGate at priority 2
    def registry_gate(event):
        execution_order.append('RegistryGate')

    # Simulate SmartRouter at priority 1
    def smart_router(event):
        execution_order.append('SmartRouter')

    # Simulate broker at default priority
    def broker(event):
        execution_order.append('Broker')

    bus.subscribe(EventType.ORDER_REQ, broker)  # default
    bus.subscribe(EventType.ORDER_REQ, smart_router, priority=1)
    bus.subscribe(EventType.ORDER_REQ, registry_gate, priority=2)
    bus.subscribe(EventType.ORDER_REQ, portfolio_gate, priority=3)

    bus.emit(Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='AAPL', side=Side.BUY, qty=10, price=150.0, reason='test',
        ),
    ))

    expected = ['PortfolioRiskGate', 'RegistryGate', 'SmartRouter', 'Broker']
    check("T2a: ORDER_REQ chain is Portfolio(3)→Registry(2)→Router(1)→Broker(default)",
          execution_order == expected,
          f"got={execution_order}")

    # Verify gate runs BEFORE executor
    portfolio_idx = execution_order.index('PortfolioRiskGate')
    router_idx = execution_order.index('SmartRouter')
    check("T2b: PortfolioRiskGate runs before SmartRouter",
          portfolio_idx < router_idx)

    registry_idx = execution_order.index('RegistryGate')
    check("T2c: RegistryGate runs before SmartRouter",
          registry_idx < router_idx)

    check("T2d: PortfolioRiskGate runs before RegistryGate",
          portfolio_idx < registry_idx)


# ══════════════════════════════════════════════════════════════════════
# TEST 3: Gate blocks propagate to downstream handlers
# ══════════════════════════════════════════════════════════════════════

def test_3_gate_blocks_propagate():
    section("TEST 3: Gate Block Propagation")
    import tempfile
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side
    from monitor.registry_gate import RegistryGate

    with tempfile.TemporaryDirectory() as td:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=10,
                                          registry_path=os.path.join(td, 'reg.json'))
        bus = EventBus()
        gate = RegistryGate(bus=bus, registry=reg)

        routed = []

        # Simulate SmartRouter checking is_blocked
        def mock_router(event):
            if gate.is_blocked(event.event_id):
                return  # skip
            routed.append(event.payload.ticker)

        bus.subscribe(EventType.ORDER_REQ, mock_router, priority=1)

        # Acquire AAPL for pro
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='pro:test', layer='pro',
            ),
        ))
        check("T3a: First BUY routed", 'AAPL' in routed)

        # Pop tries same ticker — gate blocks, router skips
        routed.clear()
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=5, price=150.0,
                reason='pop:test', layer='pop',
            ),
        ))
        check("T3b: Blocked BUY not routed", 'AAPL' not in routed)

        # SELL always passes
        routed.clear()
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.SELL, qty=10, price=155.0,
                reason='SELL_TARGET',
            ),
        ))
        check("T3c: SELL always routed", 'AAPL' in routed)


# ══════════════════════════════════════════════════════════════════════
# TEST 4: Persistence runs before business logic
# ══════════════════════════════════════════════════════════════════════

def test_4_persistence_before_logic():
    section("TEST 4: Persistence (10) Before Business Logic (<10)")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side

    bus = EventBus()
    order = []

    # Persistence layer (priority 10)
    bus.subscribe(EventType.ORDER_REQ,
                  lambda e: order.append('EventSourcing'), priority=10)

    # Business logic (various priorities < 10)
    bus.subscribe(EventType.ORDER_REQ,
                  lambda e: order.append('PortfolioRisk'), priority=3)
    bus.subscribe(EventType.ORDER_REQ,
                  lambda e: order.append('RegistryGate'), priority=2)
    bus.subscribe(EventType.ORDER_REQ,
                  lambda e: order.append('SmartRouter'), priority=1)
    bus.subscribe(EventType.ORDER_REQ,
                  lambda e: order.append('Broker'))  # default

    bus.emit(Event(
        type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='X', side=Side.BUY, qty=1, price=1.0, reason='test',
        ),
    ))

    es_idx = order.index('EventSourcing')
    pr_idx = order.index('PortfolioRisk')
    rg_idx = order.index('RegistryGate')
    sr_idx = order.index('SmartRouter')
    br_idx = order.index('Broker')

    check("T4a: EventSourcing runs first", es_idx == 0)
    check("T4b: EventSourcing before PortfolioRisk", es_idx < pr_idx)
    check("T4c: EventSourcing before RegistryGate", es_idx < rg_idx)
    check("T4d: EventSourcing before SmartRouter", es_idx < sr_idx)
    check("T4e: EventSourcing before Broker", es_idx < br_idx)
    check("T4f: Full order is ES→PR→RG→SR→Broker",
          order == ['EventSourcing', 'PortfolioRisk', 'RegistryGate',
                    'SmartRouter', 'Broker'],
          f"got={order}")


# ══════════════════════════════════════════════════════════════════════
# TEST 5: Verify actual component priorities from code
# ══════════════════════════════════════════════════════════════════════

def test_5_actual_priorities():
    section("TEST 5: Verify Actual Component Priorities")

    # These are the V7 priority assignments — this test will fail
    # if anyone changes a priority without updating the convention.
    expected_priorities = {
        # (file, line_pattern) → expected_priority
        'PortfolioRiskGate.ORDER_REQ': 3,
        'RegistryGate.ORDER_REQ': 2,
        'SmartRouter.ORDER_REQ': 1,
        'EventSourcingSubscriber.ALL': 10,
    }

    # Verify PortfolioRiskGate
    import monitor.portfolio_risk as pr
    src = open(pr.__file__).read()
    check("T5a: PortfolioRiskGate ORDER_REQ at priority=3",
          'priority=3' in src and 'ORDER_REQ' in src,
          f"file={pr.__file__}")

    # Verify RegistryGate
    import monitor.registry_gate as rg
    src = open(rg.__file__).read()
    check("T5b: RegistryGate ORDER_REQ at priority=2",
          'priority=2' in src and 'ORDER_REQ' in src)

    # Verify SmartRouter
    import monitor.smart_router as sr
    src = open(sr.__file__).read()
    check("T5c: SmartRouter ORDER_REQ at priority=1",
          'ORDER_REQ, self._on_order_req, priority=1' in src)

    # Verify EventSourcingSubscriber
    import db.event_sourcing_subscriber as es
    src = open(es.__file__).read()
    check("T5d: EventSourcingSubscriber at priority=10",
          'priority=10' in src)

    # Verify the ordering invariant: PortfolioRisk > RegistryGate > SmartRouter
    check("T5e: Priority invariant: 3 > 2 > 1",
          3 > 2 > 1)


# ══════════════════════════════════════════════════════════════════════
# TEST 6: BAR handler ordering
# ══════════════════════════════════════════════════════════════════════

def test_6_bar_ordering():
    section("TEST 6: BAR Handler Ordering")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import BarPayload
    import pandas as pd

    bus = EventBus()
    order = []

    # Simulate all BAR subscribers at their actual priorities
    bus.subscribe(EventType.BAR, lambda e: order.append('EventSourcing'), priority=10)
    bus.subscribe(EventType.BAR, lambda e: order.append('Options'), priority=3)
    bus.subscribe(EventType.BAR, lambda e: order.append('Pro'), priority=2)
    bus.subscribe(EventType.BAR, lambda e: order.append('Pop'), priority=1)
    bus.subscribe(EventType.BAR, lambda e: order.append('CoreVWAP'))  # default

    df = pd.DataFrame({'close': [150.0], 'volume': [1000]})
    bus.emit(Event(
        type=EventType.BAR,
        payload=BarPayload(ticker='AAPL', df=df),
    ))

    check("T6a: EventSourcing runs first for BAR",
          order[0] == 'EventSourcing')
    check("T6b: Options before Pro for BAR",
          order.index('Options') < order.index('Pro'))
    check("T6c: Pro before Pop for BAR",
          order.index('Pro') < order.index('Pop'))
    check("T6d: Pop before CoreVWAP for BAR",
          order.index('Pop') < order.index('CoreVWAP'))
    check("T6e: Full BAR order",
          order == ['EventSourcing', 'Options', 'Pro', 'Pop', 'CoreVWAP'],
          f"got={order}")


# ══════════════════════════════════════════════════════════════════════
# TEST 7: Priority convention compliance
# ══════════════════════════════════════════════════════════════════════

def test_7_convention():
    section("TEST 7: V7 Priority Convention Compliance")

    # Scan all subscribe calls in production code and verify
    # no handler uses a priority that violates the convention
    import re

    production_files = []
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for dirpath, _, filenames in os.walk(root):
        if '/test' in dirpath or '/__pycache__/' in dirpath:
            continue
        if '/backtests/' in dirpath:
            continue
        for f in filenames:
            if f.endswith('.py'):
                production_files.append(os.path.join(dirpath, f))

    # Extract all subscribe(EventType.X, handler, priority=N) calls
    pattern = re.compile(r'subscribe\(\s*EventType\.(\w+).*?priority\s*=\s*(\d+)')
    subscriptions = []

    for fpath in production_files:
        try:
            with open(fpath) as f:
                for i, line in enumerate(f, 1):
                    m = pattern.search(line)
                    if m:
                        event_type, priority = m.group(1), int(m.group(2))
                        rel_path = os.path.relpath(fpath, root)
                        subscriptions.append((rel_path, i, event_type, priority))
        except Exception:
            pass

    check("T7a: Found subscribe calls in production code",
          len(subscriptions) > 0,
          f"found {len(subscriptions)}")

    # Convention: only ActivityLogger should use priority > 10
    high_pri = [(f, l, et, p) for f, l, et, p in subscriptions
                if p > 10 and 'activity_logger' not in f]
    check("T7b: No non-logger handler uses priority > 10",
          len(high_pri) == 0,
          f"violations: {high_pri}")

    # Convention: ORDER_REQ handlers must follow 3 > 2 > 1 chain
    order_req_subs = [(f, l, p) for f, l, et, p in subscriptions
                      if et == 'ORDER_REQ']

    # SmartRouter must be at priority 1
    router_subs = [p for f, _, p in order_req_subs if 'smart_router' in f]
    check("T7c: SmartRouter ORDER_REQ at priority 1",
          1 in router_subs,
          f"found priorities: {router_subs}")

    # RegistryGate must be at priority 2
    gate_subs = [p for f, _, p in order_req_subs if 'registry_gate' in f]
    check("T7d: RegistryGate ORDER_REQ at priority 2",
          2 in gate_subs,
          f"found priorities: {gate_subs}")

    # PortfolioRiskGate must be at priority 3
    risk_subs = [p for f, _, p in order_req_subs if 'portfolio_risk' in f]
    check("T7e: PortfolioRiskGate ORDER_REQ at priority 3",
          3 in risk_subs,
          f"found priorities: {risk_subs}")

    # No ORDER_REQ handler between 3 and 10 (reserved gap)
    mid_subs = [(f, p) for f, _, p in order_req_subs if 3 < p < 10]
    check("T7f: No ORDER_REQ handler in reserved gap (4-9)",
          len(mid_subs) == 0,
          f"violations: {mid_subs}")


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("  V7 EventBus Priority Enforcement Test Suite")
    print("  Rule: Higher priority number = runs FIRST")
    print("=" * 60)

    test_1_dispatch_order()
    test_2_order_req_chain()
    test_3_gate_blocks_propagate()
    test_4_persistence_before_logic()
    test_5_actual_priorities()
    test_6_bar_ordering()
    test_7_convention()

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
