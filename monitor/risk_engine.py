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
import time
from typing import Optional, Set, Dict

from .alerts import send_alert
from .event_bus import Event, EventBus, EventType
from .events import (
    OrderRequestPayload,
    RiskBlockPayload,
    SignalPayload,
)
from .risk_sizing import RiskSizer

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

SLIPPAGE_PCT   = 0.0001   # 0.01% — slippage allowance added to ask for sizing
MAX_SPREAD_PCT = 0.002    # 0.2%  — max bid/ask spread; wider → skip entry
MIN_RVOL       = 2.0      # minimum relative volume for momentum confirmation
RSI_LOW        = 40.0     # RSI must be >= RSI_LOW (allow recovery trades)
RSI_HIGH       = 75.0     # RSI must be <= RSI_HIGH (allow early momentum)


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
        self._alert_email     = alert_email
        self._sizer           = RiskSizer()

        bus.subscribe(EventType.SIGNAL, self._on_signal)

    # ── Event handler ────────────────────────────────────────────────────────

    def _on_signal(self, event: Event) -> None:
        p: SignalPayload = event.payload

        if p.action == 'BUY':
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
            # 1. Max positions
            if len(self._positions) >= self._max_positions:
                self._block(ticker, p.action,
                            f"max positions reached ({self._max_positions})", event)
                return

            # 2. Cooldown
            elapsed = time.time() - self._last_order_time.get(ticker, 0.0)
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

            # 4. RVOL (lower threshold for ETFs — they never hit 2.0x)
            from monitor.sector_map import is_etf, ETF_RVOL_MIN
            rvol_threshold = ETF_RVOL_MIN if is_etf(ticker) else MIN_RVOL
            if p.rvol < rvol_threshold:
                self._block(ticker, p.action,
                            f"RVOL too low ({p.rvol:.2f} < {rvol_threshold})", event)
                return

            # 5. RSI range
            if not (RSI_LOW <= p.rsi_value <= RSI_HIGH):
                self._block(ticker, p.action,
                            f"RSI out of bullish band ({p.rsi_value:.1f}, "
                            f"need {RSI_LOW}–{RSI_HIGH})", event)
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
                ticker, set(self._positions.keys()), news_context=news_ctx,
            )
            if not corr_ok:
                self._block(ticker, p.action, corr_reason, event)
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

            log.info(
                f"[RiskEngine] BUY approved: {ticker} "
                f"qty={qty} ask=${ask_price:.2f} entry=${effective_entry:.2f} "
                f"rvol={p.rvol:.1f}x rsi={p.rsi_value:.1f} spread={spread_pct:.3%}"
            )

            self._bus.emit(Event(
                type=EventType.ORDER_REQ,
                payload=OrderRequestPayload(
                    ticker=ticker,
                    side='BUY',
                    qty=qty,
                    price=effective_entry,
                    reason='VWAP reclaim',
                    needs_ask_refresh=p.needs_ask_refresh,
                    stop_price=p.stop_price,
                    target_price=p.target_price,
                    atr_value=p.atr_value,
                    layer='vwap',  # V7: RegistryGate acquires
                ),
                correlation_id=event.event_id,
            ))
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
        self._bus.emit(Event(
            type=EventType.ORDER_REQ,
            payload=OrderRequestPayload(
                ticker=ticker,
                side='SELL',
                qty=sell_qty,
                price=p.current_price,
                reason=p.action,
            ),
        ))

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

    def _get_spread(self, ticker: str, signal_ask: float):
        """
        Fetch a fresh Level-1 quote from the data client.

        Returns (spread_pct, ask_price) on success.
        Returns (None, None) on any failure so the caller can block the trade
        rather than silently accepting stale or missing price data.

        Also returns (None, None) if the live ask diverges more than 0.5% from
        the signal's ask — a fast-moving price means the entry thesis has changed.
        """
        try:
            spread_pct, ask_price = self._data.check_spread(ticker)
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
        except Exception as e:
            log.warning(f"[RiskEngine] check_spread({ticker}) failed: {e} — blocking entry")
            return None, None
