#!/usr/bin/env python3
"""
scripts/post_session_analytics.py — Post-Session Batch Analytics

Runs AFTER market close (4:00 PM PST / 7:00 PM ET).
Performs heavy computation that would be too expensive during trading hours.

Populates ML tables from event_store:
  1. ml_trade_outcomes   — MFE/MAE excursion analysis
  2. ml_signal_context   — Feature snapshots at signal time
  3. ml_rejection_log    — Counterfactual analysis for blocked trades
  4. iv_history          — Daily IV snapshots
  5. ml_daily_regime     — Market regime classification

Properties:
  - Idempotent: safe to run multiple times (upserts everywhere)
  - Read-only on event_store: never mutates source of truth
  - Writes only to ML projection tables
  - Targets < 5 min for a full day's data

Usage:
  python scripts/post_session_analytics.py                    # today
  python scripts/post_session_analytics.py --date 2026-04-13  # specific date
  python scripts/post_session_analytics.py --backfill 30      # last 30 days
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("post_session_analytics")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DSN = os.getenv(
    "TRADING_DB_DSN",
    "postgresql://trading:trading_secret@localhost:5432/tradinghub",
)

# Account size used for regime classification threshold
ACCOUNT_SIZE = float(os.getenv("ACCOUNT_SIZE", "100000"))


def get_conn():
    """Return a new psycopg2 connection with RealDictCursor factory."""
    return psycopg2.connect(DSN, cursor_factory=psycopg2.extras.RealDictCursor)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _date_range(target_date: date) -> Tuple[datetime, datetime]:
    """Return (start_of_day_utc, end_of_day_utc) for a given date.

    Trading day is defined as 04:00 ET (pre-market) to 20:00 ET (after-hours),
    which in UTC is 08:00 to 00:00 next day (EST) or 09:00 to 01:00 (EDT).
    We use a generous window: midnight-to-midnight UTC for the date.
    """
    start = datetime(target_date.year, target_date.month, target_date.day,
                     tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def _safe_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_json(raw: Any) -> dict:
    """Parse event_payload which may be str or already a dict."""
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
        return raw
    return {}


# ── V9: Bar data helpers (market_bars first, event_store fallback) ─────────

def _fetch_bars(cur, ticker: str, start_time, end_time) -> list[dict]:
    """Fetch bar data between start_time and end_time as list of dicts.

    V9: Reads from market_bars table first (lean, fast).
    Falls back to event_store BarReceived for pre-V9 data.

    Returns list of {open, high, low, close, volume, vwap} dicts.
    """
    try:
        cur.execute("""
            SELECT open, high, low, close, volume, vwap
            FROM market_bars
            WHERE ticker = %s
              AND bar_time >= %s AND bar_time <= %s
            ORDER BY bar_time
        """, (ticker, start_time, end_time))
        rows = cur.fetchall()
        if rows:
            return [{'open': r[0], 'high': r[1], 'low': r[2],
                     'close': r[3], 'volume': r[4], 'vwap': r[5]} for r in rows]
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass

    # Fallback: legacy event_store BarReceived
    cur.execute("""
        SELECT event_payload
        FROM event_store
        WHERE event_type = 'BarReceived'
          AND event_payload->>'ticker' = %s
          AND event_time >= %s AND event_time <= %s
        ORDER BY event_time
    """, (ticker, start_time, end_time))
    return [_parse_json(r["event_payload"]) for r in cur.fetchall()]


def _fetch_latest_bar(cur, ticker: str, before_time) -> Optional[dict]:
    """Fetch the most recent bar for ticker before a given time.

    Returns {open, high, low, close, volume, vwap} dict or None.
    """
    try:
        cur.execute("""
            SELECT open, high, low, close, volume, vwap
            FROM market_bars
            WHERE ticker = %s AND bar_time <= %s
            ORDER BY bar_time DESC
            LIMIT 1
        """, (ticker, before_time))
        row = cur.fetchone()
        if row:
            return {'open': row[0], 'high': row[1], 'low': row[2],
                    'close': row[3], 'volume': row[4], 'vwap': row[5]}
    except Exception:
        try:
            cur.connection.rollback()
        except Exception:
            pass

    # Fallback: legacy event_store
    cur.execute("""
        SELECT event_payload FROM event_store
        WHERE event_type = 'BarReceived'
          AND event_payload->>'ticker' = %s
          AND event_time <= %s
        ORDER BY event_time DESC
        LIMIT 1
    """, (ticker, before_time))
    row = cur.fetchone()
    if row:
        return _parse_json(row["event_payload"])
    return None


# ═══════════════════════════════════════════════════════════════════════════
# JOB 1: ml_trade_outcomes — MFE / MAE excursion analysis
# ═══════════════════════════════════════════════════════════════════════════

def job_trade_outcomes(conn, target_date: date) -> int:
    """Populate ml_trade_outcomes with MFE/MAE for trades closed on target_date."""
    log.info("Job 1: ml_trade_outcomes — starting for %s", target_date)
    t0 = time.monotonic()
    day_start, day_end = _date_range(target_date)

    cur = conn.cursor()

    # Step 1: Find all PositionClosed events for the target date
    cur.execute("""
        SELECT event_id, aggregate_id, event_time, event_payload,
               correlation_id, session_id
        FROM event_store
        WHERE event_type = 'PositionClosed'
          AND event_time >= %s AND event_time < %s
        ORDER BY event_time
    """, (day_start, day_end))
    closed_events = cur.fetchall()

    if not closed_events:
        log.info("  No PositionClosed events found for %s", target_date)
        return 0

    inserted = 0

    for closed in closed_events:
        close_payload = _parse_json(closed["event_payload"])
        ticker = close_payload.get("ticker")
        if not ticker:
            continue

        agg_id = closed["aggregate_id"]  # e.g. "position_AAPL"

        # Step 2: Find matching PositionOpened event via aggregate_id
        cur.execute("""
            SELECT event_id, event_time, event_payload
            FROM event_store
            WHERE aggregate_id = %s
              AND event_type = 'PositionOpened'
              AND event_time < %s
            ORDER BY event_time DESC
            LIMIT 1
        """, (agg_id, closed["event_time"]))
        opened = cur.fetchone()

        if not opened:
            log.warning("  No PositionOpened found for %s (agg=%s)", ticker, agg_id)
            continue

        open_payload = _parse_json(opened["event_payload"])
        entry_price = _safe_float(open_payload.get("entry_price"))
        exit_price = _safe_float(close_payload.get("current_price"))
        pnl = _safe_float(close_payload.get("pnl"))

        if entry_price is None or entry_price == 0:
            continue

        entry_time = opened["event_time"]
        exit_time = closed["event_time"]
        time_in_position_sec = int((exit_time - entry_time).total_seconds())

        # Step 3: Get bar data between entry and exit for MFE/MAE
        # V9: market_bars first, fallback to event_store BarReceived
        bar_rows = _fetch_bars(cur, ticker, entry_time, exit_time)

        mfe = 0.0
        mae = 0.0

        for bp in bar_rows:
            high = _safe_float(bp.get("high"))
            low = _safe_float(bp.get("low"))

            if high is not None:
                excursion_up = high - entry_price
                if excursion_up > mfe:
                    mfe = excursion_up
            if low is not None:
                excursion_down = entry_price - low
                if excursion_down > mae:
                    mae = excursion_down

        mfe_pct = (mfe / entry_price) if entry_price else None
        mae_pct = (mae / entry_price) if entry_price else None
        pnl_pct = (pnl / (entry_price * int(open_payload.get("qty", 1)))) if pnl is not None and entry_price else None

        # Step 4: Count concurrent positions at entry time
        cur.execute("""
            SELECT COUNT(DISTINCT aggregate_id) AS cnt
            FROM event_store
            WHERE event_type = 'PositionOpened'
              AND aggregate_id != %s
              AND event_time <= %s
              AND aggregate_id NOT IN (
                  SELECT aggregate_id FROM event_store
                  WHERE event_type = 'PositionClosed'
                    AND event_time <= %s
              )
        """, (agg_id, entry_time, entry_time))
        concurrent_row = cur.fetchone()
        concurrent_positions = concurrent_row["cnt"] if concurrent_row else 0

        # Determine exit reason from payload
        exit_reason = close_payload.get("action", "unknown")
        if exit_reason == "CLOSED":
            exit_reason = "closed"

        # Determine layer/strategy from signal context if available
        layer = "vwap"
        strategy_name = "vwap_reclaim"

        # Try to find the originating signal for better layer/strategy info
        if closed.get("correlation_id"):
            cur.execute("""
                SELECT event_type, event_payload
                FROM event_store
                WHERE correlation_id = %s
                  AND event_type IN ('StrategySignal', 'PopStrategySignal',
                                     'ProStrategySignal', 'OptionsSignal')
                LIMIT 1
            """, (closed["correlation_id"],))
            sig_row = cur.fetchone()
            if sig_row:
                sig_type = sig_row["event_type"]
                sig_payload = _parse_json(sig_row["event_payload"])
                if sig_type == "ProStrategySignal":
                    layer = "pro"
                    strategy_name = sig_payload.get("strategy_name", "pro_setup")
                elif sig_type == "PopStrategySignal":
                    layer = "pop"
                    strategy_name = sig_payload.get("strategy_type", "pop_setup")
                elif sig_type == "OptionsSignal":
                    layer = "options"
                    strategy_name = sig_payload.get("strategy_type", "options")

        # Step 5: Upsert into ml_trade_outcomes
        cur.execute("""
            INSERT INTO ml_trade_outcomes (
                ticker, layer, strategy_name,
                entry_ts, entry_price,
                entry_rsi, entry_atr, entry_rvol,
                exit_ts, exit_price, exit_reason,
                qty, realized_pnl, realized_pnl_pct,
                max_favorable_excursion, max_adverse_excursion,
                mfe_pct, mae_pct,
                time_in_position_sec, concurrent_positions,
                opened_event_id, closed_event_id, session_id
            ) VALUES (
                %(ticker)s, %(layer)s, %(strategy_name)s,
                %(entry_ts)s, %(entry_price)s,
                %(entry_rsi)s, %(entry_atr)s, %(entry_rvol)s,
                %(exit_ts)s, %(exit_price)s, %(exit_reason)s,
                %(qty)s, %(realized_pnl)s, %(realized_pnl_pct)s,
                %(mfe)s, %(mae)s,
                %(mfe_pct)s, %(mae_pct)s,
                %(time_in_position_sec)s, %(concurrent_positions)s,
                %(opened_event_id)s, %(closed_event_id)s, %(session_id)s
            )
            ON CONFLICT (trade_id) DO NOTHING
        """, {
            "ticker": ticker,
            "layer": layer,
            "strategy_name": strategy_name,
            "entry_ts": entry_time,
            "entry_price": entry_price,
            "entry_rsi": _safe_float(open_payload.get("rsi_value")),
            "entry_atr": _safe_float(open_payload.get("atr_value")),
            "entry_rvol": _safe_float(open_payload.get("rvol")),
            "exit_ts": exit_time,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "qty": int(open_payload.get("qty", 1)),
            "realized_pnl": pnl or 0,
            "realized_pnl_pct": pnl_pct,
            "mfe": mfe,
            "mae": mae,
            "mfe_pct": mfe_pct,
            "mae_pct": mae_pct,
            "time_in_position_sec": time_in_position_sec,
            "concurrent_positions": concurrent_positions,
            "opened_event_id": opened["event_id"],
            "closed_event_id": closed["event_id"],
            "session_id": closed.get("session_id"),
        })
        inserted += 1

    conn.commit()
    elapsed = time.monotonic() - t0
    log.info("  Job 1 complete: %d trades processed in %.1fs", inserted, elapsed)
    return inserted


# ═══════════════════════════════════════════════════════════════════════════
# JOB 2: ml_signal_context — Feature snapshot at signal time
# ═══════════════════════════════════════════════════════════════════════════

def job_signal_context(conn, target_date: date) -> int:
    """Populate ml_signal_context for all signals on target_date."""
    log.info("Job 2: ml_signal_context — starting for %s", target_date)
    t0 = time.monotonic()
    day_start, day_end = _date_range(target_date)

    cur = conn.cursor()

    signal_types = (
        "StrategySignal", "PopStrategySignal",
        "ProStrategySignal", "OptionsSignal",
    )

    cur.execute("""
        SELECT event_id, event_type, event_time, event_payload,
               correlation_id
        FROM event_store
        WHERE event_type IN %s
          AND event_time >= %s AND event_time < %s
        ORDER BY event_time
    """, (signal_types, day_start, day_end))
    signals = cur.fetchall()

    if not signals:
        log.info("  No signal events found for %s", target_date)
        return 0

    inserted = 0

    for sig in signals:
        payload = _parse_json(sig["event_payload"])
        ticker = payload.get("ticker") or payload.get("symbol")
        if not ticker:
            continue

        sig_type = sig["event_type"]
        corr_id = sig.get("correlation_id")

        # Determine layer
        layer_map = {
            "StrategySignal": "vwap",
            "PopStrategySignal": "pop",
            "ProStrategySignal": "pro",
            "OptionsSignal": "options",
        }
        layer = layer_map.get(sig_type, "unknown")

        # Determine action
        action = payload.get("action") or payload.get("direction") or "unknown"

        # Core indicators
        current_price = _safe_float(payload.get("current_price") or payload.get("entry_price") or payload.get("underlying_price"))
        rsi = _safe_float(payload.get("rsi_value"))
        atr = _safe_float(payload.get("atr_value"))
        rvol = _safe_float(payload.get("rvol"))
        vwap = _safe_float(payload.get("vwap"))
        confidence = _safe_float(payload.get("confidence") or payload.get("strategy_confidence"))
        strategy_name = payload.get("strategy_name") or payload.get("strategy_type")

        # VWAP distance
        vwap_distance_pct = None
        if current_price and vwap and vwap > 0:
            vwap_distance_pct = (current_price - vwap) / vwap

        # Check if executed (FillExecuted with same correlation_id)
        was_executed = False
        if corr_id:
            cur.execute("""
                SELECT 1 FROM event_store
                WHERE event_type = 'FillExecuted'
                  AND correlation_id = %s
                LIMIT 1
            """, (corr_id,))
            was_executed = cur.fetchone() is not None

        # Check if rejected (RiskBlocked with same correlation_id)
        was_rejected = False
        rejection_reason = None
        if corr_id:
            cur.execute("""
                SELECT event_payload FROM event_store
                WHERE event_type = 'RiskBlocked'
                  AND correlation_id = %s
                LIMIT 1
            """, (corr_id,))
            rej_row = cur.fetchone()
            if rej_row:
                was_rejected = True
                rej_payload = _parse_json(rej_row["event_payload"])
                rejection_reason = rej_payload.get("reason")

        # If executed, get outcome P&L from PositionClosed
        outcome_pnl = None
        outcome_pnl_pct = None
        if was_executed and corr_id:
            cur.execute("""
                SELECT event_payload FROM event_store
                WHERE event_type = 'PositionClosed'
                  AND event_payload->>'ticker' = %s
                  AND event_time >= %s AND event_time < %s
                ORDER BY event_time ASC
                LIMIT 1
            """, (ticker, sig["event_time"], day_end))
            close_row = cur.fetchone()
            if close_row:
                cp = _parse_json(close_row["event_payload"])
                outcome_pnl = _safe_float(cp.get("pnl"))
                if outcome_pnl is not None and current_price and current_price > 0:
                    qty = int(payload.get("qty", 1))
                    if qty > 0:
                        outcome_pnl_pct = outcome_pnl / (current_price * qty)

        # Bar context at signal time: find the most recent bar for this ticker
        # V9: market_bars first, fallback to event_store
        bar_return = None
        bar_range_pct = None
        bp = _fetch_latest_bar(cur, ticker, sig["event_time"])
        if bp:
            o = _safe_float(bp.get("open"))
            h = _safe_float(bp.get("high"))
            lo = _safe_float(bp.get("low"))
            c = _safe_float(bp.get("close"))
            if o and o > 0 and c is not None:
                bar_return = (c - o) / o
            if o and o > 0 and h is not None and lo is not None:
                bar_range_pct = (h - lo) / o

        # Pro signals: count detectors fired
        n_detectors_fired = None
        detector_agreement = None
        if sig_type == "ProStrategySignal":
            det = payload.get("detector_signals")
            if isinstance(det, dict) and det:
                total_det = len(det)
                fired = sum(1 for v in det.values()
                            if v is True or (isinstance(v, dict) and v.get("fired")))
                n_detectors_fired = fired
                detector_agreement = fired / total_det if total_det > 0 else None

        # Options-specific
        iv_estimate = None
        iv_rank = None
        if sig_type == "OptionsSignal":
            iv_estimate = _safe_float(payload.get("iv_estimate"))
            iv_rank = _safe_float(payload.get("iv_rank"))

        # Upsert
        cur.execute("""
            INSERT INTO ml_signal_context (
                source_event_id, ts, ticker, layer, action, strategy_name,
                current_price, rsi, atr, rvol, vwap, vwap_distance_pct,
                confidence, iv_estimate, iv_rank,
                bar_return, bar_range_pct,
                was_executed, was_rejected, rejection_reason,
                outcome_pnl, outcome_pnl_pct,
                n_detectors_fired, detector_agreement
            ) VALUES (
                %(source_event_id)s, %(ts)s, %(ticker)s, %(layer)s,
                %(action)s, %(strategy_name)s,
                %(current_price)s, %(rsi)s, %(atr)s, %(rvol)s,
                %(vwap)s, %(vwap_distance_pct)s,
                %(confidence)s, %(iv_estimate)s, %(iv_rank)s,
                %(bar_return)s, %(bar_range_pct)s,
                %(was_executed)s, %(was_rejected)s, %(rejection_reason)s,
                %(outcome_pnl)s, %(outcome_pnl_pct)s,
                %(n_detectors_fired)s, %(detector_agreement)s
            )
            ON CONFLICT (signal_id) DO NOTHING
        """, {
            "source_event_id": sig["event_id"],
            "ts": sig["event_time"],
            "ticker": ticker,
            "layer": layer,
            "action": action,
            "strategy_name": strategy_name,
            "current_price": current_price,
            "rsi": rsi,
            "atr": atr,
            "rvol": rvol,
            "vwap": vwap,
            "vwap_distance_pct": vwap_distance_pct,
            "confidence": confidence,
            "iv_estimate": iv_estimate,
            "iv_rank": iv_rank,
            "bar_return": bar_return,
            "bar_range_pct": bar_range_pct,
            "was_executed": was_executed,
            "was_rejected": was_rejected,
            "rejection_reason": rejection_reason,
            "outcome_pnl": outcome_pnl,
            "outcome_pnl_pct": outcome_pnl_pct,
            "n_detectors_fired": n_detectors_fired,
            "detector_agreement": detector_agreement,
        })
        inserted += 1

    conn.commit()
    elapsed = time.monotonic() - t0
    log.info("  Job 2 complete: %d signals processed in %.1fs", inserted, elapsed)
    return inserted


# ═══════════════════════════════════════════════════════════════════════════
# JOB 3: ml_rejection_log — Counterfactual analysis
# ═══════════════════════════════════════════════════════════════════════════

def job_rejection_log(conn, target_date: date) -> int:
    """Populate ml_rejection_log with counterfactual analysis for target_date."""
    log.info("Job 3: ml_rejection_log — starting for %s", target_date)
    t0 = time.monotonic()
    day_start, day_end = _date_range(target_date)

    cur = conn.cursor()

    cur.execute("""
        SELECT event_id, event_time, event_payload, correlation_id
        FROM event_store
        WHERE event_type = 'RiskBlocked'
          AND event_time >= %s AND event_time < %s
        ORDER BY event_time
    """, (day_start, day_end))
    rejections = cur.fetchall()

    if not rejections:
        log.info("  No RiskBlocked events found for %s", target_date)
        return 0

    inserted = 0

    for rej in rejections:
        rej_payload = _parse_json(rej["event_payload"])
        ticker = rej_payload.get("ticker")
        if not ticker:
            continue

        corr_id = rej.get("correlation_id")
        reason = rej_payload.get("reason", "unknown")
        action_blocked = rej_payload.get("signal_action", "unknown")

        # Parse reason into category
        reason_lower = reason.lower()
        if "max_position" in reason_lower or "position" in reason_lower:
            reason_category = "max_positions"
        elif "cooldown" in reason_lower:
            reason_category = "cooldown"
        elif "daily_loss" in reason_lower or "loss" in reason_lower:
            reason_category = "daily_loss"
        elif "spread" in reason_lower:
            reason_category = "spread_wide"
        elif "volatility" in reason_lower or "vol" in reason_lower:
            reason_category = "volatility"
        else:
            reason_category = "other"

        # Find the preceding signal event for features
        signal_rsi = None
        signal_atr = None
        signal_rvol = None
        signal_confidence = None
        signal_price = None
        layer = "vwap"

        if corr_id:
            cur.execute("""
                SELECT event_type, event_payload FROM event_store
                WHERE correlation_id = %s
                  AND event_type IN ('StrategySignal', 'PopStrategySignal',
                                     'ProStrategySignal', 'OptionsSignal')
                LIMIT 1
            """, (corr_id,))
            sig_row = cur.fetchone()
            if sig_row:
                sp = _parse_json(sig_row["event_payload"])
                signal_rsi = _safe_float(sp.get("rsi_value"))
                signal_atr = _safe_float(sp.get("atr_value"))
                signal_rvol = _safe_float(sp.get("rvol"))
                signal_confidence = _safe_float(sp.get("confidence") or sp.get("strategy_confidence"))
                signal_price = _safe_float(sp.get("current_price") or sp.get("entry_price") or sp.get("underlying_price"))
                sig_type = sig_row["event_type"]
                if sig_type == "ProStrategySignal":
                    layer = "pro"
                elif sig_type == "PopStrategySignal":
                    layer = "pop"
                elif sig_type == "OptionsSignal":
                    layer = "options"

        # Count positions held at rejection time
        cur.execute("""
            SELECT COUNT(DISTINCT aggregate_id) AS cnt
            FROM event_store
            WHERE event_type = 'PositionOpened'
              AND event_time <= %s
              AND aggregate_id NOT IN (
                  SELECT aggregate_id FROM event_store
                  WHERE event_type = 'PositionClosed'
                    AND event_time <= %s
              )
        """, (rej["event_time"], rej["event_time"]))
        pos_row = cur.fetchone()
        positions_held = pos_row["cnt"] if pos_row else 0

        # Counterfactual: look at next 30-60 min of bar data after rejection
        # V9: market_bars first, fallback to event_store
        window_end = rej["event_time"] + timedelta(minutes=60)
        future_bars = _fetch_bars(cur, ticker, rej["event_time"], window_end)

        would_have_won = None
        counterfactual_pnl = None

        if signal_price and signal_price > 0 and future_bars:
            # Determine direction from the blocked action
            is_long = action_blocked.upper() in ("BUY", "LONG")

            max_favorable = 0.0
            atr_val = signal_atr or 1.0  # fallback to avoid division by zero

            for fbp in future_bars:
                high = _safe_float(fbp.get("high"))
                low = _safe_float(fbp.get("low"))
                close = _safe_float(fbp.get("close"))

                if is_long:
                    if high is not None:
                        move = high - signal_price
                        if move > max_favorable:
                            max_favorable = move
                else:
                    if low is not None:
                        move = signal_price - low
                        if move > max_favorable:
                            max_favorable = move

            # Use last close for counterfactual P&L
            last_bar = _parse_json(future_bars[-1]["event_payload"])
            last_close = _safe_float(last_bar.get("close"))
            if last_close is not None:
                if is_long:
                    counterfactual_pnl = last_close - signal_price
                else:
                    counterfactual_pnl = signal_price - last_close

            # Would have won if price moved > 1 ATR in signal direction
            would_have_won = max_favorable > atr_val

        cur.execute("""
            INSERT INTO ml_rejection_log (
                source_event_id, ts, ticker, layer, action_blocked,
                reason_category, reason_detail,
                signal_rsi, signal_atr, signal_rvol,
                signal_confidence, signal_price,
                positions_held,
                would_have_won, counterfactual_pnl
            ) VALUES (
                %(source_event_id)s, %(ts)s, %(ticker)s, %(layer)s,
                %(action_blocked)s,
                %(reason_category)s, %(reason_detail)s,
                %(signal_rsi)s, %(signal_atr)s, %(signal_rvol)s,
                %(signal_confidence)s, %(signal_price)s,
                %(positions_held)s,
                %(would_have_won)s, %(counterfactual_pnl)s
            )
            ON CONFLICT (rejection_id) DO NOTHING
        """, {
            "source_event_id": rej["event_id"],
            "ts": rej["event_time"],
            "ticker": ticker,
            "layer": layer,
            "action_blocked": action_blocked,
            "reason_category": reason_category,
            "reason_detail": reason,
            "signal_rsi": signal_rsi,
            "signal_atr": signal_atr,
            "signal_rvol": signal_rvol,
            "signal_confidence": signal_confidence,
            "signal_price": signal_price,
            "positions_held": positions_held,
            "would_have_won": would_have_won,
            "counterfactual_pnl": counterfactual_pnl,
        })
        inserted += 1

    conn.commit()
    elapsed = time.monotonic() - t0
    log.info("  Job 3 complete: %d rejections processed in %.1fs", inserted, elapsed)
    return inserted


# ═══════════════════════════════════════════════════════════════════════════
# JOB 4: iv_history — Persist IV tracker state
# ═══════════════════════════════════════════════════════════════════════════

IV_HISTORY_FILE = Path(__file__).resolve().parent.parent / "data" / "iv_history.json"

IV_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS iv_history (
    ts DATE NOT NULL,
    ticker TEXT NOT NULL,
    iv NUMERIC(6,4),
    iv_rank NUMERIC(6,2),
    iv_percentile NUMERIC(6,2),
    PRIMARY KEY (ts, ticker)
);
"""


