"""
EquityTracker — broker equity monitoring and P&L drift detection.

Captures baseline equity at session start from ALL brokers (via BrokerRegistry),
then periodically compares computed P&L against actual broker equity changes.

Alerts on drift:
    > $50  → WARNING in log
    > $500 → CRITICAL alert email

Works with any number of brokers — adding a new broker to BrokerRegistry
automatically includes it in equity tracking.

Usage:
    tracker = EquityTracker(broker_registry, pnl_fn, alert_email)
    tracker.set_baseline()                    # once at session start
    drift = tracker.check_drift()             # hourly
    snapshot = tracker.get_snapshot()          # for dashboards
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

log = logging.getLogger(__name__)

DRIFT_WARN_THRESHOLD = 50.0     # $50 — log warning
DRIFT_ALERT_THRESHOLD = 500.0   # $500 — send email alert


class EquityTracker:
    """Tracks equity across all brokers and detects P&L drift."""

    def __init__(
        self,
        broker_registry,
        pnl_fn: Callable[[], float],
        alert_email: Optional[str] = None,
    ):
        """
        Args:
            broker_registry: BrokerRegistry instance with all brokers registered.
            pnl_fn: Callable returning today's computed realized P&L.
                     During shadow mode: lambda: sum(t['pnl'] for t in trade_log)
                     After switchover:   lambda: ledger.daily_realized_pnl()
            alert_email: Email for critical drift alerts.
        """
        self._registry = broker_registry
        self._pnl_fn = pnl_fn
        self._alert_email = alert_email
        self._baseline: Optional[Dict[str, Optional[float]]] = None

    def set_baseline(self) -> None:
        """Capture equity baseline from all brokers. Call once at session start."""
        self._baseline = self._registry.get_all_equity()
        parts = []
        for name, eq in self._baseline.items():
            if eq is not None:
                parts.append(f"  {name}: ${eq:,.2f}")
            else:
                parts.append(f"  {name}: N/A")
        log.info("SESSION EQUITY BASELINE\n%s", "\n".join(parts))

    def check_drift(self) -> float:
        """Compare computed P&L vs broker equity change.

        Call periodically (e.g., hourly from _run_loop).

        Returns:
            Absolute drift amount in dollars.
        """
        if self._baseline is None:
            self.set_baseline()
            return 0.0

        current = self._registry.get_all_equity()

        # Compute broker equity P&L (current - baseline)
        broker_pnl = 0.0
        parts = []
        for name in self._baseline:
            base_eq = self._baseline.get(name)
            curr_eq = current.get(name)
            if base_eq is not None and curr_eq is not None:
                delta = curr_eq - base_eq
                broker_pnl += delta
                parts.append(f"{name}=${delta:+.2f}")
            else:
                parts.append(f"{name}=N/A")

        # Computed P&L from trade log or FillLedger
        try:
            computed_pnl = self._pnl_fn()
        except Exception as exc:
            log.warning("[EquityTracker] pnl_fn failed: %s", exc)
            computed_pnl = 0.0

        drift = abs(computed_pnl - broker_pnl)

        log.info(
            "EQUITY CHECK | Computed: $%+.2f | Broker: $%+.2f (%s) | Drift: $%.2f",
            computed_pnl, broker_pnl, ", ".join(parts), drift,
        )

        if drift > DRIFT_ALERT_THRESHOLD:
            log.error("[PNL_DRIFT] CRITICAL: $%.2f drift", drift)
            self._send_alert(
                f"PNL DRIFT ALERT: computed=${computed_pnl:+.2f} vs "
                f"broker=${broker_pnl:+.2f} (drift=${drift:.2f}). "
                f"Breakdown: {', '.join(parts)}"
            )
        elif drift > DRIFT_WARN_THRESHOLD:
            log.warning(
                "[PNL_DRIFT] $%.2f drift (computed=$%+.2f broker=$%+.2f)",
                drift, computed_pnl, broker_pnl,
            )

        return drift

    def get_snapshot(self) -> dict:
        """Current equity snapshot for dashboards/status."""
        current = self._registry.get_all_equity()
        return {
            'baseline': dict(self._baseline) if self._baseline else {},
            'current': current,
            'broker_names': self._registry.names(),
        }

    def _send_alert(self, message: str) -> None:
        """Send email alert for critical drift."""
        if not self._alert_email:
            return
        try:
            from .alerts import send_alert
            send_alert(self._alert_email, message, severity='CRITICAL')
        except Exception as exc:
            log.error("[EquityTracker] Alert send failed: %s", exc)
