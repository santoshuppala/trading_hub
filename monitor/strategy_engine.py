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
import math
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from config import TRADE_START_TIME, FORCE_CLOSE_TIME, MIN_BARS_REQUIRED

from .edge_context import categorize_regime, compute_time_bucket
from .event_bus import Event, EventBus, EventType
from .events import BarPayload, SignalPayload, QuotePayload
from .exit_engine import PositionLifecycle, get_exit_profile, ExitDecision
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

        # V10: Per-position lifecycle managers (Phase 0-4 exit engine)
        self._lifecycles: Dict[str, PositionLifecycle] = {}
        self._bar_counter: Dict[str, int] = {}  # monotonic bar counter per ticker

        bus.subscribe(EventType.BAR, self._on_bar)
        # V10: Listen for POSITION CLOSED to clean up lifecycles
        # Catches: kill switch halts, reconciliation phantoms, manual closes
        bus.subscribe(EventType.POSITION, self._on_position_lifecycle_cleanup, priority=99)
        # V9: Subscribe to QUOTE for real-time exit monitoring (<1s detection).
        # QUOTE handler ONLY checks stop/target for open positions.
        # No entry signals, no full analysis — just fast price comparison.
        bus.subscribe(EventType.QUOTE, self._on_quote)

        # V9: Track last BAR timestamp per ticker to reject stale QUOTEs
        self._last_bar_mono: Dict[str, float] = {}

    # ── V10: Lifecycle cleanup on any position close ───────────────────────

    def _on_position_lifecycle_cleanup(self, event: Event) -> None:
        """Clean up lifecycle when position closes (any path).
        Catches: kill switch, reconciliation, manual close, ORDER_FAIL phantom.
        """
        try:
            p = event.payload
            if str(getattr(p, 'action', '')).upper() == 'CLOSED':
                ticker = getattr(p, 'ticker', '')
                if ticker and ticker in self._lifecycles:
                    self._lifecycles.pop(ticker, None)
        except Exception:
            pass

    # ── QUOTE Handler (V9 — real-time exit monitoring) ────────────────────────

    def _on_quote(self, event: Event) -> None:
        """V10 Hybrid: Real-time lifecycle evaluation on streaming quotes.

        Runs lifecycle.evaluate() on every QUOTE with real-time price.
        Uses last-known RSI/VWAP from BAR (refreshed every ~60s).

        This gives:
          - Real-time Phase 0 thesis validation (<1s, not 12s)
          - Real-time stop/trail hit detection (<1s)
          - Real-time phase transitions (price-based)
          - RSI/VWAP tightening uses last BAR values (acceptable lag)
        """
        p: QuotePayload = event.payload
        ticker = p.ticker

        # Only check tickers with open positions
        if ticker not in self._positions:
            return

        pos = self._positions[ticker]

        # Grace window: skip QUOTEs arriving within 500ms of position open.
        import time as _time
        fill_ts = pos.get('_opened_mono', 0)
        if fill_ts > 0 and _time.monotonic() - fill_ts < 0.5:
            return

        # Trading window check
        now = datetime.now(ET)
        hour, minute = now.hour, now.minute
        in_window = (
            (hour == _TRADE_START[0] and minute >= _TRADE_START[1])
            or (_TRADE_START[0] < hour < _FORCE_CLOSE[0])
        )
        if not in_window:
            return

        # Use BID for long exit checks
        bid = p.bid
        if bid <= 0:
            return

        # ── V10: Run lifecycle on real-time price ────────────────────
        lifecycle = self._lifecycles.get(ticker)
        if lifecycle:
            # Use bid as bar proxy: high=bid, low=bid, close=bid
            # RSI/VWAP from last BAR (stored on position by _check_exits)
            last_rsi = pos.get('_last_rsi', 50.0)
            last_vwap = pos.get('_last_vwap', 0.0)
            last_volume = pos.get('_last_volume', 0.0)
            entry_volume = pos.get('_entry_volume', last_volume)

            decision = lifecycle.evaluate(
                bar_high=bid, bar_low=bid, bar_close=bid,
                bar_volume=last_volume, entry_bar_volume=entry_volume,
                rsi=last_rsi, vwap=last_vwap,
            )

            if decision.action == 'EXIT':
                # Capture lifecycle data before cleanup
                _lifecycle_data = {
                    'exit_phase': decision.phase,
                    'exit_reason': decision.reason,
                    'bars_held': lifecycle.bars_held,
                    'unrealized_r': round(decision.unrealized_r, 3),
                    'running_high': round(lifecycle.running_high, 4),
                    'trail_stop': round(lifecycle.trail_stop, 4),
                    'confluence_count': lifecycle._confluence_count,
                    'partial_done': lifecycle.partial_done,
                    'partial_pct': 0,
                    'phase0_passed': decision.phase > 0,
                    'is_impulse': lifecycle.profile.is_impulse,
                    'R': round(lifecycle.R, 4),
                    'key_level': round(lifecycle.key_level, 4),
                    'invalidation': round(lifecycle.invalidation, 4),
                    'events': lifecycle._events,
                    'source': 'QUOTE_realtime',
                }
                pos['_lifecycle_data'] = _lifecycle_data
                self._lifecycles.pop(ticker, None)

                log.info(
                    "[StrategyEngine] QUOTE LIFECYCLE EXIT: %s reason=%s phase=%d "
                    "bid=$%.2f unrealized=%.1fR",
                    ticker, decision.reason, decision.phase, bid, decision.unrealized_r,
                )
                self._emit_quote_exit(ticker, bid, decision.reason, pos, event)
                return

            elif decision.action == 'PARTIAL':
                lifecycle.partial_done = True
                pos['_lifecycle_data'] = {
                    'exit_phase': decision.phase, 'exit_reason': decision.reason,
                    'partial_pct': round(decision.partial_pct, 2),
                    'source': 'QUOTE_realtime',
                }
                log.info(
                    "[StrategyEngine] QUOTE LIFECYCLE PARTIAL: %s %.0f%% at %.1fR bid=$%.2f",
                    ticker, decision.partial_pct * 100, decision.unrealized_r, bid,
                )
                self._emit_quote_exit(ticker, bid, 'PARTIAL_SELL', pos, event)
                return

            elif decision.action == 'TIGHTEN_TRAIL':
                # Update position stop in real-time
                if decision.new_stop > 0:
                    pos['stop_price'] = max(pos.get('stop_price', 0), decision.new_stop)
                return

            # HOLD — do nothing, wait for next quote
            return

        # ── Fallback: no lifecycle (legacy positions) ────────────────
        stop_price = pos.get('stop_price', 0)
        target_price = pos.get('target_price', 0)

        if stop_price <= 0 or target_price <= 0:
            return

        if bid <= stop_price:
            log.info("[StrategyEngine] QUOTE EXIT (stop/legacy): %s bid=$%.2f <= stop=$%.2f",
                     ticker, bid, stop_price)
            self._emit_quote_exit(ticker, bid, 'SELL_STOP', pos, event)
            return

        if bid >= target_price:
            log.info("[StrategyEngine] QUOTE EXIT (target/legacy): %s bid=$%.2f >= target=$%.2f",
                     ticker, bid, target_price)
            self._emit_quote_exit(ticker, bid, 'SELL_TARGET', pos, event)
            return

    def _emit_quote_exit(self, ticker: str, price: float, action: str,
                          pos: dict, parent: Event) -> None:
        """Emit a SIGNAL from QUOTE-based exit check."""
        atr_value = pos.get('atr_value', 1.0) or 1.0
        self._bus.emit(Event(
            type=EventType.SIGNAL,
            payload=SignalPayload(
                ticker=ticker,
                action=action,
                current_price=price,
                ask_price=price,
                atr_value=atr_value,
                rsi_value=50.0,               # not computed from QUOTE
                rvol=1.0,                      # not computed from QUOTE
                vwap=price,                    # placeholder
                stop_price=pos.get('stop_price', price * 0.97),
                target_price=pos.get('target_price', price * 1.05),
                half_target=pos.get('half_target', price * 1.025),
                reclaim_candle_low=price,      # placeholder
            ),
            correlation_id=parent.event_id,
        ))

    # ── BAR Handler ───────────────────────────────────────────────────────────

    def _on_bar(self, event: Event) -> None:
        p: BarPayload = event.payload
        ticker = p.ticker
        df     = p.df
        rvol_df = p.rvol_df   # may be None

        if df is None or df.empty or len(df) < _MIN_BARS:
            return

        # V9 (R1): Validate bar data before strategy processing
        # Rejects: price=0, NaN, negative volume, stale, OHLC inconsistent,
        # out-of-order bars (sequence check)
        try:
            _last = df.iloc[-1]
            _close = float(_last.get('close', _last.get('c', 0)))
            _vol = float(_last.get('volume', _last.get('v', 0)))
            _high = float(_last.get('high', _last.get('h', 0)))
            _low = float(_last.get('low', _last.get('l', 0)))
            _open = float(_last.get('open', _last.get('o', 0)))

            # Basic: price and volume
            if _close <= 0 or not math.isfinite(_close):
                log.warning("[StrategyEngine] BAD BAR %s: close=%.4f — skipping", ticker, _close)
                return
            if _vol < 0 or not math.isfinite(_vol):
                log.warning("[StrategyEngine] BAD BAR %s: volume=%.0f — skipping", ticker, _vol)
                return

            # OHLC consistency: high >= low, O/C within [L,H]
            if _high > 0 and _low > 0 and _high < _low:
                log.warning("[StrategyEngine] BAD BAR %s: high=%.2f < low=%.2f — skipping",
                            ticker, _high, _low)
                return
            if _high > 0 and _close > _high * 1.001:
                log.warning("[StrategyEngine] BAD BAR %s: close=%.2f > high=%.2f — skipping",
                            ticker, _close, _high)
                return

            # Stale duplicate: same close + zero volume
            if len(df) >= 2:
                _prev = df.iloc[-2]
                _prev_close = float(_prev.get('close', _prev.get('c', 0)))
                if _close == _prev_close and _vol == 0:
                    return  # stale bar, silently skip

            # Sequence check: reject BAR if older than last processed for this ticker
            # Prevents out-of-order BAR processing from dual-frequency polling
            if not hasattr(self, '_last_bar_seq'):
                self._last_bar_seq = {}
            _seq = getattr(event, 'stream_seq', 0) or 0
            if _seq > 0:
                _prev_seq = self._last_bar_seq.get(ticker, 0)
                if _seq <= _prev_seq:
                    return  # out-of-order BAR, silently skip
                self._last_bar_seq[ticker] = _seq
        except Exception:
            pass  # don't block on validation errors

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
                    self._lifecycles.pop(ticker, None)
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

        # V10: Capture edge context at signal time
        _regime_str = ''
        try:
            from data_sources.market_regime import regime as _regime
            from data_sources.alt_data_reader import alt_data as _alt
            _regime_str = categorize_regime(_regime.score(), _alt.vix())
        except Exception:
            pass
        _time_bucket = compute_time_bucket()

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
                strategy='vwap_reclaim',
                tier=1,
                timeframe='1min',
                regime_at_entry=_regime_str,
                time_bucket=_time_bucket,
            ),
            correlation_id=parent.event_id,
        ))

    # ── Exit analysis ─────────────────────────────────────────────────────────

    def _check_exits(self, ticker: str, df, rvol_df, parent: Event) -> None:
        pos = self._positions[ticker]

        # Extract bar data
        result = self._analyzer.analyze(ticker, df, {ticker: rvol_df} if rvol_df is not None else {})
        if result is None:
            return

        current_price = result['current_price']
        atr_value     = result['atr_value']
        rsi_value     = result['rsi_value']
        rvol          = result['rvol']
        vwap          = result['vwap']

        # V10: Cache indicators on position for real-time QUOTE lifecycle
        pos['_last_rsi'] = rsi_value
        pos['_last_vwap'] = vwap
        pos['_last_volume'] = 0  # will be set from bar below

        # Get bar OHLC for lifecycle
        try:
            last_bar = df.iloc[-1]
            bar_high = float(last_bar.get('high', last_bar.get('h', current_price)))
            bar_low = float(last_bar.get('low', last_bar.get('l', current_price)))
            bar_close = float(last_bar.get('close', last_bar.get('c', current_price)))
            bar_volume = float(last_bar.get('volume', last_bar.get('v', 0)))
            pos['_last_volume'] = bar_volume  # cache for QUOTE handler
        except Exception:
            bar_high = bar_low = bar_close = current_price
            bar_volume = 0

        # Increment bar counter
        self._bar_counter[ticker] = self._bar_counter.get(ticker, 0) + 1

        # ── V10: Create lifecycle if not exists ──────────────────────
        if ticker not in self._lifecycles:
            strategy = pos.get('strategy', 'vwap_reclaim') or 'vwap_reclaim'
            entry_price = pos.get('entry_price', current_price)
            stop_price = pos.get('stop_price', entry_price * 0.97)
            atr = atr_value or abs(entry_price - stop_price) or entry_price * 0.005

            # Strategy-aware key levels
            # These are stubs — real values come from detectors once wired
            clean_strat = strategy.replace('pro:', '').split(':')[0]

            if clean_strat in ('sr_flip', 'inside_bar', 'orb', 'gap_and_go'):
                # Structure-based: key level is near entry, invalidation at stop
                key_level = entry_price - atr * 0.3
                invalidation = stop_price
                structure_trail = entry_price - atr * 0.5
            elif clean_strat in ('momentum_ignition', 'bollinger_squeeze'):
                # Momentum-based: wider invalidation, key level below entry
                key_level = entry_price - atr * 0.5
                invalidation = entry_price - atr * 2.0
                structure_trail = entry_price - atr * 1.0
            elif clean_strat == 'liquidity_sweep':
                # Sweep: invalidation is the sweep low (approximated by stop)
                key_level = stop_price + atr * 0.3
                invalidation = stop_price
                structure_trail = stop_price + atr * 0.5
            else:
                # Default (vwap_reclaim, trend_pullback, fib_confluence)
                key_level = entry_price - atr * 0.3
                invalidation = stop_price
                structure_trail = entry_price - atr * 0.5

            lc = PositionLifecycle(
                ticker=ticker,
                strategy=strategy,
                entry_price=entry_price,
                entry_bar_num=self._bar_counter.get(ticker, 0),
                key_level=key_level,
                invalidation=invalidation,
                structure_trail=structure_trail,
                atr=atr,
            )

            # V10: Recovery — if position already has profit or partial done,
            # fast-forward lifecycle to appropriate phase (prevents re-taking partials)
            if pos.get('partial_done'):
                lc.phase = 3  # already took partial → Phase 3
                lc.partial_done = True
                lc.trail_stop = max(stop_price, entry_price)
                log.info("[Lifecycle] %s recovered at Phase 3 (partial_done)", ticker)
            elif current_price > entry_price + atr:
                lc.phase = 2  # in profit → Phase 2
                lc.trail_stop = entry_price
                log.info("[Lifecycle] %s recovered at Phase 2 (in profit)", ticker)
            else:
                lc.phase = 1  # skip Phase 0 for existing positions (already validated)
                log.info("[Lifecycle] %s recovered at Phase 1 (existing position)", ticker)

            self._lifecycles[ticker] = lc

        # ── V10: Evaluate lifecycle ──────────────────────────────────
        lifecycle = self._lifecycles[ticker]
        entry_vol = pos.get('_entry_volume', bar_volume)  # fallback to current
        try:
            decision = lifecycle.evaluate(
                bar_high=bar_high, bar_low=bar_low, bar_close=bar_close,
                bar_volume=bar_volume, entry_bar_volume=entry_vol,
                rsi=rsi_value, vwap=vwap,
                current_bar_num=self._bar_counter.get(ticker, 0),
            )
        except Exception as lc_exc:
            # Safety net: if lifecycle crashes, fall back to stop-loss check.
            # Never leave a position unmanaged due to a code bug.
            log.error("[StrategyEngine] Lifecycle evaluate CRASHED for %s: %s — "
                      "falling back to stop check", ticker, lc_exc)
            stop = pos.get('stop_price', 0)
            if stop > 0 and bar_low <= stop:
                from .exit_engine import ExitDecision
                decision = ExitDecision(
                    action='EXIT', phase=-1, reason='LIFECYCLE_CRASH_STOP',
                    exit_price=stop, unrealized_r=0,
                )
            else:
                return  # hold — stop not hit, wait for next bar

        # ── Act on lifecycle decision ────────────────────────────────
        if decision.action == 'HOLD':
            # Lifecycle says hold — update position stop if trail tightened
            if decision.new_stop > 0:
                pos['stop_price'] = max(pos.get('stop_price', 0), decision.new_stop)
            return

        if decision.action == 'TIGHTEN_TRAIL':
            if decision.new_stop > 0:
                pos['stop_price'] = max(pos.get('stop_price', 0), decision.new_stop)
            return

        # EXIT or PARTIAL — emit signal
        # V10: Capture lifecycle data for ML/edge model
        _lifecycle_data = {
            'exit_phase': decision.phase,
            'exit_reason': decision.reason,
            'bars_held': lifecycle.bars_held,
            'unrealized_r': round(decision.unrealized_r, 3),
            'running_high': round(lifecycle.running_high, 4),
            'trail_stop': round(lifecycle.trail_stop, 4),
            'confluence_count': lifecycle._confluence_count,
            'partial_done': lifecycle.partial_done,
            'partial_pct': round(decision.partial_pct, 2) if decision.action == 'PARTIAL' else 0,
            'phase0_passed': True,  # if we got here, Phase 0 passed
            'is_impulse': lifecycle.profile.is_impulse,
            'R': round(lifecycle.R, 4),
            'key_level': round(lifecycle.key_level, 4),
            'invalidation': round(lifecycle.invalidation, 4),
            # V10: Full decision event log for ML training
            'events': lifecycle._events,
        }

        if decision.action == 'EXIT':
            self._lifecycles.pop(ticker, None)  # cleanup
            action = decision.reason
            log.info(
                "[StrategyEngine] LIFECYCLE EXIT: %s action=%s phase=%d "
                "price=$%.2f unrealized=%.1fR bars=%d",
                ticker, action, decision.phase, current_price,
                decision.unrealized_r, lifecycle.bars_held if ticker in self._lifecycles else 0,
            )
        elif decision.action == 'PARTIAL':
            action = 'PARTIAL_SELL'
            lifecycle.partial_done = True
            log.info(
                "[StrategyEngine] LIFECYCLE PARTIAL: %s %.0f%% at %.1fR "
                "price=$%.2f confluence=%d",
                ticker, decision.partial_pct * 100, decision.unrealized_r,
                current_price, lifecycle._confluence_count,
            )

        # Store lifecycle data on the position for downstream capture
        pos['_lifecycle_data'] = _lifecycle_data

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
                vwap=vwap,
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