def job_iv_history(conn, target_date: date) -> int:
    """Persist IV history from data/iv_history.json into iv_history table."""
    log.info("Job 4: iv_history — starting for %s", target_date)
    t0 = time.monotonic()

    cur = conn.cursor()

    # Table created by db/schema_ml_tables.sql or DBA — skip DDL here

    iv_path = Path(os.getenv("IV_HISTORY_PATH", str(IV_HISTORY_FILE)))
    if not iv_path.exists():
        log.info("  IV history file not found at %s — skipping", iv_path)
        return 0

    try:
        with open(iv_path, "r") as f:
            iv_data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        log.warning("  Failed to read IV history file: %s", e)
        return 0

    inserted = 0

    # iv_data is expected to be a dict keyed by ticker, with IV info
    # e.g. {"AAPL": {"iv": 0.32, "iv_rank": 45.2, "iv_percentile": 52.1}, ...}
    for ticker, iv_info in iv_data.items():
        if not isinstance(iv_info, dict):
            continue

        iv = _safe_float(iv_info.get("iv") or iv_info.get("implied_volatility"))
        iv_rank = _safe_float(iv_info.get("iv_rank"))
        iv_percentile = _safe_float(iv_info.get("iv_percentile"))

        if iv is None and iv_rank is None:
            continue

        cur.execute("""
            INSERT INTO iv_history (ts, ticker, iv, iv_rank, iv_percentile)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (ts, ticker) DO UPDATE SET
                iv = EXCLUDED.iv,
                iv_rank = EXCLUDED.iv_rank,
                iv_percentile = EXCLUDED.iv_percentile
        """, (target_date, ticker, iv, iv_rank, iv_percentile))
        inserted += 1

    conn.commit()
    elapsed = time.monotonic() - t0
    log.info("  Job 4 complete: %d tickers persisted in %.1fs", inserted, elapsed)
    return inserted


