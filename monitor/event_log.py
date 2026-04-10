"""
Durable Event Log — Redpanda (Kafka-compatible)
================================================
Provides crash-safe persistence for every event emitted on the EventBus.

Why this matters
----------------
Without a durable log the bus is volatile: a crash between FILL and
PositionManager's POSITION event can leave the bot in an unknown state.
With Redpanda every event is written to a durable, replicated log *before*
the process can forget it.  On restart, `EventLogConsumer.replay()` can
reconstruct the exact event sequence since the last checkpoint.

Architecture
------------

  DurableEventLog  (producer side)
    ├─ Subscribes to ALL EventTypes on the bus (last, so it sees everything)
    ├─ Serialises each event to JSON (non-serialisable fields handled safely)
    ├─ Produces to topic "trading-hub-events" via async confluent_kafka.Producer
    │    key   = ticker  (ensures per-ticker ordering in a partition)
    │    value = UTF-8 JSON
    │    headers:
    │      event_id       – UUID string
    │      correlation_id – UUID string or ""
    │      event_type     – EventType.name
    │      stream_seq     – str(stream_seq) or ""
    ├─ Delivery callbacks log errors but never block the bus thread
    └─ Background flush thread drains the producer queue every 250 ms

  EventLogConsumer  (consumer side)
    ├─ Wraps a confluent_kafka.Consumer
    ├─ replay(n=None)  — yields the last n messages (or all) from the topic
    │                    useful for post-crash state reconstruction
    ├─ tail(timeout_ms)— poll indefinitely; yields Events as they arrive
    └─ seek_to_time(dt) — rewind to a specific wall-clock point

Serialisation contract
----------------------
Fields always present in the JSON envelope:
  event_id, correlation_id, sequence, stream_seq, timestamp (ISO-8601),
  event_type (EventType.name), payload_type (class name), payload (dict)

Payload serialisation rules (applied per field):
  • Callable      → omitted
  • pd.DataFrame  → {"__dataframe__": true, "shape": [...], "columns": [...],
                      "last_row": {...}}   (full data omitted for size)
  • pd.Series     → {"__series__": true, "length": N}
  • datetime      → ISO-8601 string
  • set           → sorted list
  • anything else → str()  if not directly JSON-serialisable

Deserialisation produces plain dicts (not typed dataclasses); callers that
need live payloads must reconstruct them from the dict.

Topic design
------------
  Name        : trading-hub-events
  Partitions  : 9   (one per EventType keeps per-type ordering; enough headroom)
  Key         : ticker string (consistent per-ticker ordering within type)
  Retention   : Redpanda default (configurable via rpk or broker config)

Usage
-----
    from .event_log import DurableEventLog, EventLogConsumer

    # Wire up producer (call AFTER all other subscribers)
    durable = DurableEventLog(bus, brokers='127.0.0.1:9092')

    # Replay last 1000 events on restart
    consumer = EventLogConsumer(brokers='127.0.0.1:9092')
    for record in consumer.replay(n=1000):
        print(record)          # dict with event envelope + payload

    # Tail live events (e.g. from a separate audit process)
    for record in consumer.tail(timeout_sec=30):
        ...
"""
from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, Generator, Iterator, List, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo('America/New_York')

TOPIC          = 'trading-hub-events'
FLUSH_INTERVAL = 0.25   # seconds — background flush cadence
_SENTINEL      = object()


# ── Serialisation helpers ─────────────────────────────────────────────────────

def _safe_value(v: Any) -> Any:
    """
    Convert a value to something JSON-serialisable.
    Returns _SENTINEL for values that should be omitted entirely (callables).
    """
    if callable(v):
        return _SENTINEL                          # omit callables (refresh_ask etc.)

    # pandas DataFrame
    try:
        import pandas as pd
        if isinstance(v, pd.DataFrame):
            last_row = {}
            if not v.empty:
                try:
                    last_row = v.iloc[-1].to_dict()
                    last_row = {k: _safe_value(val) for k, val in last_row.items()}
                except Exception:
                    pass
            return {
                '__dataframe__': True,
                'shape':   list(v.shape),
                'columns': list(v.columns),
                'last_row': last_row,
            }
        if isinstance(v, pd.Series):
            return {'__series__': True, 'length': len(v)}
    except ImportError:
        pass

    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, set):
        return sorted(str(x) for x in v)
    if isinstance(v, (str, int, float, bool, type(None))):
        return v
    if isinstance(v, (list, tuple)):
        result = []
        for item in v:
            sv = _safe_value(item)
            if sv is not _SENTINEL:
                result.append(sv)
        return result
    if isinstance(v, dict):
        return {str(k): _safe_value(val) for k, val in v.items() if _safe_value(val) is not _SENTINEL}

    # Fallback: try str
    try:
        return str(v)
    except Exception:
        return '<unserializable>'


