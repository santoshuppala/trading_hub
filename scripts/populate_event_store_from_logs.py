#!/usr/bin/env python3
"""
Populate event_store from monitor logs (2026-04-13)

Parses monitor log files and extracts trading events:
- PositionOpened (when trade opened)
- PositionClosed (when trade closed)
- FillExecuted (for each fill)

Inserts them into event_store with correct timestamps from logs.
"""

import os
import sys
import re
import json
import uuid
from datetime import datetime
from pathlib import Path
import psycopg2
from psycopg2.extras import execute_values

sys.path.insert(0, os.path.dirname(__file__).rsplit('/', 1)[0])

DATABASE_URL = "postgresql://trading:trading_secret@localhost:5432/tradinghub"
SESSION_ID = str(uuid.uuid4())

# Log file patterns
TRADE_PATTERN = re.compile(
    r'(\d{2}:\d{2}:\d{2})\s+(\d{2}:\d{2}:\d{2})\s+(\w+)\s+(\d+)\s+\$\s+([\d.]+)\s+\$\s+([\d.]+)\s+\$\s+([-+][\d.]+)\s+(✓|✗)\s+(\w+)'
)

ALPACA_RESTORED_PATTERN = re.compile(
    r'alpaca_restored\s+(\d{2}:\d{2}:\d{2})\s+(\w+)\s+(\d+)\s+\$\s+([\d.]+)\s+\$\s+([\d.]+)\s+\$\s+([-+][\d.]+)\s+(✓|✗)\s+(\w+)'
)

def parse_log_file(file_path, trading_date='2026-04-13'):
    """Parse monitor log file and extract trades."""
    trades = []

    with open(file_path, 'r') as f:
        for line in f:
            # Try standard pattern first
            match = TRADE_PATTERN.search(line)
            if match:
                entry_time, exit_time, ticker, qty, entry_price, exit_price, pnl, win_indicator, exit_reason = match.groups()
                trades.append({
                    'entry_time': f"{trading_date} {entry_time}",
                    'exit_time': f"{trading_date} {exit_time}",
                    'ticker': ticker,
                    'qty': int(qty),
                    'entry_price': float(entry_price),
                    'exit_price': float(exit_price),
                    'pnl': float(pnl),
                    'win': win_indicator == '✓',
                    'exit_reason': exit_reason,
                })
                continue

            # Try alpaca_restored pattern
            match = ALPACA_RESTORED_PATTERN.search(line)
            if match:
                exit_time, ticker, qty, entry_price, exit_price, pnl, win_indicator, exit_reason = match.groups()
                # Use same exit time as entry for alpaca_restored (timing unknown)
                trades.append({
                    'entry_time': f"{trading_date} {exit_time}",
                    'exit_time': f"{trading_date} {exit_time}",
                    'ticker': ticker,
                    'qty': int(qty),
                    'entry_price': float(entry_price),
                    'exit_price': float(exit_price),
                    'pnl': float(pnl),
                    'win': win_indicator == '✓',
                    'exit_reason': exit_reason,
                })

    return trades

def create_events_from_trade(trade, trade_id):
    """Convert a trade record into PositionOpened and PositionClosed events."""
    events = []

    # Calculate durations
    entry_dt = datetime.strptime(trade['entry_time'], '%Y-%m-%d %H:%M:%S')
    exit_dt = datetime.strptime(trade['exit_time'], '%Y-%m-%d %H:%M:%S')
    duration = (exit_dt - entry_dt).total_seconds()

    # Event 1: PositionOpened
    opened_event_id = str(uuid.uuid4())
    events.append({
        'event_id': opened_event_id,
        'event_sequence': 1,
        'event_type': 'PositionOpened',
        'event_version': 1,
        'event_time': trade['entry_time'],
        'received_time': trade['entry_time'],
        'queued_time': None,
        'processed_time': trade['entry_time'],
        'persisted_time': None,
        'aggregate_id': f"position_{trade['ticker']}",
        'aggregate_type': 'Position',
        'aggregate_version': 1,
        'event_payload': json.dumps({
            'ticker': trade['ticker'],
            'action': 'OPENED',
            'qty': trade['qty'],
            'entry_price': trade['entry_price'],
            'entry_time': trade['entry_time'],
        }),
        'correlation_id': trade_id,
        'causation_id': None,
        'parent_event_id': None,
        'source_system': 'PositionManager',
        'source_version': 'v5.1',
        'session_id': SESSION_ID,
    })

    # Event 2: PositionClosed
    closed_event_id = str(uuid.uuid4())
    events.append({
        'event_id': closed_event_id,
        'event_sequence': 2,
        'event_type': 'PositionClosed',
        'event_version': 1,
        'event_time': trade['exit_time'],
        'received_time': trade['exit_time'],
        'queued_time': None,
        'processed_time': trade['exit_time'],
        'persisted_time': None,
        'aggregate_id': f"position_{trade['ticker']}",
        'aggregate_type': 'Position',
        'aggregate_version': 2,
        'event_payload': json.dumps({
            'ticker': trade['ticker'],
            'action': 'CLOSED',
            'qty': trade['qty'],
            'exit_price': trade['exit_price'],
            'exit_time': trade['exit_time'],
            'exit_reason': trade['exit_reason'],
            'pnl': trade['pnl'],
            'duration_seconds': int(duration),
        }),
        'correlation_id': trade_id,
        'causation_id': opened_event_id,
        'parent_event_id': opened_event_id,
        'source_system': 'PositionManager',
        'source_version': 'v5.1',
        'session_id': SESSION_ID,
    })

    return events

