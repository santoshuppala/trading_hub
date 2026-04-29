"""
SatelliteEODReport — end-of-day trade summary for satellite engines.

Modeled after monitor/observability.py:EODSummary.send().
Generates structured report with:
- Overall stats (trades, wins, P&L)
- Exit reason breakdown
- Per-strategy breakdown
- Trade-by-trade log
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo('America/New_York')


class SatelliteEODReport:

    def __init__(self, engine_name: str, adapter, alert_email: Optional[str] = None):
        self._name = engine_name
        self._adapter = adapter
        self._alert_email = alert_email

    def generate(self) -> None:
        """Generate and log/email the EOD report."""
        try:
            trades = self._adapter.get_trade_log()
            stats = self._adapter.get_daily_stats()

            if not trades:
                msg = f"[{self._name.upper()}] EOD: No trades today."
                log.info(msg)
                self._send_alert(msg)
                return

            total_pnl = sum(t.get('pnl', 0) or 0 for t in trades)
            wins = [t for t in trades if (t.get('pnl') or 0) >= 0]
            losses = [t for t in trades if (t.get('pnl') or 0) < 0]
            win_rate = len(wins) / len(trades) if trades else 0

            avg_win = sum(t.get('pnl', 0) for t in wins) / len(wins) if wins else 0
            avg_loss = sum(t.get('pnl', 0) for t in losses) / len(losses) if losses else 0
            gross_win = sum(t.get('pnl', 0) for t in wins)
            gross_loss = abs(sum(t.get('pnl', 0) for t in losses))
            profit_factor = gross_win / gross_loss if gross_loss > 0 else float('inf')

            lines = [
                "=" * 56,
                f"EOD SUMMARY — {self._name.upper()} ENGINE  "
                f"{datetime.now(ET).strftime('%Y-%m-%d')}",
                "=" * 56,
                f"Trades      : {len(trades)}",
                f"Wins        : {len(wins)} ({win_rate:.0%})",
                f"Losses      : {len(losses)}",
                f"Total PnL   : ${total_pnl:+.2f}",
                f"Avg Win     : ${avg_win:+.2f}",
                f"Avg Loss    : ${avg_loss:+.2f}",
                f"Profit Factor: {profit_factor:.2f}",
                "-" * 56,
            ]

            # Exit reason breakdown
            reasons = {}
            for t in trades:
                r = t.get('reason', 'unknown')
                if r not in reasons:
                    reasons[r] = {'count': 0, 'pnl': 0.0}
                reasons[r]['count'] += 1
                reasons[r]['pnl'] += t.get('pnl', 0) or 0

            if reasons:
                lines.append("Exit Reasons:")
                for r, d in sorted(reasons.items(), key=lambda x: -x[1]['pnl']):
                    lines.append(f"  {r:25s} {d['count']:>3d} trades  ${d['pnl']:+.2f}")

            # Per-strategy breakdown
            strategies = {}
            for t in trades:
                s = t.get('strategy', 'unknown')
                if s not in strategies:
                    strategies[s] = {'count': 0, 'pnl': 0.0, 'wins': 0}
                strategies[s]['count'] += 1
                strategies[s]['pnl'] += t.get('pnl', 0) or 0
                if (t.get('pnl') or 0) >= 0:
                    strategies[s]['wins'] += 1

            if strategies:
                lines.append("Strategies:")
                for s, d in sorted(strategies.items(), key=lambda x: -x[1]['pnl']):
                    wr = d['wins'] / d['count'] if d['count'] > 0 else 0
                    lines.append(f"  {s:25s} {d['count']:>3d} trades  "
                                 f"${d['pnl']:+.2f}  ({wr:.0%} win)")

            # Trade-by-trade log
            lines.append("-" * 56)
            lines.append("Trade Log:")
            for t in trades:
                pnl = t.get('pnl', 0) or 0
                flag = "+" if pnl >= 0 else "-"
                lines.append(
                    f"  {flag} {t.get('ticker', '?'):6s} "
                    f"{t.get('qty', 0):>4} sh "
                    f"entry=${t.get('entry_price', 0):.2f} "
                    f"exit=${t.get('exit_price', 0):.2f} "
                    f"pnl=${pnl:+.2f}  ({t.get('reason', '')})"
                )

            # Engine-specific stats
            extra = stats.get('extra', {})
            if extra:
                lines.append("-" * 56)
                for k, v in extra.items():
                    lines.append(f"  {k}: {v}")

            lines.append("=" * 56)

            report = "\n".join(lines)
            log.info("\n%s", report)
            self._send_alert(report)

        except Exception as exc:
            log.warning("[%s] EOD report generation failed: %s", self._name, exc)

        # V10: ML analytics moved to lifecycle/core.py shutdown() — runs on
        # EVERY shutdown (crash, restart, EOD), not just EOD report.

        # V10: Weekly options PF review — auto-triggers on Fridays
        if self._name == 'options':
            self._check_weekly_review()

    def _run_post_session_analytics(self) -> None:
        """Auto-run ML data population jobs at EOD.

        Populates ml_signal_context, ml_trade_outcomes, ml_rejection_log
        from today's event_store data. Critical for ML training — without
        this, signal features are lost daily.
        """
        try:
            from datetime import date
            from scripts.post_session_analytics import (
                job_signal_context, job_trade_outcomes, job_rejection_log,
            )
            import psycopg2
            from config import DATABASE_URL

            conn = psycopg2.connect(DATABASE_URL)
            today = date.today()

            sc = job_signal_context(conn, today)
            to = job_trade_outcomes(conn, today)
            rl = job_rejection_log(conn, today)

            conn.close()
            log.info("[%s] Post-session analytics complete: "
                     "signal_context=%d, trade_outcomes=%d, rejection_log=%d",
                     self._name, sc, to, rl)
        except Exception as exc:
            log.warning("[%s] Post-session analytics failed (non-fatal): %s",
                        self._name, exc)

    def _check_weekly_review(self) -> None:
        """Auto-run weekly PF analysis on Fridays."""
        try:
            now = datetime.now(ET)
            if now.weekday() != 4:  # 4 = Friday
                return

            log.info("[%s] Friday EOD — running weekly options PF review", self._name)
            from scripts.weekly_options_review import load_trades, generate_report
            trades = load_trades(days=7)
            report = generate_report(trades, days=7)
            log.info("\n%s", report)
            self._send_alert(report)

            # Save report file
            import os
            report_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'data', 'reports')
            os.makedirs(report_dir, exist_ok=True)
            path = os.path.join(report_dir,
                                f'options_weekly_{now.strftime("%Y%m%d")}.txt')
            with open(path, 'w') as f:
                f.write(report)
            log.info("[%s] Weekly report saved: %s", self._name, path)
        except Exception as exc:
            log.warning("[%s] Weekly review failed: %s", self._name, exc)

    def _send_alert(self, message: str) -> None:
        try:
            from monitor.alerts import send_alert
            send_alert(self._alert_email, message, severity='INFO')
        except Exception:
            pass
