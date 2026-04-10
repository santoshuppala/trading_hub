"""
T1 — Event Bus  (production hardened, v4)
==========================================
Central pub/sub backbone for the trading system.

Seven production gaps fixed in this version
--------------------------------------------
1. Priority enforced in async queues   — _BoundedPriorityQueue replaces
   queue.Queue.  DROP_OLDEST now evicts the lowest-urgency item (highest
   priority integer) rather than the FIFO-oldest, preserving critical events.

2. N workers per EventType             — _PartitionedAsyncDispatcher routes
   events by hash(ticker) % n_workers (Kafka partition model).  Same ticker
   always lands on the same worker → per-ticker order preserved.  Different
   tickers run in parallel.

3. Split locks                         — single RLock replaced by four
   purpose-specific locks: _sub_lock (subscribers), _seq_lock (stream_seqs),
   _count_lock (counters), _idem_lock (idempotency).  _HandlerState gets its
   own _lock so circuit-breaker updates don't contend with subscribe().

4. emit_batch()                        — emits a list of events with O(4)
   lock acquisitions instead of O(4N), dramatically reducing contention
   on the BAR fan-out loop (170 tickers × 4 locks each → 4 locks total).

5. Systemic backpressure monitor       — _BackpressureMonitor aggregates
   depth/capacity across all queues.  emit() samples it before enqueueing:
   ≥60% → WARNING, ≥80% → adaptive micro-sleep, ≥95% → ERROR + alert_fn.

6. stream_seqs memory cap              — _stream_seqs is now an OrderedDict
   with LRU eviction at max_streams entries (default 1 000).  Prevents
   unbounded growth when many ticker/EventType combinations are active.

7. Durable write-then-deliver          — add_before_emit_hook() + emit(durable=True).
   Hooks run synchronously before any handler.  DurableEventLog registers
   a produce+flush hook so Redpanda acks the event before in-process state
   changes begin — eliminating the async-write/handler-race window.

Earlier fixes (v3)
------------------
A. Stream consistency  — per-(EventType, ticker) stream_seq
B. Async dispatch mode — DispatchMode.SYNC / ASYNC + BackpressurePolicy
C. Handler ordering    — subscribe(priority=) + sorted snapshot
D. TimeSource          — injected clock; SimulatedTimeSource for backtests
E. Idempotency         — bounded OrderedDict seen-id dedup window
F. EventPriority       — QoS tiers CRITICAL → LOWEST stamped on emit()
G. CausalityTracker    — OrderedDict + reverse _parent map for O(1) eviction

Payload schema lives in events.py.  This module re-exports all payload types.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import queue
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

ET  = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)


# ── D — Unified clock interface ───────────────────────────────────────────────

class TimeSource(ABC):
    @abstractmethod
    def now(self) -> datetime: ...
    @abstractmethod
    def monotonic(self) -> float: ...


class WallClockTimeSource(TimeSource):
    def now(self) -> datetime:      return datetime.now(ET)
    def monotonic(self) -> float:   return time.monotonic()


class SimulatedTimeSource(TimeSource):
    """
    Injected clock for backtesting and event replay.

        clock = SimulatedTimeSource(start=datetime(2026, 1, 2, 9, 30, tzinfo=ET))
        bus   = EventBus(time_source=clock)
        for bar in historical_bars:
            clock.set_time(bar.timestamp)
            bus.emit(Event(type=EventType.BAR, payload=...))
    """
    def __init__(self, start: Optional[datetime] = None):
        self._t    = start or datetime.now(ET)
        self._mono = time.monotonic()
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock: return self._t

    def monotonic(self) -> float:
        with self._lock: return self._mono

    def advance(self, seconds: float) -> None:
        from datetime import timedelta
        with self._lock:
            self._t    += timedelta(seconds=seconds)
            self._mono += seconds

    def set_time(self, dt: datetime) -> None:
        with self._lock:
            elapsed    = (dt - self._t).total_seconds()
            self._t    = dt
            self._mono += max(0.0, elapsed)


_WALL_CLOCK = WallClockTimeSource()


# ── Tuning constants ──────────────────────────────────────────────────────────

SLOW_THRESHOLD_SEC        = 0.10
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_BREAKER_COOLDOWN  = 60.0
LATENCY_WINDOW            = 100


# ── Dispatch modes & backpressure ─────────────────────────────────────────────

class DispatchMode(Enum):
    SYNC  = 'sync'
    ASYNC = 'async'


class BackpressurePolicy(Enum):
    DROP_OLDEST = 'drop_oldest'
    DROP_NEWEST = 'drop_newest'
    BLOCK       = 'block'
    RAISE       = 'raise'


class BackpressureError(RuntimeError):
    """Raised by RAISE policy when the dispatcher queue is full."""


# ── F — QoS priority tiers ────────────────────────────────────────────────────

class EventPriority(IntEnum):
    """
    Lower integer = higher urgency = dequeued first.

    Tier      EventTypes                    Rationale
    --------  ----------------------------  ------------------------------------
    CRITICAL  FILL, ORDER_FAIL              Broker confirmation; must not lag.
    HIGH      ORDER_REQ, POSITION           Execution & position state path.
    MEDIUM    SIGNAL, RISK_BLOCK            Strategy & risk layer.
    LOW       BAR, QUOTE                    Market data; high volume.
    LOWEST    HEARTBEAT                     Background telemetry only.
    """
    CRITICAL = 0
    HIGH     = 1
    MEDIUM   = 2
    LOW      = 3
    LOWEST   = 4


_DEFAULT_PRIORITY: Dict = {}
_DEFAULT_ASYNC_CONFIG: Dict = {}
_STOP = object()   # clean-shutdown sentinel


# ── Event types ───────────────────────────────────────────────────────────────

class EventType(Enum):
    BAR          = auto()
    QUOTE        = auto()
    SIGNAL       = auto()
    ORDER_REQ    = auto()
    FILL         = auto()
    ORDER_FAIL   = auto()
    POSITION     = auto()
    RISK_BLOCK   = auto()
    HEARTBEAT    = auto()


# Populate config dicts now that EventType exists
_DEFAULT_ASYNC_CONFIG.update({
    # High-frequency market data — n_workers=4 for parallel ticker processing
    EventType.BAR:        {'maxsize': 200, 'policy': BackpressurePolicy.DROP_OLDEST, 'n_workers': 4},
    EventType.QUOTE:      {'maxsize': 100, 'policy': BackpressurePolicy.DROP_OLDEST, 'n_workers': 2},
    # Signals — latest supersedes stale
    EventType.SIGNAL:     {'maxsize':  50, 'policy': BackpressurePolicy.DROP_OLDEST, 'n_workers': 2},
    # Critical order/fill path — NEVER drop; n_workers=1 for strict ordering
    EventType.ORDER_REQ:  {'maxsize': 100, 'policy': BackpressurePolicy.BLOCK,       'n_workers': 1},
    EventType.FILL:       {'maxsize': 100, 'policy': BackpressurePolicy.BLOCK,       'n_workers': 1},
    EventType.ORDER_FAIL: {'maxsize':  50, 'policy': BackpressurePolicy.BLOCK,       'n_workers': 1},
    # Position state — must be consistent
    EventType.POSITION:   {'maxsize':  50, 'policy': BackpressurePolicy.BLOCK,       'n_workers': 1},
    # Informational — acceptable to lose some
    EventType.RISK_BLOCK: {'maxsize':  50, 'policy': BackpressurePolicy.DROP_NEWEST, 'n_workers': 1},
    EventType.HEARTBEAT:  {'maxsize':  10, 'policy': BackpressurePolicy.DROP_OLDEST, 'n_workers': 1},
})

_DEFAULT_PRIORITY.update({
    EventType.FILL:       EventPriority.CRITICAL,
    EventType.ORDER_FAIL: EventPriority.CRITICAL,
    EventType.ORDER_REQ:  EventPriority.HIGH,
    EventType.POSITION:   EventPriority.HIGH,
    EventType.SIGNAL:     EventPriority.MEDIUM,
    EventType.RISK_BLOCK: EventPriority.MEDIUM,
    EventType.BAR:        EventPriority.LOW,
    EventType.QUOTE:      EventPriority.LOW,
    EventType.HEARTBEAT:  EventPriority.LOWEST,
})


# ── Issue 1 — Bounded priority queue ─────────────────────────────────────────

class _BoundedPriorityQueue:
    """
    Thread-safe bounded min-heap priority queue.

    Items dequeue in priority order: lower integer = higher urgency (CRITICAL=0 first).
    put_nowait() raises queue.Full when at capacity.
    get() blocks until an item is available or timeout elapses.
    evict_lowest_urgency() removes the item with the highest priority integer
    (least urgent) — used by DROP_OLDEST to preserve critical events when full.
    """

    def __init__(self, maxsize: int) -> None:
        self._maxsize = max(1, maxsize)
        self._heap:   list            = []
        self._seq:    itertools.count = itertools.count()
        self._cond    = threading.Condition(threading.Lock())

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def put_nowait(self, priority: int, item: Any) -> None:
        """Non-blocking put. Raises queue.Full when at capacity."""
        with self._cond:
            if len(self._heap) >= self._maxsize:
                raise queue.Full
            heapq.heappush(self._heap, (priority, next(self._seq), item))
            self._cond.notify()

    def put(self, priority: int, item: Any) -> None:
        """Blocking put. Waits indefinitely until space is available."""
        entry = (priority, next(self._seq), item)
        with self._cond:
            while len(self._heap) >= self._maxsize:
                self._cond.wait(timeout=0.1)
            heapq.heappush(self._heap, entry)
            self._cond.notify()

    def get(self, timeout: Optional[float] = None) -> Tuple[int, Any]:
        """
        Blocking get. Returns (priority_int, item).
        Raises queue.Empty if timeout elapses before an item is available.
        """
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        with self._cond:
            while not self._heap:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise queue.Empty
                    self._cond.wait(timeout=remaining)
                else:
                    self._cond.wait(timeout=0.5)
            pri, _, item = heapq.heappop(self._heap)
            self._cond.notify()   # wake a blocked put() if any
            return pri, item

    def evict_lowest_urgency(self) -> Optional[Any]:
        """
        Remove the item with the highest priority integer (lowest urgency).
        Used by DROP_OLDEST to preserve the most critical queued events.
        O(n) scan — acceptable for small queues (≤ 200 items).
        Returns the evicted item, or None if the queue is empty.
        """
        with self._cond:
            if not self._heap:
                return None
            worst_idx = max(range(len(self._heap)), key=lambda i: self._heap[i][0])
            evicted   = self._heap[worst_idx][2]
            last      = len(self._heap) - 1
            if worst_idx != last:
                self._heap[worst_idx] = self._heap[last]
            self._heap.pop()
            if self._heap:
                heapq.heapify(self._heap)
            self._cond.notify()
            return evicted

    def qsize(self) -> int:
        with self._cond:
            return len(self._heap)


# ── Issue 1 + 2 — Partitioned async dispatcher ───────────────────────────────

class _PartitionedAsyncDispatcher:
    """
    N worker threads per EventType with ticker-based partitioning.

    Partitioning model (Kafka-inspired)
    ------------------------------------
    hash(ticker) % n_workers → routes to a dedicated worker.
    • Same ticker → same worker → per-ticker causal order preserved.
    • Different tickers → different workers → parallel processing.

    Priority enforcement (issue 1)
    --------------------------------
    Each worker owns a _BoundedPriorityQueue.  CRITICAL events (priority=0)
    dequeue before BAR events (priority=3), even if the BAR arrived first.
    DROP_OLDEST evicts the least-urgent queued item, not the FIFO-oldest.

    Worker count guidelines
    -----------------------
    BAR / QUOTE / SIGNAL : n_workers=2–4  (high volume, stateless per-ticker)
    ORDER_REQ / FILL      : n_workers=1   (strict global ordering required)
    POSITION / RISK_BLOCK : n_workers=1
    """

    def __init__(
        self,
        event_type: EventType,
        maxsize:    int,
        policy:     BackpressurePolicy,
        bus:        'EventBus',
        n_workers:  int = 2,
    ) -> None:
        self._event_type = event_type
        self._policy     = policy
        self._bus        = bus
        self._n          = max(1, n_workers)

        # Per-partition priority queues (maxsize split evenly)
        per_q = max(1, maxsize // self._n)
        self._queues: List[_BoundedPriorityQueue] = [
            _BoundedPriorityQueue(per_q) for _ in range(self._n)
        ]
        # Stop flags — set when queue is full at shutdown time
        self._stop_flags: List[threading.Event] = [
            threading.Event() for _ in range(self._n)
        ]
        self._dropped  = 0
        self._d_lock   = threading.Lock()

        self._threads: List[threading.Thread] = []
        for i in range(self._n):
            t = threading.Thread(
                target=self._worker,
                args=(i,),
                name=f'bus-{event_type.name}-{i}',
                daemon=True,
            )
            t.start()
            self._threads.append(t)

    # ── Routing ───────────────────────────────────────────────────────────────

    def _partition(self, event: 'Event') -> int:
        ticker = getattr(event.payload, 'ticker', '') or ''
        return hash(ticker) % self._n

    # ── Enqueue ───────────────────────────────────────────────────────────────

    def put(self, event: 'Event', snapshot: list) -> None:
        partition = self._partition(event)
        q         = self._queues[partition]
        priority  = event.priority   # lower int = higher urgency
        item      = (event, snapshot)
        policy    = self._policy

        if policy == BackpressurePolicy.BLOCK:
            q.put(priority, item)

        elif policy == BackpressurePolicy.RAISE:
            try:
                q.put_nowait(priority, item)
            except queue.Full:
                raise BackpressureError(
                    f"[backpressure] Queue full for {self._event_type.name} "
                    f"partition={partition} (maxsize={q.maxsize})"
                )

        elif policy == BackpressurePolicy.DROP_NEWEST:
            try:
                q.put_nowait(priority, item)
            except queue.Full:
                with self._d_lock:
                    self._dropped += 1
                log.warning(
                    f"[backpressure] DROP_NEWEST {self._event_type.name} "
                    f"p={partition}: incoming event discarded (dropped={self._dropped})"
                )

        elif policy == BackpressurePolicy.DROP_OLDEST:
            while True:
                try:
                    q.put_nowait(priority, item)
                    break
                except queue.Full:
                    evicted = q.evict_lowest_urgency()
                    if evicted is not None:
                        with self._d_lock:
                            self._dropped += 1
                        log.debug(
                            f"[backpressure] DROP_OLDEST {self._event_type.name} "
                            f"p={partition}: evicted low-urgency event "
                            f"(dropped={self._dropped})"
                        )

    # ── Worker ────────────────────────────────────────────────────────────────

    def _worker(self, partition_idx: int) -> None:
        q         = self._queues[partition_idx]
        stop_flag = self._stop_flags[partition_idx]
        while not stop_flag.is_set():
            try:
                _, item = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is _STOP:
                break
            event, snapshot = item
            now = self._bus._time_source.monotonic()
            for key, state in snapshot:
                self._bus._deliver(event, key, state, now)

    # ── Lifecycle & metrics ───────────────────────────────────────────────────

    def stop(self, timeout: float = 2.0) -> None:
        """
        Drain remaining events then stop workers.
        _STOP sentinel has priority=9999 (lowest urgency) so all real events
        are processed first.  Falls back to threading.Event if queue is full.
        """
        for i, q in enumerate(self._queues):
            try:
                q.put_nowait(9999, _STOP)
            except queue.Full:
                self._stop_flags[i].set()
        for t in self._threads:
            t.join(timeout=timeout)

    @property
    def queue_depth(self) -> int:
        return sum(q.qsize() for q in self._queues)

    @property
    def total_capacity(self) -> int:
        return sum(q.maxsize for q in self._queues)

    @property
    def dropped(self) -> int:
        with self._d_lock:
            return self._dropped


# ── Issue 5 — Systemic backpressure monitor ───────────────────────────────────

class _BackpressureMonitor:
    """
    Global pressure monitor aggregating depth/capacity across all queues.

    Called by emit() before each enqueue.  Provides adaptive throttling
    before individual queues hit their DROP thresholds.

    Thresholds
    ----------
    WARN_THRESHOLD      60%  → log WARNING (no action)
    THROTTLE_THRESHOLD  80%  → sleep THROTTLE_SLEEP_SEC (give workers time)
    ALERT_THRESHOLD     95%  → log ERROR + call alert_fn (edge-triggered)
    """

    WARN_THRESHOLD     = 0.60
    THROTTLE_THRESHOLD = 0.80
    ALERT_THRESHOLD    = 0.95
    THROTTLE_SLEEP_SEC = 0.001   # 1 ms yield

    def __init__(
        self,
        dispatchers: Dict[EventType, '_PartitionedAsyncDispatcher'],
        alert_fn:    Optional[Callable[[float], None]] = None,
    ) -> None:
        self._dispatchers = dispatchers
        self._alert_fn    = alert_fn
        self._alerted     = False
        self._lock        = threading.Lock()

    def pressure(self) -> float:
        total_depth    = sum(d.queue_depth    for d in self._dispatchers.values())
        total_capacity = sum(d.total_capacity for d in self._dispatchers.values())
        return (total_depth / total_capacity) if total_capacity else 0.0

    def check(self) -> float:
        """Sample pressure and apply throttle. Returns current ratio."""
        ratio = self.pressure()

        if ratio >= self.ALERT_THRESHOLD:
            with self._lock:
                if not self._alerted:
                    self._alerted = True
                    log.error(
                        f"[backpressure] CRITICAL pressure {ratio:.1%} — "
                        f"reduce tick frequency or increase queue sizes."
                    )
                    if self._alert_fn:
                        try:
                            self._alert_fn(ratio)
                        except Exception:
                            pass
        elif ratio < self.WARN_THRESHOLD:
            with self._lock:
                self._alerted = False
        elif ratio >= self.WARN_THRESHOLD:
            log.warning(f"[backpressure] High system pressure: {ratio:.1%}")

        if ratio >= self.THROTTLE_THRESHOLD:
            time.sleep(self.THROTTLE_SLEEP_SEC)

        return ratio


# ── Re-export payload types from events.py ────────────────────────────────────

from .events import (           # noqa: E402
    BarPayload, QuotePayload, SignalPayload,
    OrderRequestPayload, FillPayload, OrderFailPayload,
    PositionPayload, RiskBlockPayload, HeartbeatPayload,
)

_PAYLOAD_TYPES: Dict[EventType, type] = {
    EventType.BAR:        BarPayload,
    EventType.QUOTE:      QuotePayload,
    EventType.SIGNAL:     SignalPayload,
    EventType.ORDER_REQ:  OrderRequestPayload,
    EventType.FILL:       FillPayload,
    EventType.ORDER_FAIL: OrderFailPayload,
    EventType.POSITION:   PositionPayload,
    EventType.RISK_BLOCK: RiskBlockPayload,
    EventType.HEARTBEAT:  HeartbeatPayload,
}

__all__ = [
    # Core
    'EventType', 'Event', 'EventBus', 'BusMetrics',
    # Dispatch & backpressure
    'DispatchMode', 'BackpressurePolicy', 'BackpressureError',
    # QoS priority
    'EventPriority',
    # Unified clock
    'TimeSource', 'WallClockTimeSource', 'SimulatedTimeSource',
    # Observability
    'StreamMonitor', 'CausalityTracker',
    # Payload re-exports
    'BarPayload', 'QuotePayload', 'SignalPayload',
    'OrderRequestPayload', 'FillPayload', 'OrderFailPayload',
    'PositionPayload', 'RiskBlockPayload', 'HeartbeatPayload',
    # Tuning constants
    'SLOW_THRESHOLD_SEC', 'CIRCUIT_BREAKER_THRESHOLD', 'CIRCUIT_BREAKER_COOLDOWN',
]


# ── Global sequence counter (bus-level, module-scoped) ────────────────────────

_global_seq_lock = threading.Lock()
_global_seq_iter = itertools.count(1)

def _next_global_seq() -> int:
    with _global_seq_lock:
        return next(_global_seq_iter)


# ── Stable handler identity ───────────────────────────────────────────────────

class _HandlerKey:
    """
    Stable identity for handler callables across repeated attribute access.
    Bound methods create a new object on every access; decompose into
    (id(instance), __func__) for stable __hash__ / __eq__.
    """
    __slots__ = ('_key', '_fn', '__qualname__')

    def __init__(self, fn: Callable):
        if hasattr(fn, '__func__') and hasattr(fn, '__self__'):
            self._key = (id(fn.__self__), fn.__func__)
        else:
            self._key = (None, fn)
        self._fn = fn
        self.__qualname__: str = getattr(fn, '__qualname__', repr(fn))

    def __call__(self, event: 'Event') -> None:
        self._fn(event)

    def __hash__(self) -> int:
        return hash(self._key)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _HandlerKey) and self._key == other._key

    def __repr__(self) -> str:
        return f"_HandlerKey({self.__qualname__})"


# ── Circuit breaker state ─────────────────────────────────────────────────────

class _HandlerState:
    """
    Per-handler mutable state including circuit breaker.

    Issue 3: each _HandlerState has its own _lock so concurrent workers
    processing different tickers don't contend on the same global lock.

    Circuit states
    --------------
    closed    : normal operation  (disabled_until == 0)
    open      : suspended         (now < disabled_until)
    half-open : trial run         (now >= disabled_until > 0)
    """
    __slots__ = (
        'consecutive_failures', 'disabled_until', 'latencies',
        'error_count', 'call_count', 'trip_count', '_lock',
    )

    def __init__(self):
        self.consecutive_failures: int   = 0
        self.disabled_until:       float = 0.0
        self.latencies:            deque = deque(maxlen=LATENCY_WINDOW)
        self.error_count:          int   = 0
        self.call_count:           int   = 0
        self.trip_count:           int   = 0
        self._lock = threading.Lock()    # issue 3: per-handler lock

    def circuit_state(self, now: float) -> str:
        """Call while holding self._lock."""
        if self.disabled_until == 0.0:
            return 'closed'
        return 'open' if now < self.disabled_until else 'half-open'


# ── Core event wrapper ────────────────────────────────────────────────────────

@dataclass
class Event:
    """
    Wrapper pairing an EventType with its validated payload.

    Identity
    --------
    event_id       : UUID — unique per event; dedup / tracing
    correlation_id : optional UUID linking causally related events
    sequence       : globally monotonic int across ALL events on this bus instance
    stream_seq     : per-(EventType, ticker) monotonic int, assigned by bus.emit()
    priority       : QoS tier (stamped by emit() from _DEFAULT_PRIORITY if -1)
    """
    type:           EventType
    payload:        Any
    event_id:       str           = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: Optional[str] = None
    sequence:       int           = field(default_factory=_next_global_seq)
    stream_seq:     Optional[int] = None
    timestamp:      datetime      = field(default_factory=lambda: datetime.now(ET))
    priority:       int           = -1   # -1 = "stamp from _DEFAULT_PRIORITY"

    def __repr__(self) -> str:
        cid  = f" corr={self.correlation_id[:8]}" if self.correlation_id else ""
        sseq = f" sseq={self.stream_seq}" if self.stream_seq is not None else ""
        pri  = f" pri={self.priority}" if self.priority >= 0 else ""
        return (
            f"Event({self.type.name} seq={self.sequence}{sseq}{pri}"
            f"{cid} @ {self.timestamp.strftime('%H:%M:%S.%f')[:-3]})"
        )


# ── Metrics snapshot ──────────────────────────────────────────────────────────

@dataclass
class BusMetrics:
    """Point-in-time snapshot returned by bus.metrics()."""
    emit_counts:              Dict[str, int]
    handler_calls:            Dict[str, int]
    handler_errors:           Dict[str, int]
    handler_trips:            Dict[str, int]
    handler_avg_ms:           Dict[str, float]
    handler_max_ms:           Dict[str, float]
    handler_circuit:          Dict[str, str]
    slow_calls:               int
    circuit_breaks:           int
    active_breaks:            List[str]
    stream_seqs:              Dict[str, int]
    # Async-mode only (empty dicts in sync mode)
    queue_depths:             Dict[str, int]
    dropped_counts:           Dict[str, int]
    # C — deterministic handler ordering
    handler_execution_order:  Dict[str, List[str]]
    # E — idempotency
    duplicate_events_dropped: int
    # Issue 5 — systemic backpressure
    system_pressure:          float   # 0.0–1.0+; ratio of total depth / total capacity
    # Issue 6 — memory stats
    stream_count:             int     # active entries in stream_seqs LRU
    seen_ids_count:           int     # entries currently in idempotency window


# ── Consumer-side ordering validator ─────────────────────────────────────────

class StreamMonitor:
    """
    Detects gaps and out-of-order delivery on a per-(EventType, ticker) stream.

        monitor = StreamMonitor('StrategyEngine')
        def on_bar(event):
            for w in monitor.check(event): log.warning(w)
            # ... process ...
        bus.subscribe(EventType.BAR, on_bar)
    """

    def __init__(self, name: str = ''):
        self.name      = name
        self.gap_count = 0
        self.ooo_count = 0
        self._last:    Dict[Tuple, int] = {}
        self._lock     = threading.Lock()

    def check(self, event: Event) -> List[str]:
        if event.stream_seq is None:
            return []
        ticker    = getattr(event.payload, 'ticker', '')
        key       = (event.type, ticker)
        stream_id = f"{event.type.name}/{ticker}" if ticker else event.type.name
        warnings: List[str] = []
        with self._lock:
            last = self._last.get(key)
            if last is not None:
                if event.stream_seq < last:
                    self.ooo_count += 1
                    warnings.append(
                        f"[{self.name}] Out-of-order in {stream_id}: "
                        f"stream_seq={event.stream_seq} < last={last}"
                    )
                elif event.stream_seq > last + 1:
                    gap = event.stream_seq - last - 1
                    self.gap_count += 1
                    warnings.append(
                        f"[{self.name}] Gap in {stream_id}: "
                        f"missed {gap} event(s) (stream_seq {last} → {event.stream_seq})"
                    )
            self._last[key] = event.stream_seq
        return warnings


# ── Event Bus ─────────────────────────────────────────────────────────────────

Handler = Callable[[Event], None]


class EventBus:
    """
    Synchronous / async, thread-safe, production-hardened pub/sub event bus.

    Issue 3 — four dedicated locks replace the single global RLock:
        _sub_lock   (RLock) — subscribers, handler_states, before_emit_hooks
        _seq_lock   (Lock)  — stream_seqs LRU dict
        _count_lock (Lock)  — emit_counts, slow_calls, circuit_breaks
        _idem_lock  (Lock)  — seen_ids, dup_dropped
    Each _HandlerState has its own _lock for circuit-breaker fields, so
    concurrent workers updating different handlers never contend.
    """

    def __init__(
        self,
        mode:                   DispatchMode                     = DispatchMode.SYNC,
        async_config:           Optional[Dict[EventType, dict]]  = None,
        time_source:            Optional[TimeSource]             = None,
        idempotency_window:     int                              = 10_000,
        max_streams:            int                              = 1_000,
        backpressure_alert_fn:  Optional[Callable[[float], None]] = None,
    ):
        """
        Parameters
        ----------
        mode
            SYNC  — handlers run in the emitting thread.
            ASYNC — handlers run in per-EventType worker threads;
                    emit() returns immediately.
        async_config
            Per-EventType overrides: {'maxsize': int, 'policy': BackpressurePolicy,
            'n_workers': int}.  Merged with _DEFAULT_ASYNC_CONFIG.
        time_source
            Injected clock (SimulatedTimeSource for backtests).
        idempotency_window
            Max number of event_ids remembered for dedup.  0 = disabled.
        max_streams
            LRU cap on stream_seqs dict (issue 6).  Default 1 000.
        backpressure_alert_fn
            Optional callback(pressure_ratio: float) fired once when pressure
            crosses ALERT_THRESHOLD (issue 5).
        """
        self._mode        = mode
        self._time_source: TimeSource = time_source or _WALL_CLOCK

        # Issue 3: four purpose-specific locks
        self._sub_lock   = threading.RLock()   # RLock: handlers can call subscribe()
        self._seq_lock   = threading.Lock()
        self._count_lock = threading.Lock()
        self._idem_lock  = threading.Lock()

        # Subscribers: sorted (neg_priority, insertion_idx, key) tuples
        self._subscribers:    Dict[EventType, List[Tuple[int, int, _HandlerKey]]] = {}
        self._sub_counter:    int                                                   = 0
        self._handler_states: Dict[_HandlerKey, _HandlerState]                     = {}

        # Issue 7: before_emit hooks (protected by _sub_lock)
        self._before_emit_hooks: List[Callable[['Event'], None]] = []

        # Counters (protected by _count_lock)
        self._emit_counts:    Dict[EventType, int] = defaultdict(int)
        self._slow_calls:     int = 0
        self._circuit_breaks: int = 0

        # Issue 6: LRU-capped stream_seqs (protected by _seq_lock)
        self._max_streams  = max_streams
        self._stream_seqs: OrderedDict = OrderedDict()

        # E: idempotency (protected by _idem_lock)
        self._idempotency_window: int         = idempotency_window
        self._seen_ids:           OrderedDict = OrderedDict()
        self._dup_dropped:        int         = 0

        # Async mode
        if mode == DispatchMode.ASYNC:
            merged = {**_DEFAULT_ASYNC_CONFIG, **(async_config or {})}
            self._dispatchers: Dict[EventType, _PartitionedAsyncDispatcher] = {
                et: _PartitionedAsyncDispatcher(
                    event_type=et,
                    maxsize=merged[et]['maxsize'],
                    policy=merged[et]['policy'],
                    bus=self,
                    n_workers=merged[et].get('n_workers', 2),
                )
                for et in EventType
            }
            # Issue 5: systemic backpressure monitor
            self._bp_monitor: Optional[_BackpressureMonitor] = _BackpressureMonitor(
                self._dispatchers, alert_fn=backpressure_alert_fn,
            )
            log.info(
                f"[EventBus] ASYNC mode: {len(self._dispatchers)} dispatcher(s) started "
                f"(total workers: {sum(d._n for d in self._dispatchers.values())})"
            )
        else:
            self._dispatchers = {}
            self._bp_monitor  = None
            log.debug("[EventBus] SYNC mode.")

    # ── Subscribe / unsubscribe ───────────────────────────────────────────────

    def subscribe(
        self,
        event_type: EventType,
        handler:    Handler,
        priority:   int = 0,
    ) -> None:
        """
        Register handler for event_type.

        priority
            Higher integer = runs earlier in delivery sequence.
            Equal-priority handlers execute in registration order (stable).
            Use EventPriority constants:
                bus.subscribe(EventType.FILL, on_fill, priority=EventPriority.CRITICAL)
        """
        key = _HandlerKey(handler)
        with self._sub_lock:
            bucket = self._subscribers.setdefault(event_type, [])
            entry  = (-priority, self._sub_counter, key)
            self._sub_counter += 1
            bucket.append(entry)
            bucket.sort(key=lambda e: (e[0], e[1]))
            if key not in self._handler_states:
                self._handler_states[key] = _HandlerState()
        log.debug(f"Subscribed {key.__qualname__} → {event_type.name} priority={priority}")

    def unsubscribe(self, event_type: EventType, handler: Handler) -> None:
        """Remove handler and clean up its state if not registered elsewhere."""
        key = _HandlerKey(handler)
        with self._sub_lock:
            bucket = self._subscribers.get(event_type, [])
            self._subscribers[event_type] = [e for e in bucket if e[2] != key]
            still_registered = any(
                any(e[2] == key for e in entries)
                for entries in self._subscribers.values()
            )
            if not still_registered:
                self._handler_states.pop(key, None)

    # Issue 7: before_emit hooks
    def add_before_emit_hook(self, hook: Callable[['Event'], None]) -> None:
        """
        Register a synchronous hook called before any handler runs.

        Use case — durable ordering guarantee
        --------------------------------------
        Call bus.emit(event, durable=True) on ORDER_REQ / FILL events.
        DurableEventLog registers a produce+flush hook here so Redpanda
        acks the event BEFORE any in-process handler (PositionManager,
        ExecutionFeedback) mutates shared state.  This closes the
        async-write / handler-race window that existed in v3.

        Hooks run in registration order.  Exceptions are caught and logged;
        they do not block handler delivery.
        """
        with self._sub_lock:
            self._before_emit_hooks.append(hook)
        log.debug(f"[EventBus] before_emit hook: {getattr(hook, '__qualname__', repr(hook))}")

    # ── Emit ─────────────────────────────────────────────────────────────────

    def emit(self, event: Event, durable: bool = False) -> None:
        """
        Deliver event to all registered handlers.

        Parameters
        ----------
        event
            The event to emit.
        durable : bool, default False
            If True, all before_emit_hooks are called synchronously before
            any handler runs.  Use for ORDER_REQ / FILL events to guarantee
            Redpanda persistence precedes in-process state changes (issue 7).
        """
        # Step 1 — payload type check (no lock; _PAYLOAD_TYPES is read-only)
        expected = _PAYLOAD_TYPES.get(event.type)
        if expected is not None and not isinstance(event.payload, expected):
            raise TypeError(
                f"EventType.{event.type.name} requires {expected.__name__}, "
                f"got {type(event.payload).__name__}"
            )

        # Step 2 — E: idempotency (idem_lock)
        if self._idempotency_window > 0:
            with self._idem_lock:
                if event.event_id in self._seen_ids:
                    self._dup_dropped += 1
                    log.debug(
                        f"[idempotent] Duplicate {event.event_id[:8]} "
                        f"({event.type.name}) dropped."
                    )
                    return
                self._seen_ids[event.event_id] = None
                if len(self._seen_ids) > self._idempotency_window:
                    self._seen_ids.popitem(last=False)

        # Step 3 — D: timestamp
        event.timestamp = self._time_source.now()

        # Step 4 — F: priority
        if event.priority < 0:
            event.priority = _DEFAULT_PRIORITY.get(event.type, EventPriority.MEDIUM)

        # Step 5 — stream_seq with LRU cap (seq_lock) + emit_count (count_lock)
        stream_key = (event.type, getattr(event.payload, 'ticker', ''))
        with self._seq_lock:
            if stream_key not in self._stream_seqs:
                if len(self._stream_seqs) >= self._max_streams:
                    self._stream_seqs.popitem(last=False)   # evict LRU
                self._stream_seqs[stream_key] = 0
            self._stream_seqs[stream_key] += 1
            self._stream_seqs.move_to_end(stream_key)       # mark as recently used
            event.stream_seq = self._stream_seqs[stream_key]

        with self._count_lock:
            self._emit_counts[event.type] += 1

        # Step 6 — snapshot handlers + before_hooks (sub_lock)
        with self._sub_lock:
            snapshot = [
                (key, self._handler_states.get(key))
                for (_, _, key) in self._subscribers.get(event.type, [])
            ]
            before_hooks = list(self._before_emit_hooks) if durable else []

        # Step 7 — Issue 7: synchronous durable write before any handler
        for hook in before_hooks:
            try:
                hook(event)
            except Exception as exc:
                log.error(
                    f"[before_emit_hook] {getattr(hook, '__qualname__', repr(hook))} "
                    f"failed on {event.type.name}: {exc}",
                    exc_info=True,
                )

        # Step 8 — Issue 5: systemic backpressure check (ASYNC only)
        if self._bp_monitor is not None:
            self._bp_monitor.check()

        # Step 9 — deliver
        if self._mode == DispatchMode.ASYNC:
            self._dispatchers[event.type].put(event, snapshot)
        else:
            now = self._time_source.monotonic()
            for key, state in snapshot:
                self._deliver(event, key, state, now)

    # Issue 4: batch emit
    def emit_batch(self, events: List[Event], durable: bool = False) -> None:
        """
        Emit a list of events with O(4) lock acquisitions instead of O(4N).

        For a BAR fan-out of 170 tickers, emit() acquires 680 locks total.
        emit_batch() acquires 4 — one per lock region — regardless of batch size.

        Ordering: events are delivered in list order within each EventType partition.
        durable: if True, all before_emit_hooks are called for every event in the batch.
        """
        if not events:
            return

        # Step 1 — payload type check (no lock)
        for event in events:
            expected = _PAYLOAD_TYPES.get(event.type)
            if expected is not None and not isinstance(event.payload, expected):
                raise TypeError(
                    f"EventType.{event.type.name} requires {expected.__name__}, "
                    f"got {type(event.payload).__name__}"
                )

        # Step 2 — idempotency (one idem_lock acquisition for the whole batch)
        if self._idempotency_window > 0:
            filtered: List[Event] = []
            with self._idem_lock:
                for event in events:
                    if event.event_id in self._seen_ids:
                        self._dup_dropped += 1
                        continue
                    self._seen_ids[event.event_id] = None
                    if len(self._seen_ids) > self._idempotency_window:
                        self._seen_ids.popitem(last=False)
                    filtered.append(event)
            events = filtered
        if not events:
            return

        # Step 3 — D + F: timestamp + priority (no lock; time_source is thread-safe)
        now_dt = self._time_source.now()
        for event in events:
            event.timestamp = now_dt
            if event.priority < 0:
                event.priority = _DEFAULT_PRIORITY.get(event.type, EventPriority.MEDIUM)

        # Step 4 — stream_seqs (one seq_lock acquisition)
        with self._seq_lock:
            for event in events:
                stream_key = (event.type, getattr(event.payload, 'ticker', ''))
                if stream_key not in self._stream_seqs:
                    if len(self._stream_seqs) >= self._max_streams:
                        self._stream_seqs.popitem(last=False)
                    self._stream_seqs[stream_key] = 0
                self._stream_seqs[stream_key] += 1
                self._stream_seqs.move_to_end(stream_key)
                event.stream_seq = self._stream_seqs[stream_key]

        # Step 5 — emit_counts (one count_lock acquisition)
        with self._count_lock:
            for event in events:
                self._emit_counts[event.type] += 1

        # Step 6 — snapshot handlers (one sub_lock acquisition for all events)
        snapshots: List[Tuple[Event, list]] = []
        with self._sub_lock:
            before_hooks = list(self._before_emit_hooks) if durable else []
            for event in events:
                snapshot = [
                    (key, self._handler_states.get(key))
                    for (_, _, key) in self._subscribers.get(event.type, [])
                ]
                snapshots.append((event, snapshot))

        # Step 7 — before_emit_hooks (once per event, not per batch)
        if before_hooks:
            for event, _ in snapshots:
                for hook in before_hooks:
                    try:
                        hook(event)
                    except Exception as exc:
                        log.error(
                            f"[before_emit_hook] {getattr(hook, '__qualname__', repr(hook))} "
                            f"failed: {exc}",
                            exc_info=True,
                        )

        # Step 8 — backpressure (once per batch, not per event)
        if self._bp_monitor is not None:
            self._bp_monitor.check()

        # Step 9 — deliver
        if self._mode == DispatchMode.ASYNC:
            for event, snapshot in snapshots:
                self._dispatchers[event.type].put(event, snapshot)
        else:
            now_mono = self._time_source.monotonic()
            for event, snapshot in snapshots:
                for key, state in snapshot:
                    self._deliver(event, key, state, now_mono)

    def _deliver(
        self,
        event:  Event,
        key:    _HandlerKey,
        state:  Optional[_HandlerState],
        now:    float,
    ) -> None:
        """
        Deliver one event to one handler with circuit-breaker logic.

        Issue 3: all per-handler state mutations use state._lock (not the global
        _sub_lock), so concurrent workers processing different tickers do not
        contend.  Global counters (slow_calls, circuit_breaks) use _count_lock.

        Note on handler thread-safety
        --------------------------------
        With n_workers > 1, the same handler may be called concurrently for
        different tickers.  Handlers that share mutable state (e.g. positions
        dict) must be internally thread-safe.  The bus guarantees delivery
        ordering only within the same ticker partition (same worker).
        """
        # Circuit breaker check (per-handler lock)
        if state:
            with state._lock:
                cs = state.circuit_state(now)

            if cs == 'open':
                log.warning(
                    f"[circuit-open] Skipping {key.__qualname__} "
                    f"({state.disabled_until - now:.0f}s remaining)"
                )
                return
            if cs == 'half-open':
                log.info(
                    f"[circuit-half-open] Retrying {key.__qualname__} "
                    f"after {CIRCUIT_BREAKER_COOLDOWN:.0f}s cooldown"
                )

        t0 = self._time_source.monotonic()
        try:
            key(event)
            elapsed_ms = (self._time_source.monotonic() - t0) * 1000

            if state:
                with state._lock:
                    if state.disabled_until > 0.0:
                        log.info(f"[circuit-closed] {key.__qualname__} recovered")
                        state.disabled_until = 0.0
                    state.consecutive_failures = 0
                    state.latencies.append(elapsed_ms)
                    state.call_count += 1

            if elapsed_ms > SLOW_THRESHOLD_SEC * 1000:
                with self._count_lock:
                    self._slow_calls += 1
                log.warning(
                    f"[slow-handler] {key.__qualname__} took {elapsed_ms:.1f}ms "
                    f"on {event.type.name} (threshold {SLOW_THRESHOLD_SEC * 1000:.0f}ms)"
                )

        except Exception as exc:
            elapsed_ms = (self._time_source.monotonic() - t0) * 1000
            log.error(
                f"[handler-error] {key.__qualname__} raised on "
                f"{event.type.name} (seq={event.sequence}): {exc}",
                exc_info=True,
            )
            if state:
                tripped = False
                with state._lock:
                    state.consecutive_failures += 1
                    state.error_count          += 1
                    state.latencies.append(elapsed_ms)
                    if state.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                        state.disabled_until = (
                            self._time_source.monotonic() + CIRCUIT_BREAKER_COOLDOWN
                        )
                        state.trip_count += 1
                        tripped = True
                if tripped:
                    with self._count_lock:
                        self._circuit_breaks += 1
                    log.error(
                        f"[circuit-open] {key.__qualname__} suspended for "
                        f"{CIRCUIT_BREAKER_COOLDOWN:.0f}s after "
                        f"{CIRCUIT_BREAKER_THRESHOLD} consecutive failures"
                    )

    # ── Metrics ───────────────────────────────────────────────────────────────

    def metrics(self) -> BusMetrics:
        """Return a point-in-time snapshot of bus health metrics."""
        now = self._time_source.monotonic()

        # Handler states (sub_lock; per-handler state under state._lock)
        with self._sub_lock:
            exec_order = {
                et.name: [e[2].__qualname__ for e in entries]
                for et, entries in self._subscribers.items()
            }
            states_snapshot: Dict[str, tuple] = {}
            for key, state in self._handler_states.items():
                with state._lock:
                    states_snapshot[key.__qualname__] = (
                        state.call_count,
                        state.error_count,
                        state.trip_count,
                        list(state.latencies),
                        state.circuit_state(now),
                    )

        with self._count_lock:
            emit_counts   = {et.name: cnt for et, cnt in self._emit_counts.items()}
            slow_calls    = self._slow_calls
            circuit_total = self._circuit_breaks

        with self._idem_lock:
            dup_dropped    = self._dup_dropped
            seen_ids_count = len(self._seen_ids)

        with self._seq_lock:
            stream_seqs = {
                f"{et.name}/{t}" if t else et.name: seq
                for (et, t), seq in self._stream_seqs.items()
            }
            stream_count = len(self._stream_seqs)

        handler_calls:   Dict[str, int]   = {}
        handler_errors:  Dict[str, int]   = {}
        handler_trips:   Dict[str, int]   = {}
        handler_avg_ms:  Dict[str, float] = {}
        handler_max_ms:  Dict[str, float] = {}
        handler_circuit: Dict[str, str]   = {}
        active_breaks:   List[str]        = []

        for name, (calls, errors, trips, lats, cs) in states_snapshot.items():
            handler_calls[name]   = calls
            handler_errors[name]  = errors
            handler_trips[name]   = trips
            handler_avg_ms[name]  = sum(lats) / len(lats) if lats else 0.0
            handler_max_ms[name]  = max(lats) if lats else 0.0
            handler_circuit[name] = cs
            if cs == 'open':
                active_breaks.append(name)

        if self._dispatchers:
            queue_depths   = {et.name: d.queue_depth for et, d in self._dispatchers.items()}
            dropped_counts = {et.name: d.dropped     for et, d in self._dispatchers.items()}
        else:
            queue_depths   = {}
            dropped_counts = {}

        system_pressure = self._bp_monitor.pressure() if self._bp_monitor else 0.0

        return BusMetrics(
            emit_counts              = emit_counts,
            handler_calls            = handler_calls,
            handler_errors           = handler_errors,
            handler_trips            = handler_trips,
            handler_avg_ms           = handler_avg_ms,
            handler_max_ms           = handler_max_ms,
            handler_circuit          = handler_circuit,
            slow_calls               = slow_calls,
            circuit_breaks           = circuit_total,
            active_breaks            = active_breaks,
            stream_seqs              = stream_seqs,
            queue_depths             = queue_depths,
            dropped_counts           = dropped_counts,
            handler_execution_order  = exec_order,
            duplicate_events_dropped = dup_dropped,
            system_pressure          = system_pressure,
            stream_count             = stream_count,
            seen_ids_count           = seen_ids_count,
        )

    def subscribers(self, event_type: EventType) -> List[Handler]:
        """Return underlying handler functions in execution order (C)."""
        with self._sub_lock:
            return [e[2]._fn for e in self._subscribers.get(event_type, [])]

    def shutdown(self, timeout: float = 5.0) -> None:
        """
        Gracefully stop all async dispatcher threads after draining queues.
        No-op in SYNC mode.
        """
        if self._mode != DispatchMode.ASYNC:
            return
        for et, dispatcher in self._dispatchers.items():
            dispatcher.stop(timeout=timeout)
            log.debug(f"[EventBus] Dispatcher {et.name} stopped.")
        log.info("[EventBus] All async dispatchers shut down.")


# ── Event causality graph ─────────────────────────────────────────────────────

class CausalityTracker:
    """
    Builds an in-memory causality DAG from correlation_id links.

        tracker = CausalityTracker(bus)
        chain = tracker.chain(fill.event_id)   # [BAR, SIGNAL, ORDER_REQ, FILL]

    Memory bounded by max_events with O(1) OrderedDict eviction.
    """

    def __init__(self, bus: EventBus, max_events: int = 10_000):
        self._events:   OrderedDict          = OrderedDict()
        self._children: Dict[str, List[str]] = defaultdict(list)
        self._parent:   Dict[str, str]       = {}
        self._lock      = threading.Lock()
        self._max       = max_events
        for et in EventType:
            bus.subscribe(et, self._record)

    def _record(self, event: Event) -> None:
        with self._lock:
            if len(self._events) >= self._max:
                evicted_id, _ = self._events.popitem(last=False)
                parent_id = self._parent.pop(evicted_id, None)
                if parent_id and parent_id in self._children:
                    try:
                        self._children[parent_id].remove(evicted_id)
                    except ValueError:
                        pass
                    if not self._children[parent_id]:
                        del self._children[parent_id]
                self._children.pop(evicted_id, None)

            self._events[event.event_id] = event
            if event.correlation_id:
                self._children[event.correlation_id].append(event.event_id)
                self._parent[event.event_id] = event.correlation_id

    def chain(self, event_id: str) -> List[Event]:
        """Walk correlation_id links to root. Returns [root, ..., event]."""
        with self._lock:
            result: List[Event] = []
            seen:   set         = set()
            current = event_id
            while current and current not in seen:
                ev = self._events.get(current)
                if ev is None:
                    break
                result.append(ev)
                seen.add(current)
                current = ev.correlation_id
            return list(reversed(result))

    def children(self, event_id: str) -> List[Event]:
        """Return direct child events caused by this event."""
        with self._lock:
            return [
                self._events[cid]
                for cid in self._children.get(event_id, [])
                if cid in self._events
            ]

    def summary(self, event_id: str) -> str:
        ch = self.chain(event_id)
        if not ch:
            return f"(no chain for {event_id[:8]}…)"
        return " → ".join(f"{e.type.name}(seq={e.sequence})" for e in ch)
