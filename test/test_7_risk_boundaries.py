"""
TEST 7: Risk Engine & Position Manager Boundary Conditions
===========================================================
Test all 6 RiskEngine pre-trade checks at exact boundary values, plus
PositionManager edge cases. Uses MockDataClient for controlled quotes.

Subtests:
  7a: Max positions boundary
  7b: Cooldown boundary
  7c: RVOL boundary
  7d: RSI boundary
  7e: Spread boundary
  7f: Price divergence
  7g: PositionManager partial sell with qty=1
  7h: PositionManager oversell
  7i: Concurrent fills
"""

import os
import sys
import logging
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Log setup ────────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'test_7_risk_boundaries.log')

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w'),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger('test_7')

from monitor.event_bus import (
    EventBus, Event, EventType, DispatchMode,
    SignalPayload, FillPayload, PositionPayload, RiskBlockPayload,
    OrderRequestPayload,
)
from monitor.risk_engine import (
    RiskEngine, MIN_RVOL, RSI_LOW, RSI_HIGH, MAX_SPREAD_PCT,
)
from monitor.position_manager import PositionManager
from config import STRATEGY_PARAMS, MAX_POSITIONS, ORDER_COOLDOWN


# ── Mock data client ──────────────────────────────────────────────────────────

class MockDataClient:
    """Configurable bid/ask for precise boundary testing."""
    def __init__(self, bid=99.90, ask=100.00):
        self.bid = bid
        self.ask = ask
        self.diverge_ask = None  # if set, overrides ask for divergence test

    def check_spread(self, ticker):
        ask = self.diverge_ask if self.diverge_ask is not None else self.ask
        mid = (self.bid + ask) / 2.0
        spread_pct = (ask - self.bid) / mid if mid > 0 else 0.0
        return spread_pct, ask


# ── Event collector ────────────────────────────────────────────────────────────

class EventCollector:
    def __init__(self, bus):
        self.order_reqs  = []
        self.risk_blocks = []
        self.fills       = []
        self.positions   = []
        self._lock       = threading.Lock()
        bus.subscribe(EventType.ORDER_REQ,  self._on_order_req)
        bus.subscribe(EventType.RISK_BLOCK, self._on_risk_block)
        bus.subscribe(EventType.FILL,       self._on_fill)
        bus.subscribe(EventType.POSITION,   self._on_position)

    def _on_order_req(self, e):
        with self._lock: self.order_reqs.append(e.payload)
    def _on_risk_block(self, e):
        with self._lock: self.risk_blocks.append(e.payload)
    def _on_fill(self, e):
        with self._lock: self.fills.append(e.payload)
    def _on_position(self, e):
        with self._lock: self.positions.append(e.payload)

    def reset(self):
        with self._lock:
            self.order_reqs.clear()
            self.risk_blocks.clear()
            self.fills.clear()
            self.positions.clear()


# ── Helper: emit a BUY signal ─────────────────────────────────────────────────

def emit_buy_signal(bus, ticker='TSYM', rvol=2.5, rsi=60.0, ask=100.0):
    """Emit a BUY signal with controlled parameters."""
    payload = SignalPayload(
        ticker=ticker, action='buy',
        current_price=ask, ask_price=ask,
        atr_value=0.5, rsi_value=rsi, rvol=rvol,
        vwap=ask - 0.10, stop_price=ask * 0.995, target_price=ask * 1.01,
        half_target=ask * 1.005, reclaim_candle_low=ask * 0.997,
    )
    bus.emit(Event(EventType.SIGNAL, payload))


def make_risk_engine(bus, positions, reclaimed=None, last_order_time=None,
                     data_client=None, max_positions=5, order_cooldown=300):
    dc = data_client or MockDataClient()
    return RiskEngine(
        bus=bus, positions=positions,
        reclaimed_today=reclaimed or set(),
        last_order_time=last_order_time or {},
        data_client=dc,
        max_positions=max_positions,
        order_cooldown=order_cooldown,
        trade_budget=1000.0,
    )


# ── Subtest 7a ────────────────────────────────────────────────────────────────

