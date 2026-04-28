"""
ProSetupEngine — T3.5+ BAR subscriber orchestrating 11 pro trading setups.

This is the single entry point for the pro_setups subsystem.  Instantiate
it after building the EventBus and before calling bus.start() / monitor.start().

    from pro_setups.engine import ProSetupEngine
    pro_engine = ProSetupEngine(
        bus=monitor._bus,
        max_positions=PRO_MAX_POSITIONS,
        order_cooldown=PRO_ORDER_COOLDOWN,
        trade_budget=float(PRO_TRADE_BUDGET),
    )

Pipeline (per BAR event, per ticker)
--------------------------------------
1. Guard: skip if df has < MIN_BARS or market data is stale
2. Run all 11 detectors (each returns DetectorSignal; errors silently skipped)
3. StrategyClassifier.classify() → ClassificationResult | None
4. Load strategy module from STRATEGY_REGISTRY
5. strategy.detect_signal() → 'long' | 'short' | None (final confirmation)
6. strategy.generate_entry() → entry_price
7. compute_atr() → atr
8. strategy.generate_stop() → stop_price
9. strategy.generate_exit() → (target_1, target_2)
10. Validate price levels (stop < entry < target_1 < target_2 for longs)
11. emit PRO_STRATEGY_SIGNAL (non-durable — for routing + audit logging)
    → ProStrategyRouter → RiskAdapter → ORDER_REQ (durable) → AlpacaBroker

Existing layers are not touched.  If no rule fires, nothing is emitted
and the BAR event proceeds through the normal T4 StrategyEngine path.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Dict, Optional

import pandas as pd

from monitor.edge_context import (
    categorize_regime, compute_time_bucket, compute_confluence_score,
)
from monitor.event_bus import EventBus, EventType, Event
from monitor.events import ProStrategySignalPayload

from .detectors import (
    TrendDetector, VWAPDetector, SRDetector, ORBDetector,
    InsideBarDetector, GapDetector, FlagDetector,
    LiquidityDetector, VolatilityDetector, FibDetector, MomentumDetector,
    DetectorSignal,
)
# V8: New detectors from alt data sources
from .detectors.sentiment_detector import SentimentDetector
from .detectors.news_velocity_detector import NewsVelocityDetector
from .classifiers import StrategyClassifier
from .strategies import STRATEGY_REGISTRY
from .router import ProStrategyRouter
from .risk import RiskAdapter
from .detectors._compute import compute_atr, compute_vwap, compute_rsi, compute_rvol

log = logging.getLogger(__name__)

# Minimum bars required before running any detector
_MIN_BARS: int = 52


class ProSetupEngine:
    """
    BAR subscriber that orchestrates the full pro_setups pipeline.

    Parameters
    ----------
    bus             : shared EventBus (must not have started yet)
    max_positions   : max concurrent pro-setup open positions  (default 3)
    order_cooldown  : seconds between orders on the same ticker (default 300)
    trade_budget    : dollars allocated per pro trade           (default 1000)
    """

    def __init__(
        self,
        bus:            EventBus,
        max_positions:  int   = 3,
        order_cooldown: int   = 300,
        trade_budget:   float = 1000.0,
        shared_positions: dict = None,
    ) -> None:
        self._bus = bus

        # ── Detectors (all 11 required by spec) ──────────────────────────
        self._detectors = {
            'trend':          TrendDetector(),
            'vwap':           VWAPDetector(),
            'sr':             SRDetector(),
            'orb':            ORBDetector(),
            'inside_bar':     InsideBarDetector(),
            'gap':            GapDetector(),
            'flag':           FlagDetector(),
            'liquidity':      LiquidityDetector(),
            'volatility':     VolatilityDetector(),
            'fib':            FibDetector(),
            'momentum':       MomentumDetector(),
            # V8: Alt data detectors (read from alt_data_cache.json)
            'sentiment':      SentimentDetector(mode='stocks'),
            'news_velocity':  NewsVelocityDetector(),
        }

        # ── Classifier ────────────────────────────────────────────────────
        self._classifier = StrategyClassifier()

        # ── Risk adapter (subscribes to FILL/POSITION internally) ─────────
        self._risk_adapter = RiskAdapter(
            bus               = bus,
            max_positions     = max_positions,
            order_cooldown    = order_cooldown,
            trade_budget      = trade_budget,
            shared_positions  = shared_positions,
        )

        # ── Router (subscribes to PRO_STRATEGY_SIGNAL internally) ─────────
        self._router = ProStrategyRouter(bus=bus, risk_adapter=self._risk_adapter)

        # ── Subscribe to BAR at priority=2 ────────────────────────────────
        # priority=2 runs AFTER StrategyEngine (priority=1) but that order
        # doesn't matter since they emit different event types.  Using 2
        # gives a clear separation in logs and allows future priority tuning.
        bus.subscribe(EventType.BAR, self._on_bar, priority=2)

        # V9: Per-ticker 5-min bar cache to avoid recomputing resample on every BAR
        self._5min_cache: Dict[str, object] = {}     # {ticker: df_5min}
        self._5min_cache_len: Dict[str, int] = {}    # {ticker: len(df) when cached}

        # V10.1: Skip unchanged bars — avoid re-evaluating same data
        # Key: (ticker, len(df), last_close, last_volume)
        self._last_bar_key: Dict[str, tuple] = {}

        log.info(
            "[ProSetupEngine] ready  detectors=%d  max_pos=%d  "
            "cooldown=%ds  budget=$%.0f",
            len(self._detectors), max_positions, order_cooldown, trade_budget,
        )

    # ── BAR handler ──────────────────────────────────────────────────────────

    def _on_bar(self, event: Event) -> None:
        payload = event.payload
        ticker  = payload.ticker
        df      = payload.df
        rvol_df = payload.rvol_df

        if len(df) < _MIN_BARS:
            return

        # V10.1: Skip if this exact bar was already evaluated.
        # Both BarBuilder and REST may emit BAR for the same ticker.
        # Also, REST re-emits same data when no new bar formed.
        # Key on (len, last_close, last_volume) — changes when new bar arrives.
        try:
            _last = df.iloc[-1]
            _bar_key = (len(df), float(_last.get('close', _last.get('c', 0))),
                        float(_last.get('volume', _last.get('v', 0))))
            if self._last_bar_key.get(ticker) == _bar_key:
                return  # same bar, already evaluated
            self._last_bar_key[ticker] = _bar_key
        except Exception:
            pass  # if key extraction fails, evaluate anyway

        # EOD gate: no new signals after 3:45 PM ET
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo('America/New_York'))
        if (now_et.hour, now_et.minute) >= (15, 45):
            return

        t0 = time.monotonic()

        # ── Step 0.5: precompute shared indicators once ──────────────────
        precomputed: Dict[str, object] = {}
        try:
            precomputed['vwap'] = compute_vwap(df)
            precomputed['atr'] = compute_atr(df)
            precomputed['rsi'] = compute_rsi(df)
            from .detectors._compute import compute_ema
            precomputed['ema_9'] = compute_ema(df['close'], 9)
            precomputed['ema_21'] = compute_ema(df['close'], 21)
            precomputed['ema_50'] = compute_ema(df['close'], 50)
        except Exception as exc:
            log.debug("[ProSetupEngine][%s] precompute failed: %s", ticker, exc)

        # ── V9: Multi-timeframe — aggregate 1-min → 5-min bars ──────────
        # SR, InsideBar, Flag, Fib, Trend need 5-min bars for meaningful
        # pattern detection. 1-min bars produce 80%+ false positives for
        # these structure-based detectors.
        #
        # Per-ticker cache: only recompute when new bars arrive
        # (len(df) changes → new bar → recompute). Saves ~80% of resample calls.
        try:
            cached_len = self._5min_cache_len.get(ticker, 0)
            if len(df) != cached_len:
                df_5min = df.resample('5min').agg({
                    'open': 'first', 'high': 'max', 'low': 'min',
                    'close': 'last', 'volume': 'sum',
                }).dropna()
                self._5min_cache[ticker] = df_5min
                self._5min_cache_len[ticker] = len(df)
            else:
                df_5min = self._5min_cache.get(ticker)

            if df_5min is not None:
                precomputed['df_5min'] = df_5min

                # Precompute 5-min indicators for detectors that use them
                if len(df_5min) >= 10:
                    from .detectors._compute import compute_ema
                    precomputed['ema_9_5m'] = compute_ema(df_5min['close'], 9)
                    precomputed['ema_21_5m'] = compute_ema(df_5min['close'], 21)
                    precomputed['ema_50_5m'] = compute_ema(df_5min['close'], 50)
                    precomputed['atr_5m'] = compute_atr(df_5min)
                    precomputed['rsi_5m'] = compute_rsi(df_5min)
        except Exception as exc:
            log.debug("[ProSetupEngine][%s] 5-min aggregation failed: %s", ticker, exc)

        # ── Step 1: run detectors (V9: two-phase cascade with early exit) ──
        # Phase 1: 3 cheapest detectors (~1.5ms total). If none fire,
        # skip Phase 2 (remaining 10 detectors). ~80% of tickers at market
        # open have no setup and are skipped — reduces 10s spike to <2s.
        _PHASE1_NAMES = {'trend', 'vwap', 'orb'}
        outputs: Dict[str, DetectorSignal] = {}
        any_phase1_fired = False

        for name in _PHASE1_NAMES:
            if name in self._detectors:
                sig = self._detectors[name].detect(
                    ticker, df, rvol_df, precomputed=precomputed)
                outputs[name] = sig
                if sig.fired:
                    any_phase1_fired = True

        # V10: Push pre-computed levels to TickDetector regardless of Phase 1 result.
        # TickDetector needs S/R levels, ORB range, etc. even when no bar-level signal fires.
        if hasattr(self, '_tick_detector') and self._tick_detector:
            try:
                sr_meta = outputs.get('sr', DetectorSignal.no_signal()).metadata
                orb_meta = outputs.get('orb', DetectorSignal.no_signal()).metadata
                self._tick_detector.update_levels(ticker, {
                    'sr_levels': sr_meta.get('near_levels', [])[:10],
                    'orb_high': orb_meta.get('orb_high', 0),
                    'orb_low': orb_meta.get('orb_low', 0),
                    'gap_direction': outputs.get('gap', DetectorSignal.no_signal()).metadata.get('direction'),
                    'gap_ref': outputs.get('gap', DetectorSignal.no_signal()).metadata.get('prev_close', 0),
                    'nearest_support': sr_meta.get('level', 0) if sr_meta.get('direction') == 'long' else 0,
                    'nearest_resistance': sr_meta.get('level', 0) if sr_meta.get('direction') == 'short' else 0,
                    'session_high': float(df['high'].max()) if len(df) > 0 else 0,
                    'session_low': float(df['low'].min()) if len(df) > 0 else 0,
                    'session_open': float(df['open'].iloc[0]) if len(df) > 0 else 0,
                    'close': float(df['close'].iloc[-1]) if len(df) > 0 else 0,
                    'avg_bar_volume': float(df['volume'].mean()) if len(df) > 0 else 0,
                })
                self._tick_detector.update_indicators(ticker, precomputed or {})
            except Exception as _td_exc:
                pass  # never block bar processing for tick detector

        if not any_phase1_fired:
            # No Phase 1 detector fired → no setup exists for this ticker
            # Skip Phase 2 entirely (saves ~80% of detector compute)
            return

        # Phase 2: full analysis (all remaining detectors for confluence)
        for name, detector in self._detectors.items():
            if name not in _PHASE1_NAMES:
                outputs[name] = detector.detect(
                    ticker, df, rvol_df, precomputed=precomputed)

        # ── Step 2: classify ──────────────────────────────────────────────
        classification = self._classifier.classify(ticker, outputs)
        if classification is None:
            return

        strategy_name = classification.strategy_name
        tier          = classification.tier
        direction     = classification.direction
        confidence    = classification.confidence

        # ── Step 3: load strategy + detect_signal ─────────────────────────
        strategy_cls = STRATEGY_REGISTRY.get(strategy_name)
        if strategy_cls is None:
            log.warning("[ProSetupEngine][%s] unknown strategy: %s", ticker, strategy_name)
            return

        strategy = strategy_cls()
        confirmed = strategy.detect_signal(ticker, df, outputs)
        if confirmed is None:
            return

        # ── Step 4: generate entry / stop / exit ──────────────────────────
        try:
            entry_price = strategy.generate_entry(ticker, df, confirmed, outputs)
            atr         = compute_atr(df)
            # V8: Reject signal if ATR is too low relative to price (dead market)
            if atr is None or (entry_price > 0 and atr < entry_price * 0.001):
                log.debug("[ProSetupEngine][%s] ATR too low (%.4f) — skipping",
                          ticker, atr or 0)
                return
            stop_price  = strategy.generate_stop(entry_price, confirmed, atr, df, outputs=outputs)

            # V10: Context-aware smart stop.
            # Structural stop from strategy is the thesis-invalidation level.
            # Smart buffer ensures minimum room scaled by:
            #   - Current volatility (recent range vs ATR)
            #   - Time of day (open=wide, lunch=tight, close=wide)
            # Max risk check rejects trades where stop > 2 ATR (bad R:R).
            from monitor.smart_stop import compute_stop_buffer, should_reject_wide_stop

            _buffer = compute_stop_buffer(atr, df=df)

            if confirmed == 'long':
                actual_offset = entry_price - stop_price
                if should_reject_wide_stop(actual_offset, atr):
                    log.info("[ProSetupEngine][%s] REJECTED: stop too far "
                             "(%.2f ATR, max 2.0)", ticker, actual_offset / atr)
                    return
                if actual_offset < _buffer:
                    stop_price = entry_price - _buffer
            elif confirmed == 'short':
                actual_offset = stop_price - entry_price
                if should_reject_wide_stop(actual_offset, atr):
                    log.info("[ProSetupEngine][%s] REJECTED: stop too far "
                             "(%.2f ATR, max 2.0)", ticker, actual_offset / atr)
                    return
                if actual_offset < _buffer:
                    stop_price = entry_price + _buffer

            target_1, target_2 = strategy.generate_exit(entry_price, stop_price, confirmed)
        except Exception as exc:
            log.warning("[ProSetupEngine][%s][%s] level generation failed: %s", ticker, strategy_name, exc)
            return

        # ── Step 5: validate price levels ─────────────────────────────────
        if not self._levels_valid(ticker, strategy_name, confirmed,
                                   entry_price, stop_price, target_1, target_2):
            return

        # ── Step 6: compute supplementary metrics ─────────────────────────
        try:
            vwap_series = compute_vwap(df)
            vwap        = float(vwap_series.iloc[-1])
            rsi         = compute_rsi(df)

            # Update RVOLEngine with latest bar before reading
            try:
                from monitor.rvol import _global_rvol_engine
                if _global_rvol_engine is not None and len(df) > 0:
                    last_bar = df.iloc[-1]
                    bar_ts = last_bar.name if hasattr(last_bar, 'name') else None
                    _global_rvol_engine.update(
                        ticker, float(last_bar.get('volume', 0)), bar_ts,
                    )
            except Exception:
                pass

            rvol        = compute_rvol(df, rvol_df)
        except Exception:
            vwap = float(df['close'].iloc[-1])
            rsi  = 50.0
            rvol = 1.0

        # ── Step 7: build detector snapshot for audit ─────────────────────
        det_snap = {
            name: {
                'fired':     sig.fired,
                'direction': sig.direction,
                'strength':  round(sig.strength, 3),
            }
            for name, sig in outputs.items()
            if sig.fired
        }
        det_json = json.dumps(det_snap)

        # ── Step 8: emit PRO_STRATEGY_SIGNAL ─────────────────────────────
        # V10: Capture edge context at signal time
        _regime_str = ''
        try:
            from data_sources.market_regime import regime as _regime
            from data_sources.alt_data_reader import alt_data as _alt
            _regime_str = categorize_regime(_regime.score(), _alt.vix())
        except Exception:
            pass
        _time_bucket = compute_time_bucket()
        _confluence  = compute_confluence_score(det_json)

        # Strategies whose primary detector uses 5-min bars
        _5MIN_STRATEGIES = {
            'sr_flip', 'inside_bar', 'flag_pennant', 'fib_confluence',
            'trend_pullback',
        }
        _timeframe = '5min' if strategy_name in _5MIN_STRATEGIES else '1min'

        try:
            pro_payload = ProStrategySignalPayload(
                ticker           = ticker,
                strategy_name    = strategy_name,
                tier             = tier,
                direction        = confirmed,
                entry_price      = round(entry_price, 4),
                stop_price       = round(stop_price,  4),
                target_1         = round(target_1,    4),
                target_2         = round(target_2,    4),
                atr_value        = round(atr,         4),
                rvol             = round(rvol,         2),
                rsi_value        = round(rsi,          2),
                vwap             = round(max(vwap, 0.0001), 4),
                confidence       = round(confidence,   4),
                detector_signals = det_json,
                timeframe        = _timeframe,
                regime_at_entry  = _regime_str,
                time_bucket      = _time_bucket,
                confluence_score = round(_confluence, 4),
            )
        except (ValueError, TypeError) as exc:
            log.warning("[ProSetupEngine][%s][%s] payload validation failed: %s", ticker, strategy_name, exc)
            return

        elapsed_ms = (time.monotonic() - t0) * 1000
        log.info(
            "[ProSetupEngine][%s] SIGNAL  strategy=%s  tier=%d  dir=%s  "
            "entry=%.4f  stop=%.4f  t1=%.4f  t2=%.4f  "
            "R:R=%.2f  ATR=%.4f  RVOL=%.2f  RSI=%.1f  conf=%.0f%%  "
            "detectors_fired=%s  elapsed=%.1fms",
            ticker, strategy_name, tier, confirmed,
            entry_price, stop_price, target_1, target_2,
            (target_2 - entry_price) / max(entry_price - stop_price, 1e-6)
            if confirmed == 'long' else
            (entry_price - target_2) / max(stop_price - entry_price, 1e-6),
            atr, rvol, rsi, confidence * 100,
            list(det_snap.keys()),
            elapsed_ms,
        )

        self._bus.emit(
            Event(
                type           = EventType.PRO_STRATEGY_SIGNAL,
                payload        = pro_payload,
                correlation_id = event.event_id,
            ),
            durable=False,   # non-durable; ORDER_REQ (durable) follows
        )

    # ── Validation helper ─────────────────────────────────────────────────────

    @staticmethod
    def _levels_valid(
        ticker:      str,
        strategy:    str,
        direction:   str,
        entry:       float,
        stop:        float,
        target_1:    float,
        target_2:    float,
    ) -> bool:
        tag = f"[ProSetupEngine][{ticker}][{strategy}]"
        if entry <= 0 or stop <= 0 or target_1 <= 0 or target_2 <= 0:
            log.warning("%s invalid levels (non-positive): entry=%.4f stop=%.4f t1=%.4f t2=%.4f",
                        tag, entry, stop, target_1, target_2)
            return False
        # V8: Tier-specific min R:R for target_2 validation
        from pro_setups.risk.risk_adapter import _MIN_RR

        if direction == 'long':
            if not (stop < entry < target_1 < target_2):
                log.warning("%s invalid long levels: stop=%.4f entry=%.4f t1=%.4f t2=%.4f",
                            tag, stop, entry, target_1, target_2)
                return False
            # R:R check: reward (target_1 - entry) must be >= risk (entry - stop)
            risk = entry - stop
            reward = target_1 - entry
            if risk > 0 and reward / risk < 1.0:
                log.debug("%s rejected: R:R too low (%.2f) stop=%.2f entry=%.2f t1=%.2f",
                          tag, reward / risk, stop, entry, target_1)
                return False
            # V8: target_2 R:R check using tier-specific minimum
            reward_2 = target_2 - entry
            if risk > 0:
                rr_2 = reward_2 / risk
                # Extract tier from strategy name for tier-specific min R:R
                _tier_map = {
                    'sr_flip': 1, 'trend_pullback': 1, 'vwap_reclaim': 1,
                    'orb': 2, 'gap_and_go': 2, 'inside_bar': 2, 'flag_pennant': 2,
                    'momentum_ignition': 3, 'fib_confluence': 3,
                    'bollinger_squeeze': 3, 'liquidity_sweep': 3,
                }
                tier = _tier_map.get(strategy, 2)
                min_rr = _MIN_RR.get(tier, 1.5)
                if rr_2 < min_rr:
                    log.debug("%s rejected: target_2 R:R too low (%.2f < %.2f) t2=%.2f",
                              tag, rr_2, min_rr, target_2)
                    return False
        else:
            if not (stop > entry > target_1 > target_2):
                log.warning("%s invalid short levels: stop=%.4f entry=%.4f t1=%.4f t2=%.4f",
                            tag, stop, entry, target_1, target_2)
                return False
            risk = stop - entry
            reward = entry - target_1
            if risk > 0 and reward / risk < 1.0:
                log.debug("%s rejected: R:R too low (%.2f) stop=%.2f entry=%.2f t1=%.2f",
                          tag, reward / risk, stop, entry, target_1)
                return False
            # V8: target_2 R:R check for shorts
            reward_2 = entry - target_2
            if risk > 0:
                rr_2 = reward_2 / risk
                _tier_map = {
                    'sr_flip': 1, 'trend_pullback': 1, 'vwap_reclaim': 1,
                    'orb': 2, 'gap_and_go': 2, 'inside_bar': 2, 'flag_pennant': 2,
                    'momentum_ignition': 3, 'fib_confluence': 3,
                    'bollinger_squeeze': 3, 'liquidity_sweep': 3,
                }
                tier = _tier_map.get(strategy, 2)
                min_rr = _MIN_RR.get(tier, 1.5)
                if rr_2 < min_rr:
                    log.debug("%s rejected: target_2 R:R too low (%.2f < %.2f) t2=%.2f",
                              tag, rr_2, min_rr, target_2)
                    return False
        return True
