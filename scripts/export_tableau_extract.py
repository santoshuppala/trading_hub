#!/usr/bin/env python3
"""
Export trading data to Tableau Hyper extract (.hyper file).

Creates a single .hyper file with multiple tables that Tableau Desktop
can open directly: File → Open → select the .hyper file.

Tables created:
  1. trade_details    — P&L, ticker, exit reason, session phase, outcome
  2. equity_curve     — cumulative P&L over time
  3. ml_trade_outcomes — MFE/MAE, hold time, entry indicators
  4. daily_performance — daily aggregates
  5. strategy_dim     — strategy reference data
"""
import os
import sys
import psycopg2
from datetime import datetime, date

from tableauhyperapi import (
    HyperProcess, Telemetry, Connection, TableDefinition,
    SqlType, Inserter, CreateMode, TableName, Nullability,
)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://trading:trading_secret@localhost:5432/tradinghub",
)

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), '..', 'dashboards')
OUTPUT_FILE = os.path.join(OUTPUT_DIR, 'TradingHub.hyper')


def fetch_rows(conn, query):
    """Fetch all rows from PostgreSQL query."""
    cur = conn.cursor()
    cur.execute(query)
    rows = cur.fetchall()
    cur.close()
    return rows


def create_extract():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Connect to PostgreSQL
    pg = psycopg2.connect(DATABASE_URL)
    print(f"Connected to PostgreSQL")

    # ── Define Hyper tables ──────────────────────────────────────────

    trade_details_table = TableDefinition(
        table_name=TableName("public", "trade_details"),
        columns=[
            TableDefinition.Column("trade_id", SqlType.text(), Nullability.NULLABLE),
            TableDefinition.Column("ticker", SqlType.text(), Nullability.NOT_NULLABLE),
            TableDefinition.Column("entry_ts", SqlType.timestamp(), Nullability.NULLABLE),
            TableDefinition.Column("exit_ts", SqlType.timestamp(), Nullability.NULLABLE),
            TableDefinition.Column("duration_sec", SqlType.int(), Nullability.NULLABLE),
            TableDefinition.Column("session_phase", SqlType.text(), Nullability.NULLABLE),
            TableDefinition.Column("entry_price", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("exit_price", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("qty", SqlType.int(), Nullability.NULLABLE),
            TableDefinition.Column("pnl", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("exit_reason", SqlType.text(), Nullability.NULLABLE),
            TableDefinition.Column("outcome", SqlType.text(), Nullability.NULLABLE),
            TableDefinition.Column("pnl_pct", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("entry_hour", SqlType.int(), Nullability.NULLABLE),
            TableDefinition.Column("trading_date", SqlType.date(), Nullability.NULLABLE),
        ],
    )

    equity_curve_table = TableDefinition(
        table_name=TableName("public", "equity_curve"),
        columns=[
            TableDefinition.Column("ts", SqlType.timestamp(), Nullability.NULLABLE),
            TableDefinition.Column("trading_date", SqlType.date(), Nullability.NULLABLE),
            TableDefinition.Column("cumulative_pnl", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("trade_pnl", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("ticker", SqlType.text(), Nullability.NULLABLE),
            TableDefinition.Column("trade_number", SqlType.int(), Nullability.NULLABLE),
        ],
    )

    ml_outcomes_table = TableDefinition(
        table_name=TableName("public", "ml_trade_outcomes"),
        columns=[
            TableDefinition.Column("trade_id", SqlType.text(), Nullability.NULLABLE),
            TableDefinition.Column("ticker", SqlType.text(), Nullability.NOT_NULLABLE),
            TableDefinition.Column("layer", SqlType.text(), Nullability.NULLABLE),
            TableDefinition.Column("strategy_name", SqlType.text(), Nullability.NULLABLE),
            TableDefinition.Column("entry_ts", SqlType.timestamp(), Nullability.NULLABLE),
            TableDefinition.Column("trading_date", SqlType.date(), Nullability.NULLABLE),
            TableDefinition.Column("exit_ts", SqlType.timestamp(), Nullability.NULLABLE),
            TableDefinition.Column("exit_reason", SqlType.text(), Nullability.NULLABLE),
            TableDefinition.Column("entry_price", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("exit_price", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("qty", SqlType.int(), Nullability.NULLABLE),
            TableDefinition.Column("realized_pnl", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("realized_pnl_pct", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("mfe", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("mae", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("mfe_pct", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("mae_pct", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("hold_minutes", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("concurrent_positions", SqlType.int(), Nullability.NULLABLE),
            TableDefinition.Column("entry_rsi", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("entry_atr", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("entry_rvol", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("entry_confidence", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("outcome", SqlType.text(), Nullability.NULLABLE),
        ],
    )

    daily_perf_table = TableDefinition(
        table_name=TableName("public", "daily_performance"),
        columns=[
            TableDefinition.Column("trading_date", SqlType.date(), Nullability.NOT_NULLABLE),
            TableDefinition.Column("total_trades", SqlType.int(), Nullability.NULLABLE),
            TableDefinition.Column("wins", SqlType.int(), Nullability.NULLABLE),
            TableDefinition.Column("losses", SqlType.int(), Nullability.NULLABLE),
            TableDefinition.Column("win_rate", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("total_pnl", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("avg_win", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("avg_loss", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("largest_win", SqlType.double(), Nullability.NULLABLE),
            TableDefinition.Column("largest_loss", SqlType.double(), Nullability.NULLABLE),
        ],
    )

    strategy_table = TableDefinition(
        table_name=TableName("public", "dim_strategy"),
        columns=[
            TableDefinition.Column("strategy_name", SqlType.text(), Nullability.NOT_NULLABLE),
            TableDefinition.Column("layer", SqlType.text(), Nullability.NULLABLE),
            TableDefinition.Column("description", SqlType.text(), Nullability.NULLABLE),
            TableDefinition.Column("tier", SqlType.int(), Nullability.NULLABLE),
            TableDefinition.Column("is_credit", SqlType.bool(), Nullability.NULLABLE),
            TableDefinition.Column("is_active", SqlType.bool(), Nullability.NULLABLE),
        ],
    )

    # ── Helper to convert Python types for Hyper ─────────────────────

    def to_hyper_val(val, sql_type):
        if val is None:
            return None
        if isinstance(sql_type, type) and sql_type == SqlType.timestamp:
            if isinstance(val, datetime):
                return val
            return None
        if isinstance(val, (date, datetime)):
            return val
        if isinstance(val, float):
            return val
        if isinstance(val, int):
            return val
        return str(val)

    def safe_float(v):
        if v is None:
            return None
        try:
            f = float(v)
            return f if f == f else None  # NaN check
        except (ValueError, TypeError):
            return None

    def safe_int(v):
        if v is None:
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    def safe_date(v):
        if v is None:
            return None
        if isinstance(v, date):
            return v
        if isinstance(v, datetime):
            return v.date()
        try:
            return date.fromisoformat(str(v)[:10])
        except (ValueError, TypeError):
            return None

    def safe_ts(v):
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.replace(tzinfo=None)  # Hyper doesn't support tz
        if isinstance(v, date):
            return datetime(v.year, v.month, v.day)
        return None

    # ── Create Hyper file ────────────────────────────────────────────

    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        print(f"Creating {OUTPUT_FILE}")

        with Connection(
            endpoint=hyper.endpoint,
            database=OUTPUT_FILE,
            create_mode=CreateMode.CREATE_AND_REPLACE,
        ) as conn:
            conn.catalog.create_schema_if_not_exists("public")

            # ── Table 1: trade_details ────────────────────────────────
            conn.catalog.create_table(trade_details_table)
            rows = fetch_rows(pg, """
                SELECT
                    t.trade_id::TEXT,
                    (t.pnl)::TEXT as ticker_placeholder,
                    t.entry_ts,
                    t.exit_ts,
                    t.duration_sec::INT,
                    t.session_phase,
                    t.entry_price::FLOAT,
                    t.exit_price::FLOAT,
                    t.qty::INT,
                    t.pnl::FLOAT,
                    t.exit_reason,
                    CASE WHEN t.pnl > 0 THEN 'Win' ELSE 'Loss' END,
                    ROUND(t.pnl / NULLIF(t.entry_price * t.qty, 0) * 100, 2)::FLOAT,
                    EXTRACT(HOUR FROM t.entry_ts)::INT,
                    t.entry_ts::DATE
                FROM v_trade_details t
            """)
            # Fix: re-query with correct columns
            rows = fetch_rows(pg, """
                SELECT
                    event_id::TEXT,
                    event_payload->>'ticker',
                    o_time,
                    c_time,
                    EXTRACT(EPOCH FROM (c_time - o_time))::INT,
                    CASE
                        WHEN EXTRACT(HOUR FROM o_time) * 60 + EXTRACT(MINUTE FROM o_time) < 585 THEN 'first_15min'
                        WHEN EXTRACT(HOUR FROM o_time) * 60 + EXTRACT(MINUTE FROM o_time) < 690 THEN 'morning'
                        WHEN EXTRACT(HOUR FROM o_time) * 60 + EXTRACT(MINUTE FROM o_time) < 810 THEN 'midday'
                        WHEN EXTRACT(HOUR FROM o_time) * 60 + EXTRACT(MINUTE FROM o_time) < 930 THEN 'afternoon'
                        ELSE 'power_hour'
                    END,
                    (event_payload->>'entry_price')::FLOAT,
                    (event_payload->>'current_price')::FLOAT,
                    (event_payload->>'qty')::INT,
                    COALESCE((event_payload->>'pnl')::FLOAT, 0),
                    COALESCE(event_payload->>'exit_reason', event_payload->>'action', 'unknown'),
                    CASE WHEN COALESCE((event_payload->>'pnl')::FLOAT, 0) > 0 THEN 'Win' ELSE 'Loss' END,
                    0.0,
                    EXTRACT(HOUR FROM o_time)::INT,
                    o_time::DATE
                FROM (
                    SELECT
                        c.event_id,
                        c.event_payload,
                        o.event_time as o_time,
                        c.event_time as c_time
                    FROM event_store c
                    JOIN event_store o ON c.aggregate_id = o.aggregate_id
                        AND o.event_type = 'PositionOpened'
                    WHERE c.event_type = 'PositionClosed'
                ) sub
            """)
            print(f"  trade_details: {len(rows)} rows")
            with Inserter(conn, trade_details_table) as ins:
                for r in rows:
                    ins.add_row([
                        str(r[0]) if r[0] else None,  # trade_id
                        str(r[1]) if r[1] else 'UNK',  # ticker
                        safe_ts(r[2]),   # entry_ts
                        safe_ts(r[3]),   # exit_ts
                        safe_int(r[4]),  # duration_sec
                        str(r[5]) if r[5] else None,  # session_phase
                        safe_float(r[6]),  # entry_price
                        safe_float(r[7]),  # exit_price
                        safe_int(r[8]),  # qty
                        safe_float(r[9]),  # pnl
                        str(r[10]) if r[10] else None,  # exit_reason
                        str(r[11]) if r[11] else 'Loss',  # outcome
                        safe_float(r[12]),  # pnl_pct
                        safe_int(r[13]),  # entry_hour
                        safe_date(r[14]),  # trading_date
                    ])
                ins.execute()

            # ── Table 2: equity_curve ─────────────────────────────────
            conn.catalog.create_table(equity_curve_table)
            rows = fetch_rows(pg, """
                SELECT
                    e.event_time,
                    e.event_time::DATE,
                    SUM(COALESCE((e.event_payload->>'pnl')::FLOAT, 0)) OVER (ORDER BY e.event_time, e.event_id),
                    COALESCE((e.event_payload->>'pnl')::FLOAT, 0),
                    e.event_payload->>'ticker',
                    ROW_NUMBER() OVER (ORDER BY e.event_time, e.event_id)
                FROM event_store e
                WHERE e.event_type IN ('PositionClosed', 'PartialExited')
                ORDER BY e.event_time
            """)
            print(f"  equity_curve: {len(rows)} rows")
            with Inserter(conn, equity_curve_table) as ins:
                for r in rows:
                    ins.add_row([
                        safe_ts(r[0]),
                        safe_date(r[1]),
                        safe_float(r[2]),
                        safe_float(r[3]),
                        str(r[4]) if r[4] else None,
                        safe_int(r[5]),
                    ])
                ins.execute()

            # ── Table 3: ml_trade_outcomes ────────────────────────────
            conn.catalog.create_table(ml_outcomes_table)
            rows = fetch_rows(pg, """
                SELECT
                    trade_id::TEXT, ticker, layer, strategy_name,
                    entry_ts, entry_ts::DATE, exit_ts, exit_reason,
                    entry_price::FLOAT, exit_price::FLOAT, qty,
                    realized_pnl::FLOAT, realized_pnl_pct::FLOAT,
                    max_favorable_excursion::FLOAT, max_adverse_excursion::FLOAT,
                    mfe_pct::FLOAT, mae_pct::FLOAT,
                    ROUND(time_in_position_sec / 60.0, 1)::FLOAT,
                    concurrent_positions,
                    entry_rsi::FLOAT, entry_atr::FLOAT, entry_rvol::FLOAT,
                    entry_confidence::FLOAT,
                    CASE WHEN realized_pnl > 0 THEN 'Win' ELSE 'Loss' END
                FROM ml_trade_outcomes
                WHERE exit_reason IS NOT NULL
            """)
            print(f"  ml_trade_outcomes: {len(rows)} rows")
            with Inserter(conn, ml_outcomes_table) as ins:
                for r in rows:
                    ins.add_row([
                        str(r[0]) if r[0] else None,
                        str(r[1]) if r[1] else 'UNK',
                        str(r[2]) if r[2] else None,
                        str(r[3]) if r[3] else None,
                        safe_ts(r[4]),
                        safe_date(r[5]),
                        safe_ts(r[6]),
                        str(r[7]) if r[7] else None,
                        safe_float(r[8]),
                        safe_float(r[9]),
                        safe_int(r[10]),
                        safe_float(r[11]),
                        safe_float(r[12]),
                        safe_float(r[13]),
                        safe_float(r[14]),
                        safe_float(r[15]),
                        safe_float(r[16]),
                        safe_float(r[17]),
                        safe_int(r[18]),
                        safe_float(r[19]),
                        safe_float(r[20]),
                        safe_float(r[21]),
                        safe_float(r[22]),
                        str(r[23]) if r[23] else 'Loss',
                    ])
                ins.execute()

            # ── Table 4: daily_performance ────────────────────────────
            conn.catalog.create_table(daily_perf_table)
            rows = fetch_rows(pg, """
                SELECT
                    trading_date, total_trades::INT, wins::INT, losses::INT,
                    win_rate::FLOAT, total_pnl::FLOAT,
                    avg_win::FLOAT, avg_loss::FLOAT,
                    largest_win::FLOAT, largest_loss::FLOAT
                FROM v_daily_performance
            """)
            print(f"  daily_performance: {len(rows)} rows")
            with Inserter(conn, daily_perf_table) as ins:
                for r in rows:
                    ins.add_row([
                        safe_date(r[0]),
                        safe_int(r[1]),
                        safe_int(r[2]),
                        safe_int(r[3]),
                        safe_float(r[4]),
                        safe_float(r[5]),
                        safe_float(r[6]),
                        safe_float(r[7]),
                        safe_float(r[8]),
                        safe_float(r[9]),
                    ])
                ins.execute()

            # ── Table 5: dim_strategy ─────────────────────────────────
            conn.catalog.create_table(strategy_table)
            rows = fetch_rows(pg, """
                SELECT strategy_name, layer, description, tier, is_credit, is_active
                FROM dim_strategy
            """)
            print(f"  dim_strategy: {len(rows)} rows")
            with Inserter(conn, strategy_table) as ins:
                for r in rows:
                    ins.add_row([
                        str(r[0]),
                        str(r[1]) if r[1] else None,
                        str(r[2]) if r[2] else None,
                        safe_int(r[3]),
                        bool(r[4]) if r[4] is not None else None,
                        bool(r[5]) if r[5] is not None else None,
                    ])
                ins.execute()

    pg.close()
    print(f"\nExport complete: {OUTPUT_FILE}")
    print(f"Open in Tableau: File → Open → select {os.path.basename(OUTPUT_FILE)}")


if __name__ == '__main__':
    create_extract()