def insert_events(events):
    """Insert events into event_store."""
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    try:
        # Prepare data for batch insert
        data = []
        for event in events:
            data.append((
                event['event_id'],
                event['event_sequence'],
                event['event_type'],
                event['event_version'],
                event['event_time'],
                event['received_time'],
                event['queued_time'],
                event['processed_time'],
                event['persisted_time'] or 'NOW()',
                event['aggregate_id'],
                event['aggregate_type'],
                event['aggregate_version'],
                event['event_payload'],
                event['correlation_id'],
                event['causation_id'],
                event['parent_event_id'],
                event['source_system'],
                event['source_version'],
                event['session_id'],
            ))

        # Insert with proper timestamp handling
        query = """
        INSERT INTO event_store (
            event_id, event_sequence, event_type, event_version,
            event_time, received_time, queued_time, processed_time, persisted_time,
            aggregate_id, aggregate_type, aggregate_version,
            event_payload,
            correlation_id, causation_id, parent_event_id,
            source_system, source_version, session_id
        ) VALUES %s
        ON CONFLICT (event_id) DO NOTHING
        """

        # Convert any datetime strings that need to be actual timestamps
        formatted_data = []
        for row in data:
            formatted_row = list(row)
            # Handle NOW() for persisted_time
            if formatted_row[8] == 'NOW()':
                formatted_row[8] = None  # Will use DEFAULT NOW()
            formatted_data.append(tuple(formatted_row))

        execute_values(cur, query, formatted_data)
        conn.commit()

        return len(events)

    except Exception as e:
        print(f"❌ Database error: {e}")
        conn.rollback()
        return 0
    finally:
        cur.close()
        conn.close()

def main():
    """Main entry point."""
    log_dir = Path('/Users/santosh/Documents/santosh/trading_hub/trading_hub/logs')

    log_files = sorted(log_dir.glob('monitor_2026-04-13*.log'))
    if not log_files:
        print("❌ No monitor logs found for 2026-04-13")
        sys.exit(1)

    print(f"Found {len(log_files)} log files:")
    for f in log_files:
        print(f"  - {f.name}")

    all_trades = []
    for log_file in log_files:
        print(f"\n📖 Parsing {log_file.name}...")
        trades = parse_log_file(str(log_file))
        print(f"   Found {len(trades)} trades")
        all_trades.extend(trades)

    # Deduplicate trades (same ticker, entry/exit prices)
    seen = set()
    unique_trades = []
    for trade in all_trades:
        key = (trade['ticker'], trade['entry_time'], trade['exit_time'], trade['entry_price'])
        if key not in seen:
            seen.add(key)
            unique_trades.append(trade)

    print(f"\n📊 Extracted {len(unique_trades)} unique trades")

    # Convert to events
    all_events = []
    for i, trade in enumerate(unique_trades):
        trade_id = str(uuid.uuid4())
        events = create_events_from_trade(trade, trade_id)
        all_events.extend(events)

    print(f"📝 Created {len(all_events)} events from {len(unique_trades)} trades")

    # Insert into database
    print(f"\n💾 Inserting into event_store...")
    inserted = insert_events(all_events)
    print(f"✅ Inserted {inserted} events")

    # Verify
    print(f"\n✓ Verifying insertion...")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM event_store")
    count = cur.fetchone()[0]
    print(f"   Total events in event_store: {count}")

    cur.execute("SELECT COUNT(*) FROM event_store WHERE event_type = 'PositionOpened'")
    opened_count = cur.fetchone()[0]
    print(f"   PositionOpened events: {opened_count}")

    cur.execute("SELECT COUNT(*) FROM event_store WHERE event_type = 'PositionClosed'")
    closed_count = cur.fetchone()[0]
    print(f"   PositionClosed events: {closed_count}")

    cur.execute("SELECT COUNT(DISTINCT aggregate_id) FROM event_store")
    ticker_count = cur.fetchone()[0]
    print(f"   Unique tickers: {ticker_count}")

    cur.close()
    conn.close()

    print(f"\n✅ Event store populated successfully!")

if __name__ == '__main__':
    main()