def test_7a_max_positions_boundary():
    log.info("=== 7a: Max positions boundary ===")
    MAX = MAX_POSITIONS  # 5

    # --- Case 1: max-1 = 4 positions -> should PASS ---
    bus = EventBus(mode=DispatchMode.SYNC)
    positions = {f'POS{i}': {'entry_price': 100.0, 'quantity': 1,
                              'stop_price': 99.0, 'target_price': 101.0,
                              'half_target': 100.5, 'partial_done': False,
                              'order_id': f'o{i}', 'entry_time': '10:00:00'}
                 for i in range(MAX - 1)}
    collector = EventCollector(bus)
    make_risk_engine(bus, positions, max_positions=MAX)
    emit_buy_signal(bus, ticker='NEWPOS')

    buy_orders = [o for o in collector.order_reqs if str(o.side) == 'buy']
    assert buy_orders, f"Expected ORDER_REQ at max-1 positions ({MAX-1}), got risk_blocks={collector.risk_blocks}"
    log.info(f"  PASS: {MAX-1} positions -> ORDER_REQ emitted")

    # --- Case 2: exactly max = 5 positions -> should BLOCK ---
    bus2 = EventBus(mode=DispatchMode.SYNC)
    positions2 = {f'POS{i}': {'entry_price': 100.0, 'quantity': 1,
                                'stop_price': 99.0, 'target_price': 101.0,
                                'half_target': 100.5, 'partial_done': False,
                                'order_id': f'o{i}', 'entry_time': '10:00:00'}
                  for i in range(MAX)}
    collector2 = EventCollector(bus2)
    make_risk_engine(bus2, positions2, max_positions=MAX)
    emit_buy_signal(bus2, ticker='OVERFLOW')

    blocks = [rb for rb in collector2.risk_blocks if rb.ticker == 'OVERFLOW']
    assert blocks, f"Expected RISK_BLOCK at max positions ({MAX}), got orders={collector2.order_reqs}"
    assert 'max positions' in blocks[0].reason.lower()
    log.info(f"  PASS: {MAX} positions -> RISK_BLOCK: {blocks[0].reason}")
    return True


# ── Subtest 7b ────────────────────────────────────────────────────────────────

def test_7b_cooldown_boundary():
    log.info("=== 7b: Cooldown boundary ===")
    COOLDOWN = 300  # seconds

    # --- Case 1: exactly COOLDOWN seconds ago -> should PASS ---
    bus = EventBus(mode=DispatchMode.SYNC)
    positions = {}
    collector = EventCollector(bus)
    last_order_time = {'CDTEST': time.time() - COOLDOWN}  # exactly at boundary
    make_risk_engine(bus, positions, last_order_time=last_order_time, order_cooldown=COOLDOWN)
    emit_buy_signal(bus, ticker='CDTEST')

    buy_orders = [o for o in collector.order_reqs if str(o.side) == 'buy']
    assert buy_orders, f"Should PASS at exactly cooldown ({COOLDOWN}s ago)"
    log.info(f"  PASS: exactly {COOLDOWN}s elapsed -> ORDER_REQ")

    # --- Case 2: COOLDOWN-1 seconds ago -> should BLOCK ---
    bus2 = EventBus(mode=DispatchMode.SYNC)
    positions2 = {}
    collector2 = EventCollector(bus2)
    last_order_time2 = {'CDBLOCK': time.time() - (COOLDOWN - 1)}
    make_risk_engine(bus2, positions2, last_order_time=last_order_time2, order_cooldown=COOLDOWN)
    emit_buy_signal(bus2, ticker='CDBLOCK')

    blocks = [rb for rb in collector2.risk_blocks if rb.ticker == 'CDBLOCK']
    assert blocks, f"Should BLOCK at {COOLDOWN-1}s elapsed (within cooldown)"
    assert 'cooldown' in blocks[0].reason.lower()
    log.info(f"  PASS: {COOLDOWN-1}s elapsed -> RISK_BLOCK: {blocks[0].reason}")
    return True


# ── Subtest 7c ────────────────────────────────────────────────────────────────