# ═══════════════════════════════════════════════════════════════════════════
# JOB 5: ml_daily_regime — Market regime classification
# ═══════════════════════════════════════════════════════════════════════════

def job_daily_regime(conn, target_date: date) -> int:
    """Compute and upsert ml_daily_regime for target_date."""
    log.info("Job 5: ml_daily_regime — starting for %s", target_date)
    t0 = time.monotonic()
    day_start, day_end = _date_range(target_date)

    cur = conn.cursor()

    # Count total signals
    cur.execute("""
        SELECT COUNT(*) AS cnt FROM event_store
        WHERE event_type IN ('StrategySignal', 'PopStrategySignal',
                             'ProStrategySignal', 'OptionsSignal')
          AND event_time >= %s AND event_time < %s
    """, (day_start, day_end))
    total_signals = cur.fetchone()["cnt"]

    # Get trades and P&L from PositionClosed events
    cur.execute("""
        SELECT
            COUNT(*) AS total_trades,
            COALESCE(SUM((event_payload->>'pnl')::NUMERIC), 0) AS total_pnl,
            COALESCE(SUM(CASE WHEN (event_payload->>'pnl')::NUMERIC >= 0 THEN 1 ELSE 0 END), 0) AS wins
        FROM event_store
        WHERE event_type = 'PositionClosed'
          AND event_time >= %s AND event_time < %s
    """, (day_start, day_end))
    trade_stats = cur.fetchone()
    total_trades = trade_stats["total_trades"]
    total_pnl = float(trade_stats["total_pnl"] or 0)
    wins = int(trade_stats["wins"] or 0)
    win_rate = (wins * 100.0 / total_trades) if total_trades > 0 else 0.0

    # Compute average RVOL across all tickers from bar data
    # V9: market_bars first, fallback to event_store BarReceived
    avg_rvol = None
    try:
        cur.execute("""
            SELECT AVG(rvol) AS avg_rvol
            FROM market_bars
            WHERE rvol IS NOT NULL
              AND bar_time >= %s AND bar_time < %s
        """, (day_start, day_end))
        rvol_row = cur.fetchone()
        avg_rvol = _safe_float(rvol_row["avg_rvol"]) if rvol_row else None
    except Exception:
        conn.rollback()
    if avg_rvol is None:
        # Fallback: legacy event_store
        cur.execute("""
            SELECT AVG((event_payload->>'rvol')::NUMERIC) AS avg_rvol
            FROM event_store
            WHERE event_type = 'BarReceived'
              AND event_payload->>'rvol' IS NOT NULL
              AND event_time >= %s AND event_time < %s
        """, (day_start, day_end))
        rvol_row = cur.fetchone()
        avg_rvol = _safe_float(rvol_row["avg_rvol"]) if rvol_row else None

    # Classify regime
    if total_trades == 0:
        regime_label = "no_data"
    elif total_pnl > 0 and win_rate > 55:
        regime_label = "bull_trend"
    elif total_pnl < 0 and win_rate < 45:
        regime_label = "bear_trend"
    elif abs(total_pnl) < ACCOUNT_SIZE * 0.005:
        regime_label = "range_bound"
    else:
        regime_label = "volatile"

    # Classify volatility regime from RVOL
    if avg_rvol is None:
        volatility_regime = "unknown"
    elif avg_rvol < 0.8:
        volatility_regime = "low"
    elif avg_rvol < 1.5:
        volatility_regime = "normal"
    elif avg_rvol < 2.5:
        volatility_regime = "elevated"
    else:
        volatility_regime = "extreme"

    # Upsert
    cur.execute("""
        INSERT INTO ml_daily_regime (
            regime_date,
            avg_rvol_universe, regime_label, volatility_regime,
            total_signals, total_trades, total_pnl, win_rate
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (regime_date) DO UPDATE SET
            avg_rvol_universe = EXCLUDED.avg_rvol_universe,
            regime_label = EXCLUDED.regime_label,
            volatility_regime = EXCLUDED.volatility_regime,
            total_signals = EXCLUDED.total_signals,
            total_trades = EXCLUDED.total_trades,
            total_pnl = EXCLUDED.total_pnl,
            win_rate = EXCLUDED.win_rate
    """, (target_date, avg_rvol, regime_label, volatility_regime,
          total_signals, total_trades, total_pnl, win_rate))

    conn.commit()
    elapsed = time.monotonic() - t0
    log.info("  Job 5 complete: regime=%s, %d trades, pnl=%.2f, win_rate=%.1f%% (%.1fs)",
             regime_label, total_trades, total_pnl, win_rate, elapsed)
    return 1


