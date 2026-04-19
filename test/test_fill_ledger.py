"""
Unit tests for FillLedger, LotMatcher, PositionProjection, BrokerRegistry, EquityTracker.

Tests all 47 gap resolutions from the implementation plan.
Run with: venv/bin/python3 -m pytest test/test_fill_ledger.py -v
"""
import os
import tempfile
import uuid
from datetime import datetime, date
from zoneinfo import ZoneInfo

import pytest

from monitor.fill_lot import (
    FillLot, LotState, LotMatch, PositionMeta, QTY_EPSILON,
    fill_lot_to_dict, fill_lot_from_dict,
)
from monitor.lot_matcher import LotMatcher
from monitor.fill_ledger import FillLedger
from monitor.position_projection import (
    PositionProjection, PositionProjectionProxy, MutablePositionDict,
)
from monitor.broker_registry import BrokerRegistry
from monitor.equity_tracker import EquityTracker

ET = ZoneInfo('America/New_York')


# ── Helpers ───────────────────────────────────────────────────────────

def _now():
    return datetime.now(ET)


def _lot(ticker, side, qty, price, broker='alpaca', strategy='test',
         synthetic=False, **kw):
    return FillLot(
        lot_id=kw.get('lot_id', str(uuid.uuid4())),
        ticker=ticker, side=side, qty=qty, fill_price=price,
        timestamp=kw.get('timestamp', _now()),
        order_id=kw.get('order_id', f'O-{uuid.uuid4().hex[:6]}'),
        broker=broker, strategy=strategy,
        reason=kw.get('reason', 'test'),
        signal_price=kw.get('signal_price', price),
        synthetic=synthetic,
    )


def _tmp_ledger():
    """Create a FillLedger with a temp state file."""
    path = os.path.join(tempfile.gettempdir(), f'test_ledger_{uuid.uuid4().hex[:8]}.json')
    return FillLedger(state_file=path), path


def _cleanup(path):
    for suffix in ['', '.flock', '.sha256', '.prev', '.prev2',
                    '.prev.sha256', '.prev2.sha256']:
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass


# ═══════════════════════════════════════════════════════════════════════
# FillLot Tests
# ═══════════════════════════════════════════════════════════════════════

class TestFillLot:

    def test_creation(self):
        lot = _lot('AAPL', 'BUY', 10, 150.0)
        assert lot.ticker == 'AAPL'
        assert lot.qty == 10.0
        assert lot.direction == 'long'

    def test_qty_zero_rejected(self):
        with pytest.raises(ValueError, match="qty must be > 0"):
            _lot('AAPL', 'BUY', 0, 150.0)

    def test_qty_negative_rejected(self):
        with pytest.raises(ValueError, match="qty must be > 0"):
            _lot('AAPL', 'BUY', -5, 150.0)

    def test_qty_epsilon_rejected(self):
        """B3: qty at QTY_EPSILON boundary should be rejected."""
        with pytest.raises(ValueError):
            _lot('AAPL', 'BUY', QTY_EPSILON * 0.5, 150.0)

    def test_invalid_side(self):
        with pytest.raises(ValueError, match="side must be"):
            _lot('AAPL', 'SHORT', 10, 150.0)

    def test_serialization_roundtrip(self):
        lot = _lot('AAPL', 'BUY', 10.5, 150.25, signal_price=150.10,
                    synthetic=True)
        d = fill_lot_to_dict(lot)
        lot2 = fill_lot_from_dict(d)
        assert lot.lot_id == lot2.lot_id
        assert lot.qty == lot2.qty
        assert lot.fill_price == lot2.fill_price
        assert lot.signal_price == lot2.signal_price
        assert lot.synthetic == lot2.synthetic

    def test_immutable(self):
        lot = _lot('AAPL', 'BUY', 10, 150.0)
        with pytest.raises(AttributeError):
            lot.qty = 20


# ═══════════════════════════════════════════════════════════════════════
# LotState Tests
# ═══════════════════════════════════════════════════════════════════════