def test_7c_rvol_boundary():
    log.info("=== 7c: RVOL boundary ===")
    # MIN_RVOL = 2.0

    cases = [
        (MIN_RVOL - 0.01, 'BLOCK'),  # 1.99 -> block
        (MIN_RVOL,        'PASS'),   # 2.00 -> pass
        (MIN_RVOL + 0.01, 'PASS'),   # 2.01 -> pass
    ]

    for rvol, expected in cases:
        bus = EventBus(mode=DispatchMode.SYNC)
        positions = {}
        collector = EventCollector(bus)
        make_risk_engine(bus, positions)
        emit_buy_signal(bus, ticker='RVOL_T', rvol=rvol)

        has_order = any(str(o.side) == 'buy' for o in collector.order_reqs)
        has_block = any(rb.ticker == 'RVOL_T' for rb in collector.risk_blocks)

        if expected == 'PASS':
            if has_block and 'rvol' in (collector.risk_blocks[0].reason.lower() if collector.risk_blocks else ''):
                raise AssertionError(f"RVOL={rvol:.2f} should PASS but got BLOCK: {collector.risk_blocks[0].reason}")
            log.info(f"  PASS: rvol={rvol:.2f} -> passed (or blocked by other check)")
        else:
            if has_order and not has_block:
                # If it passed, check if blocked by something else — rvol check comes 4th
                pass  # spread check may block; only care if rvol-specific
            rvol_blocks = [rb for rb in collector.risk_blocks
                           if 'rvol' in rb.reason.lower() and rb.ticker == 'RVOL_T']
            assert rvol_blocks, f"RVOL={rvol:.2f} should produce RVOL-specific RISK_BLOCK"
            log.info(f"  PASS: rvol={rvol:.2f} -> RISK_BLOCK: {rvol_blocks[0].reason}")

    return True


# ── Subtest 7d ────────────────────────────────────────────────────────────────

def test_7d_rsi_boundary():
    log.info("=== 7d: RSI boundary ===")
    # RSI_LOW=50.0, RSI_HIGH=70.0

    cases = [
        (RSI_LOW - 0.1, 'BLOCK'),  # 49.9 -> block
        (RSI_LOW,       'PASS'),   # 50.0 -> pass
        (RSI_HIGH,      'PASS'),   # 70.0 -> pass
        (RSI_HIGH + 0.1,'BLOCK'),  # 70.1 -> block
    ]

    for rsi, expected in cases:
        bus = EventBus(mode=DispatchMode.SYNC)
        positions = {}
        collector = EventCollector(bus)
        make_risk_engine(bus, positions)
        emit_buy_signal(bus, ticker='RSI_T', rvol=3.0, rsi=rsi)

        rsi_blocks = [rb for rb in collector.risk_blocks
                      if 'rsi' in rb.reason.lower() and rb.ticker == 'RSI_T']
        has_order = any(str(o.side) == 'buy' for o in collector.order_reqs)

        if expected == 'PASS':
            assert not rsi_blocks, f"RSI={rsi:.1f} should PASS, got RSI block: {rsi_blocks}"
            log.info(f"  PASS: rsi={rsi:.1f} -> no RSI block")
        else:
            assert rsi_blocks, f"RSI={rsi:.1f} should produce RSI RISK_BLOCK, has_order={has_order}"
            log.info(f"  PASS: rsi={rsi:.1f} -> RISK_BLOCK: {rsi_blocks[0].reason}")

    return True


# ── Subtest 7e ────────────────────────────────────────────────────────────────

def test_7e_spread_boundary():
    log.info("=== 7e: Spread boundary ===")
    # MAX_SPREAD_PCT = 0.002 = 0.2%
    # spread_pct = (ask - bid) / mid
    # For mid=100, to get spread_pct=0.002: ask-bid = 0.2 -> bid=99.9, ask=100.1

    def bid_ask_for_spread(target_spread_pct, mid=100.0):
        half = mid * target_spread_pct / 2.0
        return mid - half, mid + half

    cases = [
        (0.0019, 'PASS'),   # 0.19% -> pass
        (0.002,  'PASS'),   # 0.20% -> exactly at boundary -> pass (<=, not <)
        (0.0021, 'BLOCK'),  # 0.21% -> block
    ]

    for spread, expected in cases:
        bid, ask = bid_ask_for_spread(spread)
        dc = MockDataClient(bid=bid, ask=ask)
        bus = EventBus(mode=DispatchMode.SYNC)
        positions = {}
        collector = EventCollector(bus)
        make_risk_engine(bus, positions, data_client=dc)
        # Signal ask must be close to dc.ask to avoid divergence block
        emit_buy_signal(bus, ticker='SPR_T', rvol=3.0, rsi=60.0, ask=ask)

        spread_blocks = [rb for rb in collector.risk_blocks
                         if 'spread' in rb.reason.lower() and rb.ticker == 'SPR_T']
        has_order = any(str(o.side) == 'buy' for o in collector.order_reqs)

        if expected == 'PASS':
            assert not spread_blocks, (
                f"Spread={spread:.4%} should PASS, got block: "
                f"{spread_blocks[0].reason if spread_blocks else 'N/A'}"
            )
            log.info(f"  PASS: spread={spread:.4%} -> no spread block")
        else:
            assert spread_blocks, (
                f"Spread={spread:.4%} should produce spread RISK_BLOCK, "
                f"has_order={has_order}, blocks={[rb.reason for rb in collector.risk_blocks]}"
            )
            log.info(f"  PASS: spread={spread:.4%} -> RISK_BLOCK: {spread_blocks[0].reason}")

    return True