# ═══════════════════════════════════════════════════════════════════════════
# Job 6: P&L Attribution — Alpha/Beta Decomposition
# ═══════════════════════════════════════════════════════════════════════════

def _compute_intraday_beta(cur, ticker: str, target_date: date) -> Tuple[float, float, int, bool]:
    """Compute realized intraday beta from 1-min return regression vs SPY.

    Returns (beta, r_squared, n_bars, fallback_used).

    Quality gate: n ≥ 30 AND r² ≥ 0.15.
    Fallback chain: intraday regression → 5-day rolling → Yahoo static → 1.0.
    """
    import numpy as np

    # Check cache first
    cur.execute("""
        SELECT intraday_beta, r_squared, n_bars, fallback_used
        FROM trading.ml_intraday_beta
        WHERE ticker = %s AND session_date = %s
    """, (ticker, target_date))
    cached = cur.fetchone()
    if cached:
        return (float(cached['intraday_beta']), float(cached['r_squared'] or 0),
                int(cached['n_bars'] or 0), bool(cached['fallback_used']))

    day_start, day_end = _date_range(target_date)

    # Fetch ticker + SPY 1-min bars for the day
    cur.execute("""
        SELECT bar_time, close FROM trading.market_bars
        WHERE ticker = %s AND bar_time >= %s AND bar_time < %s
        ORDER BY bar_time
    """, (ticker, day_start, day_end))
    stock_bars = cur.fetchall()

    cur.execute("""
        SELECT bar_time, close FROM trading.market_bars
        WHERE ticker = 'SPY' AND bar_time >= %s AND bar_time < %s
        ORDER BY bar_time
    """, (day_start, day_end))
    spy_bars = cur.fetchall()

    if not stock_bars or not spy_bars:
        beta, r2, n, fallback = 1.0, 0.0, 0, True
        _cache_beta(cur, ticker, target_date, beta, r2, n, fallback)
        return beta, r2, n, fallback

    # Align on bar_time (inner join)
    spy_map = {r['bar_time']: float(r['close']) for r in spy_bars}
    aligned = []
    for sb in stock_bars:
        bt = sb['bar_time']
        if bt in spy_map:
            aligned.append((float(sb['close']), spy_map[bt]))

    n = len(aligned)
    if n < 30:
        # Try 5-day rolling intraday beta
        beta, r2, n, fallback = _compute_rolling_intraday_beta(cur, ticker, target_date)
        _cache_beta(cur, ticker, target_date, beta, r2, n, fallback)
        return beta, r2, n, fallback

    # Compute 1-min log returns
    stock_prices = np.array([a[0] for a in aligned])
    spy_prices = np.array([a[1] for a in aligned])

    stock_returns = np.diff(np.log(stock_prices))
    spy_returns = np.diff(np.log(spy_prices))

    # Remove any NaN/Inf
    valid = np.isfinite(stock_returns) & np.isfinite(spy_returns)
    stock_returns = stock_returns[valid]
    spy_returns = spy_returns[valid]

    if len(spy_returns) < 30:
        beta, r2, n, fallback = _compute_rolling_intraday_beta(cur, ticker, target_date)
        _cache_beta(cur, ticker, target_date, beta, r2, n, fallback)
        return beta, r2, n, fallback

    # OLS: stock_return = alpha + beta * spy_return
    spy_var = np.var(spy_returns)
    if spy_var < 1e-12:
        beta, r2, n_out, fallback = 1.0, 0.0, len(spy_returns), True
        _cache_beta(cur, ticker, target_date, beta, r2, n_out, fallback)
        return beta, r2, n_out, fallback

    cov = np.cov(stock_returns, spy_returns, ddof=1)
    beta = float(cov[0, 1] / cov[1, 1])
    # R²
    ss_res = np.sum((stock_returns - (np.mean(stock_returns) + beta * (spy_returns - np.mean(spy_returns)))) ** 2)
    ss_tot = np.sum((stock_returns - np.mean(stock_returns)) ** 2)
    r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
    n_out = len(spy_returns)

    # Quality gate: r² ≥ 0.15
    if r2 < 0.15:
        fallback_beta, fallback_r2, fallback_n, _ = _compute_rolling_intraday_beta(cur, ticker, target_date)
        if fallback_r2 >= 0.15:
            _cache_beta(cur, ticker, target_date, fallback_beta, fallback_r2, fallback_n, True)
            return fallback_beta, fallback_r2, fallback_n, True
        # Use static beta as last resort
        static_beta = _get_static_beta(ticker)
        _cache_beta(cur, ticker, target_date, static_beta, r2, n_out, True)
        return static_beta, r2, n_out, True

    _cache_beta(cur, ticker, target_date, beta, r2, n_out, False)
    return beta, r2, n_out, False


