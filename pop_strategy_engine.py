"""
pop_strategy_engine.py — T3.5 Pipeline Layer (PopStrategyEngine)
================================================================
A new pipeline layer inserted between the BAR fanout (T3) and the existing
VWAP Reclaim StrategyEngine (T4).

Position in the 10-layer pipeline
-----------------------------------
  T3    BAR events  → StrategyEngine (existing, unchanged)
  T3.5  BAR events  → PopStrategyEngine  ← THIS MODULE
         └─ ingestion + features + screener + classifier + router
              └─ emit POP_SIGNAL  (durable, for audit/Redpanda)
              └─ PopExecutor executes order on the DEDICATED pop Alpaca account
                 └─ emit FILL → PositionManager (shared position tracking)

Execution account separation
------------------------------
  Main VWAP strategy  → APCA_API_KEY_ID / APCA_API_SECRET_KEY  (existing broker)
  Pop strategies      → APCA_POPUP_KEY  / APCA_PUPUP_SECRET_KEY (PopExecutor)

The pop strategy engine has its own Alpaca TradingClient so its trades never
touch the main strategy's account capital, margin, or position count.

  PopExecutor runs its own lightweight risk gate:
    - POP_MAX_POSITIONS   (default 3) — independent of main MAX_POSITIONS
    - POP_TRADE_BUDGET    (default $500) — per-trade dollar limit
    - POP_ORDER_COOLDOWN  (default 300s) — per-ticker cooldown
    - Spread check using the pop account's quote (live ask price)

  If APCA_POPUP_KEY / APCA_PUPUP_SECRET_KEY are absent or POP_PAPER_TRADING=true,
  PopExecutor silently runs in paper mode (logs fills, no real orders).

Design principles
-----------------
- Does NOT modify the existing StrategyEngine, RiskEngine, AlpacaBroker, or
  any other layer.
- POP_SIGNAL is emitted with durable=True so Redpanda acks the event before
  any in-process state change begins.
- FILL events from PopExecutor flow onto the shared bus so the existing
  PositionManager, StateEngine, and DurableEventLog all pick them up
  exactly as they would for main-strategy fills.
- All source adapters are injected via the constructor — swap mocks for real
  APIs without changing this file.
- Per-ticker ordering is preserved: BAR events for the same ticker always
  land on the same EventBus worker.

How to integrate into the main monitor loop
--------------------------------------------
    # In run_monitor.py, after building the EventBus — add one block:
    from pop_strategy_engine import PopStrategyEngine
    from config import (
        ALPACA_POPUP_KEY, ALPACA_PUPUP_SECRET_KEY,
        POP_PAPER_TRADING, POP_MAX_POSITIONS, POP_TRADE_BUDGET, POP_ORDER_COOLDOWN,
    )

    pop_engine = PopStrategyEngine(
        bus=bus,
        pop_alpaca_key=ALPACA_POPUP_KEY,
        pop_alpaca_secret=ALPACA_PUPUP_SECRET_KEY,
        pop_paper=POP_PAPER_TRADING,
        pop_max_positions=POP_MAX_POSITIONS,
        pop_trade_budget=POP_TRADE_BUDGET,
        pop_order_cooldown=POP_ORDER_COOLDOWN,
        alert_email=ALERT_EMAIL,
    )

How to swap mock data sources for real APIs
-------------------------------------------
1. For news:   implement get_news(symbol, window_hours) → list[NewsData]
2. For social: implement get_social(symbol, window_hours) → SocialData
3. Pass new instances to PopStrategyEngine constructor.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Dict, List, Optional, Set

from monitor.event_bus import Event, EventBus, EventType
from monitor.events import FillPayload, OrderFailPayload, PopSignalPayload
from monitor.sector_map import get_sector, count_sector_positions
from pop_screener.classifier      import StrategyClassifier
from pop_screener.features        import FeatureEngineer
from pop_screener.ingestion       import (
    MarketBehaviorSource, MomentumSource,
    NewsSentimentSource, SocialSentimentSource,
)
from pop_screener.models          import (
    EntrySignal, EngineeredFeatures, FloatCategory,
    MarketDataSlice, OHLCVBar,
)
from pop_screener.screener        import PopScreener
from pop_screener.strategy_router import StrategyRouter

log = logging.getLogger(__name__)


# ── Pop-specific executor (dedicated Alpaca account) ──────────────────────────

class PopExecutor:
    """
    Lightweight order executor for the pop strategy that uses its own
    Alpaca TradingClient (separate credentials from the main broker).

    Responsibilities
    ----------------
    - Maintains its own position count and ticker cooldown state.
    - Submits marketable limit buy orders; retries up to MAX_RETRIES times.
    - Emits FILL on success, ORDER_FAIL on abandonment — both on the shared bus
      so PositionManager, StateEngine, and DurableEventLog stay in sync.
    - In paper mode: instantly simulates a fill at the signal entry price.

    Parameters
    ----------
    bus              : shared EventBus
    trading_client   : Alpaca TradingClient (pop account); None → paper mode
    paper            : if True, simulate fills without sending orders
    max_positions    : pop-specific position limit (independent of main limit)
    trade_budget     : max dollars per pop trade
    order_cooldown   : seconds between orders on the same ticker
    alert_email      : optional email address for order alerts
    """

    FILL_TIMEOUT_SEC = 2.0
    FILL_POLL_SEC    = 0.25
    MAX_RETRIES      = 3
    MAX_SLIPPAGE_PCT = 0.005   # abandon retry if ask drifts > 0.5%

    def __init__(
        self,
        bus:            EventBus,
        trading_client  = None,
        paper:          bool  = True,
        max_positions:  int   = 3,
        trade_budget:   float = 500.0,
        order_cooldown: int   = 300,
        alert_email:    Optional[str] = None,
    ):
        self._bus            = bus
        self._client         = trading_client
        self._paper          = paper or (trading_client is None)
        self._max_positions  = max_positions
        self._trade_budget   = trade_budget
        self._order_cooldown = order_cooldown
        self._alert_email    = alert_email

        # Mutable state — protected by _lock for thread safety.
        self._lock = threading.Lock()
        self._positions:        Set[str]         = set()   # currently open pop positions
        self._last_order_time:  Dict[str, float] = {}      # ticker → monotonic timestamp

        mode = "paper" if self._paper else "live (pop account)"
        log.info("PopExecutor initialised — mode=%s max_pos=%d budget=$%d",
                 mode, max_positions, int(trade_budget))

    def execute_entry(self, entry: EntrySignal, pop_payload: PopSignalPayload) -> None:
        """
        Run risk checks and execute a buy order for one EntrySignal.

        Risk checks (pop-specific, independent of main RiskEngine)
        ----------------------------------------------------------
        1. Max pop positions not exceeded.
        2. Ticker cooldown — not ordered within the last order_cooldown seconds.
        3. Not already in an open pop position on this ticker.

        After passing all checks, submits the order and emits FILL / ORDER_FAIL.
        """
        symbol = entry.symbol
        now    = time.monotonic()

        # ── Risk gate (protected by _lock) ───────────────────────────────────
        # Cross-layer dedup: check global registry first
        from monitor.position_registry import registry
        if not registry.try_acquire(symbol, layer="pop"):
            log.info("POP risk block %s: held by another strategy layer (%s)",
                     symbol, registry.held_by(symbol))
            return

        _approved = False
        try:
            with self._lock:
                if len(self._positions) >= self._max_positions:
                    log.info("POP risk block %s: max pop positions (%d) reached",
                             symbol, self._max_positions)
                    return

                # Sector concentration limit (max 2 per sector)
                _MAX_PER_SECTOR = 2
                sector = get_sector(symbol)
                sector_counts = count_sector_positions(self._positions)
                if sector_counts.get(sector, 0) >= _MAX_PER_SECTOR:
                    log.info("POP risk block %s: sector %s already has %d positions",
                             symbol, sector, sector_counts[sector])
                    return

                last = self._last_order_time.get(symbol, 0.0)
                if (now - last) < self._order_cooldown:
                    remaining = int(self._order_cooldown - (now - last))
                    log.info("POP risk block %s: cooldown %ds remaining", symbol, remaining)
                    return

                if symbol in self._positions:
                    log.info("POP risk block %s: already in open pop position", symbol)
                    return

                # ── Size: shares = floor(budget / entry_price) ────────────────────
                qty = int(self._trade_budget // entry.entry_price)
                if qty <= 0:
                    log.warning("POP risk block %s: trade_budget $%.0f too small for price $%.2f",
                                symbol, self._trade_budget, entry.entry_price)
                    return

                # Mark cooldown and approve while still holding the lock
                self._last_order_time[symbol] = now
                _approved = True
        finally:
            if not _approved:
                registry.release(symbol)

        if self._paper:
            self._paper_fill(entry, qty, pop_payload)
        else:
            self._live_buy(entry, qty, pop_payload)

    def close_position(self, symbol: str, reason: str, current_price: float) -> None:
        """Mark a pop position as closed (called by PopStrategyEngine on exit signal)."""
        with self._lock:
            self._positions.discard(symbol)
        from monitor.position_registry import registry
        registry.release(symbol)
        log.info("POP position closed: %s @ $%.4f reason=%s", symbol, current_price, reason)

    # ── Private execution methods ──────────────────────────────────────────────

    def _paper_fill(
        self, entry: EntrySignal, qty: int, pop_payload: PopSignalPayload
    ) -> None:
        """Simulate an instant fill at entry_price (paper mode)."""
        order_id  = f"pop-paper-{uuid.uuid4().hex[:12]}"
        fill_price = entry.entry_price

        with self._lock:
            self._positions.add(entry.symbol)
        log.info(
            "POP PAPER FILL: BUY %d %s @ $%.4f  stop=%.4f  t1=%.4f  t2=%.4f  "
            "strategy=%s  order_id=%s",
            qty, entry.symbol, fill_price,
            entry.stop_loss, entry.target_1, entry.target_2,
            entry.strategy_type, order_id,
        )

        self._bus.emit(Event(
            type=EventType.FILL,
            payload=FillPayload(
                ticker=entry.symbol,
                side='BUY',
                qty=qty,
                fill_price=fill_price,
                order_id=order_id,
                reason=f"pop:{entry.strategy_type}",
                stop_price=entry.stop_loss,
                target_price=entry.target_2,
                atr_value=max(entry.metadata.get('atr', pop_payload.atr_value), 1e-6),
            ),
        ), durable=True)

    def _live_buy(
        self, entry: EntrySignal, qty: int, pop_payload: PopSignalPayload
    ) -> None:
        """Submit a marketable limit buy to the pop Alpaca account with retry."""
        try:
            from alpaca.trading.requests import LimitOrderRequest
            from alpaca.trading.enums    import OrderSide, TimeInForce
        except ImportError:
            log.error("alpaca-py not installed — cannot execute live pop orders")
            return

        symbol        = entry.symbol
        original_ask  = entry.entry_price
        ask_price     = original_ask

        for attempt in range(1, self.MAX_RETRIES + 1):
            limit_price = round(ask_price, 2)

            # ── Submit ────────────────────────────────────────────────────────
            try:
                req = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    limit_price=limit_price,
                    time_in_force=TimeInForce.DAY,
                )
                order    = self._client.submit_order(req)
                order_id = str(order.id)
                log.info(
                    "POP BUY [attempt %d] %d %s @ $%.2f  strategy=%s  order=%s",
                    attempt, qty, symbol, limit_price, entry.strategy_type, order_id,
                )
            except Exception as exc:
                log.warning("POP BUY submit failed %s: %s", symbol, exc)
                self._emit_fail(entry, qty, ask_price, reason=str(exc))
                return

            # ── Poll for fill ─────────────────────────────────────────────────
            filled   = False
            deadline = time.monotonic() + self.FILL_TIMEOUT_SEC
            while time.monotonic() < deadline:
                time.sleep(self.FILL_POLL_SEC)
                try:
                    order = self._client.get_order_by_id(order_id)
                    if order.status in ('filled', 'partially_filled'):
                        filled = True
                        break
                except Exception as exc:
                    log.warning("POP order status check failed (%s): %s", order_id, exc)
                    break

            if filled:
                filled_qty = int(float(order.filled_qty  or qty))
                avg_price  = float(order.filled_avg_price or limit_price)
                with self._lock:
                    self._positions.add(symbol)
                log.info(
                    "POP FILL: %d %s avg $%.4f  stop=%.4f  t1=%.4f  t2=%.4f  order=%s",
                    filled_qty, symbol, avg_price,
                    entry.stop_loss, entry.target_1, entry.target_2, order_id,
                )
                self._bus.emit(Event(
                    type=EventType.FILL,
                    payload=FillPayload(
                        ticker=symbol,
                        side='BUY',
                        qty=filled_qty,
                        fill_price=avg_price,
                        order_id=order_id,
                        reason=f"pop:{entry.strategy_type}",
                        stop_price=entry.stop_loss,
                        target_price=entry.target_2,
                        atr_value=max(entry.metadata.get('atr', pop_payload.atr_value), 1e-6),
                    ),
                ), durable=True)
                return

            # ── Cancel unfilled ───────────────────────────────────────────────
            try:
                self._client.cancel_order_by_id(order_id)
                log.info("POP BUY cancelled (unfilled): %s attempt %d", symbol, attempt)
            except Exception as exc:
                log.warning("POP cancel error (%s): %s", order_id, exc)

            if attempt == self.MAX_RETRIES:
                log.warning("POP BUY abandoned: %s after %d attempts", symbol, attempt)
                self._emit_fail(entry, qty, ask_price, reason="max retries exceeded")
                return

            # Slippage guard before next attempt
            if ask_price > original_ask * (1 + self.MAX_SLIPPAGE_PCT):
                log.warning(
                    "POP BUY abandoned: %s ask drifted $%.2f > $%.2f + %.1f%%",
                    symbol, ask_price, original_ask, self.MAX_SLIPPAGE_PCT * 100,
                )
                self._emit_fail(entry, qty, ask_price, reason="slippage exceeded")
                return

    def _emit_fail(self, entry: EntrySignal, qty: int, price: float, reason: str) -> None:
        self._bus.emit(Event(
            type=EventType.ORDER_FAIL,
            payload=OrderFailPayload(
                ticker=entry.symbol,
                side='BUY',
                qty=qty,
                price=price,
                reason=f"pop: {reason}",
            ),
        ))

    @property
    def open_positions(self) -> Set[str]:
        with self._lock:
            return set(self._positions)


# ── T3.5 pop strategy engine ──────────────────────────────────────────────────

class PopStrategyEngine:
    """
    T3.5 pipeline layer — subscribes to BAR, runs the full pop-screener
    pipeline, and executes trades on the dedicated pop Alpaca account.

    Parameters
    ----------
    bus                 : EventBus (shared with the rest of the pipeline)
    pop_alpaca_key      : APCA_POPUP_KEY — pop-account API key ID
    pop_alpaca_secret   : APCA_PUPUP_SECRET_KEY — pop-account secret key
    pop_paper           : if True (or keys absent), run in paper mode
    pop_max_positions   : max concurrent pop positions (default 3)
    pop_trade_budget    : dollars per pop trade (default $500)
    pop_order_cooldown  : seconds cooldown per ticker (default 300)
    alert_email         : optional email for order alerts
    news_source         : NewsSentimentSource or compatible duck-type
    social_source       : SocialSentimentSource or compatible duck-type
    market_source       : MarketBehaviorSource or compatible duck-type
    momentum_screener   : optional — re-use existing MomentumScreener universe
    universe_filter     : explicit symbol list (overrides momentum_screener)
    headline_baseline   : 7-day avg headline rate for velocity normalisation
    social_baseline     : 7-day avg mention rate for velocity normalisation
    enabled             : master on/off switch
    """

    def __init__(
        self,
        bus:                EventBus,
        pop_alpaca_key:     Optional[str]  = None,
        pop_alpaca_secret:  Optional[str]  = None,
        pop_paper:          bool           = True,
        pop_max_positions:  int            = 3,
        pop_trade_budget:   float          = 500.0,
        pop_order_cooldown: int            = 300,
        alert_email:        Optional[str]  = None,
        news_source:        Optional[NewsSentimentSource]   = None,
        social_source:      Optional[SocialSentimentSource] = None,
        market_source:      Optional[MarketBehaviorSource]  = None,
        momentum_screener                  = None,
        universe_filter:    Optional[List[str]] = None,
        headline_baseline:  float          = 2.0,
        social_baseline:    float          = 100.0,
        enabled:            bool           = True,
    ):
        self._bus               = bus
        self._headline_baseline = headline_baseline
        self._social_baseline   = social_baseline
        self._enabled           = enabled
        self._news_failures     = 0
        self._social_failures   = 0
        self._max_data_failures = 10  # disable source after 10 consecutive failures

        # Per-ticker baselines from historical DB data (replaces static defaults)
        from pop_screener.sentiment_baseline import SentimentBaselineEngine
        self._baseline_engine = SentimentBaselineEngine(lookback_days=7)
        try:
            self._baseline_engine.load_from_db()
        except Exception as exc:
            log.warning("SentimentBaseline load failed (using static defaults): %s", exc)

        # ── Data sources ──────────────────────────────────────────────────────
        self._news     = news_source   or NewsSentimentSource()
        self._social   = social_source or SocialSentimentSource()
        self._market   = market_source or MarketBehaviorSource()
        self._momentum = MomentumSource(
            screener=momentum_screener,
            universe=universe_filter,
        )

        # ── Pipeline components ───────────────────────────────────────────────
        self._engineer   = FeatureEngineer()
        self._screener   = PopScreener()
        self._classifier = StrategyClassifier()
        self._router     = StrategyRouter()

        # ── Pop executor (dedicated Alpaca account) ────────────────────────────
        trading_client = None
        effective_paper = pop_paper

        if not pop_paper and pop_alpaca_key and pop_alpaca_secret:
            trading_client = _build_alpaca_client(
                pop_alpaca_key, pop_alpaca_secret, paper=False
            )
            if trading_client is None:
                log.warning(
                    "PopStrategyEngine: failed to build pop Alpaca client — "
                    "falling back to paper mode"
                )
                effective_paper = True
        else:
            if not pop_paper:
                log.warning(
                    "PopStrategyEngine: APCA_POPUP_KEY or APCA_PUPUP_SECRET_KEY "
                    "not set — running pop orders in paper mode"
                )
            effective_paper = True

        self._executor = PopExecutor(
            bus=bus,
            trading_client=trading_client,
            paper=effective_paper,
            max_positions=pop_max_positions,
            trade_budget=pop_trade_budget,
            order_cooldown=pop_order_cooldown,
            alert_email=alert_email,
        )

        # ── Subscribe to BAR events ───────────────────────────────────────────
        bus.subscribe(EventType.BAR, self._on_bar, priority=1)

        log.info(
            "PopStrategyEngine initialised | account=%s | max_pos=%d | "
            "budget=$%d | cooldown=%ds | enabled=%s",
            "paper" if effective_paper else "pop-live",
            pop_max_positions, int(pop_trade_budget), pop_order_cooldown, enabled,
        )

    # ── Main BAR handler ──────────────────────────────────────────────────────

    def _on_bar(self, event: Event) -> None:
        """
        Called by the EventBus for every BAR event.

        Flow
        ----
          1. BarPayload.df  → MarketDataSlice
          2. get_news() + get_social()  → raw data
          3. FeatureEngineer.compute()  → EngineeredFeatures
          4. PopScreener.screen()       → PopCandidate | None (early exit if None)
          5. StrategyClassifier.classify() → StrategyAssignment
          6. StrategyRouter.route()     → list[EntrySignal]
          7. For each EntrySignal (long only):
             a. emit POP_SIGNAL (durable) → Redpanda audit trail
             b. PopExecutor.execute_entry() → live or paper fill on pop account
                  └─ emits FILL → PositionManager / StateEngine
        """
        if not self._enabled:
            return

        from monitor.events import BarPayload
        payload: BarPayload = event.payload
        symbol  = payload.ticker

        # 1. Market data
        market_slice = self._bar_payload_to_slice(payload)
        if not market_slice.bars:
            return

        # Early exit: skip news/social fetch if bar shows no unusual activity
        # This eliminates ~90% of unnecessary API calls (main slow-handler fix)
        # ETFs use lower RVOL threshold (they never reach 1.5x)
        if market_slice.bars:
            from monitor.sector_map import is_etf, ETF_RVOL_MIN
            last_bar = market_slice.bars[-1]
            bar_range_pct = (last_bar.high - last_bar.low) / last_bar.open if last_bar.open > 0 else 0
            # Skip if bar range < 0.3% AND volume not elevated (no pop candidate)
            last_rvol = market_slice.rvol_series[-1] if market_slice.rvol_series else 1.0
            rvol_filter = ETF_RVOL_MIN if is_etf(symbol) else 1.5
            if bar_range_pct < 0.003 and last_rvol < rvol_filter and abs(market_slice.gap_size) < 0.02:
                log.debug("POP skip %s: range=%.4f rvol=%.2f gap=%.4f (below thresholds)",
                          symbol, bar_range_pct, last_rvol, market_slice.gap_size)
                return

        # 2. External data (with circuit breaker for consecutive failures)
        if self._news_failures >= self._max_data_failures:
            log.debug("PopEngine: news source disabled after %d consecutive failures, skipping %s",
                      self._news_failures, symbol)
            return
        if self._social_failures >= self._max_data_failures:
            log.debug("PopEngine: social source disabled after %d consecutive failures, skipping %s",
                      self._social_failures, symbol)
            return
        try:
            news_1h  = self._news.get_news(symbol, window_hours=1.0)
            news_24h = self._news.get_news(symbol, window_hours=24.0)
            self._news_failures = 0  # reset on success
        except Exception as exc:
            self._news_failures += 1
            if self._news_failures >= self._max_data_failures:
                log.error("PopEngine: news source disabled after %d consecutive failures", self._news_failures)
            log.warning("PopStrategyEngine: news fetch failed for %s: %s", symbol, exc)
            return
        try:
            social   = self._social.get_social(symbol, window_hours=1.0)
            self._social_failures = 0  # reset on success
        except Exception as exc:
            self._social_failures += 1
            if self._social_failures >= self._max_data_failures:
                log.error("PopEngine: social source disabled after %d consecutive failures", self._social_failures)
            log.warning("PopStrategyEngine: social fetch failed for %s: %s", symbol, exc)
            return
        try:
            log.debug("POP data %s: news_1h=%d news_24h=%d social_mentions=%d bull=%.0f%% bear=%.0f%%",
                      symbol, len(news_1h), len(news_24h),
                      getattr(social, 'mention_count', 0),
                      getattr(social, 'bullish_pct', 0) * 100,
                      getattr(social, 'bearish_pct', 0) * 100)
        except Exception:
            pass

        # 2b. Smart persistence: only persist if data meaningfully changed
        try:
            from data_sources.persistence import SmartPersistence, benzinga_changed, stocktwits_changed
            from monitor.events import NewsDataPayload, SocialDataPayload
            from datetime import datetime as _dt, timezone as _tz

            if not hasattr(self, '_smart_persist'):
                self._smart_persist = SmartPersistence(writer_fn=lambda *a: None)

            news_fetched_at = _dt.now(_tz.utc).isoformat()
            avg_sent_1h = (sum(n.sentiment_score for n in news_1h) / len(news_1h)) if news_1h else 0.0
            avg_sent_24h = (sum(n.sentiment_score for n in news_24h) / len(news_24h)) if news_24h else 0.0
            top_headline = news_1h[-1].headline if news_1h else (news_24h[-1].headline if news_24h else '')

            # Extract headline timestamps for latency analysis
            all_news = news_1h or news_24h or []
            latest_ts = max((n.timestamp for n in all_news), default=None)
            oldest_1h_ts = min((n.timestamp for n in news_1h), default=None) if news_1h else None

            # Benzinga — only emit event if data meaningfully changed
            news_snapshot = {
                'ticker': symbol,
                'headlines_1h': len(news_1h),
                'headlines_24h': len(news_24h),
                'avg_sentiment_1h': round(avg_sent_1h, 4),
                'avg_sentiment_24h': round(avg_sent_24h, 4),
                'top_headline': top_headline[:200],
                'latest_headline_time': latest_ts.isoformat() if latest_ts else '',
                'oldest_headline_time': oldest_1h_ts.isoformat() if oldest_1h_ts else '',
                'news_fetched_at': news_fetched_at,
            }
            if self._smart_persist.persist_if_changed('benzinga_news', symbol, news_snapshot, benzinga_changed):
                self._bus.emit(Event(
                    type=EventType.NEWS_DATA,
                    payload=NewsDataPayload(
                        ticker=symbol,
                        headlines_1h=len(news_1h),
                        headlines_24h=len(news_24h),
                        avg_sentiment_1h=round(avg_sent_1h, 4),
                        avg_sentiment_24h=round(avg_sent_24h, 4),
                        top_headline=top_headline[:200],
                        latest_headline_time=latest_ts.isoformat() if latest_ts else '',
                        oldest_headline_time=oldest_1h_ts.isoformat() if oldest_1h_ts else '',
                        news_fetched_at=news_fetched_at,
                    ),
                ))

            # StockTwits — only emit event if data meaningfully changed
            social_snapshot = {
                'ticker': symbol,
                'mention_count': getattr(social, 'mention_count', 0),
                'mention_velocity': round(getattr(social, 'mention_velocity', 0.0), 4),
                'bullish_pct': round(getattr(social, 'bullish_pct', 0.0), 4),
                'bearish_pct': round(getattr(social, 'bearish_pct', 0.0), 4),
            }
            social_fetched_at = _dt.now(_tz.utc).isoformat()
            if self._smart_persist.persist_if_changed('stocktwits_social', symbol, social_snapshot, stocktwits_changed):
                self._bus.emit(Event(
                    type=EventType.SOCIAL_DATA,
                    payload=SocialDataPayload(
                        ticker=symbol,
                        mention_count=getattr(social, 'mention_count', 0),
                        mention_velocity=round(getattr(social, 'mention_velocity', 0.0), 4),
                        bullish_pct=round(getattr(social, 'bullish_pct', 0.0), 4),
                        bearish_pct=round(getattr(social, 'bearish_pct', 0.0), 4),
                        newest_message_time=getattr(social, 'newest_message_time', ''),
                        oldest_message_time=getattr(social, 'oldest_message_time', ''),
                        social_fetched_at=social_fetched_at,
                    ),
                ))
        except Exception:
            pass  # persistence must never crash the pop engine

        # Update intraday baselines for tickers with no DB history
        self._baseline_engine.update_intraday(
            symbol,
            headlines_1h=len(news_1h),
            mention_velocity=getattr(social, 'mention_velocity', 0.0),
        )

        # 3. Feature engineering
        try:
            features = self._engineer.compute(
                symbol=symbol,
                news_1h=news_1h,
                news_24h=news_24h,
                social=social,
                market=market_slice,
                social_baseline_velocity=self._baseline_engine.social_baseline(symbol),
                headline_baseline_velocity=self._baseline_engine.headline_baseline(symbol),
            )
            log.debug("POP features %s: rvol=%.2f gap=%.4f sent_delta=%.3f headline_vel=%.1f social_vel=%.1f momentum=%.4f",
                      symbol, features.rvol, features.gap_size, features.sentiment_delta,
                      features.headline_velocity, features.social_velocity, features.price_momentum)
        except Exception as exc:
            log.warning("PopStrategyEngine: feature engineering failed for %s: %s", symbol, exc)
            return

        # 4. Screening
        candidate = self._screener.screen(features)
        if candidate is None:
            log.debug("POP no candidate %s: screener returned None", symbol)
            return
        log.info("POP CANDIDATE %s: reason=%s rvol=%.2f gap=%.4f sent=%.3f",
                 symbol, candidate.pop_reason.value, features.rvol, features.gap_size,
                 features.sentiment_delta)

        # 5. Classification
        assignment = self._classifier.classify(candidate)

        # 6. Signal generation
        entries, _ = self._router.route(
            symbol=symbol,
            bars=market_slice.bars,
            vwap_series=market_slice.vwap_series,
            features=features,
            assignment=assignment,
        )

        # 7. Emit + execute
        for entry in entries:
            self._handle_entry(entry, features, assignment, event)

    # ── Entry handler ─────────────────────────────────────────────────────────

    def _handle_entry(
        self,
        entry:        EntrySignal,
        features:     EngineeredFeatures,
        assignment,
        source_event: Event,
    ) -> None:
        """
        For one EntrySignal:
          a. Build and emit POP_SIGNAL (durable audit trail)
          b. For long entries — execute via PopExecutor (pop Alpaca account)
          c. Short entries (PARABOLIC_REVERSAL) — POP_SIGNAL only; not executed
             until short-selling is wired into the pop account.
        """
        # ── a. POP_SIGNAL ────────────────────────────────────────────────────
        pop_payload = self._build_pop_payload(entry, features, assignment)
        if pop_payload is None:
            return

        self._bus.emit(
            Event(
                type=EventType.POP_SIGNAL,
                payload=pop_payload,
                correlation_id=source_event.event_id,
            ),
            durable=True,
        )

        log.debug(
            "POP_SIGNAL: %s strategy=%s entry=%.4f stop=%.4f t1=%.4f t2=%.4f",
            entry.symbol, entry.strategy_type,
            entry.entry_price, entry.stop_loss, entry.target_1, entry.target_2,
        )

        # ── b. Execute on pop account (long entries only) ─────────────────────
        if entry.side != 'buy':
            log.debug(
                "POP short entry %s (%s) — POP_SIGNAL logged, execution skipped "
                "(short-selling not yet wired on pop account)",
                entry.symbol, entry.strategy_type,
            )
            return

        self._executor.execute_entry(entry, pop_payload)

    # ── Payload builder ───────────────────────────────────────────────────────

    def _build_pop_payload(
        self,
        entry:      EntrySignal,
        features:   EngineeredFeatures,
        assignment,
    ) -> Optional[PopSignalPayload]:
        try:
            snap = {
                'sentiment_score':         features.sentiment_score,
                'sentiment_delta':         features.sentiment_delta,
                'headline_velocity':       features.headline_velocity,
                'social_velocity':         features.social_velocity,
                'social_sentiment_skew':   features.social_sentiment_skew,
                'rvol':                    features.rvol,
                'volatility_score':        features.volatility_score,
                'price_momentum':          features.price_momentum,
                'gap_size':                features.gap_size,
                'vwap_distance':           features.vwap_distance,
                'trend_cleanliness_score': features.trend_cleanliness_score,
            }
            return PopSignalPayload(
                symbol=entry.symbol,
                strategy_type=str(entry.strategy_type),
                entry_price=entry.entry_price,
                stop_price=entry.stop_loss,
                target_1=entry.target_1,
                target_2=entry.target_2,
                pop_reason=entry.metadata.get('pop_reason', 'UNKNOWN'),
                atr_value=max(entry.metadata.get('atr', features.atr_value), 1e-6),
                rvol=features.rvol,
                vwap_distance=features.vwap_distance,
                strategy_confidence=assignment.strategy_confidence,
                features_json=json.dumps(snap),
            )
        except Exception as exc:
            log.warning(
                "PopStrategyEngine: failed to build PopSignalPayload for %s: %s",
                entry.symbol, exc,
            )
            return None

    # ── BarPayload → MarketDataSlice ──────────────────────────────────────────

    @staticmethod
    def _bar_payload_to_slice(payload) -> MarketDataSlice:
        """Convert a BarPayload (DataFrame) into a MarketDataSlice (List[OHLCVBar])."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        ET = ZoneInfo('America/New_York')

        df     = payload.df
        symbol = payload.ticker
        bars: List[OHLCVBar] = []

        required = {'open', 'high', 'low', 'close', 'volume'}
        if not required.issubset(df.columns):
            return MarketDataSlice(
                symbol=symbol, bars=[], vwap_series=[], rvol_series=[],
                gap_size=0.0, float_category=FloatCategory.NORMAL, earnings_flag=False,
            )

        for ts, row in df.iterrows():
            try:
                dt = (ts.to_pydatetime().replace(tzinfo=ET)
                      if hasattr(ts, 'to_pydatetime') else datetime.now(ET))
                bars.append(OHLCVBar(
                    timestamp=dt,
                    open=float(row['open']),
                    high=float(row['high']),
                    low=float(row['low']),
                    close=float(row['close']),
                    volume=int(row['volume']),
                ))
            except (TypeError, ValueError):
                continue

        if not bars:
            return MarketDataSlice(
                symbol=symbol, bars=[], vwap_series=[], rvol_series=[],
                gap_size=0.0, float_category=FloatCategory.NORMAL, earnings_flag=False,
            )

        if 'vwap' in df.columns:
            vwap_series = [float(v) for v in df['vwap']]
        else:
            vwap_series, cum_tp, cum_vol = [], 0.0, 0
            for b in bars:
                cum_tp  += ((b.high + b.low + b.close) / 3) * b.volume
                cum_vol += b.volume
                vwap_series.append(cum_tp / cum_vol if cum_vol > 0 else b.close)

        # Compute real RVOL from historical daily bars (payload.rvol_df)
        rvol_series = []
        rvol_df = payload.rvol_df if hasattr(payload, 'rvol_df') else None
        if rvol_df is not None and not rvol_df.empty and 'volume' in rvol_df.columns:
            # Average daily volume from historical data
            hist_volumes = rvol_df['volume'].astype(float)
            avg_daily_vol = float(hist_volumes.mean()) if len(hist_volumes) > 0 else 1.0
            if avg_daily_vol > 0:
                # Cumulative intraday volume vs expected fraction of daily volume
                # 390 trading minutes in a day; compute time-fraction-adjusted RVOL
                cum_vol = 0.0
                for i, b in enumerate(bars):
                    cum_vol += b.volume
                    minutes_elapsed = i + 1
                    # Expected volume at this point = avg_daily * (minutes / 390)
                    expected = avg_daily_vol * (minutes_elapsed / 390.0)
                    rvol_val = cum_vol / expected if expected > 0 else 1.0
                    rvol_series.append(round(rvol_val, 4))
            else:
                rvol_series = [1.0] * len(bars)
        else:
            rvol_series = [1.0] * len(bars)

        today_open = bars[0].open
        # Compute gap from prior close using historical data
        prior_close = today_open  # fallback
        rvol_df = payload.rvol_df if hasattr(payload, 'rvol_df') else None
        if rvol_df is not None and not rvol_df.empty and 'close' in rvol_df.columns:
            prior_close = float(rvol_df['close'].iloc[-1])
        gap_size = (today_open - prior_close) / prior_close if prior_close > 0 else 0.0

        return MarketDataSlice(
            symbol=symbol,
            bars=bars,
            vwap_series=vwap_series,
            rvol_series=rvol_series,
            gap_size=gap_size,
            float_category=FloatCategory.NORMAL,
            earnings_flag=False,
            prior_close=prior_close,
        )

    # ── Control API ───────────────────────────────────────────────────────────

    def enable(self)  -> None: self._enabled = True
    def disable(self) -> None: self._enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def open_positions(self) -> Set[str]:
        """Symbols currently held by the pop executor."""
        return self._executor.open_positions


# ── Alpaca client factory ─────────────────────────────────────────────────────

def _build_alpaca_client(api_key: str, api_secret: str, paper: bool = False):
    """
    Build an Alpaca TradingClient for the pop account.
    Returns None on import error or auth failure.
    """
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(api_key=api_key, secret_key=api_secret, paper=paper)
        log.info("PopStrategyEngine: pop Alpaca client connected (paper=%s)", paper)
        return client
    except ImportError:
        log.error("alpaca-py not installed — pip install alpaca-py")
        return None
    except Exception as exc:
        log.error("PopStrategyEngine: failed to connect pop Alpaca client: %s", exc)
        return None
