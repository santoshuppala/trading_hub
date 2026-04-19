"""
BacktestEngine — Main orchestrator for event-replay backtesting.

Coordinates:
1. Data loading (Tradier/yfinance/DB → intraday bars)
2. Dual-timeframe: 1-min (20 days) + 5-min (40 days) from Tradier
3. Daily state reset (VWAP, session boundaries)
4. Per-bar processing (all tickers at same timestamp)
5. Event emission → signal capture → fill simulation
6. Metrics computation
7. Optional: persist bars to DB (Option C hybrid)

No modifications to live code. All I/O boundaries replaced.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from zoneinfo import ZoneInfo

from backtests.sync_bus import BacktestBus
from backtests.fill_simulator import FillSimulator
from backtests.data_loader import BarDataLoader
from backtests.metrics import MetricsEngine, BacktestResult
from backtests.adapters.pro_adapter import ProBacktestAdapter
from monitor.event_bus import Event, EventType
from monitor.events import BarPayload
from monitor.risk_sizing import RiskSizer

try:
    from backtests.adapters.options_adapter import OptionsBacktestAdapter
    _HAS_OPTIONS_ADAPTER = True
except ImportError:
    _HAS_OPTIONS_ADAPTER = False

log = logging.getLogger(__name__)
ET = ZoneInfo('America/New_York')


class BacktestEngine:
    """
    Main backtesting orchestrator.

    Supports dual-timeframe loading:
      - 1-min bars: 20 trading days (Tradier limit)
      - 5-min bars: 40 trading days (Tradier limit)

    For sessions with 1-min data available: all detectors run normally.
    For sessions with only 5-min data: 5-min detectors work correctly,
    1-min detectors run on 5-min bars (coarser but functional).
    """

    def __init__(
        self,
        tickers: List[str],
        start_date: str | datetime,
        end_date: str | datetime,
        engines: List[str] = None,
        data_source: str = 'yfinance',
        trade_budget: float = 1000.0,
        use_risk_sizing: bool = True,
        save_to_db: bool = False,
        tradier_token: str = None,
        db_dsn: str = None,
        **engine_kwargs,
    ):
        """
        Initialize backtest engine.

        Args:
            tickers: list of symbols to backtest
            start_date: start of backtest period
            end_date: end of backtest period
            engines: ['pro', 'pop', 'options'] — which engines to run
            data_source: 'yfinance' | 'tradier' | 'db'
            trade_budget: starting capital per trade
            use_risk_sizing: enable beta/correlation risk checks
            save_to_db: persist fetched bars to market_bars table (Option C)
            tradier_token: Tradier API token (or TRADIER_TOKEN env var)
            db_dsn: PostgreSQL DSN (or DATABASE_URL env var)
        """
        self.tickers = tickers
        self.start_date = self._parse_date(start_date)
        self.end_date = self._parse_date(end_date)
        self.engines_to_run = engines or ['pro']
        self.data_source = data_source
        self.trade_budget = trade_budget
        self.use_risk_sizing = use_risk_sizing
        self.save_to_db = save_to_db
        self.engine_kwargs = engine_kwargs

        # Risk sizing
        self._sizer = RiskSizer() if use_risk_sizing else None
        self._atr_cache: Dict[str, float] = {}

        # Data loader
        self.loader = BarDataLoader(
            source=data_source,
            tradier_token=tradier_token,
            db_dsn=db_dsn,
        )
        self.rvol_baselines: Dict[str, pd.DataFrame] = {}
        self.intraday_1m: Dict[str, pd.DataFrame] = {}
        self.intraday_5m: Dict[str, pd.DataFrame] = {}

        # Initialize sync bus and fill simulator
        self.bus = BacktestBus()
        self.fill_simulator = FillSimulator(trade_budget=trade_budget)
        self.bus.capture.fill_simulator = self.fill_simulator

        # Wire risk sizing
        if self._sizer:
            self._wire_risk_sizing()

        # Initialize strategy engines
        self.adapters: List[Any] = []
        self._init_engines()

        # Metrics
        self.metrics_engine = MetricsEngine()

        # Session tracking
        self._sessions_1m = 0
        self._sessions_5m = 0

        log.info(
            "[BacktestEngine] Initialized: %d tickers, %s → %s, "
            "engines=%s, source=%s, save_to_db=%s",
            len(tickers),
            self.start_date.strftime('%Y-%m-%d'),
            self.end_date.strftime('%Y-%m-%d'),
            self.engines_to_run, data_source, save_to_db,
        )

    def run(self) -> BacktestResult:
        """Run complete backtest. Returns BacktestResult with all metrics."""
        log.info("[BacktestEngine] Loading data...")
        self._load_all_data()

        log.info("[BacktestEngine] Starting replay...")
        trading_days = self._get_trading_days()

        for session_date in trading_days:
            log.debug("[BacktestEngine] Processing %s", session_date.date())
            self._process_session(session_date)

        log.info(
            "[BacktestEngine] Replay done: %d sessions (%d×1m + %d×5m)",
            self._sessions_1m + self._sessions_5m,
            self._sessions_1m, self._sessions_5m,
        )

        log.info("[BacktestEngine] Computing metrics...")
        result = self.metrics_engine.compute(
            self.fill_simulator.closed_trades,
            self.bus.capture.all_signals,
        )

        return result

    # ══════════════════════════════════════════════════════════════════════
    # Data loading
    # ══════════════════════════════════════════════════════════════════════

    def _load_all_data(self) -> None:
        """Load RVOL baselines + dual-timeframe intraday bars."""
        # RVOL baseline (daily bars — always needed)
        log.info("[BacktestEngine] Loading RVOL baselines...")
        self.rvol_baselines = self.loader.load_rvol_baseline(
            self.tickers, self.start_date,
        )

        # 1-min bars (20 trading days from Tradier, full range from others)
        log.info("[BacktestEngine] Loading 1-min bars...")
        self.intraday_1m = self.loader.load_intraday(
            self.tickers, self.start_date, self.end_date, interval='1m',
        )

        # 5-min bars (40 trading days — wider window for structure detectors)
        log.info("[BacktestEngine] Loading 5-min bars...")
        self.intraday_5m = self.loader.load_intraday(
            self.tickers, self.start_date, self.end_date, interval='5m',
        )

        # Save to DB if requested (Option C)
        if self.save_to_db:
            log.info("[BacktestEngine] Persisting bars to DB...")
            n1 = self.loader.save_to_db(self.intraday_1m, '1m')
            n5 = self.loader.save_to_db(self.intraday_5m, '5m')
            log.info("[BacktestEngine] DB save: %d 1m rows + %d 5m rows", n1, n5)

        # Summary
        days_1m = len(set(
            d for df in self.intraday_1m.values()
            for d in df.index.date
        )) if self.intraday_1m else 0
        days_5m = len(set(
            d for df in self.intraday_5m.values()
            for d in df.index.date
        )) if self.intraday_5m else 0

        log.info(
            "[BacktestEngine] Data loaded: %d tickers with 1m (%d days), "
            "%d tickers with 5m (%d days), %d RVOL baselines",
            len(self.intraday_1m), days_1m,
            len(self.intraday_5m), days_5m,
            len(self.rvol_baselines),
        )

        # Warn about missing data
        for ticker in self.tickers:
            has_1m = ticker in self.intraday_1m and not self.intraday_1m[ticker].empty
            has_5m = ticker in self.intraday_5m and not self.intraday_5m[ticker].empty
            if not has_1m and not has_5m:
                log.warning("[BacktestEngine] No data at all for %s", ticker)

    # ══════════════════════════════════════════════════════════════════════
    # Session processing
    # ══════════════════════════════════════════════════════════════════════

    def _process_session(self, session_date: datetime) -> None:
        """Process one trading session (single day)."""
        # Determine best data source for this session
        has_1m = self._has_data_for_date(self.intraday_1m, session_date)
        has_5m = self._has_data_for_date(self.intraday_5m, session_date)

        if has_1m:
            data_source = self.intraday_1m
            self._sessions_1m += 1
        elif has_5m:
            data_source = self.intraday_5m
            self._sessions_5m += 1
        else:
            log.debug("[BacktestEngine] No data for %s", session_date.date())
            return

        # Collect all timestamps for this session across all tickers
        session_timestamps: List[datetime] = []
        for ticker in self.tickers:
            if ticker not in data_source:
                continue
            df = data_source[ticker]
            mask = df.index.date == session_date.date()
            session_timestamps.extend(df[mask].index.tolist())

        if not session_timestamps:
            return

        session_timestamps = sorted(set(session_timestamps))

        # Rolling windows per ticker (reset each session)
        rolling_windows: Dict[str, pd.DataFrame] = {
            t: pd.DataFrame() for t in self.tickers
        }

        # Emit bars in timestamp order
        for i, timestamp in enumerate(session_timestamps):
            is_eod = (i == len(session_timestamps) - 1)

            for ticker in self.tickers:
                if ticker not in data_source:
                    continue
                df = data_source[ticker]
                if timestamp not in df.index:
                    continue

                bar = df.loc[timestamp]

                # Update rolling window (capped at 200 bars)
                rolling_windows[ticker] = self._extend_window(
                    rolling_windows[ticker], bar, max_rows=200,
                )

                # Fill simulation
                self.fill_simulator.process_bar(ticker, bar, is_eod=is_eod)

                # Update ATR cache for risk sizing
                if len(rolling_windows[ticker]) >= 14:
                    try:
                        rw = rolling_windows[ticker]
                        tr = rw['high'].iloc[-14:] - rw['low'].iloc[-14:]
                        self._atr_cache[ticker] = float(tr.mean())
                        for adapter in self.adapters:
                            if hasattr(adapter, 'update_bar'):
                                spot = float(bar['close']) if isinstance(bar, dict) else float(bar.close)
                                adapter.update_bar(ticker, self._atr_cache[ticker], spot)
                    except Exception:
                        pass

                # Skip until enough data
                if len(rolling_windows[ticker]) < 30:
                    continue

                # Emit BAR event
                payload = BarPayload(
                    ticker=ticker,
                    df=rolling_windows[ticker].copy(),
                    rvol_df=self.rvol_baselines.get(ticker),
                )
                event = Event(EventType.BAR, payload)
                self.bus.bus.emit(event)

            # Equity snapshot
            self.metrics_engine.record(timestamp, self.fill_simulator.equity_value())

        # Reset for next session
        self.fill_simulator.reset_session()
        self.bus.reset_for_session()

    def _has_data_for_date(
        self,
        data: Dict[str, pd.DataFrame],
        session_date: datetime,
    ) -> bool:
        """Check if any ticker has data for the given date."""
        target_date = session_date.date()
        for df in data.values():
            if df is not None and not df.empty:
                if (df.index.date == target_date).any():
                    return True
        return False

    # ══════════════════════════════════════════════════════════════════════
    # Helper methods (unchanged from original)
    # ══════════════════════════════════════════════════════════════════════

    def _get_trading_days(self) -> List[datetime]:
        """Get list of weekdays in the period."""
        all_days = []
        current = self.start_date
        while current <= self.end_date:
            if current.weekday() < 5:  # Monday=0, Friday=4
                all_days.append(current)
            current += timedelta(days=1)
        return all_days

    def _wire_risk_sizing(self) -> None:
        """Wrap SignalCapture handlers to apply correlation + beta sizing."""
        capture = self.bus.capture
        fill_sim = self.fill_simulator
        sizer = self._sizer

        _orig_on_pro = capture.on_pro
        _orig_on_pop = capture.on_pop

        def _held_tickers() -> set:
            held = set()
            for layer_positions in fill_sim.open_positions.values():
                for pos in layer_positions:
                    held.add(pos.ticker)
            return held

        def _risked_on_pro(event) -> None:
            payload = event.payload
            ticker = payload.ticker

            held = _held_tickers()
            corr_ok, corr_reason = sizer.check_correlation(ticker, held)
            if not corr_ok:
                log.debug("[Backtest] Correlation block: %s — %s", ticker, corr_reason)
                capture.all_signals.append(('pro_blocked', payload))
                return

            base_qty = getattr(payload, 'qty', 1)
            price = getattr(payload, 'entry_price', 0.0)
            atr_value = self._atr_cache.get(ticker, 0.0)

            sizing = sizer.adjust_size(
                ticker=ticker, base_qty=base_qty, price=price,
                atr_value=atr_value, trade_budget=self.trade_budget,
            )
            if sizing.adjusted_qty != base_qty:
                log.debug("[Backtest] Risk sizing %s: %d → %d (%s)",
                          ticker, base_qty, sizing.adjusted_qty,
                          sizing.adjustment_reason)
            payload.qty = sizing.adjusted_qty

            capture.all_signals.append(('pro', payload))
            if fill_sim:
                fill_sim.queue_from_pro(payload)

        def _risked_on_pop(event) -> None:
            payload = event.payload
            ticker = payload.ticker

            held = _held_tickers()
            corr_ok, corr_reason = sizer.check_correlation(ticker, held)
            if not corr_ok:
                log.debug("[Backtest] Correlation block: %s — %s", ticker, corr_reason)
                capture.all_signals.append(('pop_blocked', payload))
                return

            base_qty = getattr(payload, 'qty', 1)
            price = getattr(payload, 'entry_price', 0.0)
            atr_value = self._atr_cache.get(ticker, 0.0)

            sizing = sizer.adjust_size(
                ticker=ticker, base_qty=base_qty, price=price,
                atr_value=atr_value, trade_budget=self.trade_budget,
            )
            if sizing.adjusted_qty != base_qty:
                log.debug("[Backtest] Risk sizing %s: %d → %d (%s)",
                          ticker, base_qty, sizing.adjusted_qty,
                          sizing.adjustment_reason)
            payload.qty = sizing.adjusted_qty

            capture.all_signals.append(('pop', payload))
            if fill_sim:
                fill_sim.queue_from_pop(payload)

        capture.on_pro = _risked_on_pro
        capture.on_pop = _risked_on_pop
        log.info("[BacktestEngine] Risk sizing wired: correlation + beta/vol adjustments")

    def _init_engines(self) -> None:
        """Initialize strategy engines based on engines_to_run."""
        # VWAP StrategyEngine — always init alongside pro (it's the core strategy)
        if 'pro' in self.engines_to_run or 'vwap' in self.engines_to_run:
            try:
                from monitor.strategy_engine import StrategyEngine
                from config import STRATEGY_PARAMS
                # StrategyEngine needs positions dict — create shared one
                self._bt_positions = {}
                self._vwap_engine = StrategyEngine(
                    bus=self.bus.bus,
                    positions=self._bt_positions,
                    strategy_params=STRATEGY_PARAMS,
                )
                # Sync positions on fill close so VWAP can re-enter
                self.fill_simulator.register_close_callback(
                    'pro', lambda ticker: self._bt_positions.pop(ticker, None),
                )
                log.info("[BacktestEngine] Initialized StrategyEngine (VWAP)")
            except Exception as e:
                log.warning("[BacktestEngine] Failed to init StrategyEngine: %s", e)

        if 'pro' in self.engines_to_run:
            try:
                adapter = ProBacktestAdapter(self.bus.bus, self.fill_simulator)
                self.adapters.append(adapter)
                log.info("[BacktestEngine] Initialized ProSetupEngine adapter")
            except Exception as e:
                log.warning("[BacktestEngine] Failed to init ProSetupEngine: %s", e)

        if 'options' in self.engines_to_run and _HAS_OPTIONS_ADAPTER:
            try:
                adapter = OptionsBacktestAdapter(self.bus.bus, self.fill_simulator)
                self.adapters.append(adapter)
                log.info("[BacktestEngine] Initialized OptionsEngine adapter")
            except Exception as e:
                log.warning("[BacktestEngine] Failed to init OptionsEngine: %s", e)

    @staticmethod
    def _extend_window(df: pd.DataFrame, bar: Any, max_rows: int = 200) -> pd.DataFrame:
        """Extend rolling window with new bar."""
        try:
            bar_dict = {
                'open': float(bar['open']) if isinstance(bar, dict) else float(bar.open),
                'high': float(bar['high']) if isinstance(bar, dict) else float(bar.high),
                'low': float(bar['low']) if isinstance(bar, dict) else float(bar.low),
                'close': float(bar['close']) if isinstance(bar, dict) else float(bar.close),
                'volume': float(bar['volume']) if isinstance(bar, dict) else float(bar.volume),
            }
        except (KeyError, AttributeError, TypeError):
            return df

        if isinstance(bar, dict) and 'Datetime' in bar:
            index = [bar['Datetime']]
        else:
            index = [bar.name] if hasattr(bar, 'name') else [None]

        new_row = pd.DataFrame([bar_dict], index=index)
        df = pd.concat([df, new_row], ignore_index=False)

        if len(df) > max_rows:
            df = df.iloc[-max_rows:]

        return df

    @staticmethod
    def _parse_date(date_input: str | datetime) -> datetime:
        """Parse date string or datetime object."""
        if isinstance(date_input, datetime):
            if date_input.tzinfo is None:
                return date_input.replace(tzinfo=ET)
            return date_input
        try:
            dt = datetime.strptime(date_input, '%Y-%m-%d')
            return dt.replace(tzinfo=ET)
        except ValueError:
            dt = datetime.fromisoformat(date_input)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ET)
            return dt
