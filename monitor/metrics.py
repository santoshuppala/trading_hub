"""
V7 P3-3: Metrics Export — periodic EventBus metrics snapshot.

Writes EventBus metrics to data/metrics.json every 60 seconds.
Dashboards and monitoring tools can poll this file.

No new infrastructure required (file-based, like heartbeat.json).

Metrics exposed:
  - emit_counts: per-EventType emit counts
  - handler_avg_ms / handler_max_ms: per-handler latencies
  - handler_errors: per-handler error counts
  - circuit_breaks: total circuit breaker trips
  - active_breaks: currently open circuit breakers
  - queue_depths: per-EventType queue depths (async mode)
  - system_pressure: 0.0-1.0 backpressure ratio
  - slow_calls: handlers exceeding SLOW_THRESHOLD_SEC
  - duplicate_events_dropped: idempotency dedup count
  - sla_breaches: events delivered past deadline

Usage:
    from monitor.metrics import MetricsWriter
    mw = MetricsWriter(bus, interval_sec=60)
    # in main loop:
    mw.tick()
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ET = ZoneInfo('America/New_York')

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
METRICS_FILE = os.path.join(PROJECT_ROOT, 'data', 'metrics.json')


class MetricsWriter:
    """Periodically snapshot EventBus metrics to a JSON file."""

    def __init__(self, bus, interval_sec: float = 60.0,
                 metrics_path: str = None):
        self._bus = bus
        self._interval = interval_sec
        self._path = metrics_path or METRICS_FILE
        self._last_write = 0.0
        os.makedirs(os.path.dirname(self._path), exist_ok=True)

    def tick(self) -> None:
        """Write metrics if interval has elapsed. Call from main loop."""
        now = time.monotonic()
        if now - self._last_write < self._interval:
            return
        self._last_write = now
        self._write()

    def _write(self) -> None:
        """Snapshot EventBus metrics and write to file."""
        try:
            m = self._bus.metrics()
            data = {
                'timestamp': datetime.now(ET).isoformat(),
                'wall_clock': time.time(),

                # Event throughput
                'emit_counts': m.emit_counts,

                # Handler performance
                'handler_avg_ms': m.handler_avg_ms,
                'handler_max_ms': m.handler_max_ms,
                'handler_calls': m.handler_calls,
                'handler_errors': m.handler_errors,
                'handler_circuit': m.handler_circuit,

                # Health indicators
                'slow_calls': m.slow_calls,
                'circuit_breaks': m.circuit_breaks,
                'active_breaks': m.active_breaks,
                'sla_breaches': m.sla_breaches,
                'dlq_count': m.dlq_count,

                # Resource usage
                'queue_depths': m.queue_depths,
                'system_pressure': round(m.system_pressure, 4),
                'stream_count': m.stream_count,
                'seen_ids_count': m.seen_ids_count,
                'duplicate_events_dropped': m.duplicate_events_dropped,
                'retried_deliveries': m.retried_deliveries,
            }

            tmp = self._path + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, self._path)

        except Exception as exc:
            log.debug("[Metrics] Write failed: %s", exc)

    def read(self) -> dict | None:
        """Read latest metrics snapshot (for dashboards)."""
        try:
            with open(self._path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
