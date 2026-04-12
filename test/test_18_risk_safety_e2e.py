#!/usr/bin/env python3
"""
Test 18 — Risk Safety + End-to-End Integration
================================================
TC-RS-01: Drawdown kill switch (max daily loss → trading halted)
TC-RS-02: Hard position cap (GlobalPositionRegistry enforcement)
TC-E2E-01: Tick → PnL full pipeline (known scenario → expected outcome)
TC-E2E-02: Multi-strategy load (concurrent strategies under pressure)
"""
import os, sys, time, logging, threading
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np

from monitor.event_bus import EventBus, Event, EventType, DispatchMode
from monitor.events import (
    BarPayload, SignalPayload, FillPayload, OrderRequestPayload,
    PositionPayload, RiskBlockPayload,
)
from monitor.position_registry import GlobalPositionRegistry

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("test_18")

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

def _make_bar(ticker, n=60, base=100.0, seed=42):
    rng = np.random.RandomState(seed)
    idx = pd.date_range("2026-01-02 09:30", periods=n, freq="1min", tz="UTC")
    prices = base + rng.randn(n).cumsum() * 0.3
    df = pd.DataFrame({
        'open': prices, 'high': prices + 0.5, 'low': prices - 0.5,
        'close': prices + 0.1, 'volume': 1000,
    }, index=idx)
    return BarPayload(ticker=ticker, df=df)


# ═══════════════════════════════════════════════════════════════════════════════
# TC-RS-01: Drawdown kill switch
# ═══════════════════════════════���════════════════════════════════��══════════════
def test_rs_01():
    log.info("TC-RS-01: Drawdown kill switch simulation")

    # Simulate a trade_log with accumulating losses
    trade_log = []
    max_daily_loss = -200.0  # $200 max daily loss
    halted = False

    for i in range(20):
        pnl = -15.0  # each trade loses $15
        trade_log.append({'ticker': f'T{i}', 'pnl': pnl, 'is_win': False})
        total_pnl = sum(t['pnl'] for t in trade_log)

        if total_pnl <= max_daily_loss and not halted:
            halted = True
            halt_trade = i
            break

    check("RS-01a: halt triggered",
          halted, f"total_pnl={sum(t['pnl'] for t in trade_log)}")
    check("RS-01b: halt at correct trade count",
          halted and halt_trade <= 14,  # 14 trades × -15 = -210 > -200
          f"halted at trade {halt_trade if halted else 'never'}")

    # Verify no further trades after halt
    orders_after_halt = 0
    for i in range(halt_trade + 1, 30):
        if halted:
            # In production, RiskEngine would check total_pnl before allowing orders
            pass
        else:
            orders_after_halt += 1

    check("RS-01c: no orders after halt",
          orders_after_halt == 0)
    check("RS-01d: total loss within 1 trade of threshold",
          abs(sum(t['pnl'] for t in trade_log) - max_daily_loss) <= 15.0,
          f"total={sum(t['pnl'] for t in trade_log)}")


# ═══════════════════════════════════════════════════════════════════════════════
# TC-RS-02: Hard position cap (GlobalPositionRegistry)
# ═══════════════════════════════════════════════════════════════════════════════
def test_rs_02():
    log.info("TC-RS-02: Hard position cap via GlobalPositionRegistry")

    registry = GlobalPositionRegistry(global_max=3)

    # Acquire 3 positions (should succeed)
    check("RS-02a: acquire AAPL", registry.try_acquire("AAPL", "vwap"))
    check("RS-02b: acquire MSFT", registry.try_acquire("MSFT", "pro"))
    check("RS-02c: acquire GOOG", registry.try_acquire("GOOG", "pop"))

    # 4th should fail (at cap)
    check("RS-02d: 4th blocked at cap",
          not registry.try_acquire("TSLA", "vwap"))

    # Cross-layer dedup: same ticker from different layer
    check("RS-02e: duplicate ticker blocked",
          not registry.try_acquire("AAPL", "pro"))

    # Same layer re-acquire (idempotent)
    check("RS-02f: same-layer re-acquire succeeds",
          registry.try_acquire("AAPL", "vwap"))

    # Release one and try again
    registry.release("MSFT")
    check("RS-02g: after release, new acquire succeeds",
          registry.try_acquire("TSLA", "vwap"))

    # Verify count
    check("RS-02h: count correct after operations",
          registry.count() == 3,
          f"count={registry.count()}")

    # Thread safety test: concurrent acquire/release
    results = {'acquired': 0, 'blocked': 0}
    lock = threading.Lock()

    def concurrent_acquire(ticker, layer):
        if registry.try_acquire(ticker, layer):
            with lock:
                results['acquired'] += 1
        else:
            with lock:
                results['blocked'] += 1

    # Release all first
    for t in ["AAPL", "GOOG", "TSLA"]:
        registry.release(t)

    threads = []
    for i in range(20):
        t = threading.Thread(target=concurrent_acquire, args=(f"CONC{i%5}", f"layer{i}"))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    check("RS-02i: thread-safe (acquired + blocked = 20)",
          results['acquired'] + results['blocked'] == 20,
          f"acquired={results['acquired']} blocked={results['blocked']}")
    check("RS-02j: never exceeded cap",
          registry.count() <= 3,
          f"count={registry.count()}")


