#!/usr/bin/env python3
"""
Test 15 — Order Manager (PaperBroker + PositionManager integration)
====================================================================
TC-OM-01: New -> Filled — buy ORDER_REQ produces FILL, position opened
TC-OM-02: Partial fills — sell partial reduces qty, partial_done=True
TC-OM-03: Cancel / Full close — sell full qty removes position, trade_log has PnL
TC-OM-04: Duplicate fill event — idempotency prevents double position update
"""
import os, sys, time, logging, uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from monitor.event_bus import EventBus, Event, EventType, DispatchMode
from monitor.events import OrderRequestPayload, FillPayload, PositionPayload
from monitor.brokers import PaperBroker
from monitor.position_manager import PositionManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("test_15")

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        log.info(f"  PASS  {name}")
    else:
        FAIL += 1
        log.error(f"  FAIL  {name} — {detail}")


def _make_shared_state():
    """Return fresh shared state dicts for each test."""
    return {}, [], set(), {}


def _make_stack(positions, trade_log, reclaimed_today, last_order_time):
    """Create EventBus (SYNC), PaperBroker, PositionManager wired together."""
    bus = EventBus(mode=DispatchMode.SYNC)
    broker = PaperBroker(bus)
    # Patch save_state and send_alert to avoid disk I/O and email side effects
    with patch('monitor.position_manager.save_state'), \
         patch('monitor.position_manager.send_alert'):
        pm = PositionManager(
            bus=bus,
            positions=positions,
            reclaimed_today=reclaimed_today,
            last_order_time=last_order_time,
            trade_log=trade_log,
        )
    return bus, broker, pm


# =============================================================================
# TC-OM-01: New -> Filled
# =============================================================================
def test_om_01():
    log.info("TC-OM-01: New -> Filled — buy ORDER_REQ opens position")

    positions, trade_log, reclaimed_today, last_order_time = _make_shared_state()
    bus, broker, pm = _make_stack(positions, trade_log, reclaimed_today, last_order_time)

    # Collect FILL events
    fills = []
    bus.subscribe(EventType.FILL, lambda e: fills.append(e))

    # Collect POSITION events
    pos_events = []
    bus.subscribe(EventType.POSITION, lambda e: pos_events.append(e))

    # Emit ORDER_REQ — PaperBroker fills instantly, PositionManager opens position
    with patch('monitor.position_manager.save_state'), \
         patch('monitor.position_manager.send_alert'):
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='AAPL',
                side='buy',
                qty=100,
                price=150.00,
                reason='test_entry',
                stop_price=149.00,
                target_price=152.00,
                atr_value=1.50,
            ),
        ))

    check("OM-01a: FILL event emitted",
          len(fills) >= 1,
          f"expected >= 1 fill, got {len(fills)}")

    if fills:
        fp = fills[0].payload
        check("OM-01b: FILL side is buy",
              str(fp.side) == 'buy',
              f"got {fp.side}")
        check("OM-01c: FILL ticker is AAPL",
              fp.ticker == 'AAPL',
              f"got {fp.ticker}")
        check("OM-01d: FILL price is 150.00",
              fp.fill_price == 150.00,
              f"got {fp.fill_price}")
        check("OM-01e: FILL qty is 100",
              fp.qty == 100,
              f"got {fp.qty}")

    check("OM-01f: position opened in positions dict",
          'AAPL' in positions,
          f"positions keys: {list(positions.keys())}")

    if 'AAPL' in positions:
        pos = positions['AAPL']
        check("OM-01g: entry_price correct",
              pos['entry_price'] == 150.00,
              f"got {pos['entry_price']}")
        check("OM-01h: quantity correct",
              pos['quantity'] == 100,
              f"got {pos['quantity']}")
        check("OM-01i: partial_done is False",
              pos['partial_done'] is False,
              f"got {pos['partial_done']}")

    check("OM-01j: POSITION event emitted with action=opened",
          any(e.payload.action == 'opened' for e in pos_events),
          f"pos_events actions: {[e.payload.action for e in pos_events]}")


# =============================================================================
# TC-OM-02: Partial fills
# =============================================================================
def test_om_02():
    log.info("TC-OM-02: Partial fills — sell partial reduces qty")

    positions, trade_log, reclaimed_today, last_order_time = _make_shared_state()
    bus, broker, pm = _make_stack(positions, trade_log, reclaimed_today, last_order_time)

    # Pre-populate an open position (as if a buy fill already happened)
    positions['TSLA'] = {
        'entry_price':  200.00,
        'entry_time':   '09:35:00',
        'quantity':     10,
        'partial_done': False,
        'order_id':     str(uuid.uuid4()),
        'stop_price':   199.00,
        'target_price': 204.00,
        'half_target':  202.00,
        'atr_value':    2.00,
    }

    pos_events = []
    bus.subscribe(EventType.POSITION, lambda e: pos_events.append(e))

    # Emit sell ORDER_REQ for partial qty (5 of 10)
    with patch('monitor.position_manager.save_state'), \
         patch('monitor.position_manager.send_alert'):
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='TSLA',
                side='sell',
                qty=5,
                price=205.00,
                reason='partial_sell',
            ),
        ))

    check("OM-02a: position still exists",
          'TSLA' in positions,
          f"positions keys: {list(positions.keys())}")

    if 'TSLA' in positions:
        pos = positions['TSLA']
        check("OM-02b: quantity reduced to 5",
              pos['quantity'] == 5,
              f"got {pos['quantity']}")
        check("OM-02c: partial_done is True",
              pos['partial_done'] is True,
              f"got {pos['partial_done']}")
        check("OM-02d: stop moved to breakeven (entry_price)",
              pos['stop_price'] >= 200.00,
              f"got stop_price={pos['stop_price']}")

    check("OM-02e: trade_log has entry",
          len(trade_log) == 1,
          f"trade_log length: {len(trade_log)}")

    if trade_log:
        t = trade_log[0]
        expected_pnl = round((205.00 - 200.00) * 5, 2)
        check("OM-02f: trade_log PnL correct",
              t['pnl'] == expected_pnl,
              f"expected {expected_pnl}, got {t['pnl']}")
        check("OM-02g: trade_log reason is partial_sell",
              t['reason'] == 'partial_sell',
              f"got {t['reason']}")

    check("OM-02h: POSITION event with action=partial_exit",
          any(e.payload.action == 'partial_exit' for e in pos_events),
          f"pos_events actions: {[str(e.payload.action) for e in pos_events]}")