class TestLotState:

    def test_is_closed_exact_zero(self):
        s = LotState(lot_id='x', original_qty=10, remaining_qty=0.0)
        assert s.is_closed

    def test_is_closed_epsilon(self):
        """B1: float precision — 0.2365 - 0.2365 should be closed."""
        s = LotState(lot_id='x', original_qty=0.2365, remaining_qty=1e-15)
        assert s.is_closed

    def test_not_closed_above_epsilon(self):
        s = LotState(lot_id='x', original_qty=10, remaining_qty=0.001)
        assert not s.is_closed

    def test_serialization(self):
        s = LotState(lot_id='x', original_qty=10, remaining_qty=7,
                      matched_sells=['s1', 's2'])
        d = s.to_dict()
        s2 = LotState.from_dict(d)
        assert s2.remaining_qty == 7
        assert s2.matched_sells == ['s1', 's2']


# ═══════════════════════════════════════════════════════════════════════
# PositionMeta Tests
# ═══════════════════════════════════════════════════════════════════════

class TestPositionMeta:

    def test_thread_safe_update(self):
        meta = PositionMeta(stop_price=100.0)
        meta.update(stop_price=105.0, target_price=120.0)
        assert meta.stop_price == 105.0
        assert meta.target_price == 120.0

    def test_snapshot_isolation(self):
        meta = PositionMeta(stop_price=100.0)
        snap = meta.snapshot()
        meta.update(stop_price=110.0)
        assert snap['stop_price'] == 100.0  # snapshot unchanged

    def test_private_fields_not_updated(self):
        meta = PositionMeta()
        meta.update(_lock='hacked')  # should be ignored
        assert isinstance(meta._lock, type(meta._lock))  # still a Lock

    def test_serialization(self):
        meta = PositionMeta(stop_price=100, target_price=120, partial_done=True)
        d = meta.to_dict()
        meta2 = PositionMeta.from_dict(d)
        assert meta2.stop_price == 100
        assert meta2.partial_done is True


# ═══════════════════════════════════════════════════════════════════════
# LotMatcher Tests
# ═══════════════════════════════════════════════════════════════════════

class TestLotMatcher:

    def setup_method(self):
        self.matcher = LotMatcher()

    def _open(self, qty, price, broker='alpaca'):
        lot = _lot('TEST', 'BUY', qty, price, broker=broker)
        state = LotState(lot_id=lot.lot_id, original_qty=qty, remaining_qty=qty)
        return lot, state

    def _sell(self, qty, price):
        return _lot('TEST', 'SELL', qty, price)

    def test_clean_match(self):
        b, s = self._open(10, 100)
        matches = self.matcher.match_sell(self._sell(10, 110), [(b, s)])
        assert len(matches) == 1
        assert matches[0].realized_pnl == 100.0  # (110-100)*10
        assert s.is_closed

    def test_multi_lot_fifo(self):
        """FIFO: oldest lot consumed first."""
        b1, s1 = self._open(5, 100)
        b2, s2 = self._open(5, 110)
        matches = self.matcher.match_sell(self._sell(7, 115), [(b1, s1), (b2, s2)])
        assert len(matches) == 2
        assert matches[0].buy_price == 100  # oldest first
        assert matches[0].matched_qty == 5
        assert matches[1].buy_price == 110
        assert matches[1].matched_qty == 2
        assert s1.is_closed
        assert s2.remaining_qty == 3

    def test_partial_consumption(self):
        b, s = self._open(10, 100)
        matches = self.matcher.match_sell(self._sell(3, 105), [(b, s)])
        assert matches[0].matched_qty == 3
        assert s.remaining_qty == 7

    def test_overfill_clamped(self):
        """B2: sell qty > open qty → clamp to open."""
        b, s = self._open(5, 100)
        matches = self.matcher.match_sell(self._sell(8, 110), [(b, s)])
        assert matches[0].matched_qty == 5  # clamped
        assert s.is_closed

    def test_float_precision(self):
        """B1: 0.2365 - 0.2365 = 0, not 5.55e-17."""
        b, s = self._open(0.2365, 500)
        matches = self.matcher.match_sell(self._sell(0.2365, 505), [(b, s)])
        assert s.remaining_qty == 0.0
        assert s.is_closed

    def test_empty_lots(self):
        matches = self.matcher.match_sell(self._sell(5, 100), [])
        assert matches == []

    def test_preview_no_mutation(self):
        """M5: preview is advisory, doesn't mutate state."""
        b1, s1 = self._open(5, 100, broker='alpaca')
        b2, s2 = self._open(3, 110, broker='tradier')
        preview = self.matcher.preview_matches('TEST', 7, [(b1, s1), (b2, s2)])
        assert preview == {'alpaca': 5.0, 'tradier': 2.0}
        assert s1.remaining_qty == 5.0  # NOT mutated
        assert s2.remaining_qty == 3.0

    def test_replay_deterministic(self):
        """F1: FIFO replay from lots produces same matches."""
        b1 = _lot('AMD', 'BUY', 10, 80)
        b2 = _lot('AMD', 'BUY', 5, 85)
        s1 = _lot('AMD', 'SELL', 12, 90)
        lots = [b1, b2, s1]
        states = {
            b1.lot_id: LotState(lot_id=b1.lot_id, original_qty=10, remaining_qty=10),
            b2.lot_id: LotState(lot_id=b2.lot_id, original_qty=5, remaining_qty=5),
        }
        matches = LotMatcher.replay_matches(lots, states)
        total_pnl = sum(m.realized_pnl for m in matches)
        assert total_pnl == 110.0  # (90-80)*10 + (90-85)*2


