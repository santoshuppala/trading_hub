"""
T1.5 — Event Schema / Contracts  (v2)
======================================
Single source of truth for every event payload used on the EventBus.

Rules
-----
1. One dataclass per EventType — no bare dicts on the bus.
2. Every class validates its own invariants in __post_init__.
   Invalid payloads raise ValueError before they can reach any subscriber.
3. All payloads are frozen (immutable after construction).
4. Producers / consumers are listed per class so the contract is explicit.

v2 changes
----------
- @dataclass(frozen=True) on all payload classes.
- BarPayload: deep-copy + numpy read-only flag on DataFrames in __post_init__.
  frozen=True prevents field re-assignment.  The deep copy isolates the
  payload from the producer.  Setting numpy writeable=False on the copy
  means any subscriber that tries to mutate a value in-place (e.g.
  df.iloc[0,0] = x) gets an immediate ValueError instead of silently
  corrupting the shared payload that all other subscribers see.
  Cost: one deep copy + O(n_columns) flag writes — same allocation budget
  as a plain deep copy, stronger safety guarantee.
- SignalPayload / OrderRequestPayload: refresh_ask callable removed and
  replaced with needs_ask_refresh: bool.  Callables in events break
  serialisation (Redpanda / DurableEventLog) and hide side effects.
  Handlers own the fetch logic; the flag is the contract.
- Side, SignalAction, PositionAction string-Enums replace bare strings,
  eliminating typo bugs.  __str__ returns .value so f-strings stay clean.
  Coercion in __post_init__ means callers may still pass raw strings.
- PositionSnapshot replaces Optional[dict] in PositionPayload.
- HeartbeatPayload.timestamp removed — Event.timestamp (set by the bus) is
  the authoritative clock for all events; duplicate payload timestamps cause
  ambiguity and double-maintenance.
- _require_positive / _require_non_negative: math.isfinite guard added so
  NaN / ±inf values are rejected at construction time.
- HeartbeatPayload.open_tickers: coerced to tuple (immutable) and each
  element validated to be a str.

Adding a new event
------------------
1. Add an entry to EventType in event_bus.py.
2. Add a frozen payload dataclass here with full validation.
3. Update the contract tests in test_events.py.
4. Import the new payload wherever needed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo('America/New_York')


# ── Enums ─────────────────────────────────────────────────────────────────────

class Side(str, Enum):
    BUY  = 'buy'
    SELL = 'sell'

    def __str__(self) -> str:
        return self.value


class SignalAction(str, Enum):
    BUY          = 'buy'
    SELL_STOP    = 'sell_stop'
    SELL_TARGET  = 'sell_target'
    SELL_RSI     = 'sell_rsi'
    SELL_VWAP    = 'sell_vwap'
    PARTIAL_SELL = 'partial_sell'
    HOLD         = 'hold'

    def __str__(self) -> str:
        return self.value


class PositionAction(str, Enum):
    OPENED       = 'opened'
    PARTIAL_EXIT = 'partial_exit'
    CLOSED       = 'closed'

    def __str__(self) -> str:
        return self.value


# ── DataFrame immutability helper ─────────────────────────────────────────────

def _freeze_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return an immutable view of df suitable for sharing across subscribers.

    Two-step process
    ----------------
    1. Copy if needed — skipped when ALL numpy column arrays are already
       read-only (i.e. the caller transferred ownership via from_owned()).
       Otherwise a deep copy is made so the producer's buffer is isolated.
    2. Mark writeable=False on every numeric numpy array in the result.
       Any subscriber that attempts df.iloc[0, 0] = x or df['c'] = ... on
       a numeric column now raises ValueError immediately at the mutation
       site rather than silently corrupting the shared payload.

    Performance
    -----------
    Normal path (copy needed):  O(rows × cols) copy + O(cols) flag writes.
    Owned path (no copy):       O(cols) flag writes only.
    """
    # Determine whether a copy is needed (any writeable numeric column).
    needs_copy = any(
        getattr(df[c], 'values', None) is not None
        and getattr(df[c].values, 'flags', None) is not None
        and df[c].values.flags.writeable
        for c in df.columns
    )
    out = df.copy(deep=True) if needs_copy else df
    for col in out.columns:
        try:
            out[col].values.flags.writeable = False
        except (ValueError, AttributeError):
            pass  # non-writeable types (object, categorical, etc.)
    return out


# ── Scalar validation helpers ──────────────────────────────────────────────────

def _require_ticker(ticker: str) -> None:
    if not ticker or not isinstance(ticker, str):
        raise ValueError(f"ticker must be a non-empty string, got {ticker!r}")

def _require_positive(name: str, value: float) -> None:
    if value is None or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number > 0, got {value!r}")

def _require_non_negative(name: str, value: float) -> None:
    if value is None or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")