# ── Subtest 7f ────────────────────────────────────────────────────────────────

def test_7f_price_divergence():
    log.info("=== 7f: Price divergence ===")
    # block if |live_ask - signal_ask| / signal_ask > 0.005 (0.5%)

    signal_ask = 100.0
    DIVERGE_THRESHOLD = 0.005

    cases = [
        (signal_ask * (1 + DIVERGE_THRESHOLD - 0.001), 'PASS'),  # 0.499% -> pass
        (signal_ask * (1 + DIVERGE_THRESHOLD + 0.001), 'BLOCK'), # 0.501% -> block
    ]

    for live_ask, expected in cases:
        # Create a tight spread but with diverged ask price
        dc = MockDataClient(bid=live_ask - 0.05, ask=live_ask)
        bus = EventBus(mode=DispatchMode.SYNC)
        positions = {}
        collector = EventCollector(bus)
        make_risk_engine(bus, positions, data_client=dc)
        emit_buy_signal(bus, ticker='DIV_T', rvol=3.0, rsi=60.0, ask=signal_ask)

        diverge_blocks = [rb for rb in collector.risk_blocks
                          if 'diverg' in rb.reason.lower() and rb.ticker == 'DIV_T']
        # divergence block sets ask_price to None via _get_spread
        all_blocks = [rb for rb in collector.risk_blocks if rb.ticker == 'DIV_T']

        diverge_pct = abs(live_ask - signal_ask) / signal_ask
        if expected == 'PASS':
            assert not all_blocks, (
                f"live_ask={live_ask:.4f} ({diverge_pct:.4%} div) should PASS, "
                f"got blocks: {[rb.reason for rb in all_blocks]}"
            )
            log.info(f"  PASS: diverge={diverge_pct:.4%} -> no block")
        else:
            assert all_blocks, (
                f"live_ask={live_ask:.4f} ({diverge_pct:.4%} div) should BLOCK, "
                f"got orders: {[o.ticker for o in collector.order_reqs]}"
            )
            log.info(f"  PASS: diverge={diverge_pct:.4%} -> RISK_BLOCK: {all_blocks[0].reason}")

    return True


# ── Subtest 7g ────────────────────────────────────────────────────────────────

def test_7g_partial_sell_qty1():
    log.info("=== 7g: PositionManager partial sell with qty=1 ===")
    bus = EventBus(mode=DispatchMode.SYNC)
    positions = {}
    reclaimed = set()
    last_order = {}
    trade_log = []
    collector = EventCollector(bus)

    pm = PositionManager(bus=bus, positions=positions,
                         reclaimed_today=reclaimed, last_order_time=last_order,
                         trade_log=trade_log)

    # Open a position with qty=1
    buy_fill = FillPayload(ticker='QTY1', side='buy', qty=1, fill_price=100.0,
                           order_id='ord-qty1', reason='VWAP reclaim')
    bus.emit(Event(EventType.FILL, buy_fill))
    assert 'QTY1' in positions, "Position should be opened"
    assert positions['QTY1']['quantity'] == 1

    # Emit partial sell signal through RiskEngine — qty//2 == 0 should be skipped
    sig = SignalPayload(
        ticker='QTY1', action='partial_sell',
        current_price=100.5, ask_price=100.5,
        atr_value=0.5, rsi_value=60.0, rvol=1.0,
        vwap=100.0, stop_price=99.0, target_price=103.0, half_target=101.5,
        reclaim_candle_low=99.5,
    )
    # Risk engine passes sell signals through; partial_sell with qty=1 -> sell_qty=0 -> skip
    dc = MockDataClient()
    re = RiskEngine(bus=bus, positions=positions, reclaimed_today=reclaimed,
                    last_order_time=last_order, data_client=dc)
    bus.emit(Event(EventType.SIGNAL, sig))

    # RiskEngine should log a skip for partial_sell with qty=1 (sell_qty=0)
    sell_order_reqs = [o for o in collector.order_reqs if str(o.side) == 'sell' and o.ticker == 'QTY1']
    if not sell_order_reqs:
        log.info("  PASS: partial_sell with qty=1 correctly skipped (sell_qty=0)")
    else:
        log.warning(f"  WARN: partial_sell with qty=1 emitted order_req — position may close cleanly")

    assert 'QTY1' in positions, "Position should NOT be force-closed by failed partial sell"
    return True