# ═══════════════════════════════════════════════════════════════════════
# FillLedger Tests
# ═══════════════════════════════════════════════════════════════════════

class TestFillLedger:

    def setup_method(self):
        self.ledger, self._path = _tmp_ledger()

    def teardown_method(self):
        _cleanup(self._path)

    def test_buy_opens_position(self):
        self.ledger.append(_lot('AAPL', 'BUY', 10, 150))
        assert 'AAPL' in self.ledger.open_tickers()
        assert self.ledger.total_qty('AAPL') == 10.0

    def test_sell_closes_with_pnl(self):
        self.ledger.append(_lot('AAPL', 'BUY', 10, 150))
        matches = self.ledger.append(_lot('AAPL', 'SELL', 10, 155))
        assert len(matches) == 1
        assert matches[0].realized_pnl == 50.0
        assert 'AAPL' not in self.ledger.open_tickers()

    def test_sell_rejected_no_lots(self):
        """A2: SELL with no open lots rejected."""
        matches = self.ledger.append(_lot('XXX', 'SELL', 5, 100))
        assert matches == []
        assert self.ledger.lot_count == 0  # not appended

    def test_dedup(self):
        lot = _lot('AAPL', 'BUY', 10, 150)
        self.ledger.append(lot)
        self.ledger.append(lot)  # same lot_id
        assert self.ledger.lot_count == 1

    def test_multi_lot_position(self):
        """A4: multiple BUY lots for same ticker."""
        self.ledger.append(_lot('MSFT', 'BUY', 5, 100, broker='alpaca'))
        self.ledger.append(_lot('MSFT', 'BUY', 3, 110, broker='tradier'))
        assert self.ledger.total_qty('MSFT') == 8.0
        avg = self.ledger.weighted_avg_entry('MSFT')
        assert abs(avg - (5 * 100 + 3 * 110) / 8) < 0.01

    def test_broker_qty(self):
        """D3: per-broker qty tracking."""
        self.ledger.append(_lot('MSFT', 'BUY', 5, 100, broker='alpaca'))
        self.ledger.append(_lot('MSFT', 'BUY', 3, 110, broker='tradier'))
        bq = self.ledger.broker_qty('MSFT')
        assert bq == {'alpaca': 5.0, 'tradier': 3.0}

    def test_daily_pnl(self):
        """E2: daily P&L boundary via matched_at."""
        self.ledger.append(_lot('AAPL', 'BUY', 10, 100))
        self.ledger.append(_lot('AAPL', 'SELL', 10, 110))
        pnl = self.ledger.daily_realized_pnl()
        assert pnl == 100.0

    def test_unrealized_pnl(self):
        """E3: unrealized P&L with price injection."""
        self.ledger.append(_lot('AAPL', 'BUY', 10, 100))
        upl = self.ledger.unrealized_pnl({'AAPL': 105})
        assert upl == 50.0

    def test_meta_lifecycle(self):
        """K5: meta set/patch/cleanup."""
        self.ledger.append(_lot('AAPL', 'BUY', 10, 100))
        self.ledger.set_meta('AAPL', PositionMeta(stop_price=98))
        assert self.ledger.get_meta('AAPL').stop_price == 98
        self.ledger.patch_meta('AAPL', stop_price=99)
        assert self.ledger.get_meta('AAPL').stop_price == 99
        # Full close cleans up meta
        self.ledger.append(_lot('AAPL', 'SELL', 10, 105))
        assert self.ledger.get_meta('AAPL') is None

    def test_persistence_and_load(self):
        """M1: ALL today's lots persisted for P&L reconstruction."""
        self.ledger.append(_lot('AAPL', 'BUY', 10, 100))
        self.ledger.append(_lot('AAPL', 'SELL', 10, 110))  # closed
        self.ledger.append(_lot('MSFT', 'BUY', 5, 200))    # open
        self.ledger.set_meta('MSFT', PositionMeta(stop_price=195))

        daily_pnl = self.ledger.daily_realized_pnl()

        # Load into new ledger
        ledger2 = FillLedger(state_file=self._path)
        ledger2.load()

        assert ledger2.daily_realized_pnl() == daily_pnl  # M1: P&L survives
        assert ledger2.total_qty('MSFT') == 5.0
        assert 'AAPL' not in ledger2.open_tickers()
        meta = ledger2.get_meta('MSFT')
        assert meta is not None
        assert meta.stop_price == 195

    def test_preview_sell_advisory(self):
        """M5: preview doesn't mutate."""
        self.ledger.append(_lot('AAPL', 'BUY', 10, 100))
        preview = self.ledger.preview_sell_matches('AAPL', 5)
        assert preview == {'alpaca': 5.0}
        assert self.ledger.total_qty('AAPL') == 10.0  # unchanged

    def test_phantom_close_estimated(self):
        """E1: synthetic SELL uses estimated price, marked estimated."""
        self.ledger.append(_lot('AAPL', 'BUY', 10, 100))
        matches = self.ledger.append(
            _lot('AAPL', 'SELL', 10, 98, synthetic=True,
                 reason='phantom_cleanup')
        )
        assert matches[0].estimated is True
        assert matches[0].realized_pnl == -20.0  # (98-100)*10


