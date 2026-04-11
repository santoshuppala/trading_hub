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

from .event_bus import Event, EventBus, EventType
from .events import BarPayload, SignalPayload
from .signals import SignalAnalyzer

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)

# ── Trading window ────────────────────────────────────────────────────────────
_TRADE_START   = (9, 45)   # (hour, minute) inclusive
_FORCE_CLOSE   = (15, 0)   # (hour, minute) — EOD gate; also used for force-sell
_MIN_BARS      = 30        # minimum bars before analysis is attempted


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
                self._emit_eod_close(ticker, df, event)
            return  # no new entries after 15:00

        # ── Trading window gate ───────────────────────────────────────────────
        in_window = (
            (hour == _TRADE_START[0] and minute >= _TRADE_START[1])
            or (_TRADE_START[0] < hour < _FORCE_CLOSE[0])
        )
        if not in_window:
            return

        if ticker in self._positions:
            self._check_exits(ticker, df, rvol_df, event)
        else:
            self._check_entry(ticker, df, rvol_df, event)

    # ── Entry analysis ────────────────────────────────────────────────────────

    def _check_entry(self, ticker: str, df, rvol_df, parent: Event) -> None:
        result = self._analyzer.analyze(ticker, df, {ticker: rvol_df} if rvol_df is not None else {})
        if result is None:
            return

        vwap_reclaim      = result['_vwap_reclaim']
        opened_above_vwap = result['_opened_above_vwap']
        rsi_value         = result['rsi_value']
        rvol              = result['rvol']

        if not (vwap_reclaim and opened_above_vwap):
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
                action='buy',
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
                stop_price=pos['stop_price'],
                target_price=pos['target_price'],
                half_target=pos['half_target'],
                reclaim_candle_low=result['reclaim_candle_low'],
            ),
            correlation_id=parent.event_id,
        ))

    # ── EOD force-close ───────────────────────────────────────────────────────

    def _emit_eod_close(self, ticker: str, df, parent: Event) -> None:
        pos = self._positions[ticker]
        try:
            current_price = float(df['close'].iloc[-1])
        except Exception:
            current_price = pos['entry_price']

        log.info(f"[StrategyEngine] EOD force-close signal: {ticker} @ ${current_price:.2f}")

        atr_value = pos.get('atr_value', 1.0) or 1.0

        self._bus.emit(Event(
            type=EventType.SIGNAL,
            payload=SignalPayload(
                ticker=ticker,
                action='sell_stop',
                current_price=current_price,
                ask_price=current_price,
                atr_value=atr_value,
                rsi_value=50.0,          # neutral placeholder
                rvol=1.0,
                vwap=current_price,      # placeholder
                stop_price=pos['stop_price'],
                target_price=pos['target_price'],
                half_target=pos['half_target'],
                reclaim_candle_low=current_price,
            ),
            correlation_id=parent.event_id,
        ))
