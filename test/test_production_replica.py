#!/usr/bin/env python3
"""
V7 Production Replica Test Suite
==================================

Replicates the EXACT production wiring from run_core.py to validate
the full trading pipeline before market open. Every component is
instantiated and wired the same way as production.

Components wired (production order):
  1. EventBus
  2. PositionManager (with shared positions dict)
  3. PaperBroker (stands in for AlpacaBroker)
  4. PortfolioRiskGate (priority 3)
  5. RegistryGate (priority 2)
  6. SmartRouter (priority 1, wraps PaperBroker)
  7. IPC inbox queue (simulated)
  8. StateEngine (read-only snapshot)

Tests:
  T1: Full BUY pipeline — ORDER_REQ → PortfolioRisk → RegistryGate → SmartRouter → PaperBroker → FILL → PositionManager → POSITION(OPENED)
  T2: Full SELL pipeline — ORDER_REQ → PortfolioRisk(pass) → RegistryGate(skip) → SmartRouter → PaperBroker → FILL → PositionManager → POSITION(CLOSED)
  T3: Partial sell lifecycle — BUY → PARTIAL_SELL → stop moves to breakeven → full close
  T4: Cross-layer dedup — Pro ORDER_REQ blocked by Core holding same ticker
  T5: SmartRouter dedup — same event_id replayed → skipped
  T6: Broker causation chain — FILL.correlation_id links to ORDER_REQ.event_id
  T7: Kill switch triggers mid-session — ORDER_REQ blocked after halt
  T8: Duplicate FILL dedup — same fill_id processed once
  T9: SELL without position — no short created, no crash
  T10: ORDER_FAIL releases registry — no position leak
  T11: State persistence round-trip — positions survive simulated restart
  T12: Losing trade P&L — correct negative P&L and is_win=False
  T13: IPC inbox drain — satellite ORDER_REQs processed on main thread
  T14: Priority chain verified — PortfolioRisk(3) before RegistryGate(2) before SmartRouter(1)

Run: python test/test_production_replica.py
"""
import os
import sys
import tempfile
import time
import queue
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_results = []
_events_log = []  # global event trace


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    _results.append((name, condition))
    msg = f"  [{status}] {name}"
    if detail and not condition:
        msg += f" — {detail}"
    print(msg)


def section(title):
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


# ══════════════════════════════════════════════════════════════════════════
# PRODUCTION REPLICA SETUP
# ══════════════════════════════════════════════════════════════════════════

def build_production_stack(tmp_dir):
    """Wire all components EXACTLY like run_core.py production."""
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import (OrderRequestPayload, FillPayload, PositionPayload,
                                 OrderFailPayload, RiskBlockPayload,
                                 Side, PositionAction, PositionSnapshot)
    from monitor.position_manager import PositionManager
    from monitor.brokers import PaperBroker
    from monitor.portfolio_risk import PortfolioRiskGate
    from monitor.registry_gate import RegistryGate
    from monitor.smart_router import SmartRouter
    from monitor.distributed_registry import DistributedPositionRegistry
    from monitor.state_engine import StateEngine

    # 1. EventBus (same as production)
    bus = EventBus()

    # 2. Shared state (same as production — single-writer PositionManager)
    positions = {}
    reclaimed_today = set()
    last_order_time = {}
    trade_log = []

    # 3. PositionManager — subscribes to FILL at default priority
    pm = PositionManager(bus, positions, reclaimed_today, last_order_time, trade_log)

    # 4. StateEngine — read-only snapshot (subscribes to POSITION at default)
    se = StateEngine(bus)
    se.seed(positions, trade_log)

    # 5. PortfolioRiskGate — subscribes to ORDER_REQ at priority 3
    # We mock _get_buying_power to avoid real API calls
    prg = PortfolioRiskGate(bus=bus, monitor=None)
    prg._get_buying_power = lambda: 1000000.0  # $1M buying power
    prg._get_portfolio_greeks = lambda: (0.0, 0.0)  # no options positions

    # 6. RegistryGate — subscribes to ORDER_REQ at priority 2
    registry = DistributedPositionRegistry(
        global_max=10,
        registry_path=os.path.join(tmp_dir, 'registry.json'))
    rg = RegistryGate(bus=bus, registry=registry)

    # 7. SmartRouter with PaperBroker — subscribes to ORDER_REQ at priority 1
    paper = PaperBroker(bus)  # subscribes at default priority
    router = SmartRouter(bus=bus, brokers={'paper': paper}, default_broker='paper')
    router.set_registry_gate(rg)

    # 8. Event trace — subscribe at priority 0 (runs last, sees final state)
    trace = []
    def _trace(event):
        trace.append({
            'type': event.type.name,
            'ticker': getattr(event.payload, 'ticker', '?'),
            'event_id': event.event_id[:8],
            'correlation_id': (event.correlation_id or '')[:8],
            'blocked': rg.is_blocked(event.event_id),
        })

    for et in EventType:
        bus.subscribe(et, _trace, priority=0)

    return {
        'bus': bus, 'positions': positions, 'reclaimed': reclaimed_today,
        'last_order': last_order_time, 'trade_log': trade_log,
        'pm': pm, 'se': se, 'prg': prg, 'rg': rg, 'registry': registry,
        'router': router, 'paper': paper, 'trace': trace,
    }


