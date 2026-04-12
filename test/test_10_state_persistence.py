"""
TEST 10: State Persistence & Crash Recovery
============================================
Tests monitor/state.py save/load, StateEngine, HeartbeatEmitter, EventLogger.

Subtests:
  10a: Round-trip save/load
  10b: Yesterday's state discarded
  10c: Corrupt JSON creates .corrupt backup
  10d: Concurrent save — valid JSON
  10e: Large state (200 positions, 1000 trades)
  10f: Missing keys in loaded state
  10g: StateEngine seed + POSITION events
  10h: HeartbeatEmitter interval gating
  10i: EventLogger — all EventTypes logged without crash
"""

import os
import sys
import json
import logging
import threading
import time
import tempfile
import shutil
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Log setup ────────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, 'test_10_state_persistence.log')

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, mode='w'),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger('test_10')

ET = ZoneInfo('America/New_York')

import monitor.state as state_module
from monitor.state import save_state, load_state
from monitor.event_bus import (
    EventBus, Event, EventType, DispatchMode,
    BarPayload, FillPayload, SignalPayload, OrderRequestPayload,
    PositionPayload, RiskBlockPayload, HeartbeatPayload, OrderFailPayload,
)
from monitor.events import PositionSnapshot
from monitor.state_engine import StateEngine
from monitor.observability import HeartbeatEmitter, EventLogger


# ── State file patching helper ────────────────────────────────────────────────

class TempStateFile:
    """Context manager that redirects state.py's _STATE_FILE to a temp file."""
    def __init__(self):
        self.tmpdir  = None
        self.tmpfile = None
        self._orig   = None

    def __enter__(self):
        self.tmpdir  = tempfile.mkdtemp()
        self.tmpfile = os.path.join(self.tmpdir, 'bot_state.json')
        self._orig   = state_module._STATE_FILE
        state_module._STATE_FILE = self.tmpfile
        return self.tmpfile

    def __exit__(self, *args):
        state_module._STATE_FILE = self._orig
        shutil.rmtree(self.tmpdir, ignore_errors=True)


# ── Sample data builders ──────────────────────────────────────────────────────

def make_positions(n=3):
    return {
        f'TICK{i:03d}': {
            'entry_price': 100.0 + i,
            'entry_time': '10:00:00',
            'quantity': 5 + i,
            'partial_done': False,
            'order_id': f'ord-{i:04d}',
            'stop_price': 99.0 + i,
            'target_price': 103.0 + i,
            'half_target': 101.5 + i,
            'atr_value': 0.5,
        }
        for i in range(n)
    }


def make_trade_log(n=5):
    return [
        {
            'ticker': f'TICK{i:03d}',
            'entry_price': 100.0 + i,
            'entry_time': '10:00:00',
            'exit_price': 101.5 + i,
            'qty': 5,
            'pnl': round((1.5 + i * 0.1) * 5, 2),
            'reason': 'sell_target',
            'time': '14:30:00',
            'is_win': True,
        }
        for i in range(n)
    ]


# ── Subtest 10a ───────────────────────────────────────────────────────────────

def test_10a_round_trip():
    log.info("=== 10a: Round-trip save/load ===")
    with TempStateFile() as tmpfile:
        positions       = make_positions(3)
        reclaimed_today = {'AAPL', 'MSFT', 'NVDA'}
        trade_log       = make_trade_log(5)

        save_state(positions, reclaimed_today, trade_log)
        assert os.path.exists(tmpfile), "State file not created"

        loaded_positions, loaded_reclaimed, loaded_trades = load_state()

        # Verify positions
        assert set(loaded_positions.keys()) == set(positions.keys()), \
            f"Positions keys mismatch: {set(loaded_positions.keys())} != {set(positions.keys())}"
        for ticker, pos in positions.items():
            loaded_pos = loaded_positions[ticker]
            assert abs(loaded_pos['entry_price'] - pos['entry_price']) < 0.001, \
                f"{ticker} entry_price mismatch"
            assert loaded_pos['quantity'] == pos['quantity'], \
                f"{ticker} quantity mismatch"

        # Verify reclaimed_today
        assert loaded_reclaimed == reclaimed_today, \
            f"reclaimed_today mismatch: {loaded_reclaimed} != {reclaimed_today}"

        # Verify trade_log length and PnL
        assert len(loaded_trades) == len(trade_log), \
            f"trade_log length mismatch: {len(loaded_trades)} != {len(trade_log)}"
        total_pnl_orig   = sum(t['pnl'] for t in trade_log)
        total_pnl_loaded = sum(t['pnl'] for t in loaded_trades)
        assert abs(total_pnl_orig - total_pnl_loaded) < 0.01, \
            f"PnL mismatch: {total_pnl_orig:.2f} != {total_pnl_loaded:.2f}"

    log.info("  PASS: Round-trip save/load verified")
    return True


