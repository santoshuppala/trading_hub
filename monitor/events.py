"""
T1.5 — Event Schema / Contracts
================================
Single source of truth for every event payload used on the EventBus.

Rules
-----
1. One dataclass per EventType — no bare dicts on the bus.
2. Every class validates its own invariants in __post_init__.
   Invalid payloads raise ValueError before they can reach any subscriber.
3. Treat payloads as immutable after emission — do not mutate fields.
4. Producers / consumers are listed per class so the contract is explicit.

Adding a new event
------------------
1. Add an entry to EventType in event_bus.py.
2. Add a payload dataclass here with full validation.
3. Update the contract tests in test_events.py.
4. Import the new payload wherever needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo('America/New_York')

_VALID_SIDES   = frozenset({'buy', 'sell'})
_VALID_ACTIONS = frozenset({'buy', 'sell_stop', 'sell_target', 'sell_rsi',
                             'sell_vwap', 'partial_sell', 'hold'})
_VALID_POS_ACTIONS = frozenset({'opened', 'partial_exit', 'closed'})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_ticker(ticker: str) -> None:
    if not ticker or not isinstance(ticker, str):
        raise ValueError(f"ticker must be a non-empty string, got {ticker!r}")

def _require_positive(name: str, value: float) -> None:
    if value is None or value <= 0:
        raise ValueError(f"{name} must be > 0, got {value!r}")

def _require_non_negative(name: str, value: float) -> None:
    if value is None or value < 0:
        raise ValueError(f"{name} must be >= 0, got {value!r}")

def _require_choice(name: str, value: str, choices) -> None:
    if value not in choices:
        raise ValueError(f"{name} must be one of {sorted(choices)}, got {value!r}")


# ── BAR ───────────────────────────────────────────────────────────────────────

@dataclass
class BarPayload:
    """
    New bar data available for a ticker.

    Producer  : Market Data layer (TradierDataClient / AlpacaDataClient)
    Consumers : Strategy Engine, State Engine, Position Manager
    """
    ticker:  str
    df:      pd.DataFrame           # today's 1-min OHLCV bars (may be empty pre-market)
    rvol_df: Optional[pd.DataFrame] = None  # historical bars for RVOL; None = unavailable

    def __post_init__(self):
        _require_ticker(self.ticker)
        if not isinstance(self.df, pd.DataFrame):
            raise ValueError("df must be a pandas DataFrame")


# ── QUOTE ─────────────────────────────────────────────────────────────────────

@dataclass
class QuotePayload:
    """
    Real-time Level 1 bid/ask quote.

    Producer  : Risk Engine (spread check before entry)
    Consumers : Strategy Engine (ask price for sizing), Observability
    """
    ticker:     str
    bid:        float   # best bid price
    ask:        float   # best ask price  (ask >= bid)
    spread_pct: float   # (ask - bid) / mid  [0, 1]

    def __post_init__(self):
        _require_ticker(self.ticker)
        _require_positive('ask', self.ask)
        _require_non_negative('bid', self.bid)
        if self.bid > self.ask:
            raise ValueError(f"bid ({self.bid}) must be <= ask ({self.ask})")
        _require_non_negative('spread_pct', self.spread_pct)


# ── SIGNAL ────────────────────────────────────────────────────────────────────

@dataclass
class SignalPayload:
    """
    Entry or exit signal produced by the Strategy Engine.

    Producer  : Strategy Engine (SignalAnalyzer wrapper)
    Consumers : Risk Engine (pre-trade gate), Position Manager (exit handling)

    Field invariants
    ----------------
    stop_price   < current_price   (for buy signals)
    target_price > current_price   (for buy signals)
    half_target between current_price and target_price
    rsi_value in [0, 100]
    rvol >= 0
    """
    ticker:             str
    action:             str    # see _VALID_ACTIONS
    current_price:      float
    ask_price:          float
    atr_value:          float
    rsi_value:          float  # [0, 100]
    rvol:               float  # relative volume ratio >= 0
    vwap:               float
    stop_price:         float
    target_price:       float
    half_target:        float
    reclaim_candle_low: float
    refresh_ask: Optional[Callable[[], Optional[float]]] = None

    def __post_init__(self):
        _require_ticker(self.ticker)
        _require_choice('action', self.action, _VALID_ACTIONS)
        _require_positive('current_price', self.current_price)
        _require_positive('ask_price', self.ask_price)
        _require_positive('atr_value', self.atr_value)
        if not (0.0 <= self.rsi_value <= 100.0):
            raise ValueError(f"rsi_value must be in [0, 100], got {self.rsi_value}")
        _require_non_negative('rvol', self.rvol)
        _require_positive('vwap', self.vwap)

        if self.action == 'buy':
            if self.stop_price >= self.current_price:
                raise ValueError(
                    f"stop_price ({self.stop_price}) must be < current_price "
                    f"({self.current_price}) for a buy signal"
                )
            if self.target_price <= self.current_price:
                raise ValueError(
                    f"target_price ({self.target_price}) must be > current_price "
                    f"({self.current_price}) for a buy signal"
                )
            if not (self.current_price < self.half_target <= self.target_price):
                raise ValueError(
                    f"half_target ({self.half_target}) must be between "
                    f"current_price ({self.current_price}) and "
                    f"target_price ({self.target_price})"
                )


# ── ORDER_REQ ─────────────────────────────────────────────────────────────────

@dataclass
class OrderRequestPayload:
    """
    Order that has passed all risk checks and is ready for execution.

    Producer  : Risk Engine
    Consumers : Broker (AlpacaBroker / PaperBroker)
    """
    ticker:      str
    side:        str    # 'buy' | 'sell'
    qty:         int    # shares > 0
    price:       float  # ask for buys; last price for sells
    reason:      str    # human-readable entry/exit reason
    refresh_ask: Optional[Callable[[], Optional[float]]] = None

    def __post_init__(self):
        _require_ticker(self.ticker)
        _require_choice('side', self.side, _VALID_SIDES)
        if self.qty <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        _require_positive('price', self.price)
        if not self.reason:
            raise ValueError("reason must be a non-empty string")


# ── FILL ─────────────────────────────────────────────────────────────────────

@dataclass
class FillPayload:
    """
    Broker confirmed an order was filled.

    Producer  : AlpacaBroker / PaperBroker
    Consumers : Position Manager (open/close positions), State Engine, Observability
    """
    ticker:     str
    side:       str    # 'buy' | 'sell'
    qty:        int    # shares actually filled > 0
    fill_price: float  # average fill price > 0
    order_id:   str    # broker order ID
    reason:     str    # why the order was placed

    def __post_init__(self):
        _require_ticker(self.ticker)
        _require_choice('side', self.side, _VALID_SIDES)
        if self.qty <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        _require_positive('fill_price', self.fill_price)
        if not self.order_id:
            raise ValueError("order_id must be a non-empty string")
        if not self.reason:
            raise ValueError("reason must be a non-empty string")


# ── ORDER_FAIL ────────────────────────────────────────────────────────────────

@dataclass
class OrderFailPayload:
    """
    Order was abandoned after exhausting all retries.

    Producer  : AlpacaBroker
    Consumers : Observability (alert + log), Risk Engine (reset cooldown)
    """
    ticker: str
    side:   str    # 'buy' | 'sell'
    qty:    int
    price:  float
    reason: str

    def __post_init__(self):
        _require_ticker(self.ticker)
        _require_choice('side', self.side, _VALID_SIDES)
        if self.qty <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        _require_positive('price', self.price)


# ── POSITION ─────────────────────────────────────────────────────────────────

@dataclass
class PositionPayload:
    """
    A position was opened, partially exited, or fully closed.

    Producer  : Position Manager
    Consumers : State Engine (persistence), Observability (UI / alerts)

    Invariants
    ----------
    action='opened'       → position dict must be set; pnl is None
    action='partial_exit' → position dict must be set; pnl is the leg PnL
    action='closed'       → position must be None; pnl is the final leg PnL
    """
    ticker:   str
    action:   str             # 'opened' | 'partial_exit' | 'closed'
    position: Optional[dict]  # current position dict; None when closed
    pnl:      Optional[float] = None

    def __post_init__(self):
        _require_ticker(self.ticker)
        _require_choice('action', self.action, _VALID_POS_ACTIONS)
        if self.action == 'closed':
            if self.position is not None:
                raise ValueError("position must be None when action='closed'")
            if self.pnl is None:
                raise ValueError("pnl must be set when action='closed'")
        elif self.position is None:
            raise ValueError(
                f"position dict must be set when action='{self.action}'"
            )


# ── RISK_BLOCK ────────────────────────────────────────────────────────────────

@dataclass
class RiskBlockPayload:
    """
    An entry signal was blocked by the Risk Engine.

    Producer  : Risk Engine
    Consumers : Observability (log why trades were skipped)
    """
    ticker:        str
    reason:        str   # human-readable block reason
    signal_action: str   # the action that was blocked

    def __post_init__(self):
        _require_ticker(self.ticker)
        if not self.reason:
            raise ValueError("reason must be a non-empty string")
        if not self.signal_action:
            raise ValueError("signal_action must be a non-empty string")


# ── HEARTBEAT ────────────────────────────────────────────────────────────────

@dataclass
class HeartbeatPayload:
    """
    Periodic status snapshot — emitted every minute by the monitor loop.

    Producer  : RealTimeMonitor (run loop)
    Consumers : Observability (logging, UI refresh)
    """
    n_tickers:    int
    n_positions:  int
    open_tickers: List[str]
    n_trades:     int
    n_wins:       int
    total_pnl:    float
    timestamp:    datetime = field(default_factory=lambda: datetime.now(ET))

    def __post_init__(self):
        _require_non_negative('n_tickers',   self.n_tickers)
        _require_non_negative('n_positions', self.n_positions)
        _require_non_negative('n_trades',    self.n_trades)
        _require_non_negative('n_wins',      self.n_wins)
        if self.n_wins > self.n_trades:
            raise ValueError(
                f"n_wins ({self.n_wins}) cannot exceed n_trades ({self.n_trades})"
            )
        if not isinstance(self.open_tickers, list):
            raise ValueError("open_tickers must be a list")