def _compute_rolling_intraday_beta(cur, ticker: str, target_date: date) -> Tuple[float, float, int, bool]:
    """Fallback: compute intraday beta from last 5 trading days' 1-min bars."""
    import numpy as np

    start = target_date - timedelta(days=8)  # 8 calendar days ≈ 5 trading days
    end_dt = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)

    cur.execute("""
        SELECT bar_time, close FROM trading.market_bars
        WHERE ticker = %s AND bar_time >= %s AND bar_time < %s
        ORDER BY bar_time
    """, (ticker, start_dt, end_dt))
    stock_bars = cur.fetchall()

    cur.execute("""
        SELECT bar_time, close FROM trading.market_bars
        WHERE ticker = 'SPY' AND bar_time >= %s AND bar_time < %s
        ORDER BY bar_time
    """, (start_dt, end_dt))
    spy_bars = cur.fetchall()

    spy_map = {r['bar_time']: float(r['close']) for r in spy_bars}
    aligned = [(float(sb['close']), spy_map[sb['bar_time']])
               for sb in stock_bars if sb['bar_time'] in spy_map]

    if len(aligned) < 60:
        static_beta = _get_static_beta(ticker)
        return static_beta, 0.0, len(aligned), True

    stock_prices = np.array([a[0] for a in aligned])
    spy_prices = np.array([a[1] for a in aligned])
    stock_returns = np.diff(np.log(stock_prices))
    spy_returns = np.diff(np.log(spy_prices))
    valid = np.isfinite(stock_returns) & np.isfinite(spy_returns)
    stock_returns, spy_returns = stock_returns[valid], spy_returns[valid]

    if len(spy_returns) < 60:
        return _get_static_beta(ticker), 0.0, len(spy_returns), True

    cov = np.cov(stock_returns, spy_returns, ddof=1)
    beta = float(cov[0, 1] / cov[1, 1]) if cov[1, 1] > 1e-12 else 1.0
    ss_res = np.sum((stock_returns - (np.mean(stock_returns) + beta * (spy_returns - np.mean(spy_returns)))) ** 2)
    ss_tot = np.sum((stock_returns - np.mean(stock_returns)) ** 2)
    r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0

    return beta, r2, len(spy_returns), r2 < 0.15


