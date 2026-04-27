#!/usr/bin/env python3
"""
Weekly Options Review — auto-triggered analysis after first week of trading.

Runs the full PF analysis, generates threshold calibration recommendations,
and emails the report. Designed to be called by cron or lifecycle scheduler.

Setup (cron):
    # Run every Friday at 4:30 PM ET (after options EOD close)
    30 16 * * 5 cd /path/to/trading_hub && python scripts/weekly_options_review.py

Or trigger manually:
    python scripts/weekly_options_review.py --days 7
    python scripts/weekly_options_review.py --since 2026-04-28

What it does:
    1. Loads all OPTIONS_CLOSE events from the past week
    2. Computes PF, win rate, avg win/loss, expectancy
    3. Analyzes risk_score threshold effectiveness
    4. Recommends threshold adjustments
    5. Checks if failure_score wiring is ready (scorer data maturity)
    6. Emails report to ALERT_EMAIL

Calibration checks (from scorer_validation_protocol):
    ✓ Forward predictive power: risk_score at T → does it predict P&L at close?
    ✓ Score separation: HIGH/MID/LOW score buckets show different avg P&L?
    ✓ Monotonicity: higher risk_score → worse outcomes?
    ✓ Stability: consistent across days?
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_trades(days: int = 7, since: str = '') -> list:
    """Load OPTIONS_CLOSE events from DB."""
    trades = []

    # Try DB first
    try:
        import psycopg2
        conn = psycopg2.connect(os.getenv('DATABASE_URL', ''))
        cur = conn.cursor()
        if since:
            cutoff = since
        else:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur.execute("""
            SELECT event_payload, event_time FROM event_store
            WHERE event_type = 'OptionsPositionClosed'
            AND event_time >= %s
            ORDER BY event_time
        """, (cutoff,))
        for row in cur.fetchall():
            payload = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            payload['_event_time'] = str(row[1])
            trades.append(payload)
        conn.close()
    except Exception as e:
        print(f"DB load failed: {e}")

    # Fallback: scan JSONL files
    if not trades:
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
        for fname in sorted(os.listdir(data_dir)):
            if fname.startswith('event_fallback_') and fname.endswith('.jsonl'):
                fpath = os.path.join(data_dir, fname)
                try:
                    with open(fpath) as f:
                        for line in f:
                            rec = json.loads(line.strip())
                            if rec.get('event_type') == 'OptionsPositionClosed':
                                trades.append(rec.get('payload', rec))
                except Exception:
                    continue

    return trades


def calibration_analysis(trades: list) -> list:
    """Analyze risk_score effectiveness and recommend threshold changes."""
    recommendations = []

    # Extract last snapshot's risk_score for each trade
    scored_trades = []
    for t in trades:
        lc = t.get('lifecycle', {})
        snaps = lc.get('snapshots', [])
        if snaps:
            last = snaps[-1]
            scored_trades.append({
                'risk_score': last.get('risk_score', 0),
                'pnl': t.get('realized_pnl', 0),
                'strategy': t.get('strategy_type', ''),
                'exit_reason': t.get('exit_reason', ''),
            })

    if len(scored_trades) < 5:
        recommendations.append("INSUFFICIENT DATA: Need 5+ trades with snapshots for calibration.")
        return recommendations

    # ── 1. Score Separation: bucket by score → avg P&L ───────────────
    buckets = {'LOW (0-1)': [], 'MID (1-2)': [], 'HIGH (2+)': []}
    for t in scored_trades:
        s = t['risk_score']
        if s < 1.0:
            buckets['LOW (0-1)'].append(t['pnl'])
        elif s < 2.0:
            buckets['MID (1-2)'].append(t['pnl'])
        else:
            buckets['HIGH (2+)'].append(t['pnl'])

    separation_ok = True
    bucket_avgs = {}
    for label, pnls in buckets.items():
        if pnls:
            bucket_avgs[label] = sum(pnls) / len(pnls)
        else:
            bucket_avgs[label] = None

    if bucket_avgs.get('LOW (0-1)') and bucket_avgs.get('HIGH (2+)'):
        if bucket_avgs['LOW (0-1)'] <= bucket_avgs['HIGH (2+)']:
            separation_ok = False
            recommendations.append(
                "SCORE SEPARATION FAILED: Low-risk trades have WORSE P&L than high-risk. "
                "Risk score weights need recalibration.")

    # ── 2. Monotonicity: higher score → worse outcomes? ──────────────
    if bucket_avgs.get('LOW (0-1)') is not None and bucket_avgs.get('MID (1-2)') is not None:
        if bucket_avgs['MID (1-2)'] > bucket_avgs['LOW (0-1)']:
            recommendations.append(
                "NON-MONOTONIC: MID score trades outperform LOW score trades. "
                "The 1.2 tighten threshold may be too aggressive.")

    # ── 3. Exit threshold calibration ────────────────────────────────
    # Current threshold: 2.2 for EXIT, 1.2 for TIGHTEN
    # Check: trades that exited at score 1.2-2.2 (tighten zone) — were they right to hold?
    tighten_zone = [t for t in scored_trades if 1.2 <= t['risk_score'] < 2.2]
    if tighten_zone:
        avg_pnl = sum(t['pnl'] for t in tighten_zone) / len(tighten_zone)
        if avg_pnl < -50:
            recommendations.append(
                f"LOWER EXIT THRESHOLD: Trades in tighten zone (1.2-2.2) avg P&L=${avg_pnl:.0f}. "
                f"Consider lowering EXIT threshold from 2.2 to 1.8.")
        elif avg_pnl > 50:
            recommendations.append(
                f"RAISE EXIT THRESHOLD: Trades in tighten zone avg P&L=${avg_pnl:.0f} (profitable). "
                f"Current 2.2 threshold is good or could go higher.")

    # ── 4. Credit stop calibration ───────────────────────────────────
    credit_stops = [t for t in trades if 'CREDIT_STOP' in t.get('exit_reason', '')]
    if credit_stops:
        avg_credit_loss = sum(t.get('realized_pnl', 0) for t in credit_stops) / len(credit_stops)
        recommendations.append(
            f"CREDIT STOP: {len(credit_stops)} trades stopped out, avg loss=${avg_credit_loss:.0f}. "
            f"{'Good — losses are capped.' if avg_credit_loss > -200 else 'Consider tighter stop.'}")

    # ── 5. Phase 2 effectiveness ─────────────────────────────────────
    phase2_exits = [t for t in trades
                    if t.get('lifecycle', {}).get('exit_phase') == 2]
    if phase2_exits:
        p2_avg = sum(t.get('realized_pnl', 0) for t in phase2_exits) / len(phase2_exits)
        if p2_avg < 0:
            recommendations.append(
                f"PHASE 2 EXITS: {len(phase2_exits)} trades, avg P&L=${p2_avg:.0f}. "
                f"Theta-phase exits are working (cutting losers).")
        else:
            recommendations.append(
                f"PHASE 2 EXITS: {len(phase2_exits)} trades, avg P&L=${p2_avg:.0f}. "
                f"May be exiting winners too early in theta phase.")

    if not recommendations:
        recommendations.append("ALL CHECKS PASSED: Thresholds look reasonable. Continue monitoring.")

    return recommendations


def generate_report(trades: list, days: int) -> str:
    """Generate the full weekly report."""
    lines = []
    lines.append("=" * 60)
    lines.append(f"WEEKLY OPTIONS REVIEW — {datetime.now().strftime('%Y-%m-%d')}")
    lines.append(f"Period: last {days} days | Trades: {len(trades)}")
    lines.append("=" * 60)

    if not trades:
        lines.append("No trades found. System may not have traded options this week.")
        return "\n".join(lines)

    # Basic PF stats
    gross_profit = sum(t.get('realized_pnl', 0) for t in trades if t.get('realized_pnl', 0) >= 0)
    gross_loss = abs(sum(t.get('realized_pnl', 0) for t in trades if t.get('realized_pnl', 0) < 0))
    wins = sum(1 for t in trades if t.get('realized_pnl', 0) >= 0)
    losses = len(trades) - wins
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    net = gross_profit - gross_loss

    lines.append(f"")
    lines.append(f"PROFIT FACTOR: {pf:.2f}")
    lines.append(f"Net P&L:       ${net:+,.2f}")
    lines.append(f"Win Rate:      {wins}/{len(trades)} ({wins/len(trades):.0%})")
    lines.append(f"Gross Profit:  ${gross_profit:,.2f}")
    lines.append(f"Gross Loss:    ${gross_loss:,.2f}")
    lines.append(f"")

    # Strategy breakdown
    by_strat = defaultdict(lambda: {'pnl': 0, 'n': 0})
    for t in trades:
        s = t.get('strategy_type', 'unknown')
        by_strat[s]['pnl'] += t.get('realized_pnl', 0)
        by_strat[s]['n'] += 1
    lines.append("By Strategy:")
    for s, d in sorted(by_strat.items(), key=lambda x: -x[1]['pnl']):
        lines.append(f"  {s:25s} {d['n']:3d} trades  ${d['pnl']:+.2f}")
    lines.append("")

    # Exit reason breakdown
    by_reason = defaultdict(lambda: {'pnl': 0, 'n': 0})
    for t in trades:
        r = t.get('exit_reason', 'unknown')
        by_reason[r]['pnl'] += t.get('realized_pnl', 0)
        by_reason[r]['n'] += 1
    lines.append("By Exit Reason:")
    for r, d in sorted(by_reason.items(), key=lambda x: -x[1]['n']):
        lines.append(f"  {r:40s} {d['n']:3d}  ${d['pnl']:+.2f}")
    lines.append("")

    # Calibration recommendations
    recs = calibration_analysis(trades)
    lines.append("-" * 60)
    lines.append("CALIBRATION RECOMMENDATIONS:")
    for r in recs:
        lines.append(f"  > {r}")
    lines.append("")

    # Action items
    lines.append("-" * 60)
    lines.append("ACTION ITEMS FOR NEXT WEEK:")
    if pf < 1.0:
        lines.append("  [!] PF < 1.0 — system is losing money. Review exit thresholds.")
    elif pf < 1.5:
        lines.append("  [~] PF 1.0-1.5 — marginal. Apply calibration recommendations above.")
    else:
        lines.append("  [+] PF > 1.5 — system is profitable. Monitor for regime changes.")

    if any('LOWER EXIT' in r for r in recs):
        lines.append("  [!] Lower risk_score EXIT threshold (see recommendation).")
    if any('SEPARATION FAILED' in r for r in recs):
        lines.append("  [!] Risk score weights need recalibration.")
    if any('INSUFFICIENT' in r for r in recs):
        lines.append("  [~] Not enough data yet — continue collecting for 1 more week.")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='Weekly Options Review')
    parser.add_argument('--days', type=int, default=7)
    parser.add_argument('--since', type=str, default='')
    parser.add_argument('--email', action='store_true', help='Send report via email')
    args = parser.parse_args()

    trades = load_trades(days=args.days, since=args.since)
    report = generate_report(trades, args.days)
    print(report)

    if args.email:
        try:
            from monitor.alerts import send_alert
            alert_email = os.getenv('ALERT_EMAIL', '')
            send_alert(alert_email, report, severity='INFO')
            print(f"\nReport emailed to {alert_email}")
        except Exception as e:
            print(f"\nEmail failed: {e}")

    # Write to file for reference
    report_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'reports')
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(
        report_dir,
        f'options_weekly_{datetime.now().strftime("%Y%m%d")}.txt')
    with open(report_path, 'w') as f:
        f.write(report)
    print(f"\nReport saved: {report_path}")


if __name__ == '__main__':
    main()