# ── Subtest 10b ───────────────────────────────────────────────────────────────

def test_10b_yesterday_state_discarded():
    log.info("=== 10b: Yesterday's state discarded ===")
    with TempStateFile() as tmpfile:
        positions  = make_positions(2)
        reclaimed  = {'AAPL'}
        trade_log  = make_trade_log(2)
        save_state(positions, reclaimed, trade_log)

        # Manually overwrite date to yesterday
        with open(tmpfile, 'r') as f:
            state = json.load(f)
        yesterday = (datetime.now(ET) - timedelta(days=1)).strftime('%Y-%m-%d')
        state['date'] = yesterday
        with open(tmpfile, 'w') as f:
            json.dump(state, f)

        loaded_positions, loaded_reclaimed, loaded_trades = load_state()

        assert loaded_positions == {}, \
            f"Yesterday's positions should be empty, got {loaded_positions}"
        assert loaded_reclaimed == set(), \
            f"Yesterday's reclaimed should be empty, got {loaded_reclaimed}"
        assert loaded_trades == [], \
            f"Yesterday's trade_log should be empty, got {loaded_trades}"

    log.info("  PASS: Yesterday's state correctly discarded")
    return True


# ── Subtest 10c ───────────────────────────────────────────────────────────────

def test_10c_corrupt_json():
    log.info("=== 10c: Corrupt JSON creates .corrupt backup ===")
    with TempStateFile() as tmpfile:
        # Write malformed JSON
        with open(tmpfile, 'w') as f:
            f.write('{ this is not valid JSON !!! }')

        loaded_positions, loaded_reclaimed, loaded_trades = load_state()

        # Should return empty state
        assert loaded_positions == {}, "Corrupt JSON should return empty positions"
        assert loaded_reclaimed == set(), "Corrupt JSON should return empty reclaimed"
        assert loaded_trades == [], "Corrupt JSON should return empty trade_log"

        # .corrupt backup should exist
        corrupt_path = tmpfile + '.corrupt'
        assert os.path.exists(corrupt_path), \
            f"Corrupt backup file {corrupt_path} not created"

    log.info("  PASS: Corrupt JSON returns empty state and creates .corrupt backup")
    return True


# ── Subtest 10d ───────────────────────────────────────────────────────────────

