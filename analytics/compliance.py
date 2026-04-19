"""
analytics/compliance.py — Compliance and Audit Reporting

Provides four core compliance capabilities:
1. Best Execution Report — slippage analysis for every fill
2. Trade Audit Trail — full event chain reconstruction for any trade
3. Daily Compliance Summary — aggregated daily compliance metrics
4. Regulatory Snapshot — reconstruct portfolio state at any point in time

All methods accept a psycopg2 connection and query event_store directly.
Dependencies: psycopg2, pandas, json (stdlib).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Union

import pandas as pd

log = logging.getLogger(__name__)


# ── Slippage Classification Thresholds (basis points) ─────────────────────
_GOOD_BPS = 2.0
_ACCEPTABLE_BPS = 10.0


def _classify_slippage(slippage_bps: float) -> str:
    """Classify execution quality by slippage in basis points."""
    abs_bps = abs(slippage_bps)
    if abs_bps < _GOOD_BPS:
        return "good"
    elif abs_bps <= _ACCEPTABLE_BPS:
        return "acceptable"
    return "poor"


def _bps(price: float, reference: float) -> float:
    """Calculate slippage in basis points: (price - reference) / reference * 10_000."""
    if reference == 0:
        return 0.0
    return (price - reference) / reference * 10_000


def _safe_json(raw: Any) -> dict:
    """Parse JSON from string or return dict as-is."""
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
        return raw
    return {}


class ComplianceReporter:
    """
    Compliance and audit reporting against the event_store.

    All public methods accept a psycopg2 connection (or compatible cursor factory)
    and return plain dicts / lists suitable for JSON serialization.
    """

    # ── 1. Best Execution Report ──────────────────────────────────────────

    @staticmethod
    def best_execution_report(
        conn,
        trade_date: Optional[Union[str, date]] = None,
        ticker: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Analyse execution quality for every fill on a given date.

        For each FillExecuted event:
        - Was the fill price within the NBBO (bid/ask) at fill time?
        - Slippage vs arrival price (price when the signal was generated)
        - Slippage vs VWAP over the execution window
        - Classification: 'good' (<2 bps), 'acceptable' (2-10 bps), 'poor' (>10 bps)

        Parameters
        ----------
        conn       : psycopg2 connection
        trade_date : date to analyse (default: today)
        ticker     : optional ticker filter

        Returns
        -------
        list[dict] with one entry per fill
        """
        if trade_date is None:
            trade_date = date.today()
        if isinstance(trade_date, str):
            trade_date = date.fromisoformat(trade_date)

        day_start = datetime.combine(trade_date, datetime.min.time())
        day_end = datetime.combine(trade_date + timedelta(days=1), datetime.min.time())

        # ── Fetch all fills for the date ──────────────────────────────
        fill_query = """
            SELECT event_id, event_time, event_payload, correlation_id
            FROM event_store
            WHERE event_type = 'FillExecuted'
              AND event_time >= %s AND event_time < %s
        """
        params: list = [day_start, day_end]
        if ticker:
            fill_query += " AND event_payload->>'ticker' = %s"
            params.append(ticker)
        fill_query += " ORDER BY event_time ASC"

        with conn.cursor() as cur:
            cur.execute(fill_query, params)
            fills = cur.fetchall()
            fill_cols = [desc[0] for desc in cur.description]

        results: List[Dict[str, Any]] = []

        for row in fills:
            fill = dict(zip(fill_cols, row))
            payload = _safe_json(fill["event_payload"])
            fill_price = float(payload.get("fill_price", 0))
            fill_ticker = payload.get("ticker", "")
            fill_time = fill["event_time"]
            correlation_id = fill.get("correlation_id")

            # ── NBBO at fill time: closest quote before or at fill_time ───
            nbbo = _lookup_nbbo_at(conn, fill_ticker, fill_time)
            within_nbbo = None
            if nbbo:
                bid, ask = nbbo["bid"], nbbo["ask"]
                within_nbbo = (bid <= fill_price <= ask) if bid and ask else None

            # ── Arrival price: signal price linked by correlation_id ──────
            arrival_price = _lookup_arrival_price(conn, correlation_id, fill_ticker, fill_time)
            slippage_vs_arrival_bps = _bps(fill_price, arrival_price) if arrival_price else None

            # ── VWAP over the execution window ────────────────────────────
            vwap = _lookup_vwap_window(conn, fill_ticker, fill_time)
            slippage_vs_vwap_bps = _bps(fill_price, vwap) if vwap else None

            # ── Classification (use arrival slippage first, then VWAP) ────
            primary_bps = slippage_vs_arrival_bps if slippage_vs_arrival_bps is not None else slippage_vs_vwap_bps
            classification = _classify_slippage(primary_bps) if primary_bps is not None else "unknown"

            results.append({
                "fill_event_id":            str(fill["event_id"]),
                "fill_time":                fill_time.isoformat() if fill_time else None,
                "ticker":                   fill_ticker,
                "side":                     payload.get("side"),
                "qty":                      payload.get("qty"),
                "fill_price":               fill_price,
                "nbbo_bid":                 nbbo["bid"] if nbbo else None,
                "nbbo_ask":                 nbbo["ask"] if nbbo else None,
                "within_nbbo":              within_nbbo,
                "arrival_price":            arrival_price,
                "slippage_vs_arrival_bps":  round(slippage_vs_arrival_bps, 2) if slippage_vs_arrival_bps is not None else None,
                "vwap":                     vwap,
                "slippage_vs_vwap_bps":     round(slippage_vs_vwap_bps, 2) if slippage_vs_vwap_bps is not None else None,
                "classification":           classification,
            })

        return results

    # ── 2. Trade Audit Trail ──────────────────────────────────────────────

    @staticmethod
    def audit_trade(trade_id: str, conn) -> Dict[str, Any]:
        """
        Reconstruct the complete event chain for a trade.

        Follows: Signal -> Risk check -> Order -> Fill -> Position open -> Exits -> Position close
        with timestamps and latency between each step.

        Parameters
        ----------
        trade_id : str — correlation_id or aggregate_id that links the trade events
        conn     : psycopg2 connection

        Returns
        -------
        dict with 'events' list (ordered), 'latencies' between steps, and 'summary'.
        """
        # Strategy: query all events sharing the same correlation_id,
        # OR matching the ticker's position aggregate within the time window.
        with conn.cursor() as cur:
            # First try correlation_id match
            cur.execute(
                """
                SELECT event_id, event_type, event_time, event_payload,
                       aggregate_type, aggregate_id, correlation_id,
                       received_time, processed_time, source_system
                FROM event_store
                WHERE correlation_id = %s
                ORDER BY event_sequence ASC, event_time ASC
                """,
                (trade_id,),
            )
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]

            # If no results, try aggregate_id match (e.g., position_TICKER)
            if not rows:
                cur.execute(
                    """
                    SELECT event_id, event_type, event_time, event_payload,
                           aggregate_type, aggregate_id, correlation_id,
                           received_time, processed_time, source_system
                    FROM event_store
                    WHERE aggregate_id = %s
                    ORDER BY event_sequence ASC, event_time ASC
                    """,
                    (trade_id,),
                )
                rows = cur.fetchall()
                cols = [desc[0] for desc in cur.description]

            # If still nothing, try as event_id and expand to its correlation_id
            if not rows:
                cur.execute(
                    """
                    SELECT correlation_id FROM event_store
                    WHERE event_id = %s AND correlation_id IS NOT NULL
                    LIMIT 1
                    """,
                    (trade_id,),
                )
                cid_row = cur.fetchone()
                if cid_row and cid_row[0]:
                    cur.execute(
                        """
                        SELECT event_id, event_type, event_time, event_payload,
                               aggregate_type, aggregate_id, correlation_id,
                               received_time, processed_time, source_system
                        FROM event_store
                        WHERE correlation_id = %s
                        ORDER BY event_sequence ASC, event_time ASC
                        """,
                        (cid_row[0],),
                    )
                    rows = cur.fetchall()
                    cols = [desc[0] for desc in cur.description]

        if not rows:
            return {"events": [], "latencies": [], "summary": {"found": False, "trade_id": trade_id}}

        events = []
        for row in rows:
            evt = dict(zip(cols, row))
            events.append({
                "event_id":      str(evt["event_id"]),
                "event_type":    evt["event_type"],
                "event_time":    evt["event_time"].isoformat() if evt["event_time"] else None,
                "source_system": evt["source_system"],
                "aggregate":     f"{evt['aggregate_type']}:{evt['aggregate_id']}",
                "payload":       _safe_json(evt["event_payload"]),
                "received_time": evt["received_time"].isoformat() if evt.get("received_time") else None,
                "processed_time": evt["processed_time"].isoformat() if evt.get("processed_time") else None,
            })

        # ── Compute step-to-step latencies ────────────────────────────
        latencies = []
        for i in range(1, len(events)):
            prev_time = rows[i - 1][2]  # event_time
            curr_time = rows[i][2]
            if prev_time and curr_time:
                delta_ms = (curr_time - prev_time).total_seconds() * 1000
                latencies.append({
                    "from": events[i - 1]["event_type"],
                    "to":   events[i]["event_type"],
                    "latency_ms": round(delta_ms, 2),
                })

        # ── Summary ───────────────────────────────────────────────────
        event_types_seen = [e["event_type"] for e in events]
        total_latency_ms = None
        if rows[0][2] and rows[-1][2]:
            total_latency_ms = round((rows[-1][2] - rows[0][2]).total_seconds() * 1000, 2)

        summary = {
            "found":            True,
            "trade_id":         trade_id,
            "event_count":      len(events),
            "event_chain":      " -> ".join(event_types_seen),
            "total_latency_ms": total_latency_ms,
            "first_event":      events[0]["event_time"] if events else None,
            "last_event":       events[-1]["event_time"] if events else None,
        }

        return {"events": events, "latencies": latencies, "summary": summary}

    # ── 3. Daily Compliance Summary ───────────────────────────────────────

    @staticmethod
    def daily_summary(
        report_date: Union[str, date],
        conn,
    ) -> Dict[str, Any]:
        """
        Generate a daily compliance summary for a given date.

        Includes:
        - Total trades, P&L, max position count
        - Kill switch activations
        - Wash trade rejections
        - Orphaned positions count
        - Best / worst execution fills

        Parameters
        ----------
        report_date : str or date
        conn        : psycopg2 connection

        Returns
        -------
        dict with all compliance metrics for the day
        """
        if isinstance(report_date, str):
            report_date = date.fromisoformat(report_date)

        day_start = datetime.combine(report_date, datetime.min.time())
        day_end = datetime.combine(report_date + timedelta(days=1), datetime.min.time())

        with conn.cursor() as cur:
            # ── Trade counts and P&L ──────────────────────────────────
            cur.execute(
                """
                SELECT
                    COUNT(*) as total_fills,
                    COALESCE(SUM((event_payload->>'fill_price')::numeric * (event_payload->>'qty')::numeric), 0) as total_notional
                FROM event_store
                WHERE event_type = 'FillExecuted'
                  AND event_time >= %s AND event_time < %s
                """,
                (day_start, day_end),
            )
            fill_row = cur.fetchone()
            total_fills = fill_row[0] if fill_row else 0
            total_notional = float(fill_row[1]) if fill_row else 0.0

            # ── Completed trades (PositionClosed) and P&L ─────────────
            cur.execute(
                """
                SELECT
                    COUNT(*) as total_trades,
                    COALESCE(SUM((event_payload->>'pnl')::numeric), 0) as total_pnl
                FROM event_store
                WHERE event_type = 'PositionClosed'
                  AND event_time >= %s AND event_time < %s
                """,
                (day_start, day_end),
            )
            trade_row = cur.fetchone()
            total_trades = trade_row[0] if trade_row else 0
            total_pnl = float(trade_row[1]) if trade_row else 0.0

            # ── Max concurrent positions (peak of open minus closed) ──
            cur.execute(
                """
                SELECT COUNT(DISTINCT aggregate_id)
                FROM event_store
                WHERE aggregate_type = 'Position'
                  AND event_type = 'PositionOpened'
                  AND event_time >= %s AND event_time < %s
                """,
                (day_start, day_end),
            )
            max_positions = cur.fetchone()[0] or 0

            # ── Kill switch activations ───────────────────────────────
            cur.execute(
                """
                SELECT COUNT(*)
                FROM event_store
                WHERE event_type = 'RiskBlocked'
                  AND event_time >= %s AND event_time < %s
                  AND (
                      event_payload->>'reason' ILIKE '%%kill%%switch%%'
                      OR event_payload->>'reason' ILIKE '%%circuit%%breaker%%'
                      OR event_payload->>'reason' ILIKE '%%emergency%%'
                  )
                """,
                (day_start, day_end),
            )
            kill_switch_count = cur.fetchone()[0] or 0

            # ── Wash trade rejections ─────────────────────────────────
            cur.execute(
                """
                SELECT COUNT(*)
                FROM event_store
                WHERE event_type = 'RiskBlocked'
                  AND event_time >= %s AND event_time < %s
                  AND event_payload->>'reason' ILIKE '%%wash%%'
                """,
                (day_start, day_end),
            )
            wash_trade_rejections = cur.fetchone()[0] or 0

            # ── All risk blocks (for reference) ───────────────────────
            cur.execute(
                """
                SELECT COUNT(*)
                FROM event_store
                WHERE event_type = 'RiskBlocked'
                  AND event_time >= %s AND event_time < %s
                """,
                (day_start, day_end),
            )
            total_risk_blocks = cur.fetchone()[0] or 0

            # ── Orphaned positions: opened but never closed ───────────
            cur.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT aggregate_id
                    FROM event_store
                    WHERE aggregate_type = 'Position'
                      AND event_type = 'PositionOpened'
                      AND event_time >= %s AND event_time < %s
                    EXCEPT
                    SELECT aggregate_id
                    FROM event_store
                    WHERE aggregate_type = 'Position'
                      AND event_type = 'PositionClosed'
                      AND event_time >= %s AND event_time < %s
                ) orphaned
                """,
                (day_start, day_end, day_start, day_end + timedelta(days=1)),
            )
            orphaned_positions = cur.fetchone()[0] or 0

        # ── Best / worst execution fills ──────────────────────────────
        exec_report = ComplianceReporter.best_execution_report(conn, trade_date=report_date)

        best_fill = None
        worst_fill = None
        if exec_report:
            scored = [
                f for f in exec_report
                if f.get("slippage_vs_arrival_bps") is not None
                   or f.get("slippage_vs_vwap_bps") is not None
            ]
            if scored:
                def _sort_key(f):
                    return abs(f.get("slippage_vs_arrival_bps") or f.get("slippage_vs_vwap_bps") or 0)

                scored_sorted = sorted(scored, key=_sort_key)
                best_fill = {
                    "event_id": scored_sorted[0]["fill_event_id"],
                    "ticker":   scored_sorted[0]["ticker"],
                    "slippage_bps": scored_sorted[0].get("slippage_vs_arrival_bps")
                                    or scored_sorted[0].get("slippage_vs_vwap_bps"),
                    "classification": scored_sorted[0]["classification"],
                }
                worst_fill = {
                    "event_id": scored_sorted[-1]["fill_event_id"],
                    "ticker":   scored_sorted[-1]["ticker"],
                    "slippage_bps": scored_sorted[-1].get("slippage_vs_arrival_bps")
                                    or scored_sorted[-1].get("slippage_vs_vwap_bps"),
                    "classification": scored_sorted[-1]["classification"],
                }

        execution_quality = {}
        if exec_report:
            classifications = [f["classification"] for f in exec_report]
            execution_quality = {
                "total_fills_analysed": len(exec_report),
                "good":       classifications.count("good"),
                "acceptable": classifications.count("acceptable"),
                "poor":       classifications.count("poor"),
                "unknown":    classifications.count("unknown"),
            }

        return {
            "report_date":          report_date.isoformat(),
            "total_trades":         total_trades,
            "total_fills":          total_fills,
            "total_pnl":            round(total_pnl, 2),
            "total_notional":       round(total_notional, 2),
            "max_positions":        max_positions,
            "kill_switch_activations": kill_switch_count,
            "wash_trade_rejections":   wash_trade_rejections,
            "total_risk_blocks":       total_risk_blocks,
            "orphaned_positions":      orphaned_positions,
            "best_fill":            best_fill,
            "worst_fill":           worst_fill,
            "execution_quality":    execution_quality,
        }

    # ── 4. Regulatory Snapshot — Positions at Point in Time ───────────────

    @staticmethod
    def positions_at(
        timestamp: Union[str, datetime],
        conn,
    ) -> List[Dict[str, Any]]:
        """
        Reconstruct portfolio positions at a specific point in time
        by replaying position events up to that timestamp.

        Parameters
        ----------
        timestamp : datetime or ISO string — the point-in-time to reconstruct
        conn      : psycopg2 connection

        Returns
        -------
        list[dict] — each dict represents one position held at that moment
                     with ticker, qty, entry_price, entry_time, stop, target, pnl
        """
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_type, event_time, event_payload, aggregate_id
                FROM event_store
                WHERE aggregate_type = 'Position'
                  AND event_time <= %s
                ORDER BY event_sequence ASC, event_time ASC
                """,
                (timestamp,),
            )
            rows = cur.fetchall()

        # Replay events to reconstruct state at the given timestamp
        # positions keyed by aggregate_id
        positions: Dict[str, Dict[str, Any]] = {}

        for event_type, event_time, raw_payload, aggregate_id in rows:
            payload = _safe_json(raw_payload)

            if event_type == "PositionOpened":
                positions[aggregate_id] = {
                    "ticker":       payload.get("ticker"),
                    "qty":          payload.get("qty"),
                    "entry_price":  payload.get("entry_price"),
                    "entry_time":   payload.get("entry_time"),
                    "stop_price":   payload.get("stop_price"),
                    "target_price": payload.get("target_price"),
                    "half_target":  payload.get("half_target"),
                    "current_price": payload.get("current_price"),
                    "unrealised_pnl": None,
                    "status":       "OPEN",
                    "opened_at":    event_time.isoformat() if event_time else None,
                    "aggregate_id": aggregate_id,
                }

            elif event_type == "PartialExited":
                if aggregate_id in positions:
                    pos = positions[aggregate_id]
                    pos["qty"] = payload.get("qty", pos["qty"])
                    pos["unrealised_pnl"] = payload.get("pnl")
                    pos["status"] = "PARTIAL"

            elif event_type == "PositionClosed":
                # Remove from active positions — it was closed before our timestamp
                positions.pop(aggregate_id, None)

        # Return only positions that are still open at the requested time
        result = []
        for agg_id, pos in positions.items():
            if pos["status"] in ("OPEN", "PARTIAL"):
                result.append(pos)

        return result