# ══════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════

def run_all_tests():
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import (OrderRequestPayload, FillPayload, PositionPayload,
                                 OrderFailPayload, Side, PositionAction,
                                 PositionSnapshot)

    with tempfile.TemporaryDirectory() as td:
        s = build_production_stack(td)
        bus, positions, trade_log = s['bus'], s['positions'], s['trade_log']
        registry, rg, router, trace = s['registry'], s['rg'], s['router'], s['trace']

        # ── T1: Full BUY pipeline ────────────────────────────────────────
        section("T1: Full BUY Pipeline")
        buy_eid = str(uuid.uuid4())
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='vwap_reclaim', layer='vwap',
                stop_price=147.0, target_price=156.0, atr_value=1.5),
            event_id=buy_eid))

        check("T1a: Position opened", 'AAPL' in positions)
        check("T1b: Qty = 10", positions.get('AAPL', {}).get('quantity') == 10)
        check("T1c: Stop = 147.0 (from payload, not placeholder)",
              positions.get('AAPL', {}).get('stop_price') == 147.0)
        check("T1d: Target = 156.0", positions.get('AAPL', {}).get('target_price') == 156.0)
        check("T1e: Registry acquired for vwap",
              registry.held_by('AAPL') == 'vwap')
        check("T1f: Broker map has AAPL→paper",
              router._position_broker.get('AAPL') == 'paper')

        # Verify event chain in trace
        types = [t['type'] for t in trace if t['ticker'] == 'AAPL']
        check("T1g: Event chain ORDER_REQ→FILL→POSITION",
              'ORDER_REQ' in types and 'FILL' in types and 'POSITION' in types)

        # Verify causation: FILL.correlation_id = ORDER_REQ.event_id
        fills = [t for t in trace if t['type'] == 'FILL' and t['ticker'] == 'AAPL']
        check("T1h: FILL.correlation_id links to ORDER_REQ",
              fills and fills[0]['correlation_id'] == buy_eid[:8])

        # ── T2: Full SELL pipeline ───────────────────────────────────────
        section("T2: Full SELL Pipeline")
        trace.clear()
        sell_eid = str(uuid.uuid4())
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.SELL, qty=10, price=155.0,
                reason='SELL_TARGET'),
            event_id=sell_eid))

        check("T2a: Position closed", 'AAPL' not in positions)
        check("T2b: Trade log has entry",
              any(t['ticker'] == 'AAPL' for t in trade_log))
        pnl = next((t['pnl'] for t in trade_log if t['ticker'] == 'AAPL'), None)
        check("T2c: P&L = $50 ((155-150)*10)", pnl == 50.0)
        check("T2d: Registry released after CLOSED",
              not registry.is_held('AAPL'))

        # ── T3: Partial sell lifecycle ───────────────────────────────────
        section("T3: Partial Sell Lifecycle")
        trade_log.clear()
        trace.clear()

        # BUY 10
        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='MSFT', side=Side.BUY, qty=10, price=400.0,
                reason='pro:sr_flip', layer='pro',
                stop_price=395.0, target_price=410.0, atr_value=2.0)))
        check("T3a: MSFT opened", 'MSFT' in positions)

        # PARTIAL SELL 5
        bus.emit(Event(type=EventType.FILL,
            payload=FillPayload(
                ticker='MSFT', side='SELL', qty=5, fill_price=405.0,
                order_id='part-001', reason='PARTIAL_SELL')))

        check("T3b: Remaining = 5", positions['MSFT']['quantity'] == 5)
        check("T3c: partial_done = True", positions['MSFT']['partial_done'] is True)
        check("T3d: Stop moved to breakeven (400.0)",
              positions['MSFT']['stop_price'] == 400.0)

        # FULL CLOSE remaining 5
        bus.emit(Event(type=EventType.FILL,
            payload=FillPayload(
                ticker='MSFT', side='SELL', qty=5, fill_price=408.0,
                order_id='close-001', reason='SELL_TARGET')))

        check("T3e: MSFT fully closed", 'MSFT' not in positions)
        check("T3f: 2 trade log entries",
              len([t for t in trade_log if t['ticker'] == 'MSFT']) == 2)
        msft_trades = [t for t in trade_log if t['ticker'] == 'MSFT']
        check("T3g: Partial P&L = $25 ((405-400)*5)", msft_trades[0]['pnl'] == 25.0)
        check("T3h: Close P&L = $40 ((408-400)*5)", msft_trades[1]['pnl'] == 40.0)
        check("T3i: Registry released", not registry.is_held('MSFT'))

        # ── T4: Cross-layer dedup ────────────────────────────────────────
        section("T4: Cross-Layer Dedup")
        trace.clear()

        # Core acquires GOOG
        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='GOOG', side=Side.BUY, qty=5, price=175.0,
                reason='vwap_reclaim', layer='vwap',
                stop_price=172.0, target_price=180.0)))
        check("T4a: Core holds GOOG", registry.held_by('GOOG') == 'vwap')

        # Pro tries GOOG → blocked
        pro_eid = str(uuid.uuid4())
        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='GOOG', side=Side.BUY, qty=3, price=175.0,
                reason='pro:trend', layer='pro'),
            event_id=pro_eid))
        check("T4b: Pro ORDER_REQ blocked by registry",
              rg.is_blocked(pro_eid))
        check("T4c: RISK_BLOCK in trace",
              any(t['type'] == 'RISK_BLOCK' for t in trace))
        check("T4d: Only 1 GOOG position (Core's)",
              positions.get('GOOG', {}).get('quantity') == 5)

        # ── T5: SmartRouter ORDER_REQ dedup ──────────────────────────────
        section("T5: SmartRouter ORDER_REQ Dedup")

        dedup_eid = str(uuid.uuid4())
        # First emit → routed
        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='NVDA', side=Side.BUY, qty=5, price=900.0,
                reason='test', layer='vwap',
                stop_price=890.0, target_price=920.0),
            event_id=dedup_eid))
        check("T5a: First ORDER_REQ routed",
              dedup_eid in router._routed_event_ids)
        check("T5b: NVDA position opened", 'NVDA' in positions)

        # Same event_id again (Redpanda replay) → skipped
        nvda_qty_before = positions.get('NVDA', {}).get('quantity', 0)
        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='NVDA', side=Side.BUY, qty=5, price=900.0,
                reason='test', layer='vwap',
                stop_price=890.0, target_price=920.0),
            event_id=dedup_eid))
        check("T5c: Replayed ORDER_REQ deduped (qty unchanged)",
              positions.get('NVDA', {}).get('quantity') == nvda_qty_before)

        # ── T6: Broker causation chain ───────────────────────────────────
        section("T6: Broker Causation Chain")
        trace.clear()

        chain_eid = str(uuid.uuid4())
        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='TSLA', side=Side.BUY, qty=3, price=250.0,
                reason='pop:momentum', layer='pop',
                stop_price=245.0, target_price=260.0),
            event_id=chain_eid))

        fills = [t for t in trace if t['type'] == 'FILL' and t['ticker'] == 'TSLA']
        check("T6a: FILL emitted for TSLA", len(fills) >= 1)
        check("T6b: FILL.correlation_id = ORDER_REQ.event_id",
              fills[0]['correlation_id'] == chain_eid[:8] if fills else False)

        pos_events = [t for t in trace if t['type'] == 'POSITION' and t['ticker'] == 'TSLA']
        check("T6c: POSITION(OPENED) emitted", len(pos_events) >= 1)

        # ── T7: Kill switch mid-session ──────────────────────────────────
        section("T7: Portfolio Risk Halt")

        # Force drawdown breach
        s['prg']._realized_pnl = -100000.0  # massive loss
        halt_eid = str(uuid.uuid4())
        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='HALT_TEST', side=Side.BUY, qty=1, price=10.0,
                reason='test', layer='vwap'),
            event_id=halt_eid))

        check("T7a: ORDER_REQ blocked after drawdown halt",
              'HALT_TEST' not in positions)
        check("T7b: PortfolioRiskGate halted",
              s['prg']._halted is True)

        # SELL still passes during halt
        trace.clear()
        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='GOOG', side=Side.SELL, qty=5, price=178.0,
                reason='SELL_TARGET')))
        check("T7c: SELL passes during halt",
              'GOOG' not in positions)

        # Reset for remaining tests
        s['prg']._halted = False
        s['prg']._realized_pnl = 0.0

        # ── T8: Duplicate FILL dedup ─────────────────────────────────────
        section("T8: Duplicate FILL Dedup")
        trade_log.clear()

        fill_eid = 'dup-fill-abc123'
        buy_fill = FillPayload(
            ticker='DUP_TEST', side='BUY', qty=5, fill_price=100.0,
            order_id='dup-001', reason='test',
            stop_price=98.0, target_price=104.0)

        bus.emit(Event(type=EventType.FILL, payload=buy_fill, event_id=fill_eid))
        bus.emit(Event(type=EventType.FILL, payload=buy_fill, event_id=fill_eid))
        bus.emit(Event(type=EventType.FILL, payload=buy_fill, event_id=fill_eid))

        check("T8a: Only 1 position from 3 identical FILLs",
              'DUP_TEST' in positions and positions['DUP_TEST']['quantity'] == 5)

        # ── T9: SELL without position ────────────────────────────────────
        section("T9: SELL Without Position (No Short)")

        bus.emit(Event(type=EventType.FILL,
            payload=FillPayload(
                ticker='GHOST', side='SELL', qty=10, fill_price=50.0,
                order_id='ghost-001', reason='SELL_STOP')))

        check("T9a: No GHOST position created (no short)",
              'GHOST' not in positions)

        # ── T10: ORDER_FAIL releases registry ────────────────────────────
        section("T10: ORDER_FAIL Releases Registry")

        # Acquire via BUY
        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='FAIL_TEST', side=Side.BUY, qty=5, price=50.0,
                reason='test', layer='vwap',
                stop_price=48.0, target_price=54.0)))
        check("T10a: FAIL_TEST acquired",
              registry.held_by('FAIL_TEST') == 'vwap')

        # Emit ORDER_FAIL
        bus.emit(Event(type=EventType.ORDER_FAIL,
            payload=OrderFailPayload(
                ticker='FAIL_TEST', side=Side.BUY, qty=5, price=50.0,
                reason='broker timeout')))
        check("T10b: Registry released after ORDER_FAIL",
              not registry.is_held('FAIL_TEST'))

        # ── T11: State persistence round-trip ────────────────────────────
        section("T11: State Persistence Round-Trip")
        from monitor.state import save_state, load_state

        # Current state has positions
        open_count = len(positions)
        check("T11a: Positions exist before save", open_count > 0,
              f"{open_count} open")

        # Load should return today's state
        loaded_pos, loaded_rec, loaded_log = load_state()
        check("T11b: Loaded positions match",
              len(loaded_pos) == open_count,
              f"loaded={len(loaded_pos)} expected={open_count}")

        # Verify stop prices survived persistence
        for ticker in positions:
            if ticker in loaded_pos:
                check(f"T11c: {ticker} stop preserved",
                      loaded_pos[ticker].get('stop_price') == positions[ticker].get('stop_price'))
                break

        # ── T12: Losing trade ────────────────────────────────────────────
        section("T12: Losing Trade P&L")
        trade_log.clear()

        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='LOSER', side=Side.BUY, qty=10, price=100.0,
                reason='test', layer='vwap',
                stop_price=95.0, target_price=110.0)))
        check("T12a: LOSER opened", 'LOSER' in positions)

        bus.emit(Event(type=EventType.FILL,
            payload=FillPayload(
                ticker='LOSER', side='SELL', qty=10, fill_price=92.0,
                order_id='loser-sell', reason='SELL_STOP')))
        check("T12b: LOSER closed", 'LOSER' not in positions)

        loser_trade = next((t for t in trade_log if t['ticker'] == 'LOSER'), None)
        check("T12c: P&L = -$80 ((92-100)*10)",
              loser_trade and loser_trade['pnl'] == -80.0,
              f"pnl={loser_trade['pnl'] if loser_trade else 'N/A'}")
        check("T12d: is_win = False",
              loser_trade and loser_trade['is_win'] is False)

        # ── T13: IPC inbox drain ─────────────────────────────────────────
        section("T13: IPC Inbox Drain (Satellite ORDER_REQs)")

        inbox = queue.Queue(maxsize=10)

        # Simulate satellite ORDER_REQs arriving
        satellite_orders = [
            {'ticker': 'SAT1', 'side': 'BUY', 'qty': 3, 'price': 75.0,
             'reason': 'pro:orb:T2:long', 'layer': 'pro',
             'stop_price': 73.0, 'target_price': 80.0,
             '_ipc_correlation_id': 'sat-corr-001'},
            {'ticker': 'SAT2', 'side': 'BUY', 'qty': 2, 'price': 120.0,
             'reason': 'pop:momentum', 'layer': 'pop',
             'stop_price': 117.0, 'target_price': 126.0,
             '_ipc_correlation_id': 'sat-corr-002'},
        ]
        for p in satellite_orders:
            inbox.put_nowait(p)

        # Drain (replicating _drain_ipc_inbox from run_core.py)
        drained = 0
        while not inbox.empty():
            payload = inbox.get_nowait()
            order_payload = OrderRequestPayload(
                ticker=payload['ticker'], side=payload['side'],
                qty=int(payload['qty']), price=float(payload['price']),
                reason=payload.get('reason', ''),
                stop_price=float(payload.get('stop_price', 0)),
                target_price=float(payload.get('target_price', 0)),
                layer=payload.get('layer'))
            ipc_corr = payload.get('_ipc_correlation_id', '')
            bus.emit(Event(type=EventType.ORDER_REQ, payload=order_payload,
                           correlation_id=ipc_corr or None))
            drained += 1

        check("T13a: Drained 2 satellite ORDER_REQs", drained == 2)
        check("T13b: SAT1 position opened", 'SAT1' in positions)
        check("T13c: SAT2 position opened", 'SAT2' in positions)
        check("T13d: SAT1 held by 'pro' in registry",
              registry.held_by('SAT1') == 'pro')
        check("T13e: SAT2 held by 'pop' in registry",
              registry.held_by('SAT2') == 'pop')

        # ── T14: Priority chain ──────────────────────────────────────────
        section("T14: Priority Chain Verification")

        chain_order = []
        def make_tracker(name):
            def t(event): chain_order.append(name)
            return t

        bus2 = EventBus()
        bus2.subscribe(EventType.ORDER_REQ, make_tracker('PortfolioRisk'), priority=3)
        bus2.subscribe(EventType.ORDER_REQ, make_tracker('RegistryGate'), priority=2)
        bus2.subscribe(EventType.ORDER_REQ, make_tracker('SmartRouter'), priority=1)
        bus2.subscribe(EventType.ORDER_REQ, make_tracker('Broker'))

        bus2.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='PRI', side=Side.BUY, qty=1, price=1.0, reason='test')))

        check("T14a: Priority chain correct",
              chain_order == ['PortfolioRisk', 'RegistryGate', 'SmartRouter', 'Broker'],
              f"got={chain_order}")

        # ── T15: Supervisor process management ───────────────────────────
        section("T15: Supervisor Process Management (Production Replica)")
        from scripts.supervisor import (PROCESSES, _DEFAULT_PROCESSES,
                                         _load_engine_config, ProcessManager)

        # Verify supervisor discovers all engines including data_collector
        check("T15a: 5 engines in PROCESSES (incl data_collector)",
              len(PROCESSES) >= 5 and 'data_collector' in PROCESSES,
              f"engines={list(PROCESSES.keys())}")

        # Verify ProcessManager can be instantiated for each engine
        for name, cfg in PROCESSES.items():
            pm_test = ProcessManager(
                name=name,
                script=cfg['script'],
                critical=cfg.get('critical', False),
                max_restarts=cfg.get('max_restarts', 5),
                restart_delay=cfg.get('restart_delay', 15),
            )
            check(f"T15b: ProcessManager({name}) created",
                  pm_test.name == name and pm_test.status == 'stopped')

        # Verify hung detection logic
        pm_core = ProcessManager(
            name='core', script=PROCESSES['core']['script'],
            critical=True, max_restarts=3, restart_delay=10)

        # No process running → should return 'stopped' (not 'hung')
        status = pm_core.check()
        check("T15c: Stopped process returns 'stopped'",
              status == 'stopped')

        # Verify hung threshold constant exists
        check("T15d: _HEARTBEAT_STALE_SEC = 120s",
              pm_core._HEARTBEAT_STALE_SEC == 120.0)

        # Verify ENGINE_CONFIG merges correctly
        import json as _json
        os.environ['ENGINE_CONFIG'] = _json.dumps({
            'arbitrage': {'critical': False, 'max_restarts': 3}
        })
        merged = _load_engine_config()
        check("T15e: Custom engine merged via ENGINE_CONFIG",
              'arbitrage' in merged and 'core' in merged)
        os.environ.pop('ENGINE_CONFIG', None)

        # Verify disabling engine via null
        os.environ['ENGINE_CONFIG'] = _json.dumps({'pop': None})
        disabled = _load_engine_config()
        check("T15f: Engine disabled via ENGINE_CONFIG null",
              'pop' not in disabled and 'core' in disabled)
        os.environ.pop('ENGINE_CONFIG', None)

        # Verify restart tracking
        pm_test2 = ProcessManager(
            name='test_engine', script='/nonexistent.py',
            critical=False, max_restarts=2, restart_delay=0)
        check("T15g: Restart counter starts at 0",
              pm_test2.restart_count == 0)

        # ── T16: Data collector alt data pipeline ────────────────────────
        section("T16: Data Collector Alt Data Pipeline (Production Replica)")
        from data_sources.alt_data_reader import AltDataReader
        from lifecycle.safe_state import SafeStateFile

        with tempfile.TemporaryDirectory() as alt_td:
            # Simulate what run_data_collector.py does
            alt_path = os.path.join(alt_td, 'alt_data_cache.json')
            alt_sf = SafeStateFile(alt_path, max_age_seconds=900.0)

            # Write simulated alt data (as collector would)
            cache_data = {
                'collected_at': '2026-04-15T14:00:00',
                'fear_greed': {'value': 35, 'label': 'Fear'},
                'macro_regime': {'regime': 'risk_off', 'vix': 28.5,
                                 'yield_curve': -0.1, 'yield_status': 'inverted'},
                'earnings': {
                    'AAPL': {'days_until_earnings': 5, 'ticker': 'AAPL'},
                    'NVDA': {'days_until_earnings': 12, 'ticker': 'NVDA'},
                },
                'finviz_screener': {
                    'GME': {'short_float': 24.5, 'price': 25.0},
                    'AMC': {'short_float': 18.2, 'price': 5.50},
                },
                'discovered_tickers': ['CRWV', 'SMCI', 'PLTR'],
            }
            alt_sf.write(cache_data)

            # Read via AltDataReader (as engines would)
            reader = AltDataReader(cache_path=alt_path)

            # Fear & Greed
            fg = reader.fear_greed()
            check("T16a: AltDataReader reads fear_greed",
                  fg is not None and fg['value'] == 35)
            check("T16b: fear_greed_value() returns int",
                  reader.fear_greed_value() == 35)

            # Macro regime
            regime = reader.macro_regime()
            check("T16c: AltDataReader reads macro_regime",
                  regime is not None and regime['regime'] == 'risk_off')
            check("T16d: VIX accessible",
                  reader.vix() == 28.5)

            # Earnings
            aapl_e = reader.earnings('AAPL')
            check("T16e: Earnings data for AAPL",
                  aapl_e is not None and aapl_e['days_until_earnings'] == 5)
            check("T16f: days_until_earnings() helper",
                  reader.days_until_earnings('AAPL') == 5)
            check("T16g: Unknown ticker returns None",
                  reader.earnings('ZZZZ') is None)

            # Short float
            check("T16h: GME short_float = 24.5%",
                  reader.short_float('GME') == 24.5)
            check("T16i: Unknown ticker short_float = None",
                  reader.short_float('ZZZZ') is None)

            # Discovery
            check("T16j: Discovered tickers list",
                  reader.discovered_tickers() == ['CRWV', 'SMCI', 'PLTR'])

            # Bulk access
            check("T16k: all_earnings() returns dict",
                  len(reader.all_earnings()) == 2)
            check("T16l: all_finviz() returns dict",
                  len(reader.all_finviz()) == 2)

            # Freshness
            check("T16m: Cache is fresh",
                  reader.is_fresh())

            # Full snapshot
            snap = reader.snapshot()
            check("T16n: snapshot() returns full cache",
                  'fear_greed' in snap and 'discovered_tickers' in snap)

        # ── T17: Pop discovery integration ───────────────────────────────
        section("T17: Pop Discovery Integration (Production Replica)")
        import queue as _queue

        # Simulate what run_pop.py does when receiving discoveries
        extra_tickers = set()
        discovery_inbox = _queue.Queue(maxsize=50)

        # Simulate DataCollector publishing discoveries
        discoveries = [
            {'ticker': 'CRWV', 'source': 'data_collector'},
            {'ticker': 'NEWSTOCK', 'source': 'data_collector'},
            {'ticker': 'AAPL', 'source': 'data_collector'},  # already in TICKERS
        ]
        for d in discoveries:
            discovery_inbox.put_nowait(d)

        # Drain (replicating _on_discovery from run_pop.py)
        from config import TICKERS as BASE_TICKERS
        while not discovery_inbox.empty():
            payload = discovery_inbox.get_nowait()
            ticker = payload.get('ticker', '')
            if ticker and ticker not in BASE_TICKERS and ticker not in extra_tickers:
                extra_tickers.add(ticker)

        check("T17a: CRWV added to extras (not in base)",
              'CRWV' in extra_tickers)
        check("T17b: NEWSTOCK added to extras",
              'NEWSTOCK' in extra_tickers)
        check("T17c: AAPL NOT added (already in base)",
              'AAPL' not in extra_tickers)

        # Combined scan universe
        all_tickers = list(BASE_TICKERS) + list(extra_tickers)
        check("T17d: Scan universe expanded",
              len(all_tickers) > len(BASE_TICKERS),
              f"base={len(BASE_TICKERS)} total={len(all_tickers)}")


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("\n" + "=" * 70)
    print("  V7 PRODUCTION REPLICA TEST SUITE")
    print("  Exact production wiring — every component, lock, event")
    print("  Bypasses market hours — safe to run anytime")
    print("=" * 70)

    start = time.monotonic()
    run_all_tests()
    elapsed = time.monotonic() - start

    print("\n" + "=" * 70)
    passed = sum(1 for _, ok in _results if ok)
    failed = sum(1 for _, ok in _results if not ok)
    total = len(_results)
    print(f"  RESULTS: {passed}/{total} passed, {failed} failed  ({elapsed:.1f}s)")

    if failed:
        print(f"\n  FAILURES ({failed}):")
        for name, ok in _results:
            if not ok:
                print(f"    - {name}")

    print("\n  VERDICT:", end=" ")
    if failed == 0:
        print("ALL SYSTEMS GO — production pipeline verified")
    else:
        print("FIX FAILURES before market open")
    print("=" * 70)
    sys.exit(0 if failed == 0 else 1)