def _get_static_beta(ticker: str) -> float:
    """Last-resort: Yahoo Finance static beta or hardcoded defaults."""
    _STATIC = {
        'TQQQ': 3.0, 'SQQQ': -3.0, 'SOXL': 3.0, 'SOXS': -3.0,
        'SPY': 1.0, 'QQQ': 1.0, 'IWM': 1.0, 'DIA': 1.0,
    }
    if ticker in _STATIC:
        return _STATIC[ticker]
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from monitor.risk_sizing import RiskSizer
        sizer = RiskSizer.__new__(RiskSizer)
        sizer._beta_cache = {}
        return sizer.get_beta(ticker)
    except Exception:
        return 1.0


def _cache_beta(cur, ticker, session_date, beta, r2, n_bars, fallback):
    """Upsert into ml_intraday_beta cache."""
    cur.execute("""
        INSERT INTO trading.ml_intraday_beta
            (ticker, session_date, intraday_beta, r_squared, n_bars, fallback_used)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (ticker, session_date)
        DO UPDATE SET intraday_beta = EXCLUDED.intraday_beta,
                      r_squared = EXCLUDED.r_squared,
                      n_bars = EXCLUDED.n_bars,
                      fallback_used = EXCLUDED.fallback_used
    """, (ticker, session_date, beta, r2, n_bars, fallback))
    cur.connection.commit()


def _classify_session_phase(entry_time) -> str:
    """Classify a timestamp into trading session phase."""
    try:
        from zoneinfo import ZoneInfo
        et = entry_time.astimezone(ZoneInfo('America/New_York'))
    except Exception:
        et = entry_time

    h, m = et.hour, et.minute
    if h == 9 and m >= 30:
        return 'open'
    elif h == 10 or (h == 11 and m < 30):
        return 'morning'
    elif (h == 11 and m >= 30) or h in (12, 13):
        return 'midday'
    elif h == 14 or (h == 15 and m < 30):
        return 'afternoon'
    elif (h == 15 and m >= 30) or h >= 16:
        return 'close'
    return 'premarket'