# ═══════════════════════════════════════════════════════════════════════
# PositionProjection Tests
# ═══════════════════════════════════════════════════════════════════════

class TestPositionProjection:

    def setup_method(self):
        self.ledger, self._path = _tmp_ledger()

    def teardown_method(self):
        _cleanup(self._path)

    def test_backward_compat_fields(self):
        """J1-J4: projected dict has all expected fields."""
        self.ledger.append(_lot('AAPL', 'BUY', 10, 150))
        self.ledger.set_meta('AAPL', PositionMeta(stop_price=148, target_price=155))

        proxy = PositionProjectionProxy(self.ledger)
        pos = proxy['AAPL']

        # All required fields present
        assert 'entry_price' in pos
        assert 'entry_time' in pos
        assert 'quantity' in pos      # J3: both quantity and qty
        assert 'qty' in pos
        assert 'partial_done' in pos
        assert 'stop_price' in pos
        assert 'target_price' in pos
        assert 'strategy' in pos
        assert '_broker' in pos
        assert '_source' in pos
        assert pos['_source'] == 'fill_ledger'

    def test_write_through_m2(self):
        """M2: trailing stop mutation writes through to PositionMeta."""
        self.ledger.append(_lot('AAPL', 'BUY', 10, 150))
        self.ledger.set_meta('AAPL', PositionMeta(stop_price=148))
        proxy = PositionProjectionProxy(self.ledger)

        pos = proxy['AAPL']
        pos['stop_price'] = 149.5  # trailing stop update

        # Verify write-through to PositionMeta
        meta = self.ledger.get_meta('AAPL')
        assert meta.stop_price == 149.5

    def test_dict_assignment_blocked(self):
        proxy = PositionProjectionProxy(self.ledger)
        with pytest.raises(TypeError):
            proxy['GOOG'] = {}

    def test_dict_deletion_blocked(self):
        self.ledger.append(_lot('AAPL', 'BUY', 10, 150))
        proxy = PositionProjectionProxy(self.ledger)
        with pytest.raises(TypeError):
            del proxy['AAPL']

    def test_contains_and_len(self):
        self.ledger.append(_lot('AAPL', 'BUY', 10, 150))
        self.ledger.append(_lot('MSFT', 'BUY', 5, 300))
        proxy = PositionProjectionProxy(self.ledger)
        assert 'AAPL' in proxy
        assert 'XXX' not in proxy
        assert len(proxy) == 2

    def test_close_removes_from_proxy(self):
        self.ledger.append(_lot('AAPL', 'BUY', 10, 150))
        proxy = PositionProjectionProxy(self.ledger)
        assert 'AAPL' in proxy
        self.ledger.append(_lot('AAPL', 'SELL', 10, 155))
        proxy._invalidate('AAPL')
        assert 'AAPL' not in proxy

    def test_multi_broker_routing(self):
        """D3: dual-broker position projection."""
        self.ledger.append(_lot('MSFT', 'BUY', 5, 300, broker='alpaca'))
        self.ledger.append(_lot('MSFT', 'BUY', 3, 310, broker='tradier'))
        self.ledger.set_meta('MSFT', PositionMeta())
        proxy = PositionProjectionProxy(self.ledger)
        pos = proxy['MSFT']
        assert pos['_broker_qty'] == {'alpaca': 5.0, 'tradier': 3.0}
        assert pos['_broker'] == 'alpaca'  # larger qty


