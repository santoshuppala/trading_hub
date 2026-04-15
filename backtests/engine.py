"""
BacktestEngine — Main orchestrator for event-replay backtesting.

Coordinates:
1. Data loading (yfinance → intraday bars)
2. Daily state reset (VWAP, session boundaries)
3. Per-bar processing (all tickers at same timestamp)
4. Event emission → signal capture → fill simulation
5. Metrics computation

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

    Coordinates data loading, event emission, signal capture, and metrics.
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
        **engine_kwargs,
    ):
        """
        Initialize backtest engine.

        Args:
            tickers: list of symbols to backtest
            start_date: start of backtest period
            end_date: end of backtest period
            engines: ['pro', 'pop', 'options'] — which engines to run
            data_source: 'yfinance' (default) | 'tradier' | 'alpaca'
            trade_budget: starting capital
            use_risk_sizing: enable beta/correlation risk checks (default True)
            **engine_kwargs: passed to engine constructors (not yet used)
        """
        self.tickers = tickers
        self.start_date = self._parse_date(start_date)
        self.end_date = self._parse_date(end_date)
        self.engines_to_run = engines or ['pro']
        self.data_source = data_source
        self.trade_budget = trade_budget
        self.use_risk_sizing = use_risk_sizing
        self.engine_kwargs = engine_kwargs

        # Risk sizing (beta-adjusted, correlation-aware)
        self._sizer = RiskSizer() if use_risk_sizing else None
        # Per-ticker ATR cache (updated each bar for sizing adjustments)
        self._atr_cache: Dict[str, float] = {}

        # Load data
        self.loader = BarDataLoader(source=data_source)
        self.rvol_baselines: Dict[str, pd.DataFrame] = {}
        self.intraday_data: Dict[str, pd.DataFrame] = {}

        # Initialize sync bus and fill simulator
        self.bus = BacktestBus()
        self.fill_simulator = FillSimulator(trade_budget=trade_budget)
        self.bus.capture.fill_simulator = self.fill_simulator

        # Wire risk sizing into the signal capture pipeline
        if self._sizer:
            self._wire_risk_sizing()

        # Initialize strategy engines
        self.adapters: List[Any] = []
        self._init_engines()

        # Metrics
        self.metrics_engine = MetricsEngine()

        log.info(f"[BacktestEngine] Initialized: {tickers}, {self.start_date} → {self.end_date}, "
                 f"engines={self.engines_to_run}")

    def run(self) -> BacktestResult:
        """
        Run complete backtest.

        Returns:
            BacktestResult with all metrics
        """
        log.info("[BacktestEngine] Loading data...")
        self._load_all_data()

        log.info("[BacktestEngine] Starting replay...")
        trading_days = self._get_trading_days()

        for session_date in trading_days:
            log.debug(f"[BacktestEngine] Processing {session_date.date()}")
            self._process_session(session_date)

        log.info("[BacktestEngine] Computing metrics...")
        result = self.metrics_engine.compute(
            self.fill_simulator.closed_trades,
            self.bus.capture.all_signals,
        )

        return result

    # Private methods

    def _wire_risk_sizing(self) -> None:
        """Wrap SignalCapture handlers to apply correlation + beta sizing before fill queuing."""
        capture = self.bus.capture
        fill_sim = self.fill_simulator
        sizer = self._sizer

        # Save original handlers
        _orig_on_pro = capture.on_pro
        _orig_on_pop = capture.on_pop

        def _held_tickers() -> set:
            """Return set of tickers currently held across all layers."""
            held = set()
            for layer_positions in fill_sim.open_positions.values():
                for pos in layer_positions:
                    held.add(pos.ticker)
            return held

        def _risked_on_pro(event) -> None:
            payload = event.payload
            ticker = payload.ticker

            # 1. Correlation check
            held = _held_tickers()
            corr_ok, corr_reason = sizer.check_correlation(ticker, held)
            if not corr_ok:
                log.debug("[Backtest] Correlation block: %s — %s", ticker, corr_reason)
                capture.all_signals.append(('pro_blocked', payload))
                return

            # 2. Beta / volatility sizing adjustment
            base_qty = getattr(payload, 'qty', 1)
            price = getattr(payload, 'entry_price', 0.0)
            atr_value = self._atr_cache.get(ticker, 0.0)

            sizing = sizer.adjust_size(
                ticker=ticker,
                base_qty=base_qty,
                price=price,
                atr_value=atr_value,
                trade_budget=self.trade_budget,
            )
            if sizing.adjusted_qty != base_qty:
                log.debug(
                    "[Backtest] Risk sizing %s: %d → %d (%s)",
                    ticker, base_qty, sizing.adjusted_qty, sizing.adjustment_reason,
                )
            payload.qty = sizing.adjusted_qty

            # Record signal and queue fill
            capture.all_signals.append(('pro', payload))
            if fill_sim:
                fill_sim.queue_from_pro(payload)

        def _risked_on_pop(event) -> None:
            payload = event.payload
            ticker = payload.ticker

            # 1. Correlation check
            held = _held_tickers()
            corr_ok, corr_reason = sizer.check_correlation(ticker, held)
            if not corr_ok:
                log.debug("[Backtest] Correlation block: %s — %s", ticker, corr_reason)
                capture.all_signals.append(('pop_blocked', payload))
                return

            # 2. Beta / volatility sizing adjustment
            base_qty = getattr(payload, 'qty', 1)
            price = getattr(payload, 'entry_price', 0.0)
            atr_value = self._atr_cache.get(ticker, 0.0)

            sizing = sizer.adjust_size(
                ticker=ticker,
                base_qty=base_qty,
                price=price,
                atr_value=atr_value,
                trade_budget=self.trade_budget,
            )
            if sizing.adjusted_qty != base_qty:
                log.debug(
                    "[Backtest] Risk sizing %s: %d → %d (%s)",
                    ticker, base_qty, sizing.adjusted_qty, sizing.adjustment_reason,
                )
            payload.qty = sizing.adjusted_qty

            # Record signal and queue fill
            capture.all_signals.append(('pop', payload))
            if fill_sim:
                fill_sim.queue_from_pop(payload)

        # Replace handlers on the capture object
        capture.on_pro = _risked_on_pro
        capture.on_pop = _risked_on_pop
        log.info("[BacktestEngine] Risk sizing wired: correlation + beta/vol adjustments enabled")

    def _init_engines(self) -> None:
        """Initialize strategy engines based on engines_to_run."""
        if 'pro' in self.engines_to_run:
            try:
                adapter = ProBacktestAdapter(self.bus.bus, self.fill_simulator)
                self.adapters.append(adapter)
                log.info("[BacktestEngine] Initialized ProSetupEngine adapter")
            except Exception as e:
                log.warning(f"[BacktestEngine] Failed to init ProSetupEngine: {e}")

        if 'options' in self.engines_to_run and _HAS_OPTIONS_ADAPTER:
            try:
                adapter = OptionsBacktestAdapter(self.bus.bus, self.fill_simulator)
                self.adapters.append(adapter)
                log.info("[BacktestEngine] Initialized OptionsEngine adapter")
            except Exception as e:
                log.warning(f"[BacktestEngine] Failed to init OptionsEngine: {e}")

    def _load_all_data(self) -> None:
        """Load RVOL baselines and intraday bars."""
        log.info("[BacktestEngine] Loading RVOL baselines...")
        self.rvol_baselines = self.loader.load_rvol_baseline(self.tickers, self.start_date)

        log.info("[BacktestEngine] Loading intraday bars...")
        self.intraday_data = self.loader.load_intraday(self.tickers, self.start_date, self.end_date)

        # Warn if any tickers have no data
        for ticker in self.tickers:
            if ticker not in self.intraday_data or len(self.intraday_data[ticker]) == 0:
                log.warning(f"[BacktestEngine] No intraday data for {ticker}")

    def _get_trading_days(self) -> List[datetime]:
        """Get list of trading days in the period."""
        all_days = []
        current = self.start_date
        while current <= self.end_date:
            # Skip weekends
            if current.weekday() < 5:  # Monday=0, Friday=4
                all_days.append(current)
            current += timedelta(days=1)
        return all_days

    def _process_session(self, session_date: datetime) -> None:
        """Process one trading session (single day)."""
        # Get all timestamps for this session across all tickers
        session_timestamps: List[datetime] = []
        for ticker in self.tickers:
            if ticker not in self.intraday_data:
                continue
            df = self.intraday_data[ticker]
            mask = df.index.date == session_date.date()
            session_timestamps.extend(df[mask].index.tolist())

        if not session_timestamps:
            log.debug(f"[BacktestEngine] No data for {session_date.date()}")
            return

        session_timestamps = sorted(set(session_timestamps))

        # Rolling windows for indicator computation per ticker
        rolling_windows: Dict[str, pd.DataFrame] = {t: pd.DataFrame() for t in self.tickers}

        # Emit bars in timestamp order
        for i, timestamp in enumerate(session_timestamps):
            is_eod = (i == len(session_timestamps) - 1)

            # Update bar for each ticker
            for ticker in self.tickers:
                if ticker not in self.intraday_data:
                    continue

                df = self.intraday_data[ticker]
                if timestamp not in df.index:
                    continue

                bar = df.loc[timestamp]

                # Update rolling window (capped at 200 bars)
                rolling_windows[ticker] = self._extend_window(rolling_windows[ticker], bar, max_rows=200)

                # Process fill simulation
                self.fill_simulator.process_bar(ticker, bar, is_eod=is_eod)

                # Update options chain with current bar data
                if len(rolling_windows[ticker]) >= 14:
                    try:
                        spot = float(bar['close']) if isinstance(bar, dict) else float(bar.close)
                        rw = rolling_windows[ticker]
                        # Simple ATR: average true range over last 14 bars
                        if len(rw) >= 14:
                            tr = rw['high'].iloc[-14:] - rw['low'].iloc[-14:]
                            atr = float(tr.mean())
                        else:
                            atr = 0.5
                        # Cache ATR for risk sizing adjustments
                        self._atr_cache[ticker] = atr
                        for adapter in self.adapters:
                            if hasattr(adapter, 'update_bar'):
                                adapter.update_bar(ticker, atr, spot)
                    except Exception:
                        pass

                # Skip emissions until we have enough data (30-bar minimum)
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

            # Record equity snapshot
            self.metrics_engine.record(timestamp, self.fill_simulator.equity_value())

        # Reset for next session
        self.fill_simulator.reset_session()
        self.bus.reset_for_session()

    @staticmethod
    def _extend_window(df: pd.DataFrame, bar: Any, max_rows: int = 200) -> pd.DataFrame:
        """Extend rolling window with new bar."""
        # Extract bar data
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

        # Create single-row DataFrame
        if isinstance(bar, dict) and 'Datetime' in bar:
            index = [bar['Datetime']]
        else:
            index = [bar.name] if hasattr(bar, 'name') else [None]

        new_row = pd.DataFrame([bar_dict], index=index)

        # Concatenate
        df = pd.concat([df, new_row], ignore_index=False)

        # Cap at max_rows
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
        # Parse YYYY-MM-DD format and add timezone
        try:
            dt = datetime.strptime(date_input, '%Y-%m-%d')
            return dt.replace(tzinfo=ET)
        except ValueError:
            # Fallback to fromisoformat if user provided ISO format
            dt = datetime.fromisoformat(date_input)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ET)
            return dt
