"""
V10 BarBuilder — Aggregates WebSocket trade ticks into 1-minute OHLCV bars.

Replaces 12-second REST polling with sub-second bar delivery.
Trade ticks arrive from TradierStreamClient (~10-50ms between ticks).
At each minute boundary, the completed bar is emitted as a BAR event.

Data flow:
    TradierStream → trade tick (price, size, cvol)
                      ↓
                  BarBuilder.on_trade(ticker, price, size, cvol, timestamp)
                      ↓ (aggregates per-ticker per-minute)
                  At :00 boundary → emit BAR event with OHLCV dataframe
                      ↓
                  StrategyEngine, ProSetupEngine receive BAR (same as REST)

Bar accuracy:
    open   = price of first trade after minute boundary    (EXACT)
    high   = max(price) across all trades in minute        (EXACT)
    low    = min(price) across all trades in minute        (EXACT)
    close  = price of last trade before next boundary      (EXACT)
    volume = sum(size) across all trades in minute         (EXACT)
    vwap   = sum(price × size) / sum(size)                 (EXACT)

Fallback:
    If no trade ticks arrive for a ticker in a minute (illiquid),
    no bar is emitted. REST polling continues as backup for these tickers.

Usage:
    builder = BarBuilder(bus=event_bus, max_history=200)
    # Called by TradierStreamClient on every trade tick:
    builder.on_trade('AAPL', price=150.25, size=100, cvol=1234567, timestamp=time.time())
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Set
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger(__name__)
ET = ZoneInfo('America/New_York')


@dataclass
class BarAccumulator:
    """Accumulates trade ticks for one ticker during one minute."""
    open: float = 0.0
    high: float = 0.0
    low: float = float('inf')
    close: float = 0.0
    volume: int = 0
    vwap_pv: float = 0.0    # sum(price × size) for VWAP
    vwap_v: int = 0          # sum(size) for VWAP
    tick_count: int = 0
    first_tick_ts: float = 0.0
    last_tick_ts: float = 0.0

    def add_tick(self, price: float, size: int, timestamp: float):
        if self.tick_count == 0:
            self.open = price
            self.first_tick_ts = timestamp
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += size
        self.vwap_pv += price * size
        self.vwap_v += size
        self.tick_count += 1
        self.last_tick_ts = timestamp

    @property
    def vwap(self) -> float:
        return self.vwap_pv / self.vwap_v if self.vwap_v > 0 else self.close

    def is_valid(self) -> bool:
        return self.tick_count > 0 and self.close > 0

    def to_dict(self) -> dict:
        return {
            'open': self.open,
            'high': self.high,
            'low': self.low if self.low < float('inf') else self.close,
            'close': self.close,
            'volume': self.volume,
        }


class BarBuilder:
    """Aggregates WebSocket trade ticks into 1-minute OHLCV bars.

    Thread-safe. Called from TradierStreamClient's message handler thread.
    Emits BAR events on the EventBus at minute boundaries.

    Args:
        bus: EventBus instance for emitting BAR events
        max_history: max bars to keep per ticker (rolling window for detectors)
        emit_bars: if True, emit BAR events (set False for testing)
    """

    def __init__(self, bus=None, max_history: int = 200, emit_bars: bool = True):
        self._bus = bus
        self._max_history = max_history
        self._emit_bars = emit_bars
        self._lock = threading.Lock()

        # Current minute's accumulators: {ticker: BarAccumulator}
        self._current: Dict[str, BarAccumulator] = defaultdict(BarAccumulator)
        self._current_minute: int = 0  # minute boundary (epoch minutes)

        # Historical bars per ticker: {ticker: list of bar dicts}
        self._history: Dict[str, list] = defaultdict(list)

        # RVOL baseline data (daily volume profiles)
        self._rvol_baselines: Dict[str, pd.DataFrame] = {}

        # Timer thread for minute boundary flushing
        self._running = False
        self._flush_thread: Optional[threading.Thread] = None

        # Seeding state: REST history bootstraps BarBuilder so detectors
        # have enough bars (RSI needs 14, MIN_BARS_REQUIRED=30).
        self._seeded: bool = False
        self._seeded_tickers: Set[str] = set()

        # HOT tickers: only emit BAR events for these (open positions).
        # Emitting for all 225 seeded tickers overwhelms EventBus queues.
        # Updated by monitor every cycle via set_hot_tickers().
        self._hot_tickers: Set[str] = set()

        # Per-ticker freshness: tracks which tickers received WebSocket
        # ticks in the LAST flush cycle. Tickers not in this set had no
        # trades → REST should emit BAR for them as fallback.
        self._tickers_with_ticks: Set[str] = set()

        # Last successful flush time (for liveness check)
        self._last_flush_time: float = 0.0

        # Stats
        self._ticks_received = 0
        self._bars_emitted = 0
        self._tickers_active: Set[str] = set()

    def start(self):
        """Start the minute-boundary flush timer."""
        if self._running:
            return
        self._running = True
        self._current_minute = self._get_current_minute()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name='bar-builder-flush')
        self._flush_thread.start()
        log.info("[BarBuilder] Started | max_history=%d", self._max_history)

    def stop(self):
        """Stop the flush timer."""
        self._running = False
        if self._flush_thread:
            self._flush_thread.join(timeout=5)
        # Flush any remaining bars
        self._flush_minute()
        log.info("[BarBuilder] Stopped | ticks=%d bars=%d tickers=%d",
                 self._ticks_received, self._bars_emitted, len(self._tickers_active))

    def set_rvol_baselines(self, baselines: Dict[str, pd.DataFrame]):
        """Set RVOL baseline data (from daily volume profiles)."""
        self._rvol_baselines = baselines

    def set_hot_tickers(self, tickers: Set[str]):
        """Set which tickers get sub-second BAR emission.

        Only open positions need sub-second exits. The full universe
        gets REST BARs (60s) for entry signals — fast enough since
        entries are less time-critical than exits.
        """
        self._hot_tickers = set(tickers)

    # ── REST History Seeding ─────────────────────────────────────────

    def seed_from_cache(self, bars_cache: Dict[str, pd.DataFrame],
                        rvol_cache: Optional[Dict[str, pd.DataFrame]] = None,
                        min_bars: int = 30) -> int:
        """Seed historical bars from monitor's REST bars_cache.

        After seeding, BarBuilder has enough history for all detectors
        (RSI=14, Bollinger=20, ATR=14, MIN_BARS_REQUIRED=30).
        WebSocket ticks append to this history going forward.

        Called once after the first REST fetch cycle completes.
        Safe to call multiple times (idempotent — replaces history).

        Args:
            bars_cache: {ticker: DataFrame} from monitor._bars_cache
            rvol_cache: {ticker: DataFrame} from monitor._rvol_cache (optional)
            min_bars: minimum bars required to consider a ticker seeded

        Returns:
            Number of tickers successfully seeded.
        """
        seeded = 0
        now = datetime.now(ET)
        today = now.date()

        with self._lock:
            for ticker, df in bars_cache.items():
                if df is None or df.empty:
                    continue

                # Filter to today's bars only (same as monitor._build_bar_event)
                try:
                    if hasattr(df.index, 'date'):
                        today_df = df[df.index.date == today]
                    else:
                        today_df = df
                except Exception:
                    today_df = df

                if today_df.empty or len(today_df) < min_bars:
                    continue

                # Convert DataFrame rows → list of bar dicts
                bars = []
                for _, row in today_df.iterrows():
                    o = float(row.get('open', row.get('o', 0)))
                    h = float(row.get('high', row.get('h', 0)))
                    l = float(row.get('low', row.get('l', 0)))
                    c = float(row.get('close', row.get('c', 0)))
                    v = int(row.get('volume', row.get('v', 0)))
                    if c <= 0:
                        continue
                    bars.append({'open': o, 'high': h, 'low': l, 'close': c, 'volume': v})

                if len(bars) < min_bars:
                    continue

                self._history[ticker] = bars[-self._max_history:]
                self._seeded_tickers.add(ticker)
                seeded += 1

            # Merge RVOL baselines if provided
            if rvol_cache:
                for ticker, rvol_df in rvol_cache.items():
                    if rvol_df is not None and not rvol_df.empty:
                        self._rvol_baselines[ticker] = rvol_df

            if seeded > 0:
                self._seeded = True
                self._emit_bars = True  # auto-enable after successful seed

        log.info("[BarBuilder] Seeded %d tickers from REST cache "
                 "(min_bars=%d) | emit_bars=%s",
                 seeded, min_bars, self._emit_bars)
        return seeded

    @property
    def is_seeded(self) -> bool:
        """True if BarBuilder has been seeded with REST history."""
        return self._seeded

    def covers_ticker(self, ticker: str) -> bool:
        """True if BarBuilder will actually emit BAR events for this ticker.

        Must satisfy ALL three conditions:
        - Seeded (has REST history for detectors)
        - Received WebSocket ticks this flush cycle (not illiquid)
        - In _hot_tickers (open position — BarBuilder only emits for these)

        If any condition fails, REST should emit BAR for this ticker.
        """
        return (ticker in self._seeded_tickers
                and ticker in self._tickers_with_ticks
                and ticker in self._hot_tickers)

    def is_active(self) -> bool:
        """True if BarBuilder is running, seeded, and flushed recently.

        If flush hasn't happened in 2+ minutes, something is wrong
        (thread died, no ticks at all) → monitor should fall back to REST.
        """
        if not self._running or not self._seeded:
            return False
        if self._last_flush_time <= 0:
            return False
        return (time.time() - self._last_flush_time) < 120.0

    # ── Trade tick ingestion ─────────────────────────────────────────

    def on_trade(self, ticker: str, price: float, size: int,
                 cvol: int = 0, timestamp: float = 0.0):
        """Process a single trade tick from WebSocket.

        Called by TradierStreamClient._handle_message for trade events.
        Thread-safe.

        Args:
            ticker: symbol
            price: exact trade price
            size: shares in this trade
            cvol: cumulative day volume (for real-time RVOL)
            timestamp: trade timestamp (epoch seconds), defaults to now
        """
        if price <= 0 or size <= 0:
            return

        if timestamp <= 0:
            timestamp = time.time()

        with self._lock:
            self._ticks_received += 1
            self._tickers_active.add(ticker)

            # Check if we crossed a minute boundary
            current_min = self._get_current_minute()
            if current_min > self._current_minute:
                self._flush_minute_locked()
                self._current_minute = current_min

            self._current[ticker].add_tick(price, size, timestamp)

    # ── Minute boundary flush ────────────────────────────────────────

    def _flush_loop(self):
        """Background thread: check for minute boundary every 100ms."""
        while self._running:
            time.sleep(0.1)
            current_min = self._get_current_minute()
            if current_min > self._current_minute:
                with self._lock:
                    if current_min > self._current_minute:
                        self._flush_minute_locked()
                        self._current_minute = current_min

    def _flush_minute(self):
        """Flush completed bars (thread-safe wrapper)."""
        with self._lock:
            self._flush_minute_locked()

    def _flush_minute_locked(self):
        """Flush all completed bars for the previous minute.

        Must be called with self._lock held.
        """
        if not self._current:
            return

        bars_flushed = 0
        now_et = datetime.now(ET)
        tickers_this_flush: Set[str] = set()

        for ticker, acc in self._current.items():
            if not acc.is_valid():
                continue

            bar_dict = acc.to_dict()
            tickers_this_flush.add(ticker)

            # Add to history
            self._history[ticker].append(bar_dict)
            if len(self._history[ticker]) > self._max_history:
                self._history[ticker] = self._history[ticker][-self._max_history:]

            # Emit BAR event only for HOT tickers (open positions).
            # Emitting for all 225 seeded tickers overwhelms EventBus queues.
            # Entry signals for non-position tickers come from REST polling (60s).
            # Exit signals for open positions get sub-second bars here.
            if (self._emit_bars and self._bus
                    and ticker in self._seeded_tickers
                    and ticker in self._hot_tickers
                    and len(self._history[ticker]) >= 2):
                try:
                    self._emit_bar_event(ticker, now_et)
                    bars_flushed += 1
                except Exception as exc:
                    log.warning("[BarBuilder] Failed to emit BAR for %s: %s", ticker, exc)

        self._bars_emitted += bars_flushed
        self._last_flush_time = time.time()

        # Track which tickers had ticks this cycle (for covers_ticker)
        self._tickers_with_ticks = tickers_this_flush

        # Reset accumulators for new minute
        self._current = defaultdict(BarAccumulator)

    def _emit_bar_event(self, ticker: str, now_et: datetime):
        """Build and emit a BAR event for a ticker."""
        from .event_bus import Event, EventType
        from .events import BarPayload

        # Build DataFrame from history (same format detectors expect)
        history = self._history[ticker]
        df = pd.DataFrame(history)

        # Add index (detectors expect DatetimeIndex or similar)
        if len(df) > 0:
            df.index = pd.RangeIndex(len(df))

        # Get RVOL baseline
        rvol_df = self._rvol_baselines.get(ticker)

        self._bus.emit(Event(
            type=EventType.BAR,
            payload=BarPayload(
                ticker=ticker,
                df=df,
                rvol_df=rvol_df,
            ),
        ))

    # ── Public API ───────────────────────────────────────────────────

    def get_bars(self, ticker: str) -> list:
        """Get historical bars for a ticker."""
        with self._lock:
            return list(self._history.get(ticker, []))

    def get_current_bar(self, ticker: str) -> Optional[dict]:
        """Get the in-progress bar for a ticker (not yet flushed)."""
        with self._lock:
            acc = self._current.get(ticker)
            if acc and acc.is_valid():
                return acc.to_dict()
            return None

    def ticker_count(self) -> int:
        """Number of active tickers receiving trade data."""
        return len(self._tickers_active)

    def stats(self) -> dict:
        """Builder statistics."""
        return {
            'ticks_received': self._ticks_received,
            'bars_emitted': self._bars_emitted,
            'tickers_active': len(self._tickers_active),
            'tickers_seeded': len(self._seeded_tickers),
            'tickers_with_ticks': len(self._tickers_with_ticks),
            'current_minute_tickers': len(self._current),
            'history_tickers': len(self._history),
            'seeded': self._seeded,
            'emit_bars': self._emit_bars,
        }

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _get_current_minute() -> int:
        """Current time as epoch minutes (for boundary detection)."""
        return int(time.time()) // 60