def _serialise_payload(payload: Any) -> dict:
    """Serialise a payload dataclass to a plain dict, dropping non-serialisable fields."""
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        d = dataclasses.asdict(payload)
    elif hasattr(payload, '__dict__'):
        d = dict(payload.__dict__)
    else:
        return {'__raw__': str(payload)}

    result = {}
    for k, v in d.items():
        sv = _safe_value(v)
        if sv is not _SENTINEL:
            result[k] = sv
    return result


def _serialise_event(event) -> bytes:
    """Serialise an Event to UTF-8 JSON bytes."""
    envelope = {
        'event_id':       event.event_id,
        'correlation_id': event.correlation_id or '',
        'sequence':       event.sequence,
        'stream_seq':     event.stream_seq,
        'timestamp':      event.timestamp.isoformat() if event.timestamp else None,
        'event_type':     event.type.name,
        'payload_type':   type(event.payload).__name__,
        'payload':        _serialise_payload(event.payload),
    }
    return json.dumps(envelope, default=str).encode('utf-8')


# ── DurableEventLog ───────────────────────────────────────────────────────────

class DurableEventLog:
    """
    Async producer that durably writes every bus event to Redpanda.

    - Never blocks the calling (bus) thread: produce() is async.
    - Delivery errors are logged but do not raise.
    - A daemon thread flushes the producer queue every FLUSH_INTERVAL seconds.
    - Call close() at shutdown to drain any remaining messages.
    """

    def __init__(
        self,
        bus,                              # EventBus
        brokers: str = '127.0.0.1:9092',
        topic: str = TOPIC,
        extra_config: Optional[Dict] = None,
    ):
        from confluent_kafka import Producer   # imported lazily — optional dep

        self._topic    = topic
        self._lock     = threading.Lock()
        self._error_count = 0

        cfg = {
            'bootstrap.servers': brokers,
            'acks':              'all',          # wait for leader + ISR
            'retries':           5,
            'retry.backoff.ms':  200,
            'linger.ms':         5,              # small batching window
            'queue.buffering.max.messages': 100_000,
        }
        if extra_config:
            cfg.update(extra_config)

        try:
            self._producer = Producer(cfg)
            log.info(f"[DurableEventLog] Producer connected to {brokers} topic={topic}")
        except Exception as e:
            log.error(f"[DurableEventLog] Failed to create producer: {e}")
            self._producer = None
            return

        # Subscribe to all EventTypes (import here to avoid circular at module level)
        from .event_bus import EventType
        for et in EventType:
            bus.subscribe(et, self._on_event)

        # Background flush thread
        self._running = True
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name='event-log-flush'
        )
        self._flush_thread.start()

    # ── Issue 7: synchronous durable hook ────────────────────────────────────

    def register_durable_hook(self, bus) -> None:
        """
        Register _durable_produce as a before_emit_hook on the EventBus.

        After this call, any emit(event, durable=True) will synchronously
        produce and flush the event to Redpanda BEFORE any in-process handler
        runs.  Use for ORDER_REQ and FILL events where Redpanda persistence
        must precede PositionManager / ExecutionFeedback state mutations.

        The async _on_event subscription (registered in __init__) continues to
        handle all other events without blocking.
        """
        if self._producer is not None:
            bus.add_before_emit_hook(self._durable_produce)
            log.info("[DurableEventLog] Durable before_emit_hook registered.")

    def _durable_produce(self, event) -> None:
        """
        Synchronous produce + flush for durable=True events.

        Raises on flush timeout so the caller (emit) can log the failure.
        Unlike _on_event, this BLOCKS until Redpanda acks (or FLUSH_TIMEOUT elapses).
        """
        FLUSH_TIMEOUT = 2.0
        if self._producer is None:
            return
        try:
            value   = _serialise_event(event)
            key     = getattr(event.payload, 'ticker', '') or ''
            headers = {
                'event_id':       event.event_id,
                'correlation_id': event.correlation_id or '',
                'event_type':     event.type.name,
                'stream_seq':     str(event.stream_seq) if event.stream_seq is not None else '',
                'durable':        'true',
            }
            self._producer.produce(
                topic=self._topic,
                key=key.encode('utf-8'),
                value=value,
                headers=[(k, v.encode('utf-8')) for k, v in headers.items()],
                on_delivery=self._on_delivery,
            )
            remaining = self._producer.flush(timeout=FLUSH_TIMEOUT)
            if remaining:
                log.warning(
                    f"[DurableEventLog] _durable_produce: {remaining} message(s) "
                    f"unacked after {FLUSH_TIMEOUT}s for {event.type.name}."
                )
        except Exception as e:
            with self._lock:
                self._error_count += 1
            log.error(
                f"[DurableEventLog] _durable_produce failed for {event.type.name}: {e}"
            )
            raise   # re-raise so EventBus can log it as a hook failure

    # ── Async handler ─────────────────────────────────────────────────────────

    def _on_event(self, event) -> None:
        if self._producer is None:
            return
        try:
            value   = _serialise_event(event)
            key     = getattr(event.payload, 'ticker', '') or ''
            headers = {
                'event_id':       event.event_id,
                'correlation_id': event.correlation_id or '',
                'event_type':     event.type.name,
                'stream_seq':     str(event.stream_seq) if event.stream_seq is not None else '',
            }
            self._producer.produce(
                topic=self._topic,
                key=key.encode('utf-8'),
                value=value,
                headers=[(k, v.encode('utf-8')) for k, v in headers.items()],
                on_delivery=self._on_delivery,
            )
            # Non-blocking: trigger internal network loop without waiting
            self._producer.poll(0)
        except Exception as e:
            with self._lock:
                self._error_count += 1
            log.warning(f"[DurableEventLog] produce() failed for {event.type.name}: {e}")

    def _on_delivery(self, err, msg) -> None:
        if err:
            with self._lock:
                self._error_count += 1
            log.error(
                f"[DurableEventLog] Delivery failed: "
                f"topic={msg.topic()} partition={msg.partition()} "
                f"offset={msg.offset()} error={err}"
            )
        else:
            log.debug(
                f"[DurableEventLog] Delivered to "
                f"{msg.topic()}[{msg.partition()}]@{msg.offset()}"
            )

    # ── Flush loop ────────────────────────────────────────────────────────────

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(FLUSH_INTERVAL)
            if self._producer:
                try:
                    self._producer.flush(timeout=1.0)
                except Exception as e:
                    log.warning(f"[DurableEventLog] flush error: {e}")

    # ── Shutdown ─────────────────────────────────────────────────────────────

    def close(self, timeout: float = 5.0) -> None:
        """Drain all buffered messages and stop the flush thread."""
        self._running = False
        if self._producer:
            try:
                remaining = self._producer.flush(timeout=timeout)
                if remaining:
                    log.warning(
                        f"[DurableEventLog] {remaining} message(s) undelivered at shutdown."
                    )
                else:
                    log.info("[DurableEventLog] All messages flushed.")
            except Exception as e:
                log.error(f"[DurableEventLog] close() flush error: {e}")

    @property
    def error_count(self) -> int:
        with self._lock:
            return self._error_count