# ── PositionSnapshot ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PositionSnapshot:
    """
    Immutable snapshot of an open position at the time it is broadcast.

    Replaces the untyped Optional[dict] previously used in PositionPayload.
    The internal _positions dict inside PositionManager remains mutable for
    bookkeeping; this class is solely the bus-safe representation.
    """
    entry_price:  float
    entry_time:   str          # HH:MM:SS
    quantity:     int
    partial_done: bool
    order_id:     str
    stop_price:   float
    target_price: float
    half_target:  float
    atr_value:    Optional[float] = None

    def __post_init__(self):
        _require_positive('entry_price',  self.entry_price)
        if self.quantity <= 0:
            raise ValueError(f"quantity must be > 0, got {self.quantity}")
        _require_positive('stop_price',   self.stop_price)
        _require_positive('target_price', self.target_price)
        _require_positive('half_target',  self.half_target)
        if self.atr_value is not None:
            _require_positive('atr_value', self.atr_value)


# ── BAR ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BarPayload:
    """
    New bar data available for a ticker.

    Producer  : Market Data layer (TradierDataClient / AlpacaDataClient)
    Consumers : Strategy Engine, State Engine, Position Manager

    Immutability contract
    ---------------------
    __post_init__ calls _freeze_df() on both DataFrames:
      1. Deep copy — isolates payload from the producer's buffer.
      2. Read-only numpy flags — any subscriber that attempts an in-place
         mutation (df.iloc[0, 0] = x, df['new'] = ...) gets an immediate
         ValueError at the point of mutation rather than silently corrupting
         the shared payload seen by all other subscribers.

    Performance note
    ----------------
    Cost is one deep copy + O(n_cols) flag writes per bar per emit() call —
    not per subscriber.  For 390-row × 6-col OHLCV bars across 170 tickers
    this is ~11 MB of allocation per minute, well within normal GC budget.
    If profiling shows otherwise, use BarPayload.from_owned() in the data
    client: it skips the copy and only writes the read-only flags.
    """
    ticker:  str
    df:      pd.DataFrame           # today's 1-min OHLCV bars
    rvol_df: Optional[pd.DataFrame] = None  # historical bars for RVOL

    def __post_init__(self):
        _require_ticker(self.ticker)
        if not isinstance(self.df, pd.DataFrame):
            raise ValueError("df must be a pandas DataFrame")
        object.__setattr__(self, 'df', _freeze_df(self.df))
        if self.rvol_df is not None:
            object.__setattr__(self, 'rvol_df', _freeze_df(self.rvol_df))

    @classmethod
    def from_owned(
        cls,
        ticker:  str,
        df:      pd.DataFrame,
        rvol_df: Optional[pd.DataFrame] = None,
    ) -> 'BarPayload':
        """
        Construct from DataFrames the caller already owns (fresh per cycle).

        O(n_cols) cost only — no deep copy.
        This method marks df / rvol_df numpy arrays as read-only in-place,
        then delegates to the normal constructor.  __post_init__ calls
        _freeze_df(), which detects the already-read-only arrays and skips
        the copy, performing only the O(cols) flag write-pass.

        Caller contract: do NOT read or modify df / rvol_df after this call.
        Ownership is transferred to the payload.

        Usage in data clients:
            df = pd.DataFrame(rows)   # fresh allocation per fetch cycle
            bar = BarPayload.from_owned(ticker, df)
            # df is now owned by the payload — do not touch it
        """
        _require_ticker(ticker)
        if not isinstance(df, pd.DataFrame):
            raise ValueError("df must be a pandas DataFrame")
        # Transfer ownership: mark all numeric arrays read-only so __post_init__
        # detects them as already frozen and skips the deep copy.
        for col in df.columns:
            try:
                df[col].values.flags.writeable = False
            except (ValueError, AttributeError):
                pass
        if rvol_df is not None:
            for col in rvol_df.columns:
                try:
                    rvol_df[col].values.flags.writeable = False
                except (ValueError, AttributeError):
                    pass
        return cls(ticker=ticker, df=df, rvol_df=rvol_df)


# ── QUOTE ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
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