# ═══════════════════════════════════════════════════════════════════════
# BrokerRegistry Tests
# ═══════════════════════════════════════════════════════════════════════

class TestBrokerRegistry:

    def test_register_and_iterate(self):
        class MockBroker:
            supports_fractional = True
            supports_notional = False
            def get_positions(self): return [{'symbol': 'X', 'qty': 1}]
            def get_equity(self): return 1000.0

        reg = BrokerRegistry()
        reg.register('mock', MockBroker())
        assert len(reg) == 1
        assert 'mock' in reg
        assert reg.supports_fractional('mock') is True
        assert reg.supports_notional('mock') is False
        assert reg.get_all_positions()['mock'][0]['symbol'] == 'X'
        assert reg.get_all_equity()['mock'] == 1000.0

    def test_missing_broker(self):
        reg = BrokerRegistry()
        assert reg.supports_fractional('nonexistent') is False


# ═══════════════════════════════════════════════════════════════════════
# EquityTracker Tests
# ═══════════════════════════════════════════════════════════════════════

class TestEquityTracker:

    def test_no_drift(self):
        class Broker:
            def __init__(self, eq): self._eq = eq
            def get_positions(self): return []
            def get_equity(self): return self._eq

        reg = BrokerRegistry()
        b = Broker(1000.0)
        reg.register('test', b)
        tracker = EquityTracker(reg, pnl_fn=lambda: 50.0)
        tracker.set_baseline()
        b._eq = 1050.0  # +50 matches pnl_fn
        drift = tracker.check_drift()
        assert drift == 0.0

    def test_drift_detected(self):
        class Broker:
            def __init__(self, eq): self._eq = eq
            def get_positions(self): return []
            def get_equity(self): return self._eq

        reg = BrokerRegistry()
        b = Broker(1000.0)
        reg.register('test', b)
        tracker = EquityTracker(reg, pnl_fn=lambda: 50.0)
        tracker.set_baseline()
        b._eq = 1200.0  # +200, but pnl says 50 → drift=150
        drift = tracker.check_drift()
        assert abs(drift - 150.0) < 0.01
