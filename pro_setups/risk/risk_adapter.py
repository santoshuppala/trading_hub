"""
RiskAdapter — tier-aware risk gate for the pro_setups subsystem.

Wraps tier-based stop/exit logic into the existing RiskEngine OUTPUT format
(OrderRequestPayload) without modifying RiskEngine itself.

Responsibilities
----------------
1. Enforce max concurrent pro-setup positions.
2. Enforce per-ticker order cooldown.
3. Prevent duplicate positions in the same ticker.
4. Validate minimum R:R ratio per tier.
5. Size position using risk-based sizing (2% of budget per trade).
6. Emit ORDER_REQ (durable=True) → existing AlpacaBroker executes.
7. Track open pro positions via FILL and POSITION events.

The existing RiskEngine is NOT bypassed — ORDER_REQ is the shared
protocol.  AlpacaBroker does not distinguish between ORDER_REQ sources.

Execution credentials: APCA_API_KEY_ID / APCA_API_SECRET_KEY (main account).
Same account as the VWAP Reclaim strategy.  Max-positions cap shared via
POSITION events ensures no oversized book.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Set

from monitor.event_bus import EventBus, EventType, Event
from monitor.events import OrderRequestPayload, Side, PositionAction
from monitor.order_wal import wal
from monitor.sector_map import get_sector, count_sector_positions

log = logging.getLogger(__name__)


# ── Tier-specific minimum R:R ratios ─────────────────────────────────────────
_MIN_RR: Dict[int, float] = {1: 1.5, 2: 2.0, 3: 3.5}

# Minimum confidence by tier (blocks low-quality signals)
_MIN_CONFIDENCE: Dict[int, float] = {1: 0.50, 2: 0.60, 3: 0.70}

# Signal deduplication: same (ticker, strategy) within N seconds
_DEDUP_WINDOW_SEC: float = 60.0

# Risk per trade: 2% of budget (adjustable)
_RISK_PCT: float = 0.02


class RiskAdapter:
    """
    Independent risk gate for pro_setups.

    All state is local — ``_positions`` and ``_last_order`` are maintained
    by listening to FILL and POSITION events on the shared bus.

    Parameters
    ----------
    bus            : shared EventBus instance
    max_positions  : maximum concurrent pro-setup open positions
    order_cooldown : seconds between two orders on the same ticker
    trade_budget   : total dollars allocated to pro-setup trades
    """

    def __init__(
        self,
        bus:            EventBus,
        max_positions:  int,
        order_cooldown: int,
        trade_budget:   float,
        shared_positions: Optional[Dict] = None,
    ) -> None:
        self._bus            = bus
        self._max_positions  = max_positions
        self._order_cooldown = order_cooldown
        self._trade_budget   = trade_budget
        # V8: Shared positions dict from monitor (for cross-engine sector counting)
        self._shared_positions: Optional[Dict] = shared_positions

        self._positions: Set[str]         = set()    # tickers with open pro positions
        self._last_order: Dict[str, float] = {}       # ticker → monotonic timestamp
        self._last_signal: Dict[str, float] = {}      # (ticker, strategy) → monotonic timestamp (dedup)

        # Subscribe at priority=0 (after routing handlers) to track state
        bus.subscribe(EventType.FILL,       self._on_fill,       priority=0)
        bus.subscribe(EventType.POSITION,   self._on_position,   priority=0)
        # V8: Clean up _positions on ORDER_FAIL (prevents permanent ticker lock)
        bus.subscribe(EventType.ORDER_FAIL, self._on_order_fail, priority=0)
        log.info(
            "[RiskAdapter] ready  max_pos=%d  cooldown=%ds  budget=$%.0f",
            max_positions, order_cooldown, trade_budget,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def validate_and_emit(
        self,
        ticker:        str,
        direction:     str,
        entry_price:   float,
        stop_price:    float,
        target_1:      float,
        target_2:      float,
        atr_value:     float,
        tier:          int,
        strategy_name: str,
        confidence:    float,
        source_event:  Event,
    ) -> None:
        """
        Run all risk checks.  If all pass, size the position and emit
        ORDER_REQ (durable=True) → AlpacaBroker.

        All blocked signals are logged with the reason so they appear in
        the daily log for review.
        """
        tag = f"[RiskAdapter][{ticker}][{strategy_name}][T{tier}]"

        # ── Check 0a: V10 WAL dedup — block if pending BUY already active ────
        # Prevents SNAP/RIOT duplicate entries (ORB + sr_flip both fire for same ticker)
        try:
            from monitor.order_wal import wal
            if wal.has_pending_order(ticker, 'BUY'):
                log.info("%s BLOCKED: WAL dedup — pending BUY already active", tag)
                return
        except Exception:
            pass  # WAL unavailable — proceed without dedup

        # ── Check 0b: cross-layer dedup (READ-ONLY pre-flight) ────────────────
        # V7: Satellite does NOT write to registry. Core's RegistryGate acquires.
        # This is a fast read-only check to avoid sending unnecessary ORDER_REQs.
        from monitor.position_registry import registry
        holder = registry.held_by(ticker)
        if holder and holder != 'pro':
            log.info(
                "%s BLOCKED (pre-flight): held by layer '%s'",
                tag, holder,
            )
            return

        # ── Check 1: only long entries go through (short execution not yet wired) ──
        if direction != 'long':
            log.info("%s SKIPPED: direction=%s (short execution not enabled)", tag, direction)
            return

        # V7: No registry release needed — satellite doesn't own the lock
        _approved = False
        try:
            # V7.1: Conviction boost from alt data (Finviz + SEC + Yahoo)
            # Only boosts (0.0 to +0.3), never penalizes. Helps marginal setups pass.
            try:
                from data_sources.market_regime import conviction
                boost = conviction.score(ticker)
                if boost > 0:
                    log.info("%s conviction boost: +%.2f (%s)",
                             tag, boost, conviction.detail(ticker))
                    confidence = min(1.0, confidence + boost)
            except Exception as exc:
                log.warning("%s conviction import failed: %s", tag, exc)

            # ── Check 1b: minimum confidence by tier ─────────────────────────────
            min_conf = _MIN_CONFIDENCE.get(tier, 0.50)
            if confidence < min_conf:
                log.info(
                    "%s BLOCKED: confidence=%.2f < min %.2f for T%d",
                    tag, confidence, min_conf, tier,
                )
                return

            # ── Check 1c: signal deduplication (same ticker+strategy within 60s) ─
            dedup_key = f"{ticker}:{strategy_name}"
            now_dedup = time.monotonic()
            last_sig_time = self._last_signal.get(dedup_key, 0.0)
            if now_dedup - last_sig_time < _DEDUP_WINDOW_SEC:
                log.debug(
                    "%s BLOCKED: duplicate signal within %.0fs",
                    tag, _DEDUP_WINDOW_SEC,
                )
                return
            self._last_signal[dedup_key] = now_dedup

            # ── Check 2: max concurrent positions ─────────────────────────────────
            if len(self._positions) >= self._max_positions:
                log.info(
                    "%s BLOCKED: max_positions=%d reached (open: %s)",
                    tag, self._max_positions, sorted(self._positions),
                )
                return

            # ── Check 2b: sector concentration limit (max 2 per sector) ──────────
            # V8: Use shared positions dict for cross-engine sector counting
            _MAX_PER_SECTOR = 2
            sector = get_sector(ticker)
            all_held = set(self._positions)
            if self._shared_positions:
                all_held |= set(self._shared_positions.keys())
            sector_counts = count_sector_positions(all_held)
            if sector_counts.get(sector, 0) >= _MAX_PER_SECTOR:
                log.info("%s BLOCKED: sector %s already has %d positions", tag, sector, sector_counts[sector])
                return

            # ── Check 3: per-ticker cooldown ──────────────────────────────────────
            now     = time.monotonic()
            elapsed = now - self._last_order.get(ticker, 0.0)
            if elapsed < self._order_cooldown:
                log.info(
                    "%s BLOCKED: cooldown %.0fs remaining",
                    tag, self._order_cooldown - elapsed,
                )
                return

            # ── Check 4: no duplicate position ────────────────────────────────────
            if ticker in self._positions:
                log.info("%s BLOCKED: already holding position", tag)
                return

            # ── V8 Check 4b: Earnings block for T2/T3 (overnight positions) ────────
            if tier >= 2:
                try:
                    from data_sources.alt_data_reader import alt_data
                    earnings = alt_data.earnings(ticker)
                    if earnings and earnings.get('days_until_earnings', 999) <= 2:
                        log.info("%s BLOCKED: earnings in %d days (T%d holds overnight)",
                                 tag, earnings['days_until_earnings'], tier)
                        return
                except Exception:
                    pass  # alt data unavailable — allow entry

            # ── Check 5: minimum R:R ──────────────────────────────────────────────
            risk   = entry_price - stop_price       # always positive for long
            reward = target_2    - entry_price
            if risk <= 0:
                log.warning("%s BLOCKED: risk=%.4f (stop above entry?)", tag, risk)
                return
            rr = reward / risk
            min_rr = _MIN_RR.get(tier, 1.5)
            if rr < min_rr:
                log.info(
                    "%s BLOCKED: R:R=%.2f < min %.2f  (risk=%.4f reward=%.4f)",
                    tag, rr, min_rr, risk, reward,
                )
                return

            # ── Check 6: ATR sanity ───────────────────────────────────────────────
            if atr_value <= 0:
                log.warning("%s BLOCKED: atr_value=%.4f invalid", tag, atr_value)
                return

            # ── Position sizing (risk-based) ──────────────────────────────────────
            max_dollar_risk = self._trade_budget * _RISK_PCT
            qty_by_risk     = int(max_dollar_risk / risk)
            qty_by_budget   = int(self._trade_budget / entry_price)
            qty             = min(max(qty_by_risk, 1), max(qty_by_budget, 1))

            # V8: Conviction-based position sizing (from TickerRankingEngine)
            try:
                from data_sources.ticker_ranking import _engine as _ranking_engine
                cached = _ranking_engine._cache.get(ticker)
                if cached:
                    _, ranking = cached
                    conv_mult = ranking.size_multiplier
                    if conv_mult != 1.0:
                        qty = max(1, int(qty * conv_mult))
                        log.info("%s conviction sizing: conv=%.2f mult=%.2f (%s)",
                                 tag, ranking.conviction, conv_mult, ranking.catalyst)
            except Exception:
                pass

            # V7.1: Market regime sizing (F&G + VIX)
            try:
                from data_sources.market_regime import regime
                mult = regime.position_size_multiplier()
                if mult < 1.0:
                    qty = max(1, int(qty * mult))
            except Exception as exc:
                log.warning("%s market regime import failed: %s", tag, exc)

            if qty <= 0:
                log.warning("%s BLOCKED: qty=0 (budget=%.0f entry=%.4f)", tag, self._trade_budget, entry_price)
                return

            _approved = True
        finally:
            pass  # V7: No registry release — Core owns the lock

        # ── Emit ORDER_REQ ────────────────────────────────────────────────────────
        reason  = f"pro:{strategy_name}:T{tier}:long"
        payload = OrderRequestPayload(
            ticker           = ticker,
            side             = Side.BUY,
            qty              = qty,
            price            = round(entry_price, 2),
            reason           = reason,
            needs_ask_refresh= True,
            stop_price       = round(stop_price,  4),
            target_price     = round(target_2,    4),
            atr_value        = round(atr_value,   4),
            layer            = 'pro',  # V7: Core acquires registry
        )

        log.info(
            "%s ENTRY  ▶  dir=long  entry=%.4f  stop=%.4f  "
            "target1=%.4f  target2=%.4f  qty=%d  R:R=%.2f  "
            "risk_$=%.2f  conf=%.0f%%  ATR=%.4f",
            tag,
            entry_price, stop_price,
            target_1, target_2,
            qty, rr,
            risk * qty,
            confidence * 100,
            atr_value,
        )

        # Mark cooldown immediately to prevent duplicate orders this tick
        self._last_order[ticker] = now
        self._positions.add(ticker)

        # V10: WAL INTENT — record before submitting order
        _wal_cid = wal.new_client_id()
        wal.intent(_wal_cid, ticker=ticker, side=str(payload.side.value),
                   qty=qty, price=entry_price,
                   reason=f'pro:{strategy_name}:{tag}')

        _order_event = Event(
            type           = EventType.ORDER_REQ,
            payload        = payload,
            correlation_id = source_event.event_id,
        )
        _order_event._wal_client_id = _wal_cid
        self._bus.emit(_order_event, durable=True)

    # ── Event handlers ────────────────────────────────────────────────────────

    def _on_fill(self, event: Event) -> None:
        p = event.payload
        if not p.reason.startswith('pro:'):
            return   # not a pro-setup fill
        if p.side == Side.SELL:
            self._positions.discard(p.ticker)
            log.info(
                "[RiskAdapter][%s] EXIT fill  side=sell  qty=%d  "
                "fill=%.4f  reason=%s",
                p.ticker, p.qty, p.fill_price, p.reason,
            )

    def _on_position(self, event: Event) -> None:
        p = event.payload
        if p.action == PositionAction.CLOSED:
            # V8: Only remove if this was a Pro position (not VWAP).
            # Without this check, VWAP closing AAPL would remove it from
            # Pro's _positions even if Pro also holds AAPL.
            cd = getattr(p, 'close_detail', {}) or {}
            strategy = cd.get('strategy', '')
            if strategy.startswith('pro:') or p.ticker in self._positions:
                self._positions.discard(p.ticker)
                log.debug(
                    "[RiskAdapter][%s] position CLOSED (strategy=%s) — removed from tracking",
                    p.ticker, strategy,
                )

    def _on_order_fail(self, event: Event) -> None:
        """V8: Clean up _positions and _last_order on BUY ORDER_FAIL.
        Without this, a rejected Pro BUY leaves the ticker locked forever
        in _positions (blocks future entries + inflates position count)."""
        try:
            p = event.payload
            reason = str(getattr(p, 'reason', '')).lower()
            side = str(getattr(p, 'side', '')).upper()
            if side == 'BUY' and reason.startswith('pro:'):
                if p.ticker in self._positions:
                    self._positions.discard(p.ticker)
                    self._last_order.pop(p.ticker, None)
                    log.info("[RiskAdapter] Cleaned up %s after ORDER_FAIL", p.ticker)
        except Exception:
            pass