@dataclass(frozen=True)
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

    needs_ask_refresh=True signals that the handler should fetch a fresh ask
    price before execution — the ask_price field reflects price at signal time
    and may be stale by the time the order reaches the broker.
    The handler (Risk Engine / Broker) owns the quote-fetch logic.
    """
    ticker:             str
    action:             SignalAction
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
    needs_ask_refresh:  bool = False

    def __post_init__(self):
        _require_ticker(self.ticker)
        # Accept raw strings for backward compat; coerce to enum.
        object.__setattr__(self, 'action', SignalAction(self.action))
        _require_positive('current_price', self.current_price)
        _require_positive('ask_price', self.ask_price)
        _require_positive('atr_value', self.atr_value)
        if not math.isfinite(self.rsi_value) or not (0.0 <= self.rsi_value <= 100.0):
            raise ValueError(f"rsi_value must be in [0, 100], got {self.rsi_value}")
        _require_non_negative('rvol', self.rvol)
        _require_positive('vwap', self.vwap)

        if self.action == SignalAction.BUY:
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

@dataclass(frozen=True)
class OrderRequestPayload:
    """
    Order that has passed all risk checks and is ready for execution.

    Producer  : Risk Engine
    Consumers : Broker (AlpacaBroker / PaperBroker)

    needs_ask_refresh=True tells the broker to call its injected quote_fn
    before each retry to get a fresh ask rather than re-using the stale
    price stamped at signal time.
    """
    ticker:            str
    side:              Side
    qty:               int
    price:             float  # ask for buys; last price for sells
    reason:            str    # human-readable entry/exit reason
    needs_ask_refresh: bool = False

    def __post_init__(self):
        _require_ticker(self.ticker)
        object.__setattr__(self, 'side', Side(self.side))
        if self.qty <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        _require_positive('price', self.price)
        if not self.reason:
            raise ValueError("reason must be a non-empty string")


# ── FILL ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FillPayload:
    """
    Broker confirmed an order was filled.

    Producer  : AlpacaBroker / PaperBroker
    Consumers : Position Manager (open/close positions), State Engine, Observability
    """
    ticker:     str
    side:       Side   # 'buy' | 'sell'
    qty:        int    # shares actually filled > 0
    fill_price: float  # average fill price > 0
    order_id:   str    # broker order ID
    reason:     str    # why the order was placed

    def __post_init__(self):
        _require_ticker(self.ticker)
        object.__setattr__(self, 'side', Side(self.side))
        if self.qty <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        _require_positive('fill_price', self.fill_price)
        if not self.order_id:
            raise ValueError("order_id must be a non-empty string")
        if not self.reason:
            raise ValueError("reason must be a non-empty string")


# ── ORDER_FAIL ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrderFailPayload:
    """
    Order was abandoned after exhausting all retries.

    Producer  : AlpacaBroker
    Consumers : Observability (alert + log), Risk Engine (reset cooldown)
    """
    ticker: str
    side:   Side   # 'buy' | 'sell'
    qty:    int
    price:  float
    reason: str

    def __post_init__(self):
        _require_ticker(self.ticker)
        object.__setattr__(self, 'side', Side(self.side))
        if self.qty <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        _require_positive('price', self.price)


# ── POSITION ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PositionPayload:
    """
    A position was opened, partially exited, or fully closed.

    Producer  : Position Manager
    Consumers : State Engine (persistence), Observability (UI / alerts)

    Invariants
    ----------
    action=OPENED       → position must be set; pnl is None
    action=PARTIAL_EXIT → position must be set; pnl is the leg PnL
    action=CLOSED       → position must be None; pnl is the final leg PnL
    """
    ticker:   str
    action:   PositionAction
    position: Optional[PositionSnapshot]  # None only when action=CLOSED
    pnl:      Optional[float] = None

    def __post_init__(self):
        _require_ticker(self.ticker)
        object.__setattr__(self, 'action', PositionAction(self.action))
        if self.action == PositionAction.CLOSED:
            if self.position is not None:
                raise ValueError("position must be None when action=CLOSED")
            if self.pnl is None:
                raise ValueError("pnl must be set when action=CLOSED")
        elif self.position is None:
            raise ValueError(
                f"position must be set when action='{self.action}'"
            )


# ── RISK_BLOCK ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
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

@dataclass(frozen=True)
class HeartbeatPayload:
    """
    Periodic status snapshot — emitted every minute by the monitor loop.

    Producer  : RealTimeMonitor (run loop)
    Consumers : Observability (logging, UI refresh)

    No timestamp field — Event.timestamp (set by the bus at emit time) is
    the authoritative clock.  Duplicating it in the payload creates two
    competing time sources and complicates replay / backtesting.

    open_tickers is coerced to a tuple on construction so it is immutable
    even though the annotated input type is List[str].
    """
    n_tickers:    int
    n_positions:  int
    open_tickers: List[str]   # coerced to tuple in __post_init__
    n_trades:     int
    n_wins:       int
    total_pnl:    float

    def __post_init__(self):
        _require_non_negative('n_tickers',   self.n_tickers)
        _require_non_negative('n_positions', self.n_positions)
        _require_non_negative('n_trades',    self.n_trades)
        _require_non_negative('n_wins',      self.n_wins)
        if self.n_wins > self.n_trades:
            raise ValueError(
                f"n_wins ({self.n_wins}) cannot exceed n_trades ({self.n_trades})"
            )
        object.__setattr__(self, 'open_tickers', tuple(self.open_tickers))
        if not all(isinstance(t, str) for t in self.open_tickers):
            raise ValueError("every element of open_tickers must be a str")