def test_10d_concurrent_save():
    log.info("=== 10d: Concurrent save — valid JSON ===")
    with TempStateFile() as tmpfile:
        errors = []

        def save_worker(i):
            try:
                positions  = make_positions(i % 5 + 1)
                reclaimed  = {f'T{j}' for j in range(i % 3)}
                trade_log  = make_trade_log(i % 10)
                save_state(positions, reclaimed, trade_log)
            except Exception as e:
                errors.append(f"worker {i}: {e}")

        threads = [threading.Thread(target=save_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent save errors: {errors}"

        # Final file must be valid JSON
        if os.path.exists(tmpfile):
            try:
                with open(tmpfile, 'r') as f:
                    data = json.load(f)
                assert isinstance(data, dict), f"State file is not a JSON object: {type(data)}"
                assert 'date' in data, "State file missing 'date' key"
            except json.JSONDecodeError as e:
                raise AssertionError(f"State file is corrupt JSON after concurrent writes: {e}")

    log.info("  PASS: Concurrent save produced valid JSON")
    return True


# ── Subtest 10e ───────────────────────────────────────────────────────────────

def test_10e_large_state():
    log.info("=== 10e: Large state (200 positions, 1000 trades) ===")
    with TempStateFile() as tmpfile:
        positions  = make_positions(200)
        reclaimed  = {f'T{i:04d}' for i in range(100)}
        trade_log  = make_trade_log(1000)

        t0 = time.time()
        save_state(positions, reclaimed, trade_log)
        save_elapsed = time.time() - t0

        t1 = time.time()
        loaded_positions, loaded_reclaimed, loaded_trades = load_state()
        load_elapsed = time.time() - t1

        assert len(loaded_positions) == 200, \
            f"Expected 200 positions, got {len(loaded_positions)}"
        assert len(loaded_trades) == 1000, \
            f"Expected 1000 trades, got {len(loaded_trades)}"

        log.info(f"  Save: {save_elapsed*1000:.1f}ms, Load: {load_elapsed*1000:.1f}ms")

    log.info("  PASS: Large state (200 positions, 1000 trades) round-trips correctly")
    return True


# ── Subtest 10f ───────────────────────────────────────────────────────────────

def test_10f_missing_keys():
    log.info("=== 10f: Missing keys in loaded state ===")
    with TempStateFile() as tmpfile:
        today = datetime.now(ET).strftime('%Y-%m-%d')
        # Write a state file missing 'reclaimed_today'
        partial_state = {
            'date':      today,
            'positions': {'AAPL': {'entry_price': 100.0}},
            'trade_log': [],
            # 'reclaimed_today' intentionally missing
        }
        with open(tmpfile, 'w') as f:
            json.dump(partial_state, f)

        loaded_positions, loaded_reclaimed, loaded_trades = load_state()

        assert isinstance(loaded_reclaimed, set), \
            f"reclaimed_today should be empty set, got {type(loaded_reclaimed)}"
        assert loaded_reclaimed == set(), \
            f"Missing reclaimed_today should return empty set, got {loaded_reclaimed}"
        assert 'AAPL' in loaded_positions, "AAPL position should still be loaded"

    log.info("  PASS: Missing 'reclaimed_today' key returns empty set without crash")
    return True


# ── Subtest 10g ───────────────────────────────────────────────────────────────

def test_10g_state_engine_seed_and_positions():
    log.info("=== 10g: StateEngine seed + POSITION events ===")
    bus = EventBus(mode=DispatchMode.SYNC)
    se  = StateEngine(bus)

    # Seed with 3 positions
    seed_positions = make_positions(3)
    seed_trades    = make_trade_log(2)
    se.seed(seed_positions, seed_trades)

    snap = se.snapshot()
    assert len(snap['positions']) == 3, f"Expected 3 seeded positions, got {len(snap['positions'])}"
    assert len(snap['trade_log']) == 2, f"Expected 2 seeded trades, got {len(snap['trade_log'])}"

    # Open a new position via POSITION event
    snap0 = PositionSnapshot(
        entry_price=105.0, entry_time='11:00:00', quantity=3,
        partial_done=False, order_id='test-001',
        stop_price=104.0, target_price=108.0, half_target=106.5,
        atr_value=0.5,
    )
    bus.emit(Event(EventType.POSITION,
                   PositionPayload(ticker='NEWT', action='opened', position=snap0)))

    snap = se.snapshot()
    assert 'NEWT' in snap['positions'], "NEWT should be in snapshot after OPENED"
    assert len(snap['positions']) == 4

    # Partially exit TICK000
    snap1 = PositionSnapshot(
        entry_price=100.0, entry_time='10:00:00', quantity=2,
        partial_done=True, order_id='ord-0000',
        stop_price=100.0, target_price=103.0, half_target=101.5,
        atr_value=0.5,
    )
    bus.emit(Event(EventType.POSITION,
                   PositionPayload(ticker='TICK000', action='partial_exit',
                                   position=snap1, pnl=5.0)))

    snap = se.snapshot()
    assert snap['positions']['TICK000'].quantity == 2, \
        f"Partial exit: expected qty=2, got {snap['positions']['TICK000'].quantity}"

    # Close TICK001
    bus.emit(Event(EventType.POSITION,
                   PositionPayload(ticker='TICK001', action='closed',
                                   position=None, pnl=-3.0)))

    snap = se.snapshot()
    assert 'TICK001' not in snap['positions'], "TICK001 should be removed after CLOSED"
    assert len(snap['positions']) == 3  # TICK000, TICK002, NEWT

    total_pnl = snap['total_pnl']
    assert total_pnl == round(sum(t['pnl'] for t in snap['trade_log']), 2), \
        f"total_pnl calculation mismatch: {total_pnl}"

    log.info(f"  PASS: StateEngine seed + events work. "
             f"positions={list(snap['positions'].keys())} pnl=${total_pnl:.2f}")
    return True


# ── Subtest 10h ───────────────────────────────────────────────────────────────

def test_10h_heartbeat_emitter_interval_gating():
    log.info("=== 10h: HeartbeatEmitter interval gating ===")
    bus = EventBus(mode=DispatchMode.SYNC)
    se  = StateEngine(bus)
    heartbeat_count = [0]

    def on_hb(event):
        heartbeat_count[0] += 1

    bus.subscribe(EventType.HEARTBEAT, on_hb)

    interval_sec = 0.1  # 100ms for fast testing
    hb = HeartbeatEmitter(bus, se, n_tickers=5, interval_sec=interval_sec)

    # Call tick() 100 times rapidly (< 1ms apart each) — only 1 HB should fire
    for _ in range(100):
        hb.tick()

    assert heartbeat_count[0] == 1, \
        f"Expected exactly 1 heartbeat from 100 rapid ticks, got {heartbeat_count[0]}"
    log.info(f"  100 rapid ticks -> 1 HEARTBEAT (correct)")

    # Wait for interval to elapse, then call tick() again
    time.sleep(interval_sec + 0.05)
    hb.tick()

    assert heartbeat_count[0] == 2, \
        f"Expected 2nd heartbeat after interval, got {heartbeat_count[0]}"
    log.info(f"  PASS: Interval gating correct. Total heartbeats={heartbeat_count[0]}")
    return True


# ── Subtest 10i ───────────────────────────────────────────────────────────────

def test_10i_event_logger_all_types():
    log.info("=== 10i: EventLogger — all EventTypes logged without crash ===")
    bus = EventBus(mode=DispatchMode.SYNC)
    logged_types = []

    # Install EventLogger (subscribes to all EventTypes)
    el = EventLogger(bus)

    # Track what gets emitted
    def tracker(event):
        logged_types.append(event.type)
    for et in EventType:
        bus.subscribe(et, tracker)

    today = datetime.now(ET).replace(hour=10, minute=0, second=0, microsecond=0)
    idx = pd.date_range(today, periods=35, freq='1min', tz=ET)
    df = pd.DataFrame({
        'open': np.full(35, 100.0), 'high': np.full(35, 100.5),
        'low': np.full(35, 99.5),   'close': np.full(35, 100.0),
        'volume': np.full(35, 10_000.0),
    }, index=idx)

    events_to_emit = [
        Event(EventType.BAR, BarPayload(ticker='AAPL', df=df)),
        Event(EventType.FILL, FillPayload(
            ticker='AAPL', side='buy', qty=5, fill_price=100.0,
            order_id=str(uuid.uuid4()), reason='VWAP reclaim',
        )),
        Event(EventType.SIGNAL, SignalPayload(
            ticker='AAPL', action='buy',
            current_price=100.0, ask_price=100.0, atr_value=0.5,
            rsi_value=60.0, rvol=3.0, vwap=99.5,
            stop_price=99.0, target_price=102.0, half_target=101.0,
            reclaim_candle_low=99.8,
        )),
        Event(EventType.ORDER_REQ, OrderRequestPayload(
            ticker='AAPL', side='buy', qty=5, price=100.0, reason='VWAP reclaim',
        )),
        Event(EventType.POSITION, PositionPayload(
            ticker='AAPL', action='opened',
            position=PositionSnapshot(
                entry_price=100.0, entry_time='10:00:00', quantity=5,
                partial_done=False, order_id=str(uuid.uuid4()),
                stop_price=99.0, target_price=102.0, half_target=101.0,
                atr_value=0.5,
            ),
        )),
        Event(EventType.RISK_BLOCK, RiskBlockPayload(
            ticker='AAPL', reason='max positions reached', signal_action='buy',
        )),
        Event(EventType.ORDER_FAIL, OrderFailPayload(
            ticker='AAPL', side='buy', qty=5, price=100.0, reason='abandoned',
        )),
        Event(EventType.HEARTBEAT, HeartbeatPayload(
            n_tickers=10, n_positions=2, open_tickers=['AAPL', 'MSFT'],
            n_trades=5, n_wins=3, total_pnl=125.0,
        )),
    ]

    errors = []
    for ev in events_to_emit:
        try:
            bus.emit(ev)
        except Exception as e:
            errors.append(f"{ev.type.name}: {e}")

    assert not errors, f"EventLogger caused errors: {errors}"

    unique_types = set(logged_types)
    expected_types = {e.type for e in events_to_emit}
    assert expected_types.issubset(unique_types), \
        f"Not all EventTypes logged: missing {expected_types - unique_types}"

    # At least 8 distinct event types emitted and received
    assert len(unique_types) >= 8, \
        f"Expected at least 8 EventTypes logged, got {len(unique_types)}: {unique_types}"

    log.info(f"  PASS: EventLogger handled all {len(unique_types)} EventTypes without crash")
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    results = {}
    tests = [
        ('10a', test_10a_round_trip),
        ('10b', test_10b_yesterday_state_discarded),
        ('10c', test_10c_corrupt_json),
        ('10d', test_10d_concurrent_save),
        ('10e', test_10e_large_state),
        ('10f', test_10f_missing_keys),
        ('10g', test_10g_state_engine_seed_and_positions),
        ('10h', test_10h_heartbeat_emitter_interval_gating),
        ('10i', test_10i_event_logger_all_types),
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
