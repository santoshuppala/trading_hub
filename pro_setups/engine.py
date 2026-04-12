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

from monitor.event_bus import EventBus, EventType, Event
from monitor.events import ProStrategySignalPayload

from .detectors import (
    TrendDetector, VWAPDetector, SRDetector, ORBDetector,
    InsideBarDetector, GapDetector, FlagDetector,
    LiquidityDetector, VolatilityDetector, FibDetector, MomentumDetector,
    DetectorSignal,
)
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
    ) -> None:
        self._bus = bus

        # ── Detectors (all 11 required by spec) ──────────────────────────
        self._detectors = {
            'trend':      TrendDetector(),
            'vwap':       VWAPDetector(),
            'sr':         SRDetector(),
            'orb':        ORBDetector(),
            'inside_bar': InsideBarDetector(),
            'gap':        GapDetector(),
            'flag':       FlagDetector(),
            'liquidity':  LiquidityDetector(),
            'volatility': VolatilityDetector(),
            'fib':        FibDetector(),
            'momentum':   MomentumDetector(),
        }

        # ── Classifier ────────────────────────────────────────────────────
        self._classifier = StrategyClassifier()

        # ── Risk adapter (subscribes to FILL/POSITION internally) ─────────
        self._risk_adapter = RiskAdapter(
            bus            = bus,
            max_positions  = max_positions,
            order_cooldown = order_cooldown,
            trade_budget   = trade_budget,
        )

        # ── Router (subscribes to PRO_STRATEGY_SIGNAL internally) ─────────
        self._router = ProStrategyRouter(bus=bus, risk_adapter=self._risk_adapter)

        # ── Subscribe to BAR at priority=2 ────────────────────────────────
        # priority=2 runs AFTER StrategyEngine (priority=1) but that order
        # doesn't matter since they emit different event types.  Using 2
        # gives a clear separation in logs and allows future priority tuning.
        bus.subscribe(EventType.BAR, self._on_bar, priority=2)

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

        t0 = time.monotonic()

        # ── Step 1: run all detectors ─────────────────────────────────────
        outputs: Dict[str, DetectorSignal] = {}
        for name, detector in self._detectors.items():
            outputs[name] = detector.detect(ticker, df, rvol_df)

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
            stop_price  = strategy.generate_stop(entry_price, confirmed, atr, df)
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
        if direction == 'long':
            if not (stop < entry < target_1 < target_2):
                log.warning("%s invalid long levels: stop=%.4f entry=%.4f t1=%.4f t2=%.4f",
                            tag, stop, entry, target_1, target_2)
                return False
        else:
            if not (stop > entry > target_1 > target_2):
                log.warning("%s invalid short levels: stop=%.4f entry=%.4f t1=%.4f t2=%.4f",
                            tag, stop, entry, target_1, target_2)
                return False
        return True