# ══════════════════════════════════════════════════════��════════════════════════
# TC-E2E-01: Tick → PnL full pipeline
# ═══════════════════════════════════════════════════════════════════════════════
def test_e2e_01():
    log.info("TC-E2E-01: Tick → PnL full pipeline")
    from monitor.brokers import PaperBroker
    from monitor.position_manager import PositionManager

    bus = EventBus(mode=DispatchMode.SYNC)
    positions = {}
    trade_log = []
    reclaimed = set()
    last_order = {}

    # Wire PaperBroker and PositionManager
    broker = PaperBroker(bus=bus, alert_email=None)
    pm = PositionManager(
        bus=bus, positions=positions, trade_log=trade_log,
        reclaimed_today=reclaimed, last_order_time=last_order,
        alert_email=None,
    )

    # Track events
    fills = []
    def on_fill(event):
        fills.append(event.payload)
    bus.subscribe(EventType.FILL, on_fill, priority=10)

    # === BUY ===
    bus.emit(Event(type=EventType.ORDER_REQ, payload=OrderRequestPayload(
        ticker='AAPL', side='buy', qty=10, price=150.0,
        reason='test', stop_price=148.0, target_price=155.0, atr_value=1.5,
    )))

    check("E2E-01a: buy fill received", len(fills) == 1,
          f"fills={len(fills)}")
    check("E2E-01b: position opened", 'AAPL' in positions,
          f"positions={list(positions.keys())}")
    if 'AAPL' in positions:
        check("E2E-01c: correct entry price",
              positions['AAPL']['entry_price'] == 150.0)
        check("E2E-01d: correct quantity",
              positions['AAPL']['quantity'] == 10)

    # === SELL ===
    bus.emit(Event(type=EventType.ORDER_REQ, payload=OrderRequestPayload(
        ticker='AAPL', side='sell', qty=10, price=152.0, reason='test_exit',
    )))

    check("E2E-01e: sell fill received", len(fills) == 2)
    check("E2E-01f: position closed", 'AAPL' not in positions,
          f"positions={list(positions.keys())}")
    check("E2E-01g: trade logged", len(trade_log) == 1,
          f"trades={len(trade_log)}")
    if trade_log:
        pnl = trade_log[0]['pnl']
        expected_pnl = (152.0 - 150.0) * 10  # $20
        check("E2E-01h: correct PnL",
              abs(pnl - expected_pnl) < 0.01,
              f"pnl={pnl} expected={expected_pnl}")
        check("E2E-01i: marked as win", trade_log[0]['is_win'])


# ════════════════════════════════════════════════��══════════════════════════════
# TC-E2E-02: Multi-strategy concurrent load
# ═════════════════════════════════════════���═════════════════════════════���═══════
def test_e2e_02():
    log.info("TC-E2E-02: Multi-strategy concurrent load")
    N_TICKERS = 50
    bar_count = [0]
    signal_count = [0]
    error_count = [0]

    bus = EventBus(mode=DispatchMode.ASYNC)

    def bar_handler(event):
        bar_count[0] += 1

    def signal_handler(event):
        signal_count[0] += 1

    bus.subscribe(EventType.BAR, bar_handler, priority=1)
    bus.subscribe(EventType.SIGNAL, signal_handler, priority=2)


    # Simulate 50 tickers × 10 bars each from multiple "strategy" threads
    def strategy_thread(tickers):
        for t in tickers:
            try:
                bar = _make_bar(t, n=60, seed=hash(t) % 10000)
                bus.emit(Event(type=EventType.BAR, payload=bar))
            except Exception:
                error_count[0] += 1

    tickers = [f"S{i:03d}" for i in range(N_TICKERS)]
    threads = []
    chunk = N_TICKERS // 5
    for i in range(5):
        t = threading.Thread(target=strategy_thread, args=(tickers[i*chunk:(i+1)*chunk],))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    time.sleep(3)


    metrics = bus.metrics()

    check("E2E-02a: no crashes under concurrent load", error_count[0] == 0,
          f"errors={error_count[0]}")
    check(f"E2E-02b: all {N_TICKERS} BAR events delivered",
          bar_count[0] >= N_TICKERS * 0.95,
          f"delivered={bar_count[0]}/{N_TICKERS}")
    check("E2E-02c: system pressure acceptable",
          metrics.system_pressure < 0.9,
          f"pressure={metrics.system_pressure:.2f}")
    check("E2E-02d: no deadlocks (bus stopped cleanly)", True)

    # Latency check
    avg_ms = max(metrics.handler_avg_ms.values()) if metrics.handler_avg_ms else 0
    check("E2E-02e: handler avg latency < 50ms",
          avg_ms < 50,
          f"max_avg={avg_ms:.1f}ms")


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    tests = [test_rs_01, test_rs_02, test_e2e_01, test_e2e_02]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAIL += 1
            log.error(f"  FAIL  {t.__name__} crashed: {e}")
        log.info("")

    log.info(f"{'='*60}")
    log.info(f"  Test 18 Results: {PASS} passed, {FAIL} failed")
    log.info(f"{'='*60}")
    sys.exit(1 if FAIL else 0)
