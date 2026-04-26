"""
T3 — Risk Engine
================
Pre-trade gate that sits between the Strategy Engine and the Broker.

Responsibilities
----------------
  BUY signals  — run all 7 pre-trade checks; emit ORDER_REQ on pass or
                 RISK_BLOCK on fail.
  SELL signals — pass straight through as ORDER_REQ; exits never get gated.

Pre-trade checks (buy only)
---------------------------
  1. Max positions        — reject if portfolio already full.
  2. Cooldown             — reject if the ticker was ordered too recently.
  3. Reclaimed-today      — reject if this ticker already had a reclaim entry today.
  4. RVOL                 — reject if relative volume < MIN_RVOL (momentum confirmation).
  5. RSI range            — reject if RSI is outside the bullish band (RSI_LOW–RSI_HIGH).
  6. Spread               — fetch a fresh Level-1 quote; reject if bid/ask spread is too
                            wide (MAX_SPREAD_PCT).  Also computes the live ask for sizing.
  7. Correlation risk     — reject if too many correlated positions already held.

Sizing
------
  qty = max(1, floor(trade_budget / (ask * (1 + SLIPPAGE_PCT))))
  Then adjusted for beta and ATR volatility (via RiskSizer).

Events consumed
---------------
  EventType.SIGNAL  (payload: SignalPayload)

Events emitted
--------------
  EventType.ORDER_REQ   — payload: OrderRequestPayload  (buy or sell)
  EventType.RISK_BLOCK  — payload: RiskBlockPayload     (buy blocked)

Usage
-----
    from .risk_engine import RiskEngine

    engine = RiskEngine(
        bus=bus,
        positions=positions,          # shared dict — mutated by PositionManager
        reclaimed_today=reclaimed,    # shared set
        last_order_time=order_times,  # shared dict
        data_client=data_client,
        max_positions=5,
        order_cooldown=300,
        trade_budget=1000,
        alert_email=email,
    )
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional, Set, Dict

from .alerts import send_alert
from .event_bus import Event, EventBus, EventType
from .order_wal import wal
from .events import (
    OrderRequestPayload,
    RiskBlockPayload,
    SignalPayload,
)
from .risk_sizing import RiskSizer

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

import os as _os

def _safe_float(env_key: str, default: float) -> float:
    try:
        return float(_os.getenv(env_key, str(default)))
    except (ValueError, TypeError):
        return default

def _safe_int(env_key: str, default: int) -> int:
    try:
        return int(_os.getenv(env_key, str(default)))
    except (ValueError, TypeError):
        return default

SLIPPAGE_PCT   = 0.0001
MAX_SPREAD_PCT = _safe_float('RISK_MAX_SPREAD_PCT', 0.002)
MIN_RVOL       = _safe_float('RISK_MIN_RVOL', 2.0)
RSI_LOW        = _safe_float('RISK_RSI_LOW', 40.0)
RSI_HIGH       = _safe_float('RISK_RSI_HIGH', 75.0)


class RiskEngine:
    """
    Event-driven pre-trade gate.

    Subscribes to SIGNAL events on construction; no external wiring needed.
    Shares mutable state (positions, reclaimed_today, last_order_time) with
    the PositionManager — both components receive references to the same dicts
    so updates are always visible.
    """

    def __init__(
        self,
        bus: EventBus,
        positions: dict,
        reclaimed_today: Set[str],
        last_order_time: Dict[str, float],
        data_client,
        max_positions: int = 5,
        order_cooldown: int = 300,
        trade_budget: float = 1000.0,
        alert_email: Optional[str] = None,
    ):
        self._bus             = bus
        self._positions       = positions
        self._reclaimed_today = reclaimed_today
        self._last_order_time = last_order_time
        self._data            = data_client
        self._max_positions   = max_positions
        self._order_cooldown  = order_cooldown
        self._trade_budget    = trade_budget
        # V9: Optional streaming client for instant price lookups
        self._stream_client   = None  # set via set_stream_client()
        self._stream_lock     = threading.Lock()  # V10: protects _stream_client reference
        self._alert_email     = alert_email
        self._sizer           = RiskSizer()
        # V10: Track pending (in-flight) BUY orders to prevent position limit bypass
        # Ticker added when ORDER_REQ emitted, removed on FILL or ORDER_FAIL
        # Dict {ticker: monotonic_time} for TTL eviction (60s) if events are lost
        self._pending_tickers: dict = {}
        self._pending_lock = threading.Lock()
        self._PENDING_TTL = 60.0  # seconds before auto-evicting stale pending

        # V10: Read-only mode — signals generate, exits run, but no new BUY orders
        self._read_only = _os.getenv('READ_ONLY', '').lower() in ('true', '1', 'yes')
        if self._read_only:
            log.warning("[RiskEngine] READ-ONLY MODE — BUY orders will be blocked")

        bus.subscribe(EventType.SIGNAL, self._on_signal)
        bus.subscribe(EventType.FILL, self._on_fill_clear_pending, priority=99)
        bus.subscribe(EventType.ORDER_FAIL, self._on_fail_clear_pending, priority=99)

    # ── Event handler ────────────────────────────────────────────────────────

    def _on_signal(self, event: Event) -> None:
        p: SignalPayload = event.payload

        if p.action == 'BUY':
            # V10: Read-only mode blocks new entries (exits still run for safety)
            if self._read_only:
                log.info("[RiskEngine] READ-ONLY: blocked BUY for %s", p.ticker)
                return
            self._handle_buy(p, event)
        else:
            # Sell signals bypass all risk checks — exits must always execute.
            self._emit_sell_order(p)

    # ── Buy gate ─────────────────────────────────────────────────────────────

    def _handle_buy(self, p: SignalPayload, event: Event) -> None:
        ticker = p.ticker

        # 0. Cross-layer dedup (READ-ONLY pre-flight)
        # V7: Core's RegistryGate handles the actual acquire when ORDER_REQ
        # reaches the bus. This is a fast pre-flight to avoid unnecessary work.
        from .position_registry import registry
        holder = registry.held_by(ticker)
        if holder and holder != 'vwap':
            self._block(ticker, p.action,
                        f"held by layer '{holder}' (pre-flight)", event)
            return

        # V7: No registry release needed — RegistryGate owns acquire/release
        _approved = False
        try:
            # 1. Max positions — V8: count only VWAP positions (not Pro/Pop)
            # Pro positions share the same dict but have strategy='pro:...'
            # Without this filter, 5 Pro positions would block all VWAP entries
            # Snapshot to avoid RuntimeError if dict changes during iteration
            _positions_snap = list(self._positions.values())
            vwap_count = sum(1 for pos in _positions_snap
                             if not str(pos.get('strategy', '')).startswith('pro:'))
            # V10: Include pending (in-flight) orders to prevent limit bypass
            # Evict stale entries (>60s — FILL/ORDER_FAIL event was lost)
            with self._pending_lock:
                _now = time.monotonic()
                _stale = [t for t, ts in self._pending_tickers.items()
                          if _now - ts > self._PENDING_TTL]
                for t in _stale:
                    del self._pending_tickers[t]
                    log.warning("[RiskEngine] Evicted stale pending ticker %s (>%.0fs)",
                                t, self._PENDING_TTL)
                vwap_count += len(self._pending_tickers)
            if vwap_count >= self._max_positions:
                self._block(ticker, p.action,
                            f"max VWAP positions reached ({vwap_count}/{self._max_positions})",
                            event)
                return

            # 2. Cooldown
            # V10: Use monotonic clock for cooldown (immune to DST/NTP jumps)
            elapsed = time.monotonic() - self._last_order_time.get(ticker, 0.0)
            if elapsed < self._order_cooldown:
                remaining = int(self._order_cooldown - elapsed)
                self._block(ticker, p.action,
                            f"cooldown active ({remaining}s remaining)", event)
                return

            # 3. Reclaimed today
            if ticker in self._reclaimed_today:
                self._block(ticker, p.action,
                            "already reclaimed today", event)
                return

            # V8: Read alt data sentiment for threshold relaxation
            _sentiment_score = 0.0
            try:
                from data_sources.alt_data_reader import alt_data
                sent = alt_data.sentiment(ticker)
                if sent and isinstance(sent, dict):
                    _sentiment_score = float(sent.get('score', 0))
            except Exception:
                pass

            # 4. RVOL (lower threshold for ETFs — they never hit 2.0x)
            # V8: Relax RVOL threshold from 2.0→1.5 when sentiment > 0.6
            from monitor.sector_map import is_etf, ETF_RVOL_MIN
            rvol_threshold = ETF_RVOL_MIN if is_etf(ticker) else MIN_RVOL
            if _sentiment_score > 0.6:
                rvol_threshold = min(rvol_threshold, 1.5)
            if p.rvol < rvol_threshold:
                self._block(ticker, p.action,
                            f"RVOL too low ({p.rvol:.2f} < {rvol_threshold})", event)
                return

            # 5. RSI range
            # V8: Relax RSI range from 40-75 → 35-80 when sentiment > 0.6
            rsi_low = RSI_LOW
            rsi_high = RSI_HIGH
            if _sentiment_score > 0.6:
                rsi_low = 35.0
                rsi_high = 80.0
            if not (rsi_low <= p.rsi_value <= rsi_high):
                self._block(ticker, p.action,
                            f"RSI out of bullish band ({p.rsi_value:.1f}, "
                            f"need {rsi_low}–{rsi_high})", event)
                return

            # 6. Spread — fetch fresh Level-1 quote
            spread_pct, ask_price = self._get_spread(ticker, p.ask_price)
            if ask_price is None:
                self._block(ticker, p.action,
                            "live quote unavailable — cannot verify spread", event)
                return
            if spread_pct > MAX_SPREAD_PCT:
                self._block(ticker, p.action,
                            f"spread too wide ({spread_pct:.3%} > {MAX_SPREAD_PCT:.3%})",
                            event)
                return

            # 7. Correlation risk (news-aware: ticker-specific catalysts override)
            news_ctx = None
            try:
                # Build news context from the signal payload if available
                news_ctx = {
                    'headlines_1h': getattr(p, 'headlines_1h', 0) or 0,
                    'sentiment_delta': getattr(p, 'sentiment_delta', 0) or 0,
                    'is_ticker_specific': False,  # let heuristics decide
                }
            except Exception:
                pass
            corr_ok, corr_reason = self._sizer.check_correlation(
                ticker, set(list(self._positions.keys())), news_context=news_ctx,
            )
            if not corr_ok:
                self._block(ticker, p.action, corr_reason, event)
                return

            # V8: Beta-weighted exposure check
            beta_ok, beta_reason, _ = self._sizer.check_beta_exposure(
                self._positions, ticker,
                new_qty=max(1, int(self._trade_budget / (p.current_price or 1))),
                new_price=p.current_price or 0,
            )
            if not beta_ok:
                self._block(ticker, p.action, beta_reason, event)
                return

            # ── All checks passed — size and submit ──────────────────────────
            effective_entry = ask_price * (1 + SLIPPAGE_PCT)
            qty = max(1, int(self._trade_budget / effective_entry))

            # Adjust size for beta and volatility
            sizing = self._sizer.adjust_size(
                ticker=ticker, base_qty=qty, price=ask_price,
                atr_value=getattr(p, 'atr_value', 0),
                trade_budget=self._trade_budget,
            )
            if sizing.adjusted_qty != qty:
                log.info("[RiskEngine] Size adjusted %s: %d → %d (%s)",
                         ticker, qty, sizing.adjusted_qty, sizing.adjustment_reason)
                qty = sizing.adjusted_qty

            # V8: Conviction-based position sizing
            # Higher conviction = larger position (from TickerRankingEngine)
            try:
                from data_sources.ticker_ranking import _engine as _ranking_engine
                cached = _ranking_engine._cache.get(ticker)
                if cached:
                    _, ranking = cached
                    conv_mult = ranking.size_multiplier
                    if conv_mult != 1.0:
                        old_qty = qty
                        qty = max(1, int(qty * conv_mult))
                        log.info("[RiskEngine] Conviction sizing %s: %d → %d "
                                 "(conv=%.2f, %s, %s)",
                                 ticker, old_qty, qty, ranking.conviction,
                                 ranking.strategy, ranking.catalyst)
            except Exception:
                pass

            # V7.1: Market regime position sizing (F&G + VIX)
            try:
                from data_sources.market_regime import regime
                mult = regime.position_size_multiplier()
                if mult < 1.0:
                    old_qty = qty
                    qty = max(1, int(qty * mult))
                    log.info("[RiskEngine] Regime sizing %s: %d → %d (%s)",
                             ticker, old_qty, qty, regime.summary())
            except Exception as exc:
                log.warning("[RiskEngine] Market regime import failed: %s", exc)

            log.info(
                f"[RiskEngine] BUY approved: {ticker} "
                f"qty={qty} ask=${ask_price:.2f} entry=${effective_entry:.2f} "
                f"rvol={p.rvol:.1f}x rsi={p.rsi_value:.1f} spread={spread_pct:.3%}"
            )

            # V10: WAL INTENT — record intent before submitting order
            _wal_cid = wal.new_client_id()
            wal.intent(_wal_cid, ticker=ticker, side='BUY', qty=qty,
                       price=effective_entry, reason='VWAP reclaim')

            _order_event = Event(
                type=EventType.ORDER_REQ,
                payload=OrderRequestPayload(
                    ticker=ticker,
                    side='BUY',
                    qty=qty,
                    price=effective_entry,
                    reason='VWAP reclaim',
                    needs_ask_refresh=p.needs_ask_refresh,
                    # V8: Default to percentage-based if signal values are None
                    stop_price=p.stop_price if p.stop_price else effective_entry * 0.97,
                    target_price=p.target_price if p.target_price else effective_entry * 1.05,
                    atr_value=p.atr_value if p.atr_value else 0,
                    layer='vwap',  # V7: RegistryGate acquires
                ),
                correlation_id=event.event_id,
            )
            _order_event._wal_client_id = _wal_cid  # carry WAL ID downstream

            # V10: Track as pending before emit (cleared on FILL/ORDER_FAIL)
            with self._pending_lock:
                self._pending_tickers[ticker] = time.monotonic()

            self._bus.emit(_order_event)
            _approved = True
        finally:
            pass  # V7: RegistryGate handles acquire/release

    # ── Sell pass-through ────────────────────────────────────────────────────

    def _emit_sell_order(self, p: SignalPayload) -> None:
        ticker = p.ticker
        if ticker not in self._positions:
            log.warning(f"[RiskEngine] SELL signal for {ticker} but no open position.")
            return

        pos = self._positions[ticker]
        qty = pos.get('quantity', 0)
        if qty <= 0:
            log.warning(f"[RiskEngine] SELL signal for {ticker} but qty={qty}.")
            return

        # Pop now uses the same execution path as Pro (ORDER_REQ via IPC → Core SmartRouter).
        # No special handling needed — sells go through the same broker as buys.

        # Partial exit sells half; guard against qty=1 (can't split)
        if p.action == 'PARTIAL_SELL':
            sell_qty = qty // 2
            if sell_qty <= 0:
                log.info(f"[RiskEngine] Skipping partial sell for {ticker}: qty={qty} too small.")
                return
        else:
            sell_qty = qty

        log.info(f"[RiskEngine] SELL pass-through: {ticker} qty={sell_qty} reason={p.action}")

        # V10: WAL INTENT for sells
        _wal_cid = wal.new_client_id()
        wal.intent(_wal_cid, ticker=ticker, side='SELL', qty=sell_qty,
                   price=p.current_price, reason=str(p.action))

        _order_event = Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker=ticker,
                side='SELL',
                qty=sell_qty,
                price=p.current_price,
                reason=p.action,
            ),
        )
        _order_event._wal_client_id = _wal_cid
        self._bus.emit(_order_event)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _block(self, ticker: str, action: str, reason: str, event: Event) -> None:
        log.info(f"[RiskEngine] BLOCK {ticker}: {reason}")
        self._bus.emit(Event(
            type=EventType.RISK_BLOCK,
            payload=RiskBlockPayload(
                ticker=ticker,
                reason=reason,
                signal_action=action,
            ),
            correlation_id=event.event_id,
        ))

    # ── V10: Clear pending tickers on fill or failure ──────────────────────

    def _on_fill_clear_pending(self, event: Event) -> None:
        """Remove ticker from pending set when BUY fill arrives."""
        try:
            p = event.payload
            if hasattr(p, 'side') and str(p.side).upper() == 'BUY':
                with self._pending_lock:
                    self._pending_tickers.pop(getattr(p, 'ticker', ''), None)
        except Exception:
            pass

    def _on_fail_clear_pending(self, event: Event) -> None:
        """Remove ticker from pending set when order fails."""
        try:
            p = event.payload
            with self._pending_lock:
                self._pending_tickers.pop(getattr(p, 'ticker', ''), None)
        except Exception:
            pass

    def set_stream_client(self, stream_client) -> None:
        """V9: Attach TradierStreamClient for instant price lookups.

        When set, _get_spread() uses streaming bid/ask (<1ms) instead of
        REST API call (0.5-3s). Falls back to REST if streaming unavailable.
        """
        with self._stream_lock:
            self._stream_client = stream_client
        log.info("[RiskEngine] Stream client attached — using streaming prices for spread check")

    def _get_spread(self, ticker: str, signal_ask: float):
        """
        Get a fresh Level-1 quote for spread and price checks.

        V9: Tries streaming price first (<1ms). Falls back to REST API (0.5-3s).

        Returns (spread_pct, ask_price) on success.
        Returns (None, None) on any failure so the caller can block the trade
        rather than silently accepting stale or missing price data.

        Also returns (None, None) if the live ask diverges more than 0.5% from
        the signal's ask — a fast-moving price means the entry thesis has changed.
        """
        ask_price = None
        spread_pct = None

        # V9: Try streaming price first (<1ms vs 0.5-3s REST)
        with self._stream_lock:
            _sc = self._stream_client
        if _sc is not None:
            try:
                quote = _sc.get_quote(ticker)
                if quote and quote.get('ask', 0) > 0 and quote.get('bid', 0) > 0:
                    ask_price = quote['ask']
                    bid = quote['bid']
                    mid = (ask_price + bid) / 2
                    spread_pct = (ask_price - bid) / mid if mid > 0 else 0
                    log.debug("[RiskEngine] %s spread from STREAM: ask=$%.2f spread=%.4f",
                              ticker, ask_price, spread_pct)
            except Exception:
                pass  # fall through to REST

        # Fallback: REST API call (0.5-3s)
        if ask_price is None:
            try:
                spread_pct, ask_price = self._data.check_spread(ticker)
            except Exception as e:
                log.warning(f"[RiskEngine] check_spread({ticker}) failed: {e} — blocking entry")
                return None, None

        if spread_pct is None or ask_price is None or ask_price <= 0:
            log.warning(
                f"[RiskEngine] check_spread({ticker}) returned invalid data "
                f"({spread_pct}, {ask_price}) — blocking entry"
            )
            return None, None

        # Price-divergence guard: signal ask may be stale if the market moved
        if signal_ask > 0 and abs(ask_price - signal_ask) / signal_ask > 0.005:
            log.warning(
                f"[RiskEngine] {ticker}: live ask ${ask_price:.2f} diverges "
                f">0.5% from signal ask ${signal_ask:.2f} — blocking entry"
            )
            return None, None

        return spread_pct, ask_price