def job_pnl_attribution(conn, target_date: date) -> int:
    """Job 6: Compute per-trade alpha/beta decomposition.

    For each completed trade on target_date:
    1. Compute realized intraday beta (1-min regression vs SPY)
    2. Decompose P&L into beta (market) and alpha (skill)
    3. Attribute slippage costs
    4. Tag with session phase and regime context
    5. Compute statistical significance per strategy (n ≥ 20 threshold)
    """
    import numpy as np

    log.info("Job 6: ml_pnl_attribution — starting for %s", target_date)
    t0 = time.monotonic()
    day_start, day_end = _date_range(target_date)

    cur = conn.cursor()

    # Step 1: Fetch completed trades for the day
    cur.execute("""
        SELECT trade_id, ticker, strategy, qty, entry_price, exit_price,
               pnl, entry_time, exit_time, duration_seconds, lifecycle_data
        FROM trading.completed_trades
        WHERE exit_time >= %s AND exit_time < %s
        ORDER BY exit_time
    """, (day_start, day_end))
    trades = cur.fetchall()

    if not trades:
        log.info("  No completed trades for %s", target_date)
        return 0

    # Step 2: Fetch SPY bars for the full day (for SPY return lookups)
    cur.execute("""
        SELECT bar_time, close FROM trading.market_bars
        WHERE ticker = 'SPY' AND bar_time >= %s AND bar_time < %s
        ORDER BY bar_time
    """, (day_start, day_end))
    spy_bars = cur.fetchall()
    spy_times = [r['bar_time'] for r in spy_bars]
    spy_closes = [float(r['close']) for r in spy_bars]

    if not spy_bars:
        log.warning("  No SPY bars in market_bars for %s — attribution will use "
                     "beta=fallback, spy_return=0. Fix: verify BarBuilder DatetimeIndex "
                     "and _persist_market_bars on next shutdown.", target_date)

    def _spy_close_at(ts):
        """Find SPY close price nearest to timestamp."""
        if not spy_times or ts is None:
            return None
        import bisect
        # market_bars.bar_time is naive (timestamp without time zone)
        # entry_time/exit_time may be timezone-aware — strip tzinfo for comparison
        try:
            if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            idx = bisect.bisect_right(spy_times, ts) - 1
            idx = max(0, min(idx, len(spy_closes) - 1))
            return spy_closes[idx]
        except TypeError:
            return spy_closes[0] if spy_closes else None

    # Step 3: Compute per-trade attribution
    rows = []
    strategy_alphas = {}  # strategy → list of alpha_returns (for significance)

    for trade in trades:
        ticker = trade['ticker']
        entry_price = _safe_float(trade['entry_price'])
        exit_price = _safe_float(trade['exit_price'])
        pnl = _safe_float(trade['pnl']) or 0.0
        qty = int(trade['qty'] or 1)
        strategy = trade['strategy'] or 'unknown'
        trade_id = trade['trade_id'] or ''
        duration_sec = int(trade['duration_seconds'] or 0)

        if not entry_price or entry_price == 0:
            continue

        # Parse exit_time (always full ISO timestamp)
        exit_time_raw = trade['exit_time'] or ''
        exit_time = None
        if exit_time_raw:
            try:
                exit_time = datetime.fromisoformat(exit_time_raw.replace('Z', '+00:00'))
            except Exception:
                try:
                    from dateutil.parser import parse as _dtparse
                    exit_time = _dtparse(exit_time_raw)
                except Exception:
                    pass

        # Parse entry_time (may be bare "HH:MM:SS" or full ISO or empty)
        entry_time_raw = trade['entry_time'] or ''
        entry_time = None
        if entry_time_raw:
            try:
                if len(entry_time_raw) <= 8 and ':' in entry_time_raw:
                    # Bare time like "11:36:06" is in ET — combine with exit_time's date
                    if exit_time:
                        from zoneinfo import ZoneInfo as _ZI
                        _et_tz = _ZI('America/New_York')
                        parts = entry_time_raw.split(':')
                        h, m = int(parts[0]), int(parts[1])
                        s = int(parts[2]) if len(parts) > 2 else 0
                        # Build datetime in ET, then keep timezone-aware
                        _exit_et = exit_time.astimezone(_et_tz)
                        entry_time = _exit_et.replace(hour=h, minute=m, second=s, microsecond=0)
                else:
                    entry_time = datetime.fromisoformat(entry_time_raw.replace('Z', '+00:00'))
            except Exception:
                pass

        # Fallback: if no entry_time, estimate from duration
        if entry_time is None and exit_time and duration_sec > 0:
            entry_time = exit_time - timedelta(seconds=duration_sec)

        if entry_time is None or exit_time is None:
            continue

        # Trade return
        trade_return = (exit_price - entry_price) / entry_price if exit_price else 0.0

        # SPY return over holding period
        spy_entry = _spy_close_at(entry_time)
        spy_exit = _spy_close_at(exit_time)
        spy_return = ((spy_exit - spy_entry) / spy_entry) if spy_entry and spy_exit and spy_entry != 0 else 0.0

        # Intraday beta
        beta, r2, n_bars, fallback = _compute_intraday_beta(cur, ticker, target_date)

        # Attribution
        notional = entry_price * qty
        beta_pnl = beta * spy_return * notional
        alpha_pnl = pnl - beta_pnl
        alpha_return = trade_return - beta * spy_return

        # Slippage cost (from lifecycle_data if available)
        lc = {}
        if trade['lifecycle_data']:
            lc = _parse_json(trade['lifecycle_data'])
        signal_price = _safe_float(lc.get('signal_price') or lc.get('entry_signal_price'))
        fill_price = entry_price
        slippage_cost = abs(signal_price - fill_price) * qty if signal_price else 0.0

        gross_alpha_pnl = alpha_pnl + slippage_cost
        net_alpha_pnl = alpha_pnl

        # Session phase
        session_phase = _classify_session_phase(entry_time)

        # Regime context from lifecycle_data (prefer entry, fall back to close)
        regime_entry = lc.get('regime_at_entry') or lc.get('regime_at_close') or {}
        if isinstance(regime_entry, str):
            try:
                regime_entry = _parse_json(regime_entry)
            except Exception:
                regime_entry = {}
        regime_trend = _safe_float(regime_entry.get('trend'))
        regime_vrp = _safe_float(regime_entry.get('vrp'))
        regime_participation = _safe_float(regime_entry.get('participation'))

        # Collect for significance test
        strategy_alphas.setdefault(strategy, []).append(alpha_return)

        rows.append({
            'session_date': target_date,
            'trade_id': trade_id,
            'ticker': ticker,
            'strategy': strategy,
            'qty': qty,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'realized_pnl': round(pnl, 4),
            'trade_return': round(trade_return, 6),
            'spy_return': round(spy_return, 6),
            'intraday_beta': round(beta, 4),
            'beta_pnl': round(beta_pnl, 4),
            'alpha_pnl': round(alpha_pnl, 4),
            'alpha_return': round(alpha_return, 6),
            'slippage_cost': round(slippage_cost, 4),
            'gross_alpha_pnl': round(gross_alpha_pnl, 4),
            'net_alpha_pnl': round(net_alpha_pnl, 4),
            'session_phase': session_phase,
            'regime_trend': regime_trend,
            'regime_vrp': regime_vrp,
            'regime_participation': regime_participation,
            'alpha_t_stat': None,  # filled below
            'alpha_p_value': None,
            'entry_time': entry_time,
            'exit_time': exit_time,
            'duration_sec': duration_sec,
        })

    # Step 4: Per-strategy significance (rolling 20-day window)
    # Fetch historical alpha_returns per strategy from DB
    strategy_significance = {}
    for strategy in strategy_alphas:
        # Get historical alphas from last 20 trading days
        cur.execute("""
            SELECT alpha_return FROM trading.ml_pnl_attribution
            WHERE strategy = %s AND session_date >= %s AND session_date < %s
            ORDER BY session_date, entry_time
        """, (strategy, target_date - timedelta(days=30), target_date))
        historical = [float(r['alpha_return']) for r in cur.fetchall()
                      if r['alpha_return'] is not None]
        # Combine with today's
        all_alphas = historical + strategy_alphas[strategy]

        if len(all_alphas) >= 20:
            arr = np.array(all_alphas)
            mean_a = np.mean(arr)
            std_a = np.std(arr, ddof=1)
            n_a = len(arr)
            if std_a > 1e-10:
                t_stat = float(mean_a / (std_a / np.sqrt(n_a)))
                # Two-tailed p-value from t-distribution
                try:
                    from scipy.stats import t as t_dist
                    p_value = float(t_dist.sf(abs(t_stat), df=n_a - 1) * 2)
                except ImportError:
                    # Approximate with normal for large n
                    from math import erfc, sqrt
                    p_value = float(erfc(abs(t_stat) / sqrt(2)))
                strategy_significance[strategy] = (round(t_stat, 4), round(p_value, 4))
            else:
                strategy_significance[strategy] = (0.0, 1.0)
        # else: leave as None (ACCUMULATING)

    # Stamp significance on rows
    for row in rows:
        sig = strategy_significance.get(row['strategy'])
        if sig:
            row['alpha_t_stat'] = sig[0]
            row['alpha_p_value'] = sig[1]

    # Step 5: Upsert into ml_pnl_attribution
    inserted = 0
    for row in rows:
        cur.execute("""
            INSERT INTO trading.ml_pnl_attribution (
                session_date, trade_id, ticker, strategy, qty,
                entry_price, exit_price, realized_pnl,
                trade_return, spy_return, intraday_beta,
                beta_pnl, alpha_pnl, alpha_return,
                slippage_cost, gross_alpha_pnl, net_alpha_pnl,
                session_phase, regime_trend, regime_vrp, regime_participation,
                alpha_t_stat, alpha_p_value,
                entry_time, exit_time, duration_sec
            ) VALUES (
                %(session_date)s, %(trade_id)s, %(ticker)s, %(strategy)s, %(qty)s,
                %(entry_price)s, %(exit_price)s, %(realized_pnl)s,
                %(trade_return)s, %(spy_return)s, %(intraday_beta)s,
                %(beta_pnl)s, %(alpha_pnl)s, %(alpha_return)s,
                %(slippage_cost)s, %(gross_alpha_pnl)s, %(net_alpha_pnl)s,
                %(session_phase)s, %(regime_trend)s, %(regime_vrp)s, %(regime_participation)s,
                %(alpha_t_stat)s, %(alpha_p_value)s,
                %(entry_time)s, %(exit_time)s, %(duration_sec)s
            )
            ON CONFLICT (session_date, trade_id) DO UPDATE SET
                alpha_pnl = EXCLUDED.alpha_pnl,
                beta_pnl = EXCLUDED.beta_pnl,
                intraday_beta = EXCLUDED.intraday_beta,
                alpha_t_stat = EXCLUDED.alpha_t_stat,
                alpha_p_value = EXCLUDED.alpha_p_value
        """, row)
        inserted += 1

    conn.commit()

    # Summary
    total_alpha = sum(r['alpha_pnl'] for r in rows)
    total_beta = sum(r['beta_pnl'] for r in rows)
    total_pnl = sum(r['realized_pnl'] for r in rows)
    elapsed = time.monotonic() - t0

    log.info("  Job 6 complete: %d trades, P&L=$%.2f (alpha=$%.2f + beta=$%.2f), %.1fs",
             inserted, total_pnl, total_alpha, total_beta, elapsed)
    return inserted


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════