# ── Private Helper Functions ──────────────────────────────────────────────

def _lookup_nbbo_at(
    conn, ticker: str, fill_time: datetime
) -> Optional[Dict[str, Optional[float]]]:
    """
    Find the closest QuoteReceived event at or before fill_time for the ticker.
    Returns {'bid': float, 'ask': float} or None.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT event_payload
            FROM event_store
            WHERE event_type = 'QuoteReceived'
              AND event_payload->>'ticker' = %s
              AND event_time <= %s
            ORDER BY event_time DESC
            LIMIT 1
            """,
            (ticker, fill_time),
        )
        row = cur.fetchone()

    if not row:
        return None

    payload = _safe_json(row[0])
    bid = payload.get("bid")
    ask = payload.get("ask")
    return {
        "bid": float(bid) if bid is not None else None,
        "ask": float(ask) if ask is not None else None,
    }


def _lookup_arrival_price(
    conn,
    correlation_id: Optional[str],
    ticker: str,
    fill_time: datetime,
) -> Optional[float]:
    """
    Find the signal price (arrival price) for this fill.

    First tries correlation_id linkage, then falls back to the most recent
    signal for the same ticker before fill_time.
    """
    with conn.cursor() as cur:
        # Try correlation_id match first
        if correlation_id:
            cur.execute(
                """
                SELECT event_payload
                FROM event_store
                WHERE correlation_id = %s
                  AND event_type IN ('StrategySignal', 'ProStrategySignal', 'PopStrategySignal', 'OptionsSignal')
                ORDER BY event_time ASC
                LIMIT 1
                """,
                (correlation_id,),
            )
            row = cur.fetchone()
            if row:
                payload = _safe_json(row[0])
                price = payload.get("current_price") or payload.get("entry_price")
                if price is not None:
                    return float(price)

        # Fallback: most recent signal for ticker before fill
        cur.execute(
            """
            SELECT event_payload
            FROM event_store
            WHERE event_type IN ('StrategySignal', 'ProStrategySignal', 'PopStrategySignal', 'OptionsSignal')
              AND (event_payload->>'ticker' = %s OR event_payload->>'symbol' = %s)
              AND event_time <= %s
            ORDER BY event_time DESC
            LIMIT 1
            """,
            (ticker, ticker, fill_time),
        )
        row = cur.fetchone()
        if row:
            payload = _safe_json(row[0])
            price = payload.get("current_price") or payload.get("entry_price")
            if price is not None:
                return float(price)

    return None


