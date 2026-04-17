"""
T4 — Strategy Engine
=====================
Event-driven wrapper around SignalAnalyzer.

Subscribes to BAR events, runs the VWAP-reclaim analysis, and emits typed
SIGNAL events.  All trading logic lives in SignalAnalyzer; this class is
purely the event-bus interface.

Entry flow  (ticker NOT in positions)
--------------------------------------
  BAR received
    → analyze(ticker, df, rvol_df)
    → check entry conditions
        • 2-bar VWAP reclaim
        • opened above VWAP
        • RSI 50–70
        • RVOL >= 2x
        • SPY above its VWAP (market tailwind)
    → if all met: emit SIGNAL(action='buy', stop/target/half_target computed)

Exit flow  (ticker IN positions)
---------------------------------
  BAR received
    → check_position_exit(ticker, pos, df, rvol_df)
    → if exit action: emit SIGNAL(action='sell_stop' | 'sell_target' |
                                         'sell_rsi'  | 'sell_vwap' |
                                         'partial_sell')
    → trailing stop is updated in-place on the pos dict (shared reference)

EOD force-close
---------------
  If the bar arrives on or after 15:00 ET and the ticker has an open position,
  a SIGNAL(action='sell_stop', reason='EOD force close') is emitted regardless
  of price conditions.

Events consumed
---------------
  EventType.BAR  (payload: BarPayload)

Events emitted
--------------
  EventType.SIGNAL  (payload: SignalPayload)

Usage
-----
    engine = StrategyEngine(
        bus=bus,
        positions=positions,      # shared dict — also used by RiskEngine + PositionManager
        strategy_params=params,
        per_ticker_params={},
        data_client=data_client,  # for SPY VWAP bias
    )
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from config import TRADE_START_TIME, FORCE_CLOSE_TIME, MIN_BARS_REQUIRED

from .event_bus import Event, EventBus, EventType
from .events import BarPayload, SignalPayload
from .signals import SignalAnalyzer

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)

# ── Trading window (parsed from config) ──────────────────────────────────────
_h, _m = TRADE_START_TIME.split(':')
_TRADE_START = (int(_h), int(_m))
_h, _m = FORCE_CLOSE_TIME.split(':')
_FORCE_CLOSE = (int(_h), int(_m))
_MIN_BARS    = MIN_BARS_REQUIRED


class StrategyEngine:
    """
    Event-driven strategy wrapper.

    Wires itself to the bus on construction; no external subscription needed.
    Shares the mutable `positions` dict with RiskEngine and PositionManager
    so every component always sees the current portfolio state.
    """

    def __init__(
        self,
        bus: EventBus,
        positions: dict,
        strategy_params: dict,
        per_ticker_params: Optional[Dict[str, dict]] = None,
        data_client=None,
    ):
        self._bus        = bus
        self._positions  = positions
        self._data       = data_client
        self._analyzer   = SignalAnalyzer(strategy_params, per_ticker_params or {})
        self._params     = strategy_params

        bus.subscribe(EventType.BAR, self._on_bar)

    # ── Handler ───────────────────────────────────────────────────────────────

    def _on_bar(self, event: Event) -> None:
        p: BarPayload = event.payload
        ticker = p.ticker
        df     = p.df
        rvol_df = p.rvol_df   # may be None

        if df is None or df.empty or len(df) < _MIN_BARS:
            return

        now  = datetime.now(ET)
        hour, minute = now.hour, now.minute

        # ── EOD gate ─────────────────────────────────────────────────────────
        if (hour, minute) >= _FORCE_CLOSE:
            if ticker in self._positions:
                pos = self._positions[ticker]
                strategy = pos.get('strategy', 'vwap_reclaim') or 'vwap_reclaim'

                # V8: Tier-based EOD behavior
                # T1 + VWAP: full close (intraday only)
                # T2/T3: partial sell (swing — hold overnight with reduced size)
                tier = self._get_pro_tier(strategy)

                if strategy == 'vwap_reclaim' or not strategy.startswith('pro:'):
                    # VWAP + unknown: full close
                    self._emit_eod_close(ticker, df, event)
                elif tier == 1:
                    # Pro T1 (sr_flip, trend_pullback, vwap_reclaim): full close
                    self._emit_eod_close(ticker, df, event)
                elif not pos.get('partial_done') and pos.get('quantity', 0) >= 2:
                    # Pro T2/T3: partial sell (half size for overnight)
                    self._emit_partial_sell(ticker, df, event)
            return  # no new entries after 15:00

        # ── Trading window gate ───────────────────────────────────────────────
        in_window = (
            (hour == _TRADE_START[0] and minute >= _TRADE_START[1])
            or (_TRADE_START[0] < hour < _FORCE_CLOSE[0])
        )
        if not in_window:
            return

        try:
            if ticker in self._positions:
                self._check_exits(ticker, df, rvol_df, event)
            else:
                self._check_entry(ticker, df, rvol_df, event)
        except (IndexError, KeyError) as exc:
            log.debug("[StrategyEngine] %s skipped: %s", ticker, exc)

    # ── Entry analysis ────────────────────────────────────────────────────────

    def _check_entry(self, ticker: str, df, rvol_df, parent: Event) -> None:
        # V8: Sentiment-based priority — defer to Pro if sentiment present
        # and Pro has enough bars to process (>= 52). Pro runs at priority=2
        # (before VWAP at priority=0), so if Pro fires, RegistryGate blocks VWAP.
        # This explicit deferral avoids VWAP consuming a ticker that Pro would
        # handle better with sentiment boost.
        if len(df) >= 52:  # Pro's MIN_BARS — only defer if Pro CAN run
            try:
                from data_sources.alt_data_reader import alt_data
                sentiment = alt_data.news_sentiment(ticker)
                if sentiment and abs(sentiment.get('sentiment_delta', 0)) > 0.3:
                    return  # defer — Pro will handle with sentiment boost
            except Exception:
                pass  # alt data unavailable — VWAP proceeds normally

        result = self._analyzer.analyze(ticker, df, {ticker: rvol_df} if rvol_df is not None else {})
        if result is None:
            log.debug("[StrategyEngine] %s: analyze returned None (insufficient bars or data)", ticker)
            return

        vwap_reclaim      = result['_vwap_reclaim']
        opened_above_vwap = result['_opened_above_vwap']
        rsi_value         = result['rsi_value']
        rvol              = result['rvol']

        if not vwap_reclaim and not opened_above_vwap:
            log.debug("[StrategyEngine] %s SKIP: no vwap_reclaim AND not opened_above_vwap | rsi=%.1f rvol=%.2f",
                      ticker, rsi_value, rvol)
            return
        if not vwap_reclaim:
            log.debug("[StrategyEngine] %s SKIP: no vwap_reclaim (opened_above=True) | rsi=%.1f rvol=%.2f",
                      ticker, rsi_value, rvol)
            return
        if not opened_above_vwap:
            log.debug("[StrategyEngine] %s SKIP: not opened_above_vwap (reclaim=True) | rsi=%.1f rvol=%.2f",
                      ticker, rsi_value, rvol)
            return

        # SPY market bias (data_client may be None in test mode)
        if self._data is not None:
            try:
                # SPY VWAP bias needs the full bars cache; pass a minimal stub
                spy_bullish = self._data.get_spy_vwap_bias({})
            except Exception as e:
                log.warning(f"[StrategyEngine] SPY bias check failed: {e}")
                spy_bullish = True   # don't block if SPY data unavailable
        else:
            spy_bullish = True

        if not spy_bullish:
            log.debug("[StrategyEngine] %s SKIP: SPY below VWAP (no market tailwind) | rsi=%.1f rvol=%.2f",
                      ticker, rsi_value, rvol)
            return

        current_price     = result['current_price']
        atr_value         = result['atr_value']
        reclaim_candle_low = result['reclaim_candle_low']
        atr_mult          = result['_atr_mult']
        min_stop_pct      = result['_min_stop_pct']

        # Compute stop / targets (must satisfy SignalPayload invariants)
        candle_stop  = reclaim_candle_low - (atr_value * 0.5)
        pct_stop     = current_price * (1 - min_stop_pct)
        stop_price   = min(candle_stop, pct_stop)
        target_price = current_price + (atr_value * atr_mult)
        half_target  = current_price + (atr_value * 1.0)

        # Enforce minimum stop distance (0.3% of price) to avoid micro-stops
        min_stop_offset = current_price * 0.003
        if current_price - stop_price < min_stop_offset:
            stop_price = current_price - min_stop_offset

        # Guard: stop must be strictly below current; target/half above
        if stop_price >= current_price:
            stop_price = current_price * 0.995  # floor at 0.5% below
        if target_price <= current_price:
            target_price = current_price * 1.01
        if not (current_price < half_target <= target_price):
            half_target = (current_price + target_price) / 2.0

        log.info(
            f"[StrategyEngine] BUY signal: {ticker} "
            f"price=${current_price:.2f} stop=${stop_price:.2f} "
            f"target=${target_price:.2f} rsi={rsi_value:.1f} rvol={rvol:.1f}x"
        )

        self._bus.emit(Event(
            type=EventType.SIGNAL,
            payload=SignalPayload(
                ticker=ticker,
                action='BUY',
                current_price=current_price,
                ask_price=current_price,          # Risk Engine will fetch a fresh quote
                atr_value=atr_value,
                rsi_value=rsi_value,
                rvol=rvol,
                vwap=result['vwap'],
                stop_price=stop_price,
                target_price=target_price,
                half_target=half_target,
                reclaim_candle_low=reclaim_candle_low,
                needs_ask_refresh=(self._data is not None),
            ),
            correlation_id=parent.event_id,
        ))

    # ── Exit analysis ─────────────────────────────────────────────────────────

    def _check_exits(self, ticker: str, df, rvol_df, parent: Event) -> None:
        pos = self._positions[ticker]
        action = self._analyzer.check_position_exit(
            ticker, pos, df, {ticker: rvol_df} if rvol_df is not None else {}
        )
        if action is None:
            return

        result = self._analyzer.analyze(ticker, df, {ticker: rvol_df} if rvol_df is not None else {})
        if result is None:
            return

        current_price = result['current_price']
        atr_value     = result['atr_value']
        rsi_value     = result['rsi_value']
        rvol          = result['rvol']

        log.info(
            f"[StrategyEngine] EXIT signal: {ticker} action={action} "
            f"price=${current_price:.2f}"
        )

        self._bus.emit(Event(
            type=EventType.SIGNAL,
            payload=SignalPayload(
                ticker=ticker,
                action=action,
                current_price=current_price,
                ask_price=current_price,
                atr_value=atr_value,
                rsi_value=rsi_value,
                rvol=rvol,
                vwap=result['vwap'],
                stop_price=pos.get('stop_price', current_price * 0.97),
                target_price=pos.get('target_price', current_price * 1.05),
                half_target=pos.get('half_target', current_price * 1.025),
                reclaim_candle_low=result['reclaim_candle_low'],
            ),
            correlation_id=parent.event_id,
        ))

    # ── V8: Pro tier lookup ─────────────────────────────────────────────────

    _TIER_MAP = {
        'sr_flip': 1, 'trend_pullback': 1, 'vwap_reclaim': 1,
        'orb': 2, 'gap_and_go': 2, 'inside_bar': 2, 'flag_pennant': 2,
        'momentum_ignition': 3, 'fib_confluence': 3,
        'bollinger_squeeze': 3, 'liquidity_sweep': 3,
    }

    @classmethod
    def _get_pro_tier(cls, strategy: str) -> int:
        """Extract tier from Pro strategy name (e.g., 'pro:sr_flip' → 1)."""
        if not strategy or not strategy.startswith('pro:'):
            return 0  # not a Pro strategy
        sub = strategy.split(':')[1] if ':' in strategy else ''
        return cls._TIER_MAP.get(sub, 2)  # default to T2 if unknown

    # ── EOD force-close ───────────────────────────────────────────────────────

    def _emit_eod_close(self, ticker: str, df, parent: Event) -> None:
        pos = self._positions[ticker]
        try:
            current_price = float(df['close'].iloc[-1])
        except Exception as exc:
            log.debug("[StrategyEngine] EOD close price extraction failed for %s: %s", ticker, exc)
            current_price = pos['entry_price']

        log.info(f"[StrategyEngine] EOD force-close signal: {ticker} @ ${current_price:.2f}")

        atr_value = pos.get('atr_value', 1.0) or 1.0

        self._bus.emit(Event(
            type=EventType.SIGNAL,
            payload=SignalPayload(
                ticker=ticker,
                action='SELL_STOP',
                current_price=current_price,
                ask_price=current_price,
                atr_value=atr_value,
                rsi_value=50.0,          # neutral placeholder
                rvol=1.0,
                vwap=current_price,      # placeholder
                stop_price=pos.get('stop_price', current_price * 0.97),
                target_price=pos.get('target_price', current_price * 1.05),
                half_target=pos.get('half_target', current_price * 1.025),
                reclaim_candle_low=current_price,
            ),
            correlation_id=parent.event_id,
        ))

    # ── Overnight position sizing reduction ────────────────────────────────

    def _emit_partial_sell(self, ticker: str, df, parent: Event) -> None:
        """Reduce swing position to half size after 3:00 PM ET for overnight gap protection."""
        pos = self._positions[ticker]
        qty = pos.get('quantity', 0)
        sell_qty = qty // 2

        if sell_qty < 1:
            return

        try:
            current_price = float(df['close'].iloc[-1])
        except Exception as exc:
            log.debug("[StrategyEngine] Partial sell price extraction failed for %s: %s", ticker, exc)
            current_price = pos['entry_price']

        log.info(
            f"[StrategyEngine] Overnight size reduction: {ticker} "
            f"selling {sell_qty}/{qty} shares @ ${current_price:.2f}"
        )

        atr_value = pos.get('atr_value', 1.0) or 1.0

        self._bus.emit(Event(
            type=EventType.SIGNAL,
            payload=SignalPayload(
                ticker=ticker,
                action='PARTIAL_SELL',
                current_price=current_price,
                ask_price=current_price,
                atr_value=atr_value,
                rsi_value=50.0,          # neutral placeholder
                rvol=1.0,
                vwap=current_price,      # placeholder
                stop_price=pos.get('stop_price', current_price * 0.97),
                target_price=pos.get('target_price', current_price * 1.05),
                half_target=pos.get('half_target', current_price * 1.025),
                reclaim_candle_low=current_price,
            ),
            correlation_id=parent.event_id,
        ))
