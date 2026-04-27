#!/usr/bin/env python3
"""
Options PF Analysis — reads OPTIONS_CLOSE events from DB or fallback files
and computes profit factor, win rate, and per-exit-reason breakdown.

Usage:
    python scripts/analyze_options_pf.py                    # today
    python scripts/analyze_options_pf.py --days 7           # last 7 days
    python scripts/analyze_options_pf.py --file data/event_fallback_20260426.jsonl  # from fallback
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_from_db(days: int = 1) -> list:
    """Load OPTIONS_CLOSE events from PostgreSQL event_store."""
    try:
        import psycopg2
        conn = psycopg2.connect(os.getenv('DATABASE_URL', ''))
        cur = conn.cursor()
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur.execute("""
            SELECT event_payload FROM event_store
            WHERE event_type = 'OptionsPositionClosed'
            AND event_time >= %s
            ORDER BY event_time
        """, (since,))
        rows = cur.fetchall()
        conn.close()
        return [json.loads(r[0]) if isinstance(r[0], str) else r[0] for r in rows]
    except Exception as e:
        print(f"DB load failed: {e}")
        return []


def load_from_file(path: str) -> list:
    """Load from fallback JSONL file."""
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if record.get('event_type') == 'OptionsPositionClosed':
                    events.append(record.get('payload', record))
            except json.JSONDecodeError:
                continue
    return events


def analyze(trades: list) -> None:
    """Compute PF metrics from close events."""
    if not trades:
        print("No trades found.")
        return

    gross_profit = 0.0
    gross_loss = 0.0
    wins = 0
    losses = 0
    by_strategy = defaultdict(lambda: {'pnl': 0, 'count': 0, 'wins': 0})
    by_exit_reason = defaultdict(lambda: {'pnl': 0, 'count': 0})
    by_phase = defaultdict(lambda: {'pnl': 0, 'count': 0})
    holding_times = []
    pnls = []

    for t in trades:
        pnl = t.get('realized_pnl', 0)
        pnls.append(pnl)
        strategy = t.get('strategy_type', 'unknown')
        reason = t.get('exit_reason', 'unknown')
        lc = t.get('lifecycle', {})
        phase = lc.get('exit_phase', '?')
        minutes = t.get('holding_minutes', 0)
        holding_times.append(minutes)

        if pnl >= 0:
            gross_profit += pnl
            wins += 1
            by_strategy[strategy]['wins'] += 1
        else:
            gross_loss += abs(pnl)
            losses += 1

        by_strategy[strategy]['pnl'] += pnl
        by_strategy[strategy]['count'] += 1
        by_exit_reason[reason]['pnl'] += pnl
        by_exit_reason[reason]['count'] += 1
        by_phase[phase]['pnl'] += pnl
        by_phase[phase]['count'] += 1

    total = wins + losses
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    win_rate = wins / total if total > 0 else 0
    avg_win = gross_profit / wins if wins > 0 else 0
    avg_loss = gross_loss / losses if losses > 0 else 0
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    print("=" * 60)
    print("OPTIONS PF ANALYSIS")
    print("=" * 60)
    print(f"Trades:       {total} ({wins}W / {losses}L)")
    print(f"Win Rate:     {win_rate:.1%}")
    print(f"Gross Profit: ${gross_profit:,.2f}")
    print(f"Gross Loss:   ${gross_loss:,.2f}")
    print(f"Net P&L:      ${gross_profit - gross_loss:+,.2f}")
    print(f"PROFIT FACTOR: {pf:.2f}")
    print(f"Avg Win:      ${avg_win:,.2f}")
    print(f"Avg Loss:     ${avg_loss:,.2f}")
    print(f"Expectancy:   ${expectancy:+,.2f}/trade")
    print(f"Avg Hold:     {sum(holding_times)/len(holding_times):.0f} min" if holding_times else "")
    print()

    print("── By Strategy ──")
    for s, d in sorted(by_strategy.items(), key=lambda x: x[1]['pnl'], reverse=True):
        wr = d['wins'] / d['count'] if d['count'] > 0 else 0
        print(f"  {s:25s}  {d['count']:3d} trades  ${d['pnl']:+8.2f}  WR={wr:.0%}")
    print()

    print("── By Exit Reason ──")
    for r, d in sorted(by_exit_reason.items(), key=lambda x: x[1]['count'], reverse=True):
        avg = d['pnl'] / d['count'] if d['count'] > 0 else 0
        print(f"  {r:40s}  {d['count']:3d}  ${d['pnl']:+8.2f}  avg=${avg:+.2f}")
    print()

    print("── By Exit Phase ──")
    for p, d in sorted(by_phase.items()):
        print(f"  Phase {p}:  {d['count']:3d} trades  ${d['pnl']:+8.2f}")
    print()

    # Risk score analysis (from snapshots)
    risk_scores_at_exit = []
    for t in trades:
        lc = t.get('lifecycle', {})
        snaps = lc.get('snapshots', [])
        if snaps:
            last_snap = snaps[-1]
            risk_scores_at_exit.append({
                'score': last_snap.get('risk_score', 0),
                'pnl': t.get('realized_pnl', 0),
                'reason': t.get('exit_reason', ''),
            })

    if risk_scores_at_exit:
        print("── Risk Score at Exit ──")
        # Bucket by score range
        buckets = {'0.0-1.0': [], '1.0-2.0': [], '2.0+': []}
        for r in risk_scores_at_exit:
            s = r['score']
            if s < 1.0:
                buckets['0.0-1.0'].append(r['pnl'])
            elif s < 2.0:
                buckets['1.0-2.0'].append(r['pnl'])
            else:
                buckets['2.0+'].append(r['pnl'])
        for bucket, pnls_b in buckets.items():
            if pnls_b:
                avg = sum(pnls_b) / len(pnls_b)
                print(f"  Score {bucket:8s}  {len(pnls_b):3d} trades  avg_pnl=${avg:+.2f}")
        print()

    # IV analysis
    iv_changes = []
    for t in trades:
        lc = t.get('lifecycle', {})
        entry_iv = lc.get('entry_iv', 0)
        snaps = lc.get('snapshots', [])
        if snaps and entry_iv > 0:
            exit_iv = snaps[-1].get('iv', 0)
            if exit_iv > 0:
                iv_changes.append({
                    'change_pct': (exit_iv - entry_iv) / entry_iv,
                    'pnl': t.get('realized_pnl', 0),
                    'strategy': t.get('strategy_type', ''),
                })

    if iv_changes:
        print("── IV Change Impact ──")
        iv_up = [x for x in iv_changes if x['change_pct'] > 0.05]
        iv_down = [x for x in iv_changes if x['change_pct'] < -0.05]
        iv_flat = [x for x in iv_changes if -0.05 <= x['change_pct'] <= 0.05]
        for label, group in [('IV up >5%', iv_up), ('IV flat', iv_flat), ('IV down >5%', iv_down)]:
            if group:
                avg_pnl = sum(x['pnl'] for x in group) / len(group)
                print(f"  {label:15s}  {len(group):3d} trades  avg_pnl=${avg_pnl:+.2f}")
    print()
    print("=" * 60)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Options PF Analysis')
    parser.add_argument('--days', type=int, default=1, help='Days of history (default: 1)')
    parser.add_argument('--file', type=str, help='Load from fallback JSONL file instead of DB')
    args = parser.parse_args()

    if args.file:
        trades = load_from_file(args.file)
    else:
        trades = load_from_db(args.days)

    analyze(trades)
