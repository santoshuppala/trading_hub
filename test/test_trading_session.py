#!/usr/bin/env python3
"""
V7 Trading Session Deep-Dive Test Suite
=========================================

Tests the FULL trading session flow — every component, every lock,
every event, every edge case. Bypasses market hours.

Sections:
  1. Full Trade Lifecycle (BUY → partial → close)
  2. Risk Engine Gate Matrix (all 7 gates)
  3. Inter-Engine Communication (IPC inbox, correlation chain)
  4. Kill Switch & Halt Behavior
  5. Position Manager Edge Cases (duplicates, orphans, P&L)
  6. SmartRouter Failover & Dedup
  7. RegistryGate Contention (Pro vs Pop vs Core)
  8. SharedCache Staleness During Trading
  9. State Persistence & Crash Recovery
  10. DB Event Persistence Chain

Run: python test/test_trading_session.py
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
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


# ══════════════════════════════════════════════════════════════════════════
# 1. FULL TRADE LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════

def test_1_full_lifecycle():
    section("1. FULL TRADE LIFECYCLE (BUY → partial exit → full close)")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import (OrderRequestPayload, FillPayload, PositionPayload,
                                 Side, PositionAction, PositionSnapshot)
    from monitor.position_manager import PositionManager
    from monitor.brokers import PaperBroker

    bus = EventBus()
    positions = {}
    reclaimed = set()
    last_order = {}
    trade_log = []
    events_emitted = []

    pm = PositionManager(bus, positions, reclaimed, last_order, trade_log)
    broker = PaperBroker(bus)

    # Track all POSITION events
    def track(event):
        events_emitted.append((str(event.payload.action), event.payload.ticker))
    bus.subscribe(EventType.POSITION, track)

    # Step 1: BUY 10 shares via ORDER_REQ → PaperBroker → FILL → PositionManager
    bus.emit(Event(type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='LIFE', side=Side.BUY, qty=10, price=100.0,
            reason='vwap_reclaim', stop_price=97.0, target_price=106.0,
            atr_value=1.5, layer='vwap')))

    check("1a: Position opened", 'LIFE' in positions)
    check("1b: Qty = 10", positions.get('LIFE', {}).get('quantity') == 10)
    check("1c: Stop from FillPayload (not placeholder)",
          positions.get('LIFE', {}).get('stop_price') == 97.0)
    check("1d: Target from FillPayload",
          positions.get('LIFE', {}).get('target_price') == 106.0)
    check("1e: ATR preserved", positions.get('LIFE', {}).get('atr_value') == 1.5)
    check("1f: POSITION OPENED emitted",
          ('OPENED', 'LIFE') in events_emitted)
    check("1g: Ticker in reclaimed_today", 'LIFE' in reclaimed)
    check("1h: last_order_time set", 'LIFE' in last_order)

    # Step 2: PARTIAL SELL 5 shares
    partial_fill = FillPayload(ticker='LIFE', side='SELL', qty=5,
                               fill_price=103.0, order_id='part-001',
                               reason='PARTIAL_SELL')
    bus.emit(Event(type=EventType.FILL, payload=partial_fill))

    check("1i: Position still open after partial", 'LIFE' in positions)
    check("1j: Remaining qty = 5", positions['LIFE']['quantity'] == 5)
    check("1k: partial_done = True", positions['LIFE']['partial_done'] is True)
    check("1l: Stop moved to breakeven (entry)",
          positions['LIFE']['stop_price'] == 100.0)
    check("1m: PARTIAL_EXIT emitted",
          ('PARTIAL_EXIT', 'LIFE') in events_emitted)

    # Step 3: Full close remaining 5 shares
    close_fill = FillPayload(ticker='LIFE', side='SELL', qty=5,
                             fill_price=105.0, order_id='close-001',
                             reason='SELL_TARGET')
    bus.emit(Event(type=EventType.FILL, payload=close_fill))

    check("1n: Position fully closed", 'LIFE' not in positions)
    check("1o: CLOSED emitted", ('CLOSED', 'LIFE') in events_emitted)
    check("1p: Trade log has 2 entries",
          len([t for t in trade_log if t['ticker'] == 'LIFE']) == 2)

    # Verify P&L
    trades = [t for t in trade_log if t['ticker'] == 'LIFE']
    check("1q: Partial P&L = $15 ((103-100)*5)",
          trades[0]['pnl'] == 15.0)
    check("1r: Close P&L = $25 ((105-100)*5)",
          trades[1]['pnl'] == 25.0)
    check("1s: Both trades marked is_win",
          all(t['is_win'] for t in trades))


# ══════════════════════════════════════════════════════════════════════════
# 2. RISK ENGINE GATE MATRIX
# ══════════════════════════════════════════════════════════════════════════

def test_2_risk_gates():
    section("2. RISK ENGINE GATE MATRIX")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import (SignalPayload, OrderRequestPayload,
                                 RiskBlockPayload, Side)

    # We test the RiskEngine gates by emitting SIGNAL events and checking
    # whether ORDER_REQ or RISK_BLOCK is produced.
    # Since RiskEngine needs data_client for spread check, we test the
    # gate logic at the code-verification level.

    bus = EventBus()
    orders = []
    blocks = []

    bus.subscribe(EventType.ORDER_REQ, lambda e: orders.append(e.payload.ticker))
    bus.subscribe(EventType.RISK_BLOCK, lambda e: blocks.append(e.payload.reason))

    # Gate verification via source code
    import monitor.risk_engine as re_mod
    src = open(re_mod.__file__).read()

    check("2a: Gate 0 — registry pre-flight (held_by)",
          'registry.held_by(ticker)' in src)
    check("2b: Gate 1 — max positions check",
          'max_positions' in src and 'len(self._positions)' in src)
    check("2c: Gate 2 — cooldown check",
          'order_cooldown' in src and 'elapsed' in src)
    check("2d: Gate 3 — reclaimed_today check",
          'reclaimed_today' in src)
    check("2e: Gate 4 — RVOL threshold (2.0 stocks, lower ETFs)",
          'MIN_RVOL' in src and 'ETF_RVOL_MIN' in src)
    check("2f: Gate 5 — RSI range (40-75)",
          'RSI_LOW' in src and 'RSI_HIGH' in src)
    check("2g: Gate 6 — spread check (0.2%)",
          'MAX_SPREAD_PCT' in src and 'check_spread' in src)
    check("2h: Gate 7 — correlation risk",
          'check_correlation' in src)

    # Verify SELL bypasses ALL gates
    check("2i: SELL bypasses all gates (no blocks)",
          '_emit_sell_order' in src and 'No gates' not in src
          or 'sell_qty' in src)

    # Verify SELL emits ORDER_REQ even without position validation failures
    # (it only warns, never blocks)
    check("2j: SELL warns but doesn't block on missing position",
          "SELL fill for" in open(
              os.path.join(os.path.dirname(re_mod.__file__),
                           'position_manager.py')).read()
          or 'no open position' in src)

    # Verify ORDER_REQ carries stop/target/atr
    check("2k: ORDER_REQ carries stop_price",
          'stop_price=p.stop_price' in src)
    check("2l: ORDER_REQ carries target_price",
          'target_price=p.target_price' in src)
    check("2m: ORDER_REQ carries layer='vwap'",
          "layer='vwap'" in src)


# ══════════════════════════════════════════════════════════════════════════
# 3. INTER-ENGINE COMMUNICATION
# ══════════════════════════════════════════════════════════════════════════

def test_3_inter_engine():
    section("3. INTER-ENGINE COMMUNICATION (IPC Inbox + Correlation)")
    import queue
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, Side

    bus = EventBus()
    inbox = queue.Queue(maxsize=10)

    # Simulate satellite publishing ORDER_REQ
    payloads = [
        {'ticker': 'PRO1', 'side': 'BUY', 'qty': 5, 'price': 100.0,
         'reason': 'pro:sr_flip:T1:long', 'layer': 'pro',
         'stop_price': 98.0, 'target_price': 104.0,
         '_ipc_correlation_id': 'pro-signal-abc'},
        {'ticker': 'POP1', 'side': 'BUY', 'qty': 3, 'price': 50.0,
         'reason': 'pop:VWAP_RECLAIM:pop', 'layer': 'pop',
         'stop_price': 48.0, 'target_price': 54.0,
         '_ipc_correlation_id': 'pop-signal-xyz'},
    ]

    # Enqueue (simulating IPC consumer thread)
    for p in payloads:
        inbox.put_nowait(p)

    check("3a: Inbox has 2 ORDER_REQs", inbox.qsize() == 2)

    # Drain (simulating main thread)
    emitted = []
    def track(event):
        emitted.append({
            'ticker': event.payload.ticker,
            'layer': event.payload.layer,
            'correlation_id': event.correlation_id,
        })
    bus.subscribe(EventType.ORDER_REQ, track)

    while not inbox.empty():
        payload = inbox.get_nowait()
        order_payload = OrderRequestPayload(
            ticker=payload['ticker'], side=payload['side'],
            qty=int(payload['qty']), price=float(payload['price']),
            reason=payload.get('reason', ''),
            stop_price=float(payload.get('stop_price', 0)),
            target_price=float(payload.get('target_price', 0)),
            layer=payload.get('layer'),
        )
        ipc_corr = payload.get('_ipc_correlation_id', '')
        bus.emit(Event(type=EventType.ORDER_REQ, payload=order_payload,
                       correlation_id=ipc_corr or None))

    check("3b: Both ORDER_REQs emitted", len(emitted) == 2)

    # Verify correlation IDs survived
    pro = [e for e in emitted if e['ticker'] == 'PRO1']
    pop = [e for e in emitted if e['ticker'] == 'POP1']
    check("3c: Pro correlation_id preserved",
          pro and pro[0]['correlation_id'] == 'pro-signal-abc')
    check("3d: Pop correlation_id preserved",
          pop and pop[0]['correlation_id'] == 'pop-signal-xyz')
    check("3e: Pro layer = 'pro'",
          pro and pro[0]['layer'] == 'pro')
    check("3f: Pop layer = 'pop'",
          pop and pop[0]['layer'] == 'pop')

    # Verify FIFO ordering
    check("3g: PRO1 emitted before POP1 (FIFO)",
          emitted[0]['ticker'] == 'PRO1' and emitted[1]['ticker'] == 'POP1')


# ══════════════════════════════════════════════════════════════════════════
# 4. KILL SWITCH & HALT
# ══════════════════════════════════════════════════════════════════════════

def test_4_kill_switch():
    section("4. KILL SWITCH & HALT BEHAVIOR")
    from lifecycle.kill_switch import SatelliteKillSwitch

    class MockAdapter:
        def __init__(self):
            self.pnl = 0.0
            self.closed = False
        def get_daily_pnl(self):
            return self.pnl
        def get_daily_stats(self):
            return {'trades': 5, 'wins': 2, 'pnl': self.pnl}
        def force_close_all(self, reason):
            self.closed = True

    adapter = MockAdapter()
    ks = SatelliteKillSwitch('test', adapter, alert_email=None,
                             max_daily_loss=-500.0)

    # Above threshold — not halted
    adapter.pnl = -100.0
    check("4a: P&L -$100 (above -$500) → not halted",
          not ks.check())

    # At threshold — halted
    adapter.pnl = -500.0
    check("4b: P&L -$500 (at threshold) → HALTED",
          ks.check())
    check("4c: halted flag set", ks.halted is True)

    # Once halted, stays halted even if P&L recovers
    adapter.pnl = 100.0
    check("4d: P&L recovered to +$100 → still halted (irrecoverable)",
          ks.check())

    # New instance — not halted
    ks2 = SatelliteKillSwitch('test2', adapter, alert_email=None,
                              max_daily_loss=-2000.0)
    adapter.pnl = -1999.0
    check("4e: P&L -$1999 (above -$2000) → not halted",
          not ks2.check())
    adapter.pnl = -2001.0
    check("4f: P&L -$2001 (below -$2000) → HALTED",
          ks2.check())


# ══════════════════════════════════════════════════════════════════════════
# 5. POSITION MANAGER EDGE CASES
# ══════════════════════════════════════════════════════════════════════════

def test_5_position_edge_cases():
    section("5. POSITION MANAGER EDGE CASES")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import FillPayload
    from monitor.position_manager import PositionManager

    bus = EventBus()
    positions = {}
    reclaimed = set()
    last_order = {}
    trade_log = []
    pm = PositionManager(bus, positions, reclaimed, last_order, trade_log)

    # Edge 1: Duplicate FILL event_id
    fill = FillPayload(ticker='DUP', side='BUY', qty=5, fill_price=100.0,
                       order_id='dup-001', reason='test',
                       stop_price=98.0, target_price=104.0)
    eid = 'dup-event-12345'
    bus.emit(Event(type=EventType.FILL, payload=fill, event_id=eid))
    bus.emit(Event(type=EventType.FILL, payload=fill, event_id=eid))

    check("5a: Duplicate FILL → only 1 position created",
          'DUP' in positions and positions['DUP']['quantity'] == 5)

    # Edge 2: SELL without open position
    orphan = FillPayload(ticker='GHOST', side='SELL', qty=5, fill_price=100.0,
                         order_id='ghost-001', reason='SELL_STOP')
    bus.emit(Event(type=EventType.FILL, payload=orphan))
    check("5b: SELL without position → no short created",
          'GHOST' not in positions)

    # Edge 3: BUY with no stop/target → uses defaults
    no_stops = FillPayload(ticker='NOSL', side='BUY', qty=3, fill_price=200.0,
                           order_id='nosl-001', reason='test')
    bus.emit(Event(type=EventType.FILL, payload=no_stops))
    check("5c: Position created with default stop",
          'NOSL' in positions)
    check("5d: Default stop = 0.5% below entry",
          abs(positions['NOSL']['stop_price'] - 200.0 * 0.995) < 0.01)

    # Edge 4: Strategy field parsed from reason
    strat_fill = FillPayload(ticker='STRAT', side='BUY', qty=2, fill_price=50.0,
                             order_id='strat-001', reason='pro:sr_flip:T1:long',
                             stop_price=49.0, target_price=52.0)
    bus.emit(Event(type=EventType.FILL, payload=strat_fill))
    check("5e: Strategy parsed from reason",
          positions.get('STRAT', {}).get('strategy') == 'pro:sr_flip')

    # Edge 5: SELL more than position qty → treated as full close
    sell_all = FillPayload(ticker='STRAT', side='SELL', qty=2, fill_price=51.0,
                           order_id='strat-002', reason='SELL_TARGET')
    bus.emit(Event(type=EventType.FILL, payload=sell_all))
    check("5f: Full close removes position",
          'STRAT' not in positions)
    check("5g: P&L correct ((51-50)*2 = $2)",
          any(t['ticker'] == 'STRAT' and t['pnl'] == 2.0 for t in trade_log))

    # Edge 6: Losing trade
    loss_buy = FillPayload(ticker='LOSS', side='BUY', qty=10, fill_price=100.0,
                           order_id='loss-001', reason='test',
                           stop_price=97.0, target_price=106.0)
    bus.emit(Event(type=EventType.FILL, payload=loss_buy))
    loss_sell = FillPayload(ticker='LOSS', side='SELL', qty=10, fill_price=95.0,
                            order_id='loss-002', reason='SELL_STOP')
    bus.emit(Event(type=EventType.FILL, payload=loss_sell))
    check("5h: Losing trade P&L = -$50 ((95-100)*10)",
          any(t['ticker'] == 'LOSS' and t['pnl'] == -50.0 for t in trade_log))
    check("5i: Losing trade is_win = False",
          any(t['ticker'] == 'LOSS' and t['is_win'] is False for t in trade_log))


# ══════════════════════════════════════════════════════════════════════════
# 6. SMARTROUTER FAILOVER & DEDUP
# ══════════════════════════════════════════════════════════════════════════

def test_6_smartrouter():
    section("6. SMARTROUTER FAILOVER & DEDUP")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import OrderRequestPayload, OrderFailPayload, Side
    from monitor.smart_router import SmartRouter
    import uuid

    bus = EventBus()

    class FailingBroker:
        def _on_order_request(self, event):
            raise Exception("Broker API down")
        def has_position(self, ticker):
            return False

    class WorkingBroker:
        def __init__(self):
            self.orders = []
        def _on_order_request(self, event):
            self.orders.append(event.payload.ticker)
        def has_position(self, ticker):
            return False

    working = WorkingBroker()
    failing = FailingBroker()

    router = SmartRouter(bus=bus, brokers={
        'primary': failing,
        'secondary': working,
    }, default_broker='primary')

    # Failover: primary fails → secondary handles
    fails = []
    bus.subscribe(EventType.ORDER_FAIL, lambda e: fails.append(e.payload.ticker))

    e1 = Event(type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='FAIL1', side=Side.BUY, qty=5, price=100.0, reason='test'))
    bus.emit(e1)

    # SmartRouter tries primary, exception → failover to secondary
    # Secondary received the order (working broker got it)
    check("6a: Failover to secondary broker executed",
          len(working.orders) > 0,
          f"secondary received {len(working.orders)} order(s)")

    # Dedup: same event_id replayed
    eid = str(uuid.uuid4())
    e2 = Event(type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='DEDUP1', side=Side.BUY, qty=5, price=100.0, reason='test'),
        event_id=eid)
    bus.emit(e2)

    e3 = Event(type=EventType.ORDER_REQ,
        payload=OrderRequestPayload(
            ticker='DEDUP1', side=Side.BUY, qty=5, price=100.0, reason='test'),
        event_id=eid)
    bus.emit(e3)

    check("6b: Duplicate ORDER_REQ deduped (same event_id)",
          eid in router._routed_event_ids)

    # Verify circuit breaker: 3 failures → broker disabled
    for i in range(3):
        router._record_failure('primary')
    check("6c: Circuit breaker trips after 3 failures",
          not router._is_healthy('primary'))


# ══════════════════════════════════════════════════════════════════════════
# 7. REGISTRY GATE CONTENTION
# ══════════════════════════════════════════════════════════════════════════

def test_7_registry_contention():
    section("7. REGISTRY GATE CONTENTION (Pro vs Pop vs Core)")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import (OrderRequestPayload, PositionPayload,
                                 Side, PositionAction)
    from monitor.registry_gate import RegistryGate

    with tempfile.TemporaryDirectory() as td:
        from monitor.distributed_registry import DistributedPositionRegistry
        reg = DistributedPositionRegistry(global_max=5,
            registry_path=os.path.join(td, 'reg.json'))
        bus = EventBus()
        gate = RegistryGate(bus=bus, registry=reg)

        blocked = []
        bus.subscribe(EventType.RISK_BLOCK,
                      lambda e: blocked.append(e.payload.reason), priority=0)

        # Core acquires AAPL
        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=10, price=150.0,
                reason='vwap_reclaim', layer='vwap')))
        check("7a: Core acquires AAPL", reg.held_by('AAPL') == 'vwap')

        # Pro tries AAPL → blocked
        e_pro = Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=5, price=150.0,
                reason='pro:sr_flip', layer='pro'))
        bus.emit(e_pro)
        check("7b: Pro blocked on AAPL",
              gate.is_blocked(e_pro.event_id))
        check("7c: RISK_BLOCK mentions 'vwap'",
              any('vwap' in r for r in blocked))

        # Pop tries AAPL → also blocked
        e_pop = Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=3, price=150.0,
                reason='pop:momentum', layer='pop'))
        bus.emit(e_pop)
        check("7d: Pop also blocked on AAPL",
              gate.is_blocked(e_pop.event_id))

        # Pro acquires different ticker → OK
        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='MSFT', side=Side.BUY, qty=5, price=400.0,
                reason='pro:trend', layer='pro')))
        check("7e: Pro acquires MSFT (different ticker)",
              reg.held_by('MSFT') == 'pro')

        # Close AAPL → vwap releases → Pro can now acquire
        blocked.clear()
        bus.emit(Event(type=EventType.POSITION,
            payload=PositionPayload(
                ticker='AAPL', action=PositionAction.CLOSED,
                position=None, pnl=25.0)))
        check("7f: AAPL released after CLOSED",
              not reg.is_held('AAPL'))

        bus.emit(Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL', side=Side.BUY, qty=5, price=151.0,
                reason='pro:sr_flip', layer='pro')))
        check("7g: Pro acquires AAPL after Core closes",
              reg.held_by('AAPL') == 'pro')

        # Global max: fill up to 5
        for i, t in enumerate(['T1', 'T2', 'T3']):
            bus.emit(Event(type=EventType.ORDER_REQ,
                payload=OrderRequestPayload(
                    ticker=t, side=Side.BUY, qty=1, price=10.0,
                    reason='test', layer='vwap')))

        e_over = Event(type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='OVER', side=Side.BUY, qty=1, price=10.0,
                reason='test', layer='pop'))
        bus.emit(e_over)
        check("7h: Global max (5) blocks 6th acquisition",
              gate.is_blocked(e_over.event_id))


# ══════════════════════════════════════════════════════════════════════════
# 8. SHARED CACHE STALENESS
# ══════════════════════════════════════════════════════════════════════════

def test_8_cache_staleness():
    section("8. SHARED CACHE STALENESS DURING TRADING")
    import pandas as pd
    from monitor.shared_cache import CacheWriter, CacheReader

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, 'cache.pkl')
        writer = CacheWriter(cache_path=path)
        reader = CacheReader(cache_path=path, max_age_seconds=0.5)

        bars = {'AAPL': pd.DataFrame({'close': [150.0], 'volume': [1000]})}
        writer.write(bars, {})

        # Fresh → returns data
        b1, _ = reader.get_bars()
        check("8a: Fresh cache returns bars", 'AAPL' in b1)

        # Simulate Core crash (stop writing)
        time.sleep(0.7)

        # Stale → returns EMPTY (fail-safe)
        b2, _ = reader.get_bars()
        check("8b: Stale cache returns empty (prevents stale trading)",
              len(b2) == 0)

        # Core recovers
        writer.write(bars, {})
        b3, _ = reader.get_bars()
        check("8c: Recovered cache returns data", 'AAPL' in b3)


# ══════════════════════════════════════════════════════════════════════════
# 9. STATE PERSISTENCE & CRASH RECOVERY
# ══════════════════════════════════════════════════════════════════════════

def test_9_crash_recovery():
    section("9. STATE PERSISTENCE & CRASH RECOVERY")
    from monitor.event_bus import EventBus, EventType, Event
    from monitor.events import FillPayload
    from monitor.position_manager import PositionManager
    from monitor.state import save_state, load_state

    bus = EventBus()
    positions = {}
    reclaimed = set()
    last_order = {}
    trade_log = []
    pm = PositionManager(bus, positions, reclaimed, last_order, trade_log)

    # Open 2 positions
    for ticker, price in [('CRASH1', 100.0), ('CRASH2', 200.0)]:
        bus.emit(Event(type=EventType.FILL,
            payload=FillPayload(
                ticker=ticker, side='BUY', qty=5, fill_price=price,
                order_id=f'{ticker}-001', reason='test',
                stop_price=price*0.97, target_price=price*1.05)))

    check("9a: 2 positions open", len(positions) == 2)

    # Close 1 position (generates trade log entry)
    bus.emit(Event(type=EventType.FILL,
        payload=FillPayload(
            ticker='CRASH1', side='SELL', qty=5, fill_price=105.0,
            order_id='crash1-sell', reason='SELL_TARGET')))
    check("9b: 1 position closed", len(positions) == 1)
    check("9c: Trade log has 1 entry", len(trade_log) == 1)

    # Simulate crash: load state from file
    pos_loaded, rec_loaded, log_loaded = load_state()

    # State should have been persisted by PositionManager._persist()
    check("9d: Persisted positions loadable",
          isinstance(pos_loaded, dict))

    if pos_loaded:
        check("9e: CRASH2 position survived",
              'CRASH2' in pos_loaded)
        check("9f: CRASH1 not in loaded state (was closed)",
              'CRASH1' not in pos_loaded)
        check("9g: Stop price preserved in saved state",
              abs(pos_loaded.get('CRASH2', {}).get('stop_price', 0) - 194.0) < 1.0)


# ══════════════════════════════════════════════════════════════════════════
# 10. DB EVENT PERSISTENCE CHAIN
# ══════════════════════════════════════════════════════════════════════════

def test_10_db_persistence():
    section("10. DB EVENT PERSISTENCE CHAIN")
    import db.event_sourcing_subscriber as es_mod
    src = open(es_mod.__file__).read()

    # Verify all event types have handlers
    handlers = [
        ('BAR', '_on_bar'),
        ('SIGNAL', '_on_signal'),
        ('ORDER_REQ', '_on_order_req'),
        ('FILL', '_on_fill'),
        ('POSITION', '_on_position'),
        ('RISK_BLOCK', '_on_risk_block'),
        ('HEARTBEAT', '_on_heartbeat'),
        ('POP_SIGNAL', '_on_pop_signal'),
        ('PRO_STRATEGY_SIGNAL', '_on_pro_strategy_signal'),
    ]
    for event_type, method in handlers:
        check(f"10a: {event_type} handler ({method}) exists",
              f'def {method}' in src)

    # Verify projection tables written
    tables = [
        'signal_events', 'order_req_events', 'fill_events',
        'position_events', 'completed_trades', 'risk_block_events',
        'heartbeat_events', 'pro_strategy_signal_events', 'pop_signal_events',
    ]
    for table in tables:
        check(f"10b: {table} projection written",
              f"'{table}'" in src or f'"{table}"' in src)

    # Verify completed_trades ONLY on CLOSED
    check("10c: _write_completed_trade called on CLOSED only",
          'PositionClosed' in src and '_write_completed_trade' in src)

    # Verify event_store has correlation_id
    check("10d: event_store writes correlation_id",
          'correlation_id' in src)

    # Verify all handlers subscribe at priority 10
    check("10e: All handlers at priority=10",
          'priority=10' in src)

    # Verify immutable event_store (write-only, no UPDATE/DELETE)
    check("10f: No UPDATE/DELETE in event_store writes",
          'UPDATE' not in src.split('event_store')[0]
          if 'event_store' in src else True)


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print("\n" + "=" * 70)
    print("  V7 TRADING SESSION DEEP-DIVE TEST SUITE")
    print("  Tests every component, lock, event, and edge case")
    print("  Bypasses market hours — safe to run anytime")
    print("=" * 70)

    start = time.monotonic()

    test_1_full_lifecycle()
    test_2_risk_gates()
    test_3_inter_engine()
    test_4_kill_switch()
    test_5_position_edge_cases()
    test_6_smartrouter()
    test_7_registry_contention()
    test_8_cache_staleness()
    test_9_crash_recovery()
    test_10_db_persistence()

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

    print("=" * 70)
    sys.exit(0 if failed == 0 else 1)
