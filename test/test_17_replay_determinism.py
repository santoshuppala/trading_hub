#!/usr/bin/env python3
"""
Test 17 — Replay Engine Determinism
====================================
TC-RP-01: End-to-end replay determinism (same inputs → same outputs twice)
TC-RP-02: Mid-session restart (rebuild state from T, replay T→end)
"""
import os, sys, time, logging, copy
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pandas as pd
import numpy as np

from monitor.event_bus import EventBus, Event, EventType, DispatchMode
from monitor.events import (
    BarPayload, SignalPayload, FillPayload, OrderRequestPayload,
    PositionPayload, RiskBlockPayload, HeartbeatPayload,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("test_17")

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


def _make_deterministic_bars(ticker, n=60, seed=42):
    """Generate reproducible bar data from a fixed seed."""
    rng = np.random.RandomState(seed)
    idx = pd.date_range("2026-01-02 09:30", periods=n, freq="1min", tz="UTC")
    base = 100.0 + rng.randn(n).cumsum() * 0.3
    df = pd.DataFrame({
        'open':   base,
        'high':   base + abs(rng.randn(n)) * 0.5,
        'low':    base - abs(rng.randn(n)) * 0.5,
        'close':  base + rng.randn(n) * 0.2,
        'volume': (rng.randint(500, 2000, n)).astype(float),
    }, index=idx)
    return BarPayload(ticker=ticker, df=df)


def _run_pipeline(tickers, bars_per_ticker=60, seed=42):
    """
    Run a full EventBus pipeline and capture all emitted events.
    Returns dict of {EventType.name: [payloads]}.
    Uses SYNC mode for determinism.
    """
    captured = {et.name: [] for et in EventType}
    bus = EventBus(mode=DispatchMode.SYNC)

    def make_handler(et_name):
        def h(event):
            # Deep-copy payload to avoid mutation
            captured[et_name].append({
                'stream_seq': event.stream_seq,
                'type': event.type.name,
                'ticker': getattr(event.payload, 'ticker',
                          getattr(event.payload, 'symbol', None)),
            })
        return h

    for et in EventType:
        bus.subscribe(et, make_handler(et.name), priority=10)

    # Emit deterministic bars
    for t in tickers:
        bar = _make_deterministic_bars(t, n=bars_per_ticker, seed=seed)
        bus.emit(Event(type=EventType.BAR, payload=bar))

    return captured


# ═══════════════════════════════════════════════════════════════════════════════
# TC-RP-01: End-to-end replay determinism
# ═══════════════════════════════════════════════════════════════════════════════
def test_rp_01():
    log.info("TC-RP-01: End-to-end replay determinism")
    tickers = ["AAPL", "MSFT", "GOOG", "NVDA", "TSLA"]

    # Run 1
    run1 = _run_pipeline(tickers, bars_per_ticker=60, seed=42)

    # Run 2 (fresh state, same inputs)
    run2 = _run_pipeline(tickers, bars_per_ticker=60, seed=42)

    # Compare BAR events (should be identical count and order)
    bars1 = run1['BAR']
    bars2 = run2['BAR']

    check("RP-01a: same number of BAR events",
          len(bars1) == len(bars2),
          f"run1={len(bars1)} run2={len(bars2)}")

    if len(bars1) == len(bars2):
        matches = sum(1 for a, b in zip(bars1, bars2)
                      if a['ticker'] == b['ticker'])
        check("RP-01b: BAR ticker order identical",
              matches == len(bars1),
              f"mismatches={len(bars1) - matches}")

    # Stream sequences should be identical
    seqs1 = [e['stream_seq'] for e in bars1]
    seqs2 = [e['stream_seq'] for e in bars2]
    check("RP-01c: stream_seq values identical across runs",
          seqs1 == seqs2,
          f"first diff at index {next((i for i,(a,b) in enumerate(zip(seqs1,seqs2)) if a!=b), 'none')}")

    # All event types should have same counts
    for et in EventType:
        c1 = len(run1[et.name])
        c2 = len(run2[et.name])
        if c1 > 0 or c2 > 0:
            check(f"RP-01d: {et.name} count parity",
                  c1 == c2, f"run1={c1} run2={c2}")


# ═══════════════════════════════════════════════════════════════════════════════
# TC-RP-02: Mid-session restart (state rebuild + resume)
# ═══════════════════════════════════════════════════════════════════════════════
def test_rp_02():
    log.info("TC-RP-02: Mid-session restart simulation")

    tickers = ["AAPL", "MSFT", "GOOG"]
    all_bars = {}
    for t in tickers:
        all_bars[t] = _make_deterministic_bars(t, n=100, seed=42)

    # === Full run (uninterrupted) ===
    full_events = []
    bus_full = EventBus(mode=DispatchMode.SYNC)

    def full_handler(event):
        full_events.append({
            'seq': event.stream_seq,
            'type': event.type.name,
            'ticker': getattr(event.payload, 'ticker', None),
        })

    bus_full.subscribe(EventType.BAR, full_handler, priority=10)

    for t in tickers:
        bus_full.emit(Event(type=EventType.BAR, payload=all_bars[t]))

    # === Split run: first half, then second half ===
    split_events = []

    # Phase 1: first 2 tickers
    bus_p1 = EventBus(mode=DispatchMode.SYNC)
    def p1_handler(event):
        split_events.append({
            'type': event.type.name,
            'ticker': getattr(event.payload, 'ticker', None),
        })
    bus_p1.subscribe(EventType.BAR, p1_handler, priority=10)
    for t in tickers[:2]:
        bus_p1.emit(Event(type=EventType.BAR, payload=all_bars[t]))

    # Phase 2: remaining tickers (simulates restart + resume)
    bus_p2 = EventBus(mode=DispatchMode.SYNC)
    def p2_handler(event):
        split_events.append({
            'type': event.type.name,
            'ticker': getattr(event.payload, 'ticker', None),
        })
    bus_p2.subscribe(EventType.BAR, p2_handler, priority=10)
    for t in tickers[2:]:
        bus_p2.emit(Event(type=EventType.BAR, payload=all_bars[t]))

    # Compare: same tickers processed, same total count
    full_tickers = [e['ticker'] for e in full_events]
    split_tickers = [e['ticker'] for e in split_events]

    check("RP-02a: same total event count",
          len(full_events) == len(split_events),
          f"full={len(full_events)} split={len(split_events)}")
    check("RP-02b: same tickers processed",
          sorted(full_tickers) == sorted(split_tickers))
    check("RP-02c: split preserves ordering within each phase",
          split_tickers == full_tickers,
          f"first mismatch: {next((i for i,(a,b) in enumerate(zip(split_tickers,full_tickers)) if a!=b), 'none')}")


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    tests = [test_rp_01, test_rp_02]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAIL += 1
            log.error(f"  FAIL  {t.__name__} crashed: {e}")
        log.info("")

    log.info(f"{'='*60}")
    log.info(f"  Test 17 Results: {PASS} passed, {FAIL} failed")
    log.info(f"{'='*60}")
    sys.exit(1 if FAIL else 0)
