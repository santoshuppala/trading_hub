#!/usr/bin/env python3
"""
Reconcile manual position closes that bypassed the event bus.

Detects PositionOpened events in event_store that have no matching
PositionClosed event, checks if the position is still open at the broker,
and writes synthetic PositionClosed + completed_trades records for any
that were closed outside the monitor (manual API calls, liquidations, etc.).

Usage:
    python scripts/reconcile_manual_closes.py          # dry run
    python scripts/reconcile_manual_closes.py --apply  # write to DB
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env
_env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.split('#')[0].strip()
            if '=' in line:
                k, v = line.split('=', 1)
                v = v.strip().strip('"').strip("'")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v

import psycopg2


def get_orphaned_opens(cur, target_date: str):
    """Find PositionOpened events with no matching PositionClosed."""
    cur.execute("""
        WITH opens AS (
            SELECT event_id, event_time, aggregate_id,
                   event_payload->>'ticker' AS ticker,
                   event_payload->>'entry_price' AS entry_price,
                   event_payload->>'qty' AS qty,
                   event_payload->>'entry_time' AS entry_time
            FROM event_store
            WHERE event_type = 'PositionOpened'
              AND event_time::date = %s
        ),
        closes AS (
            SELECT DISTINCT event_payload->>'ticker' AS ticker
            FROM event_store
            WHERE event_type = 'PositionClosed'
              AND event_time::date = %s
        )
        SELECT o.event_id, o.event_time, o.ticker, o.entry_price, o.qty, o.entry_time
        FROM opens o
        LEFT JOIN closes c ON o.ticker = c.ticker
        WHERE c.ticker IS NULL
        ORDER BY o.event_time
    """, (target_date, target_date))
    return cur.fetchall()


def get_broker_positions():
    """Get currently open positions from both Alpaca and Tradier."""
    open_tickers = set()

    # Alpaca equity
    try:
        from alpaca.trading.client import TradingClient
        c = TradingClient(
            os.environ.get('APCA_API_KEY_ID', ''),
            os.environ.get('APCA_API_SECRET_KEY', ''),
            paper=True,
        )
        for p in c.get_all_positions():
            open_tickers.add(str(p.symbol))
    except Exception as e:
        print(f"  Warning: Alpaca check failed: {e}")

    # Tradier
    try:
        import requests
        token = os.environ.get('TRADIER_SANDBOX_TOKEN', '')
        account = os.environ.get('TRADIER_ACCOUNT_ID', '')
        if token and account:
            base = 'https://sandbox.tradier.com'
            headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
            r = requests.get(f'{base}/v1/accounts/{account}/positions', headers=headers)
            positions = r.json().get('positions', {})
            if positions and positions != 'null':
                pos_list = positions.get('position', [])
                if isinstance(pos_list, dict):
                    pos_list = [pos_list]
                for p in pos_list:
                    if int(float(p.get('quantity', 0))) != 0:
                        open_tickers.add(p['symbol'])
    except Exception as e:
        print(f"  Warning: Tradier check failed: {e}")

    return open_tickers


def write_synthetic_close(cur, conn, orphan, apply: bool):
    """Write synthetic PositionClosed event and completed_trade record."""
    event_id, event_time, ticker, entry_price, qty, entry_time = orphan
    entry_price = float(entry_price or 0)
    qty_val = int(float(qty or 0)) if qty else 0
    now = datetime.now(timezone.utc)

    # We don't know the exact exit price — mark as manual close with $0 pnl
    close_event_id = str(uuid.uuid4())

    print(f"  {'WRITING' if apply else 'WOULD WRITE'}: PositionClosed {ticker} "
          f"(opened {str(event_time)[11:19]}, qty={qty_val}, entry=${entry_price:.2f})")

    if not apply:
        return

    # Write synthetic PositionClosed event
    payload = {
        "ticker": ticker,
        "action": "CLOSED",
        "qty": qty_val,
        "entry_price": entry_price,
        "exit_price": entry_price,  # unknown — use entry as placeholder
        "pnl": 0.0,
        "reason": "manual_reconciliation",
        "strategy": "manual_close",
        "broker": "manual",
    }

    cur.execute("""
        INSERT INTO event_store (
            event_id, event_type, event_version,
            event_time, received_time, processed_time, persisted_time,
            aggregate_id, aggregate_type, aggregate_version,
            event_payload, source_system, source_version, session_id
        ) VALUES (
            %s, 'PositionClosed', 1,
            %s, %s, %s, %s,
            %s, 'Position', 1,
            %s, 'ManualReconciliation', '1.0', 'reconciliation'
        )
    """, (
        close_event_id, now, now, now, now,
        f"position_{ticker}",
        json.dumps(payload),
    ))

    # Write completed_trade record
    trade_id = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO trading.completed_trades (
            trade_id, ticker, entry_time, exit_time,
            entry_price, exit_price, qty, pnl, pnl_pct,
            duration_seconds, strategy, opened_event_id, closed_event_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        trade_id, ticker,
        str(entry_time or now.isoformat()),
        now.isoformat(),
        entry_price, entry_price,  # exit = entry (unknown)
        qty_val, 0.0, 0.0, 0,
        'manual_close',
        str(event_id), close_event_id,
    ))

    conn.commit()


def main():
    apply = '--apply' in sys.argv
    target_date = datetime.now().strftime('%Y-%m-%d')

    if '--date' in sys.argv:
        idx = sys.argv.index('--date')
        if idx + 1 < len(sys.argv):
            target_date = sys.argv[idx + 1]

    print(f"Reconciling manual closes for {target_date}")
    print(f"Mode: {'APPLY' if apply else 'DRY RUN'}")
    print()

    conn = psycopg2.connect('postgresql://trading:trading@localhost:5432/tradinghub')
    cur = conn.cursor()

    # Step 1: Find orphaned opens
    orphans = get_orphaned_opens(cur, target_date)
    print(f"Orphaned PositionOpened events (no matching close): {len(orphans)}")

    if not orphans:
        print("Nothing to reconcile.")
        conn.close()
        return

    # Step 2: Check which are actually still open at broker
    print("Checking broker positions...")
    still_open = get_broker_positions()
    print(f"  Currently open at brokers: {still_open or 'none'}")
    print()

    # Step 3: Reconcile — write synthetic closes for positions no longer open
    reconciled = 0
    for orphan in orphans:
        ticker = orphan[2]
        if ticker in still_open:
            print(f"  SKIP {ticker}: still open at broker")
            continue
        write_synthetic_close(cur, conn, orphan, apply)
        reconciled += 1

    print(f"\nReconciled: {reconciled} positions")
    if not apply and reconciled > 0:
        print("Run with --apply to write to DB")

    conn.close()


if __name__ == '__main__':
    main()
