"""
Inter-Process Communication via Redpanda for process-isolated engines.

Provides:
  - EventPublisher: publish events to Redpanda topics
  - EventConsumer: consume events from Redpanda topics with deserialization
  - Topic constants for all inter-process channels

Uses confluent-kafka (already installed, same library as event_log.py).
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Any

log = logging.getLogger(__name__)

# ── Topic names ──────────────────────────────────────────────────────────
TOPIC_ORDERS     = 'th-orders'       # Satellites → Core: ORDER_REQ
TOPIC_FILLS      = 'th-fills'        # Core → Satellites: FILL, POSITION
TOPIC_SIGNALS    = 'th-signals'      # Core VWAP → Options: SIGNAL events
TOPIC_POP        = 'th-pop-signals'  # Pop → Options: POP_SIGNAL
TOPIC_BAR_READY  = 'th-bar-ready'    # Core → All: "new bars available" notification
TOPIC_REGISTRY   = 'th-registry'     # All: position registry acquire/release
TOPIC_DISCOVERY  = 'th-discovery'    # V7.1: DataCollector → All: discovered tickers

DEFAULT_BROKERS = os.getenv('REDPANDA_BROKERS', '127.0.0.1:9092')


class EventPublisher:
    """Publish serialized events to a Redpanda topic."""

    def __init__(self, brokers: str = None, source_name: str = 'unknown'):
        from confluent_kafka import Producer
        self._source = source_name
        self._delivery_failures = 0
        self._producer = Producer({
            'bootstrap.servers': brokers or DEFAULT_BROKERS,
            'acks': '1',
            'retries': 5,
            'retry.backoff.ms': 500,
            'linger.ms': 20,
            'compression.type': 'snappy',
            'queue.buffering.max.messages': 10000,
        })
        self._running = True
        self._flush_thread = threading.Thread(target=self._bg_flush, daemon=True)
        self._flush_thread.start()

    def _delivery_report(self, err, msg):
        """V8: Callback for delivery confirmation. Logs failures after retries exhausted."""
        if err is not None:
            self._delivery_failures += 1
            log.error("[IPC] Message delivery FAILED after retries: %s topic=%s key=%s",
                      err, msg.topic(), msg.key().decode() if msg.key() else '?')

    def publish(self, topic: str, key: str, payload: dict,
                headers: dict = None, correlation_id: str = None) -> None:
        """Publish a message to a topic. Non-blocking.

        V7 P3-1: correlation_id included in envelope for cross-process tracing.
        """
        envelope = {
            'source': self._source,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'correlation_id': correlation_id or '',  # V7: cross-process tracing
            'payload': payload,
        }
        msg_headers = [(k, str(v).encode()) for k, v in (headers or {}).items()]
        try:
            self._producer.produce(
                topic=topic,
                key=key.encode('utf-8'),
                value=json.dumps(envelope, default=str).encode('utf-8'),
                headers=msg_headers,
                callback=self._delivery_report,  # V8: detect silent message loss
            )
        except Exception as exc:
            log.warning("[IPC] Publish failed on %s: %s", topic, exc)

    def publish_sync(self, topic: str, key: str, payload: dict, timeout: float = 5.0) -> bool:
        """Publish and wait for ack. Returns True on success."""
        self.publish(topic, key, payload)
        try:
            remaining = self._producer.flush(timeout)
            return remaining == 0
        except Exception:
            return False

    def flush(self, timeout: float = 5.0):
        self._producer.flush(timeout)

    def stop(self):
        self._running = False
        self._producer.flush(10)

    def _bg_flush(self):
        while self._running:
            try:
                self._producer.poll(0.25)
            except Exception:
                pass


class EventConsumer:
    """Consume events from Redpanda topics with callback dispatch."""

    def __init__(self, brokers: str = None, group_id: str = 'default',
                 topics: list = None, source_name: str = 'unknown'):
        from confluent_kafka import Consumer
        self._source = source_name
        # V7 P0-4: Manual commit — only commit offset AFTER handler succeeds.
        # Prevents at-least-once replay from creating duplicate orders on crash.
        # Auto-commit race: handler processes event → crash before 3s commit → replay.
        self._consumer = Consumer({
            'bootstrap.servers': brokers or DEFAULT_BROKERS,
            'group.id': group_id,
            'auto.offset.reset': 'latest',
            'enable.auto.commit': False,   # V7: manual commit after handler
            'session.timeout.ms': 15000,
            'max.poll.interval.ms': 60000,
        })
        self._handlers: Dict[str, Callable] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None

        if topics:
            self._consumer.subscribe(topics)

    def on(self, topic: str, handler: Callable) -> None:
        """Register a handler for a topic. handler(key: str, payload: dict)"""
        self._handlers[topic] = handler

    def start(self) -> None:
        """Start consuming in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._consume_loop, daemon=True,
                                         name=f"ipc-consumer-{self._source}")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        try:
            self._consumer.close()
        except Exception:
            pass

    def _consume_loop(self) -> None:
        while self._running:
            try:
                msg = self._consumer.poll(0.5)
                if msg is None:
                    continue
                if msg.error():
                    log.debug("[IPC] Consumer error: %s", msg.error())
                    continue

                topic = msg.topic()
                handler = self._handlers.get(topic)
                if handler is None:
                    # V7: Commit even for unhandled topics to advance offset
                    self._consumer.commit(msg, asynchronous=True)
                    continue

                key = msg.key().decode('utf-8') if msg.key() else ''
                try:
                    envelope = json.loads(msg.value().decode('utf-8'))
                    payload = envelope.get('payload', envelope)
                    # V7 P3-1: Propagate correlation_id from envelope into payload
                    # so downstream handlers can trace across process boundaries.
                    corr_id = envelope.get('correlation_id', '')
                    if corr_id:
                        payload['_ipc_correlation_id'] = corr_id
                    handler(key, payload)
                    # V7 P0-4: Commit AFTER handler succeeds.
                    self._consumer.commit(msg, asynchronous=True)
                except Exception as exc:
                    log.warning("[IPC] Handler error on %s: %s — "
                                "offset NOT committed (will replay)", topic, exc)
            except Exception as exc:
                log.warning("[IPC] Consume loop error: %s", exc)
                time.sleep(1)