# ── EventLogConsumer ──────────────────────────────────────────────────────────

class EventLogConsumer:
    """
    Read-side interface for the durable event log.

    Typical uses
    ------------
    1. Post-crash state reconstruction:
           consumer = EventLogConsumer()
           for rec in consumer.replay(since=checkpoint_ts):
               rebuild_state(rec)

    2. Audit / monitoring from a separate process:
           for rec in consumer.tail():
               audit_log(rec)

    3. Rewind to a specific point in time:
           consumer.seek_to_time(datetime(2026, 4, 10, 9, 30, tzinfo=ET))
           for rec in consumer.tail(timeout_sec=10):
               ...
    """

    def __init__(
        self,
        brokers: str = '127.0.0.1:9092',
        topic: str = TOPIC,
        group_id: str = 'trading-hub-audit',
        extra_config: Optional[Dict] = None,
    ):
        from confluent_kafka import Consumer, KafkaException   # noqa: F401 — validated here

        self._topic   = topic
        self._brokers = brokers

        cfg = {
            'bootstrap.servers':  brokers,
            'group.id':           group_id,
            'auto.offset.reset':  'earliest',
            'enable.auto.commit': False,
        }
        if extra_config:
            cfg.update(extra_config)

        self._cfg = cfg
        self._consumer = Consumer(cfg)
        log.info(f"[EventLogConsumer] Connected to {brokers} topic={topic}")

    def _make_consumer(self):
        from confluent_kafka import Consumer
        return Consumer(self._cfg)

    # ── Replay ────────────────────────────────────────────────────────────────

    def replay(
        self,
        n: Optional[int] = None,
        since: Optional[datetime] = None,
    ) -> Iterator[dict]:
        """
        Yield deserialized event records from the topic.

        Parameters
        ----------
        n     : max number of records to yield (None = all available)
        since : only yield records after this timestamp (UTC-aware datetime)

        Yields
        ------
        dict — the JSON envelope: {event_id, event_type, payload, ...}
        """
        from confluent_kafka import Consumer, TopicPartition, OFFSET_BEGINNING
        from confluent_kafka import KafkaError

        consumer = Consumer(self._cfg)
        try:
            meta = consumer.list_topics(self._topic, timeout=5.0)
            partitions = [
                TopicPartition(self._topic, p)
                for p in meta.topics[self._topic].partitions
            ]

            if since is not None:
                # seek_to_time: find offsets for the given timestamp
                ts_ms = int(since.timestamp() * 1000)
                tps_with_ts = [
                    TopicPartition(self._topic, p.partition, ts_ms)
                    for p in partitions
                ]
                offsets = consumer.offsets_for_times(tps_with_ts, timeout=5.0)
                # Use OFFSET_BEGINNING for partitions with no data after `since`
                partitions = [
                    TopicPartition(self._topic, o.partition,
                                   o.offset if o.offset >= 0 else OFFSET_BEGINNING)
                    for o in offsets
                ]
            else:
                for p in partitions:
                    p.offset = OFFSET_BEGINNING

            consumer.assign(partitions)

            count = 0
            while True:
                msg = consumer.poll(timeout=2.0)
                if msg is None:
                    break   # no more messages in the given window
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        break
                    log.warning(f"[EventLogConsumer] poll error: {msg.error()}")
                    break

                record = self._decode(msg)
                if record:
                    yield record
                    count += 1
                    if n is not None and count >= n:
                        break
        finally:
            consumer.close()

    def tail(
        self,
        timeout_sec: float = 0.0,
        commit: bool = False,
    ) -> Generator[dict, None, None]:
        """
        Yield live events as they arrive.

        Parameters
        ----------
        timeout_sec : stop after this many seconds of silence (0 = run forever)
        commit      : commit offsets after each message

        Yields
        ------
        dict — decoded event envelope
        """
        from confluent_kafka import KafkaError

        self._consumer.subscribe([self._topic])
        deadline = time.monotonic() + timeout_sec if timeout_sec > 0 else None

        try:
            while True:
                if deadline and time.monotonic() > deadline:
                    break
                msg = self._consumer.poll(timeout=1.0)
                if msg is None:
                    continue
                if msg.error():
                    if msg.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    log.warning(f"[EventLogConsumer] tail error: {msg.error()}")
                    continue

                record = self._decode(msg)
                if record:
                    if commit:
                        self._consumer.commit(asynchronous=True)
                    yield record
        finally:
            self._consumer.close()

    def seek_to_time(self, dt: datetime) -> None:
        """
        Rewind the consumer to the first message at or after `dt`.
        Call before tail() to start from a specific point in time.
        """
        from confluent_kafka import TopicPartition

        ts_ms = int(dt.timestamp() * 1000)
        meta  = self._consumer.list_topics(self._topic, timeout=5.0)
        tps   = [
            TopicPartition(self._topic, pid, ts_ms)
            for pid in meta.topics[self._topic].partitions
        ]
        offsets = self._consumer.offsets_for_times(tps, timeout=5.0)
        self._consumer.assign(offsets)
        log.info(f"[EventLogConsumer] Seeked to {dt.isoformat()}")

    def close(self) -> None:
        try:
            self._consumer.close()
        except Exception:
            pass

    # ── Decode ────────────────────────────────────────────────────────────────

    @staticmethod
    def _decode(msg) -> Optional[dict]:
        try:
            return json.loads(msg.value().decode('utf-8'))
        except Exception as e:
            log.warning(f"[EventLogConsumer] decode error: {e}")
            return None


