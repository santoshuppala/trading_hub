"""
T3.7 — Options Engine

Main orchestrator for options trading subsystem.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Dict, Optional

from monitor.event_bus import Event, EventBus, EventType
from monitor.events import OptionsSignalPayload, SignalAction
from monitor.position_registry import registry

from .broker import AlpacaOptionsBroker
from .chain import AlpacaOptionChainClient
from .risk import OptionsRiskGate, LAYER_NAME
from .selector import OptionStrategySelector
from .strategies import STRATEGY_REGISTRY

log = logging.getLogger(__name__)

# Minimum bars before running neutral/volatility scan
_MIN_BARS: int = 52


class OptionsEngine:
    """
    T3.7 — Options Engine.

    Subscribes to:
      - EventType.SIGNAL  (from StrategyEngine) — directional options
      - EventType.BAR     (direct scan)         — neutral/volatility options

    Pipeline (SIGNAL path):
      SIGNAL → OptionStrategySelector.select_from_signal()
             → selected strategy type
             → OptionsRiskGate.check()
             → AlpacaOptionChainClient.get_chain()
             → strategy.build() → OptionsTradeSpec
             → emit OPTIONS_SIGNAL (durable audit)
             → AlpacaOptionsBroker.execute()

    Pipeline (BAR path):
      BAR → compute RSI/ATR/RVOL
          → OptionStrategySelector.select_from_bar()
          → same risk + chain + build + emit + execute path

    Credentials: APCA_OPTIONS_KEY / APCA_OPTIONS_SECRET (separate account).
    Budget: $10K total, $500 per trade, max 5 concurrent positions.
    Position isolation: layer='options' in GlobalPositionRegistry.
    """

    def __init__(
        self,
        bus:            EventBus,
        options_key:    Optional[str] = None,
        options_secret: Optional[str] = None,
        paper:          bool          = True,
        max_positions:  int           = 5,
        trade_budget:   float         = 500.0,
        total_budget:   float         = 10_000.0,
        order_cooldown: int           = 300,
        min_dte:        int           = 20,
        max_dte:        int           = 45,
        leaps_dte:      int           = 365,
        delta_directional: float      = 0.35,
        delta_spread:      float      = 0.175,
        alert_email:    Optional[str] = None,
    ) -> None:
        self._bus              = bus
        self._min_dte          = min_dte
        self._max_dte          = max_dte
        self._leaps_dte        = leaps_dte
        self._delta_directional = delta_directional
        self._delta_spread     = delta_spread
        self._alert_email      = alert_email

        # ── Option chain client (read-only, data only) ──────────────────
        if options_key and options_secret:
            self._chain = AlpacaOptionChainClient(options_key, options_secret)
        else:
            self._chain = None
            log.warning("[OptionsEngine] No options keys — chain client unavailable; paper mode only")

        # ── Risk gate (independent) ─────────────────────────────────────
        self._risk = OptionsRiskGate(
            max_positions=max_positions,
            trade_budget=trade_budget,
            total_budget=total_budget,
            order_cooldown=order_cooldown,
        )

        # ── Selector ────────────────────────────────────────────────────
        self._selector = OptionStrategySelector()

        # ── Broker (dedicated options account) ──────────────────────────
        if options_key and options_secret:
            try:
                from alpaca.trading.client import TradingClient
                trading_client = TradingClient(
                    api_key=options_key,
                    secret_key=options_secret,
                    paper=paper
                )
                self._broker = AlpacaOptionsBroker(trading_client, alert_email)
            except ImportError:
                log.error("[OptionsEngine] alpaca-py not installed; broker unavailable")
                self._broker = None
        else:
            self._broker = None

        # ── Open positions tracking (for exit management) ────────────────
        # ticker → {'spec': OptionsTradeSpec, 'entry_time': float, 'cost': float}
        self._positions: Dict[str, dict] = {}
        self._positions_lock = threading.Lock()

        # ── Subscribe ───────────────────────────────────────────────────
        # priority=3: runs after StrategyEngine(1), ProSetupEngine(2), PopStrategyEngine
        bus.subscribe(EventType.SIGNAL, self._on_signal, priority=3)
        bus.subscribe(EventType.BAR,    self._on_bar,    priority=3)

        log.info(
            "[OptionsEngine] ready | max_pos=%d | budget=$%.0f/trade | total=$%.0f | "
            "DTE=%d-%d | delta_dir=%.2f | delta_spread=%.2f | paper=%s | chain=%s | broker=%s",
            max_positions, trade_budget, total_budget,
            min_dte, max_dte, delta_directional, delta_spread,
            paper, "OK" if self._chain else "NONE", "OK" if self._broker else "NONE",
        )

    # ── SIGNAL handler ────────────────────────────────────────────────────────

    def _on_signal(self, event: Event) -> None:
        """
        Convert an equity SIGNAL into an options trade if appropriate.

        Key guard: only acts on SignalAction.BUY and SELL_* signals.
        HOLD and PARTIAL_SELL are ignored.
        """
        p = event.payload   # SignalPayload
        action = str(p.action)

        # Only react to directional entry/exit signals
        if action not in (
            SignalAction.BUY.value,
            SignalAction.SELL_STOP.value,
            SignalAction.SELL_TARGET.value,
            SignalAction.SELL_RSI.value,
            SignalAction.SELL_VWAP.value,
        ):
            return

        if self._chain is None:
            return

        # Estimate IV from chain before selection (lightweight spot check)
        iv_estimate = self._estimate_iv(p.ticker, p.current_price)

        strategy_type = self._selector.select_from_signal(
            action=action,
            rvol=p.rvol,
            rsi=p.rsi_value,
            atr_value=p.atr_value,
            spot_price=p.current_price,
            iv_estimate=iv_estimate,
        )

        if strategy_type is None:
            return

        self._execute_strategy(
            ticker=p.ticker,
            spot_price=p.current_price,
            strategy_type=strategy_type,
            atr_value=p.atr_value,
            rvol=p.rvol,
            rsi_value=p.rsi_value,
            source='signal',
            source_event=event,
        )

    # ── BAR handler ──────────────────────────────────────────────────────────

    def _on_bar(self, event: Event) -> None:
        """
        Independent neutral/volatility scan on every bar.
        Only runs when SIGNAL path has not already committed to this ticker.
        """
        p = event.payload   # BarPayload
        df = p.df

        if len(df) < _MIN_BARS:
            return

        if self._chain is None:
            return

        # Compute indicators locally (mirrors ProSetupEngine pattern)
        try:
            # Try to import compute helpers (may not exist yet)
            from pro_setups.detectors._compute import compute_atr, compute_rsi, compute_rvol
            atr_value = compute_atr(df)
            rsi_value = compute_rsi(df)
            rvol = compute_rvol(df, p.rvol_df)
        except (ImportError, Exception):
            # Fallback: use basic TA if helpers not available
            atr_value = 0.5  # placeholder
            rsi_value = 50.0  # neutral
            rvol = 1.0  # neutral

        spot = float(df['close'].iloc[-1])

        iv_estimate = self._estimate_iv(p.ticker, spot)

        has_pos = registry.is_held(p.ticker)

        strategy_type = self._selector.select_from_bar(
            atr_value=atr_value,
            spot_price=spot,
            iv_estimate=iv_estimate,
            rsi=rsi_value,
            rvol=rvol,
            has_existing_position=has_pos,
        )

        if strategy_type is None:
            return

        self._execute_strategy(
            ticker=p.ticker,
            spot_price=spot,
            strategy_type=strategy_type,
            atr_value=atr_value,
            rvol=rvol,
            rsi_value=rsi_value,
            source='bar_scan',
            source_event=event,
        )

    # ── Core execution path ──────────────────────────────────────────────────

    def _execute_strategy(
        self,
        ticker:        str,
        spot_price:    float,
        strategy_type: str,
        atr_value:     float,
        rvol:          float,
        rsi_value:     float,
        source:        str,
        source_event:  Event,
    ) -> None:
        """
        Steps:
        1. Risk gate check (max_positions, cooldown, budget, registry)
        2. Fetch option chain via AlpacaOptionChainClient
        3. Build OptionsTradeSpec via strategy class
        4. Risk gate acquire (reserve position)
        5. Emit OPTIONS_SIGNAL (durable=True for Redpanda audit)
        6. Execute via AlpacaOptionsBroker.execute()
        7. On failure: release from risk gate
        """
        # Step 1: Risk gate check
        reject_reason = self._risk.check(ticker, max_risk=self._risk._trade_budget)
        if reject_reason:
            log.debug(f"[OptionsEngine] {ticker} {strategy_type} BLOCKED: {reject_reason}")
            return

        # Step 2: Get strategy class
        if strategy_type not in STRATEGY_REGISTRY:
            log.warning(f"[OptionsEngine] unknown strategy type: {strategy_type}")
            return

        strategy_class = STRATEGY_REGISTRY[strategy_type]
        strategy = strategy_class()

        # Step 3: Build trade spec
        if not self._chain:
            log.debug(f"[OptionsEngine] no chain client available")
            return

        trade_spec = strategy.build(
            ticker=ticker,
            underlying_price=spot_price,
            chain_client=self._chain,
            min_dte=self._min_dte,
            max_dte=self._max_dte,
            delta_directional=self._delta_directional,
            delta_spread=self._delta_spread,
        )

        if trade_spec is None:
            log.debug(f"[OptionsEngine] {ticker} {strategy_type} build returned None")
            return

        # Step 4: Verify risk gate one more time with actual max_risk
        reject_reason = self._risk.check(ticker, max_risk=trade_spec.max_risk)
        if reject_reason:
            log.debug(f"[OptionsEngine] {ticker} {strategy_type} BLOCKED after build: {reject_reason}")
            return

        # Step 5: Emit OPTIONS_SIGNAL (durable)
        self._emit_options_signal(
            ticker=ticker,
            spot_price=spot_price,
            strategy_type=strategy_type,
            trade_spec=trade_spec,
            atr_value=atr_value,
            rvol=rvol,
            rsi_value=rsi_value,
            source=source,
            source_event=source_event,
        )

        # Step 6: Acquire position (lock capital)
        self._risk.acquire(ticker, cost=trade_spec.net_debit)

        # Track position
        with self._positions_lock:
            self._positions[ticker] = {
                'spec': trade_spec,
                'entry_time': time.time(),
                'cost': trade_spec.net_debit,
            }

        # Step 7: Execute trade
        if self._broker:
            success = self._broker.execute(trade_spec, source_event)
            if not success:
                log.error(f"[OptionsEngine] execution failed for {ticker}; releasing position")
                self._risk.release(ticker)
                with self._positions_lock:
                    self._positions.pop(ticker, None)

    def _emit_options_signal(
        self,
        ticker:     str,
        spot_price: float,
        strategy_type: str,
        trade_spec,  # OptionsTradeSpec
        atr_value:  float,
        rvol:       float,
        rsi_value:  float,
        source:     str,
        source_event: Event,
    ) -> None:
        """Build OptionsSignalPayload, serialize legs to JSON, emit durable=True."""
        legs_list = [
            {
                'symbol': l.symbol,
                'side': l.side,
                'qty': l.qty,
                'ratio': l.ratio,
                'limit_price': l.limit_price,
            }
            for l in trade_spec.legs
        ]
        payload = OptionsSignalPayload(
            ticker=ticker,
            strategy_type=strategy_type,
            underlying_price=spot_price,
            expiry_date=trade_spec.expiry_date,
            net_debit=trade_spec.net_debit,
            max_risk=trade_spec.max_risk,
            max_reward=trade_spec.max_reward,
            atr_value=atr_value,
            rvol=rvol,
            rsi_value=rsi_value,
            legs_json=json.dumps(legs_list),
            source=source,
        )
        self._bus.emit(Event(EventType.OPTIONS_SIGNAL, payload), durable=True)
        log.info(
            f"[OptionsEngine] emitted OPTIONS_SIGNAL | {ticker} {strategy_type} | "
            f"${trade_spec.net_debit:.2f} debit | max_risk ${trade_spec.max_risk:.2f}"
        )

    def _estimate_iv(self, ticker: str, spot_price: float) -> float:
        """
        Quick IV estimate from the nearest ATM option.

        Returns 0.25 (neutral assumption) on any API error.
        Falls back gracefully — never blocks strategy selection.
        """
        if not self._chain:
            return 0.25

        try:
            contracts = self._chain.get_chain(ticker, min_dte=self._min_dte, max_dte=self._max_dte)
            if not contracts:
                return 0.25

            # Find ATM call
            atm_call = self._chain.find_atm(contracts, 'call', spot_price)
            if atm_call and atm_call.iv > 0:
                return atm_call.iv

            return 0.25
        except Exception as e:
            log.debug(f"[OptionsEngine] IV estimate error for {ticker}: {e}")
            return 0.25