def run_all_jobs(target_date: date) -> Dict[str, int]:
    """Run all five batch jobs for a single date. Returns counts per job."""
    log.info("=" * 70)
    log.info("Post-session analytics for %s", target_date)
    log.info("=" * 70)

    t0 = time.monotonic()
    results: Dict[str, int] = {}

    conn = get_conn()
    try:
        results["trade_outcomes"] = job_trade_outcomes(conn, target_date)
        results["signal_context"] = job_signal_context(conn, target_date)
        results["rejection_log"] = job_rejection_log(conn, target_date)
        results["iv_history"] = job_iv_history(conn, target_date)
        results["daily_regime"] = job_daily_regime(conn, target_date)
        results["pnl_attribution"] = job_pnl_attribution(conn, target_date)
    except Exception:
        conn.rollback()
        log.exception("Fatal error during analytics for %s", target_date)
        raise
    finally:
        conn.close()

    total_elapsed = time.monotonic() - t0
    log.info("-" * 70)
    log.info("All jobs complete for %s in %.1fs", target_date, total_elapsed)
    for job_name, count in results.items():
        log.info("  %-20s: %d rows", job_name, count)
    log.info("-" * 70)

    return results


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Post-session ML analytics batch jobs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                     # Run for today
  %(prog)s --date 2026-04-13   # Run for a specific date
  %(prog)s --backfill 30       # Run for the last 30 days
        """,
    )
    parser.add_argument(
        "--date", "-d",
        type=str,
        default=None,
        help="Target date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--backfill", "-b",
        type=int,
        default=None,
        help="Backfill the last N days (inclusive of today)",
    )
    parser.add_argument(
        "--job",
        type=str,
        default=None,
        choices=["trade_outcomes", "signal_context", "rejection_log",
                 "iv_history", "daily_regime"],
        help="Run only a specific job (default: all)",
    )
    args = parser.parse_args()

    # Determine dates to process
    if args.backfill:
        today = date.today()
        dates = [today - timedelta(days=i) for i in range(args.backfill)]
        dates.reverse()  # oldest first
        log.info("Backfilling %d days: %s to %s", len(dates), dates[0], dates[-1])
    elif args.date:
        try:
            target = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            log.error("Invalid date format: %s (expected YYYY-MM-DD)", args.date)
            sys.exit(1)
        dates = [target]
    else:
        dates = [date.today()]

    # Run
    grand_total: Dict[str, int] = {}
    total_t0 = time.monotonic()

    for target_date in dates:
        if args.job:
            # Run single job
            conn = get_conn()
            try:
                job_fn = {
                    "trade_outcomes": job_trade_outcomes,
                    "signal_context": job_signal_context,
                    "rejection_log": job_rejection_log,
                    "iv_history": job_iv_history,
                    "daily_regime": job_daily_regime,
                    "pnl_attribution": job_pnl_attribution,
                }[args.job]
                count = job_fn(conn, target_date)
                grand_total[args.job] = grand_total.get(args.job, 0) + count
            except Exception:
                conn.rollback()
                log.exception("Error running %s for %s", args.job, target_date)
            finally:
                conn.close()
        else:
            try:
                results = run_all_jobs(target_date)
                for k, v in results.items():
                    grand_total[k] = grand_total.get(k, 0) + v
            except Exception:
                log.exception("Error processing %s — continuing", target_date)

    total_elapsed = time.monotonic() - total_t0

    if len(dates) > 1:
        log.info("=" * 70)
        log.info("BACKFILL COMPLETE: %d days in %.1fs", len(dates), total_elapsed)
        for job_name, count in grand_total.items():
            log.info("  %-20s: %d total rows", job_name, count)
        log.info("=" * 70)


if __name__ == "__main__":
    main()