# ── Replay-based crash recovery helper ───────────────────────────────────────

class CrashRecovery:
    """
    Reconstruct position state from the durable event log after a crash.

    Replays FILL and POSITION events since the start of today, returning
    a positions dict and trade_log list consistent with what PositionManager
    would have built.

    Usage
    -----
        recovery = CrashRecovery()
        positions, trade_log = recovery.rebuild()
        # pass into RealTimeMonitor(...)
    """

    def __init__(
        self,
        brokers: str = '127.0.0.1:9092',
        topic: str = TOPIC,
    ):
        self._consumer = EventLogConsumer(brokers=brokers, topic=topic,
                                          group_id='trading-hub-recovery')

    def rebuild(self) -> tuple[dict, list]:
        """
        Return (positions, trade_log) rebuilt from today's event log.
        Only FILL events are used — they are the ground truth for what
        the broker confirmed.
        """
        today_start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        positions:  dict = {}
        trade_log:  list = []

        for rec in self._consumer.replay(since=today_start):
            et = rec.get('event_type', '')
            p  = rec.get('payload', {})

            if et == 'FILL':
                ticker     = p.get('ticker', '')
                side       = p.get('side', '')
                qty        = int(p.get('qty', 0))
                fill_price = float(p.get('fill_price', 0))
                reason     = p.get('reason', '')
                order_id   = p.get('order_id', '')

                if side == 'buy' and ticker:
                    positions[ticker] = {
                        'entry_price':  fill_price,
                        'entry_time':   rec.get('timestamp', '')[:8],
                        'quantity':     qty,
                        'partial_done': False,
                        'order_id':     order_id,
                        # stop/target will be patched by ExecutionFeedback
                        # if SIGNAL events are also replayed, or left as fallback
                        'stop_price':   fill_price * 0.995,
                        'target_price': fill_price * 1.01,
                        'half_target':  fill_price * 1.005,
                        'atr_value':    None,
                    }
                elif side == 'sell' and ticker:
                    if ticker in positions:
                        pos = positions[ticker]
                        sold_qty = qty
                        remaining = pos['quantity'] - sold_qty
                        pnl = round((fill_price - pos['entry_price']) * sold_qty, 2)
                        trade_log.append({
                            'ticker':      ticker,
                            'entry_price': pos['entry_price'],
                            'entry_time':  pos.get('entry_time', ''),
                            'exit_price':  fill_price,
                            'qty':         sold_qty,
                            'pnl':         pnl,
                            'reason':      reason,
                            'time':        rec.get('timestamp', '')[:8],
                            'is_win':      pnl >= 0,
                        })
                        if remaining > 0 and reason == 'partial_sell':
                            pos['quantity'] = remaining
                            pos['partial_done'] = True
                            pos['stop_price']   = pos['entry_price']
                        else:
                            del positions[ticker]

        open_count = len(positions)
        if open_count:
            log.warning(
                f"[CrashRecovery] Rebuilt {open_count} open position(s) "
                f"from event log: {list(positions.keys())}"
            )
        log.info(
            f"[CrashRecovery] Replayed {len(trade_log)} completed trade(s) from today."
        )
        self._consumer.close()
        return positions, trade_log
