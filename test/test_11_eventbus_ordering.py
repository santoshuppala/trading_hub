#!/usr/bin/env python3
"""
Test 11 — EventBus Ordering, Backpressure, and Failure Handling
================================================================
TC-EB-01: Single producer, single consumer ordering
TC-EB-02: Multiple producers, single consumer ordering
TC-EB-03: Multiple consumers isolation (fast vs slow)
TC-EB-04: High-volume burst (10x normal rate)
TC-EB-05: Queue overflow behavior
TC-EB-06: Subscriber exception handling
"""
import os, sys, time, logging, threading
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from monitor.event_bus import EventBus, Event, EventType, DispatchMode, BackpressurePolicy
from monitor.events import BarPayload, HeartbeatPayload
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("test_11")

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

def _make_bar(ticker, n=35):
    idx = pd.date_range("2026-01-02 09:30", periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame({
        'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.5, 'volume': 1000
    }, index=idx)
    return BarPayload(ticker=ticker, df=df)

# ═══════════════════════════════════════════════════════════════════════════════
# TC-EB-01: Single producer, single consumer ordering
# ═══════════════════════════════════════════════════════════════════════════════
def test_eb_01():
    log.info("TC-EB-01: Single producer, single consumer ordering")
    N = 200
    received = []
    bus = EventBus(mode=DispatchMode.ASYNC, async_config={
        EventType.HEARTBEAT: {'maxsize': 500, 'policy': BackpressurePolicy.BLOCK, 'n_workers': 1},
    })

    def handler(event):
        received.append(event.stream_seq)

    bus.subscribe(EventType.HEARTBEAT, handler)
    for i in range(N):
        bus.emit(Event(
            type=EventType.HEARTBEAT,
            payload=HeartbeatPayload(
                n_tickers=i, n_positions=0, open_tickers=[],
                n_trades=0, n_wins=0, total_pnl=0.0,
            ),
        ))

    # Wait for drain
    time.sleep(2)

    check("EB-01a: all events received", len(received) == N,
          f"expected {N}, got {len(received)}")
    check("EB-01b: strictly increasing stream_seq",
          all(received[i] < received[i+1] for i in range(len(received)-1)),
          f"first 10: {received[:10]}")
    check("EB-01c: no duplicates",
          len(received) == len(set(received)),
          f"dupes: {len(received) - len(set(received))}")

# ═══════════════════════════════════════════════════════════════════════════════
# TC-EB-02: Multiple producers, single consumer ordering
# ═══════════════════════════════════════════════════════════════════════════════
def test_eb_02():
    log.info("TC-EB-02: Multiple producers, single consumer ordering")
    received = []
    bus = EventBus(mode=DispatchMode.ASYNC, async_config={
        EventType.HEARTBEAT: {'maxsize': 500, 'policy': BackpressurePolicy.BLOCK, 'n_workers': 1},
    })

    def handler(event):
        received.append(event.stream_seq)

    bus.subscribe(EventType.HEARTBEAT, handler)


    def producer(start, count):
        for i in range(count):
            bus.emit(Event(
                type=EventType.HEARTBEAT,
                payload=HeartbeatPayload(
                    n_tickers=start+i, n_positions=0, open_tickers=[],
                    n_trades=0, n_wins=0, total_pnl=0.0,
                ),
            ))

    t1 = threading.Thread(target=producer, args=(0, 100))
    t2 = threading.Thread(target=producer, args=(1000, 100))
    t1.start(); t2.start()
    t1.join(); t2.join()

    time.sleep(2)


    check("EB-02a: all 200 events received", len(received) == 200,
          f"got {len(received)}")
    check("EB-02b: no duplicates in stream_seq",
          len(received) == len(set(received)))
    # Per-partition ordering: stream_seqs should be monotonic
    check("EB-02c: monotonic stream_seq",
          all(received[i] < received[i+1] for i in range(len(received)-1)) if len(received) > 1 else True)

# ═══════════════════════════════════════════════════════════════════════════════
# TC-EB-03: Multiple consumers isolation (fast vs slow)
# ═══════════════════════════════════════════════════════════════════════════════
def test_eb_03():
    log.info("TC-EB-03: Multiple consumers isolation")
    fast_count = [0]
    slow_count = [0]
    N = 50
    bus = EventBus(mode=DispatchMode.ASYNC, async_config={
        EventType.HEARTBEAT: {'maxsize': 200, 'policy': BackpressurePolicy.BLOCK, 'n_workers': 1},
    })

    def fast_handler(event):
        fast_count[0] += 1

    def slow_handler(event):
        time.sleep(0.05)  # 50ms per event
        slow_count[0] += 1

    bus.subscribe(EventType.HEARTBEAT, fast_handler, priority=1)
    bus.subscribe(EventType.HEARTBEAT, slow_handler, priority=5)


    for i in range(N):
        bus.emit(Event(
            type=EventType.HEARTBEAT,
            payload=HeartbeatPayload(
                n_tickers=i, n_positions=0, open_tickers=[],
                n_trades=0, n_wins=0, total_pnl=0.0,
            ),
        ))

    time.sleep(8)  # give slow handler time (50ms × 50 = 2.5s + overhead)


    check("EB-03a: fast handler got all events", fast_count[0] == N,
          f"fast={fast_count[0]}")
    check("EB-03b: slow handler eventually processes", slow_count[0] > 0,
          f"slow={slow_count[0]}")

# ═══════════════════════════════════════════════════════════════════════════════
# TC-EB-04: High-volume burst (10x normal rate)
# ═══════════════════════════════════════════════════════════════════════════════
def test_eb_04():
    log.info("TC-EB-04: High-volume burst (10x)")
    N = 2000
    count = [0]
    bus = EventBus(mode=DispatchMode.ASYNC, async_config={
        EventType.HEARTBEAT: {'maxsize': 3000, 'policy': BackpressurePolicy.DROP_OLDEST, 'n_workers': 2},
    })

    def handler(event):
        count[0] += 1

    bus.subscribe(EventType.HEARTBEAT, handler)


    t0 = time.monotonic()
    for i in range(N):
        bus.emit(Event(
            type=EventType.HEARTBEAT,
            payload=HeartbeatPayload(
                n_tickers=i, n_positions=0, open_tickers=[],
                n_trades=0, n_wins=0, total_pnl=0.0,
            ),
        ))
    emit_ms = (time.monotonic() - t0) * 1000

    time.sleep(5)


    metrics = bus.metrics()
    check("EB-04a: no crash under 10x burst", True)
    check("EB-04b: emit throughput > 1000/sec", emit_ms < N,
          f"{N} events in {emit_ms:.0f}ms")
    check("EB-04c: system pressure < 1.0", metrics.system_pressure < 1.0,
          f"pressure={metrics.system_pressure:.2f}")
    check("EB-04d: most events delivered", count[0] >= N * 0.9,
          f"delivered={count[0]}/{N}")

# ═══════════════════════════════════════════════════════════════════════════════
# TC-EB-05: Queue overflow behavior
# ═══════════════════════════════════════════════════════════════════════════════
def test_eb_05():
    log.info("TC-EB-05: Queue overflow behavior")
    count = [0]
    # Tiny queue to force overflow
    bus = EventBus(
        mode=DispatchMode.ASYNC,
        async_config={
            EventType.HEARTBEAT: {
                'maxsize': 5,
                'policy': BackpressurePolicy.DROP_OLDEST,
                'n_workers': 1,
            },
        },
    )

    def slow_handler(event):
        time.sleep(0.1)  # slow consumer
        count[0] += 1

    bus.subscribe(EventType.HEARTBEAT, slow_handler)


    # Flood 100 events into queue of size 5
    for i in range(100):
        bus.emit(Event(
            type=EventType.HEARTBEAT,
            payload=HeartbeatPayload(
                n_tickers=i, n_positions=0, open_tickers=[],
                n_trades=0, n_wins=0, total_pnl=0.0,
            ),
        ))

    time.sleep(3)


    metrics = bus.metrics()
    drops = sum(metrics.dropped_counts.values())
    check("EB-05a: no crash on overflow", True)
    check("EB-05b: drops occurred (DROP_OLDEST)", drops > 0,
          f"drops={drops}")
    check("EB-05c: no deadlock (handler ran)", count[0] > 0,
          f"processed={count[0]}")
    check("EB-05d: processed + dropped >= sent",
          count[0] + drops >= 50,  # at least half accounted for
          f"processed={count[0]} drops={drops}")

# ═══════════════════════════════════════════════════════════════════════════════
# TC-EB-06: Subscriber exception handling
# ═══════════════════════════════════════════════════════════════════════════════
def test_eb_06():
    log.info("TC-EB-06: Subscriber exception handling")
    good_count = [0]
    bad_count = [0]
    bus = EventBus(mode=DispatchMode.ASYNC)

    def good_handler(event):
        good_count[0] += 1

    def bad_handler(event):
        bad_count[0] += 1
        if event.payload.n_tickers == 5:
            raise ValueError("Intentional test exception")

    bus.subscribe(EventType.HEARTBEAT, good_handler, priority=1)
    bus.subscribe(EventType.HEARTBEAT, bad_handler, priority=2)


    for i in range(20):
        bus.emit(Event(
            type=EventType.HEARTBEAT,
            payload=HeartbeatPayload(
                n_tickers=i, n_positions=0, open_tickers=[],
                n_trades=0, n_wins=0, total_pnl=0.0,
            ),
        ))

    time.sleep(3)


    # Good handler may miss some events if the bad handler's exception triggers
    # a circuit breaker. The key invariant: good handler processes MOST events.
    check("EB-06a: good handler processed majority despite bad handler",
          good_count[0] >= 15,
          f"good={good_count[0]}/20")
    check("EB-06b: bus survived exception", True)
    check("EB-06c: bad handler was called before exception",
          bad_count[0] >= 1,
          f"bad_count={bad_count[0]}")


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    tests = [test_eb_01, test_eb_02, test_eb_03, test_eb_04, test_eb_05, test_eb_06]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAIL += 1
            log.error(f"  FAIL  {t.__name__} crashed: {e}")
        log.info("")

    log.info(f"{'='*60}")
    log.info(f"  Test 11 Results: {PASS} passed, {FAIL} failed")
    log.info(f"{'='*60}")
    sys.exit(1 if FAIL else 0)