def _lookup_vwap_window(
    conn, ticker: str, fill_time: datetime, window_minutes: int = 5
) -> Optional[float]:
    """
    Compute VWAP from bar data over the execution window
    (fill_time - window_minutes to fill_time).

    V9: Reads from market_bars table first (lean, ~10x faster).
    Falls back to event_store BarReceived for historical data pre-V9.

    Uses close * volume weighted average. Returns None if no bars found.
    """
    window_start = fill_time - timedelta(minutes=window_minutes)

    rows = []
    with conn.cursor() as cur:
        # V9: Try market_bars first (lean table)
        try:
            cur.execute(
                """
                SELECT close, volume
                FROM market_bars
                WHERE ticker = %s
                  AND bar_time >= %s AND bar_time <= %s
                ORDER BY bar_time ASC
                """,
                (ticker, window_start, fill_time),
            )
            rows = cur.fetchall()
        except Exception:
            conn.rollback()

        # Fallback: legacy event_store BarReceived (pre-V9 data)
        if not rows:
            cur.execute(
                """
                SELECT event_payload
                FROM event_store
                WHERE event_type = 'BarReceived'
                  AND event_payload->>'ticker' = %s
                  AND event_time >= %s AND event_time <= %s
                ORDER BY event_time ASC
                """,
                (ticker, window_start, fill_time),
            )
            legacy_rows = cur.fetchall()
            if legacy_rows:
                rows = []
                for row in legacy_rows:
                    payload = _safe_json(row[0])
                    rows.append({
                        'close': payload.get("close", 0),
                        'volume': payload.get("volume", 0),
                    })

    if not rows:
        return None

    total_pv = 0.0
    total_vol = 0.0
    for row in rows:
        if isinstance(row, dict):
            close, volume = row.get('close', 0), row.get('volume', 0)
        else:
            close, volume = row[0], row[1]
        try:
            close = float(close)
            volume = float(volume)
        except (TypeError, ValueError):
            continue
        if volume > 0:
            total_pv += close * volume
            total_vol += volume

    if total_vol == 0:
        return None

    return round(total_pv / total_vol, 4)