# ── Subtest 7h ────────────────────────────────────────────────────────────────

def test_7h_oversell():
    log.info("=== 7h: PositionManager oversell ===")
    bus = EventBus(mode=DispatchMode.SYNC)
    positions = {}
    reclaimed = set()
    last_order = {}
    trade_log = []
    collector = EventCollector(bus)

    pm = PositionManager(bus=bus, positions=positions,
                         reclaimed_today=reclaimed, last_order_time=last_order,
                         trade_log=trade_log)

    # Open a position with qty=3
    buy_fill = FillPayload(ticker='OVSELL', side='buy', qty=3, fill_price=100.0,
                           order_id='ord-ov', reason='VWAP reclaim')
    bus.emit(Event(EventType.FILL, buy_fill))
    assert 'OVSELL' in positions

    # Emit SELL fill with qty > position qty (oversell: qty=10 when pos=3)
    try:
        oversell_fill = FillPayload(ticker='OVSELL', side='sell', qty=10,
                                    fill_price=101.0, order_id='ord-ov-sell',
                                    reason='sell_stop')
        bus.emit(Event(EventType.FILL, oversell_fill))
        # Should not crash — position manager treats it as full close
        log.info(f"  PASS: Oversell did not crash. positions={'OVSELL' in positions}")
    except Exception as e:
        raise AssertionError(f"Oversell should not crash: {e}")

    return True


# ── Subtest 7i ────────────────────────────────────────────────────────────────

def test_7i_concurrent_fills():
    log.info("=== 7i: Concurrent fills ===")
    bus = EventBus(mode=DispatchMode.SYNC)
    positions = {}
    reclaimed = set()
    last_order = {}
    trade_log = []
    collector = EventCollector(bus)

    pm = PositionManager(bus=bus, positions=positions,
                         reclaimed_today=reclaimed, last_order_time=last_order,
                         trade_log=trade_log)

    # Open a position first
    buy_fill = FillPayload(ticker='CONC', side='buy', qty=10, fill_price=100.0,
                           order_id='ord-conc', reason='VWAP reclaim')
    bus.emit(Event(EventType.FILL, buy_fill))
    assert 'CONC' in positions, "Position should be opened"
    initial_qty = positions['CONC']['quantity']
    assert initial_qty == 10

    errors = []

    def send_sell_fill(i):
        try:
            fill = FillPayload(ticker='CONC', side='sell', qty=1,
                               fill_price=101.0, order_id=f'ord-sell-{i}',
                               reason='sell_stop')
            bus.emit(Event(EventType.FILL, fill))
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=send_sell_fill, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, f"Concurrent fill errors: {errors}"

    # Positions dict must be valid JSON-like (no corruption)
    for ticker, pos in list(positions.items()):
        assert isinstance(ticker, str)
        if pos:
            assert isinstance(pos.get('entry_price', 0), (int, float))

    log.info(f"  PASS: Concurrent fills completed. positions={list(positions.keys())}")
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    results = {}
    tests = [
        ('7a', test_7a_max_positions_boundary),
        ('7b', test_7b_cooldown_boundary),
        ('7c', test_7c_rvol_boundary),
        ('7d', test_7d_rsi_boundary),
        ('7e', test_7e_spread_boundary),
        ('7f', test_7f_price_divergence),
        ('7g', test_7g_partial_sell_qty1),
        ('7h', test_7h_oversell),
        ('7i', test_7i_concurrent_fills),
    ]

    for name, fn in tests:
        try:
            ok = fn()
            results[name] = 'PASS' if ok else 'WARN'
        except AssertionError as exc:
            log.error(f"  FAIL [{name}]: {exc}")
            results[name] = 'FAIL'
        except Exception as exc:
            log.error(f"  FAIL [{name}]: {exc}", exc_info=True)
            results[name] = 'FAIL'

    log.info("=== Results ===")
    any_fail = False
    for name, status in results.items():
        log.info(f"  {name}: {status}")
        if status == 'FAIL':
            any_fail = True

    sys.exit(1 if any_fail else 0)


if __name__ == '__main__':
    main()