# =============================================================================
# TC-OM-03: Cancel / Full close
# =============================================================================
def test_om_03():
    log.info("TC-OM-03: Full close — sell full qty removes position, trade_log has PnL")

    positions, trade_log, reclaimed_today, last_order_time = _make_shared_state()
    bus, broker, pm = _make_stack(positions, trade_log, reclaimed_today, last_order_time)

    # Pre-populate an open position
    positions['NVDA'] = {
        'entry_price':  300.00,
        'entry_time':   '10:00:00',
        'quantity':     50,
        'partial_done': False,
        'order_id':     str(uuid.uuid4()),
        'stop_price':   298.50,
        'target_price': 306.00,
        'half_target':  303.00,
        'atr_value':    3.00,
    }

    pos_events = []
    bus.subscribe(EventType.POSITION, lambda e: pos_events.append(e))

    # Emit sell ORDER_REQ for full qty
    with patch('monitor.position_manager.save_state'), \
         patch('monitor.position_manager.send_alert'), \
         patch('monitor.position_registry.registry'):
        bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker='NVDA',
                side='sell',
                qty=50,
                price=310.00,
                reason='target_hit',
            ),
        ))

    check("OM-03a: position removed from dict",
          'NVDA' not in positions,
          f"positions keys: {list(positions.keys())}")

    check("OM-03b: trade_log has entry",
          len(trade_log) == 1,
          f"trade_log length: {len(trade_log)}")

    if trade_log:
        t = trade_log[0]
        expected_pnl = round((310.00 - 300.00) * 50, 2)
        check("OM-03c: trade_log PnL correct",
              t['pnl'] == expected_pnl,
              f"expected {expected_pnl}, got {t['pnl']}")
        check("OM-03d: trade_log is_win is True",
              t['is_win'] is True,
              f"got {t['is_win']}")
        check("OM-03e: trade_log reason is target_hit",
              t['reason'] == 'target_hit',
              f"got {t['reason']}")
        check("OM-03f: trade_log ticker is NVDA",
              t['ticker'] == 'NVDA',
              f"got {t['ticker']}")

    check("OM-03g: POSITION event with action=closed",
          any(e.payload.action == 'closed' for e in pos_events),
          f"pos_events actions: {[str(e.payload.action) for e in pos_events]}")

    # Verify PnL on the POSITION event itself
    closed_events = [e for e in pos_events if str(e.payload.action) == 'closed']
    if closed_events:
        check("OM-03h: POSITION closed event has correct PnL",
              closed_events[0].payload.pnl == 500.00,
              f"got {closed_events[0].payload.pnl}")


# =============================================================================
# TC-OM-04: Duplicate fill event — idempotency
# =============================================================================
def test_om_04():
    log.info("TC-OM-04: Duplicate fill event — position updated exactly once")

    positions, trade_log, reclaimed_today, last_order_time = _make_shared_state()
    bus = EventBus(mode=DispatchMode.SYNC)

    with patch('monitor.position_manager.save_state'), \
         patch('monitor.position_manager.send_alert'):
        pm = PositionManager(
            bus=bus,
            positions=positions,
            reclaimed_today=reclaimed_today,
            last_order_time=last_order_time,
            trade_log=trade_log,
        )

    # Craft a single FILL event with a known event_id
    shared_event_id = str(uuid.uuid4())
    fill_event = Event(
        type=EventType.FILL,
        event_id=shared_event_id,
        payload=FillPayload(
            ticker='META',
            side='buy',
            qty=25,
            fill_price=450.00,
            order_id=str(uuid.uuid4()),
            reason='test_entry',
        ),
    )

    # Emit the same event twice (same event_id triggers idempotency)
    with patch('monitor.position_manager.save_state'), \
         patch('monitor.position_manager.send_alert'):
        bus.emit(fill_event)
        bus.emit(fill_event)

    check("OM-04a: position exists for META",
          'META' in positions,
          f"positions keys: {list(positions.keys())}")

    if 'META' in positions:
        pos = positions['META']
        check("OM-04b: quantity is 25 (not 50 from double-apply)",
              pos['quantity'] == 25,
              f"got {pos['quantity']}")
        check("OM-04c: entry_price is 450.00",
              pos['entry_price'] == 450.00,
              f"got {pos['entry_price']}")

    # Verify via bus metrics that the duplicate was dropped
    m = bus.metrics()
    check("OM-04d: duplicate event dropped by idempotency",
          m.duplicate_events_dropped >= 1,
          f"duplicate_events_dropped={m.duplicate_events_dropped}")


# =============================================================================
# Runner
# =============================================================================
if __name__ == '__main__':
    log.info("=" * 70)
    log.info("Test 15 — Order Manager (PaperBroker + PositionManager)")
    log.info("=" * 70)

    test_om_01()
    test_om_02()
    test_om_03()
    test_om_04()

    log.info("=" * 70)
    log.info(f"DONE  {PASS} passed, {FAIL} failed")
    log.info("=" * 70)
    sys.exit(1 if FAIL else 0)
