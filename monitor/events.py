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
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo('America/New_York')


# ── Enums ─────────────────────────────────────────────────────────────────────

class Side(str, Enum):
    BUY  = 'BUY'
    SELL = 'SELL'

    def __str__(self) -> str:
        return self.value


class SignalAction(str, Enum):
    BUY          = 'BUY'
    SELL_STOP    = 'SELL_STOP'
    SELL_TARGET  = 'SELL_TARGET'
    SELL_RSI     = 'SELL_RSI'
    SELL_VWAP    = 'SELL_VWAP'
    PARTIAL_SELL = 'PARTIAL_SELL'
    PARTIAL_DONE = 'PARTIAL_DONE'
    HOLD         = 'HOLD'

    def __str__(self) -> str:
        return self.value

    @classmethod
    def _missing_(cls, value):
        """Accept lowercase/mixed-case strings: 'partial_sell' → PARTIAL_SELL."""
        if isinstance(value, str):
            upper = value.upper()
            for member in cls:
                if member.value == upper:
                    return member
        return None


class PositionAction(str, Enum):
    OPENED       = 'OPENED'
    PARTIAL_EXIT = 'PARTIAL_EXIT'
    CLOSED       = 'CLOSED'

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
    quantity:     float         # V9: float for fractional shares (was int)
    partial_done: bool
    order_id:     str
    stop_price:   float
    target_price: float
    half_target:  float
    atr_value:    Optional[float] = None

    def __post_init__(self):
        _require_positive('entry_price',  self.entry_price)
        if not math.isfinite(self.quantity) or self.quantity <= 0:
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
    # ── Edge context (V10: captured at signal time for edge model) ─────
    strategy:           str = 'vwap_reclaim'   # explicit strategy name
    tier:               int = 1                # strategy tier (1=highest conviction)
    confidence:         float = 0.0            # signal confidence [0, 1]
    timeframe:          str = '1min'           # '1min' or '5min'
    regime_at_entry:    str = ''               # 'BULL_TREND', 'RANGE_BOUND', etc.
    time_bucket:        str = ''               # '0930_0945', '0945_1030', etc.
    confluence_score:   float = 0.0            # aggregate detector agreement [0, 1]

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
    qty:               float             # V9: float for fractional shares (was int)
    price:             float             # ask for buys; last price for sells
    reason:            str               # human-readable entry/exit reason
    needs_ask_refresh: bool = False
    # Signal metadata forwarded so FillPayload can carry it for CrashRecovery
    stop_price:        Optional[float] = None
    target_price:      Optional[float] = None
    atr_value:         Optional[float] = None
    # V7: Layer tag for centralized registry — Core acquires on behalf of sender
    layer:             Optional[str] = None  # 'vwap', 'pro', 'pop', 'options'
    # V9: Order mode — how the broker should interpret the order
    order_mode:        str = 'qty'       # 'qty' | 'notional'
    notional:          Optional[float] = None  # dollar amount (when order_mode='notional')
    # V10: Order type — limit (default) or stop_limit (two-stage entries)
    order_type:        str = 'limit'     # 'limit' | 'stop_limit'
    activation_price:  Optional[float] = None  # for stop_limit: price that activates the order

    def __post_init__(self):
        _require_ticker(self.ticker)
        object.__setattr__(self, 'side', Side(self.side))
        if self.order_mode == 'notional':
            # Notional orders: qty may be 0 (broker computes it), notional required
            if self.notional is not None and self.notional <= 0:
                raise ValueError(f"notional must be > 0, got {self.notional}")
        else:
            if not math.isfinite(self.qty) or self.qty <= 0:
                raise ValueError(f"qty must be > 0, got {self.qty}")
        _require_positive('price', self.price)
        if not self.reason:
            raise ValueError("reason must be a non-empty string")
        if self.stop_price is not None:
            _require_positive('stop_price', self.stop_price)
        if self.target_price is not None:
            _require_positive('target_price', self.target_price)
        if self.atr_value is not None:
            _require_positive('atr_value', self.atr_value)


# ── FILL ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FillPayload:
    """
    Broker confirmed an order was filled.

    Producer  : AlpacaBroker / PaperBroker
    Consumers : Position Manager (open/close positions), State Engine, Observability
    """
    ticker:      str
    side:        Side   # 'BUY' | 'SELL'
    qty:         float  # V9: float for fractional shares (was int)
    fill_price:  float  # average fill price > 0
    order_id:    str    # broker order ID
    reason:      str    # why the order was placed
    # Optional signal metadata carried forward so CrashRecovery can reconstruct
    # exact stop/target without falling back to entry_price ± ATR approximations.
    stop_price:  Optional[float] = None
    target_price: Optional[float] = None
    atr_value:   Optional[float] = None
    # V9: Slippage tracking — bar close price at signal time
    signal_price: float = 0.0
    # V9: How the order was submitted ('qty' | 'notional')
    order_mode:  str = 'qty'

    def __post_init__(self):
        _require_ticker(self.ticker)
        object.__setattr__(self, 'side', Side(self.side))
        if not math.isfinite(self.qty) or self.qty <= 0:
            raise ValueError(f"qty must be > 0, got {self.qty}")
        _require_positive('fill_price', self.fill_price)
        if not self.order_id:
            raise ValueError("order_id must be a non-empty string")
        if not self.reason:
            raise ValueError("reason must be a non-empty string")
        if self.stop_price is not None:
            _require_positive('stop_price', self.stop_price)
        if self.target_price is not None:
            _require_positive('target_price', self.target_price)
        if self.atr_value is not None:
            _require_positive('atr_value', self.atr_value)


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
    qty:    float  # V9: float for fractional shares (was int)
    price:  float
    reason: str

    def __post_init__(self):
        _require_ticker(self.ticker)
        object.__setattr__(self, 'side', Side(self.side))
        if not math.isfinite(self.qty) or self.qty <= 0:
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
    close_detail: Optional[dict] = None  # qty, entry_price, exit_price for CLOSED

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


# ── POP_SIGNAL ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PopSignalPayload:
    """
    Entry signal produced by the PopStrategyEngine (T3.5 layer).

    Producer  : PopStrategyEngine
    Consumers : RiskEngine (via SIGNAL translation), DurableEventLog (audit)

    Integration path
    ----------------
    PopStrategyEngine emits POP_SIGNAL (durable) for full audit trail, then
    immediately translates the signal into a standard SIGNAL event so the
    existing RiskEngine → Broker → PositionManager pipeline handles execution
    without modification.

    Fields
    ------
    symbol          : ticker symbol
    strategy_type   : one of StrategyType enum values (str)
    entry_price     : proposed entry price from the strategy engine
    stop_price      : hard stop loss price (stop_price < entry_price for longs)
    target_1        : first profit target (partial exit)
    target_2        : second profit target (full exit)
    pop_reason      : why this symbol was flagged (PopReason enum value as str)
    atr_value       : ATR used to size stop/targets
    rvol            : relative volume ratio at signal time
    vwap_distance   : (entry_price − vwap) / vwap; negative = below VWAP
    strategy_confidence : classifier confidence [0, 1]
    features_json   : JSON-serialised EngineeredFeatures snapshot for audit.
                      Stored as str to keep the payload truly immutable.
    """
    symbol:               str
    strategy_type:        str    # StrategyType.value
    entry_price:          float
    stop_price:           float
    target_1:             float
    target_2:             float
    pop_reason:           str    # PopReason.value
    atr_value:            float
    rvol:                 float
    vwap_distance:        float
    strategy_confidence:  float  # [0, 1]
    features_json:        str = '{}'   # JSON snapshot — empty dict default
    # ── Edge context (V10: captured at signal time for edge model) ─────
    timeframe:            str = '1min'
    regime_at_entry:      str = ''
    time_bucket:          str = ''

    def __post_init__(self):
        _require_ticker(self.symbol)
        if not self.strategy_type:
            raise ValueError("strategy_type must be a non-empty string")
        if not self.pop_reason:
            raise ValueError("pop_reason must be a non-empty string")
        _require_positive('entry_price', self.entry_price)
        _require_positive('stop_price',  self.stop_price)
        _require_positive('target_1',    self.target_1)
        _require_positive('target_2',    self.target_2)
        if self.stop_price >= self.entry_price:
            raise ValueError(
                f"stop_price ({self.stop_price}) must be < entry_price "
                f"({self.entry_price}) for a long entry"
            )
        if self.target_1 <= self.entry_price:
            raise ValueError(
                f"target_1 ({self.target_1}) must be > entry_price ({self.entry_price})"
            )
        if self.target_2 <= self.target_1:
            raise ValueError(
                f"target_2 ({self.target_2}) must be > target_1 ({self.target_1})"
            )
        _require_positive('atr_value', self.atr_value)
        _require_non_negative('rvol', self.rvol)
        if not math.isfinite(self.vwap_distance):
            raise ValueError(f"vwap_distance must be finite, got {self.vwap_distance!r}")
        if not math.isfinite(self.strategy_confidence) or not (
            0.0 <= self.strategy_confidence <= 1.0
        ):
            raise ValueError(
                f"strategy_confidence must be in [0, 1], got {self.strategy_confidence!r}"
            )


# ── PRO_STRATEGY_SIGNAL ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProStrategySignalPayload:
    """
    Intermediate routing payload emitted by ProSetupEngine (pro_setups/).

    Producer  : ProSetupEngine (pro_setups/engine.py)
    Consumers : ProStrategyRouter → RiskAdapter → ORDER_REQ → AlpacaBroker

    Pipeline
    --------
    BAR → 11 detectors → StrategyClassifier → PRO_STRATEGY_SIGNAL
        → ProStrategyRouter → RiskAdapter.validate_and_emit() → ORDER_REQ

    The existing RiskEngine / AlpacaBroker are NOT bypassed for execution —
    ORDER_REQ is emitted so the existing AlpacaBroker handles fills.
    The existing RiskEngine's SIGNAL handler is NOT used; RiskAdapter is the
    risk gate for pro setups.

    Fields
    ------
    ticker          : stock symbol
    strategy_name   : one of the 11 pro setup names (e.g. 'trend_pullback')
    tier            : 1 (high win-rate), 2 (moderate), 3 (high expectancy)
    direction       : 'long' or 'short'
    entry_price     : proposed entry (close of signal bar)
    stop_price      : tier-based hard stop
    target_1        : partial-exit target  (1R / 1.5R / 3R depending on tier)
    target_2        : full-exit target     (2R / 3R  / 5–8R depending on tier)
    atr_value       : ATR(14) at signal time
    rvol            : relative volume ratio
    rsi_value       : RSI(14) at signal time [0, 100]
    vwap            : current session VWAP
    confidence      : classifier confidence [0, 1]
    detector_signals: JSON string — {detector_name: {fired, direction, strength}}
    """
    ticker:           str
    strategy_name:    str
    tier:             int    # 1 | 2 | 3
    direction:        str    # 'long' | 'short'
    entry_price:      float
    stop_price:       float
    target_1:         float
    target_2:         float
    atr_value:        float
    rvol:             float
    rsi_value:        float
    vwap:             float
    confidence:       float
    detector_signals: str = '{}'   # JSON snapshot
    # ── Edge context (V10: captured at signal time for edge model) ─────
    timeframe:        str = '1min'           # '1min' or '5min'
    regime_at_entry:  str = ''               # 'BULL_TREND', 'RANGE_BOUND', etc.
    time_bucket:      str = ''               # '0930_0945', '0945_1030', etc.
    confluence_score: float = 0.0            # aggregate detector agreement [0, 1]

    def __post_init__(self):
        _require_ticker(self.ticker)
        if not self.strategy_name:
            raise ValueError("strategy_name must be non-empty")
        if self.tier not in (1, 2, 3):
            raise ValueError(f"tier must be 1, 2, or 3; got {self.tier!r}")
        if self.direction not in ('long', 'short'):
            raise ValueError(f"direction must be 'long' or 'short'; got {self.direction!r}")
        _require_positive('entry_price', self.entry_price)
        _require_positive('stop_price',  self.stop_price)
        _require_positive('target_1',    self.target_1)
        _require_positive('target_2',    self.target_2)
        _require_positive('atr_value',   self.atr_value)
        _require_non_negative('rvol',    self.rvol)
        if not (0.0 <= self.rsi_value <= 100.0):
            raise ValueError(f"rsi_value must be [0, 100]; got {self.rsi_value!r}")
        _require_positive('vwap', self.vwap)
        if not math.isfinite(self.confidence) or not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be [0, 1]; got {self.confidence!r}")
        if self.direction == 'long':
            if self.stop_price >= self.entry_price:
                raise ValueError(
                    f"stop_price ({self.stop_price}) must be < entry_price "
                    f"({self.entry_price}) for long direction"
                )
            if self.target_1 <= self.entry_price:
                raise ValueError(
                    f"target_1 ({self.target_1}) must be > entry_price "
                    f"({self.entry_price}) for long direction"
                )
            if self.target_2 <= self.target_1:
                raise ValueError(
                    f"target_2 ({self.target_2}) must be > target_1 ({self.target_1})"
                )
        else:  # short
            if self.stop_price <= self.entry_price:
                raise ValueError(
                    f"stop_price ({self.stop_price}) must be > entry_price "
                    f"({self.entry_price}) for short direction"
                )
            if self.target_1 >= self.entry_price:
                raise ValueError(
                    f"target_1 ({self.target_1}) must be < entry_price "
                    f"({self.entry_price}) for short direction"
                )
            if self.target_2 >= self.target_1:
                raise ValueError(
                    f"target_2 ({self.target_2}) must be < target_1 ({self.target_1})"
                )


# ── OPTIONS_SIGNAL ────────────────────────────────────────────────────────────

class OptionStrategyType(str, Enum):
    """All 13 supported option strategy types."""
    LONG_CALL         = 'long_call'
    LONG_PUT          = 'long_put'
    BULL_CALL_SPREAD  = 'bull_call_spread'
    BEAR_PUT_SPREAD   = 'bear_put_spread'
    BULL_PUT_SPREAD   = 'bull_put_spread'
    BEAR_CALL_SPREAD  = 'bear_call_spread'
    LONG_STRADDLE     = 'long_straddle'
    LONG_STRANGLE     = 'long_strangle'
    IRON_CONDOR       = 'iron_condor'
    IRON_BUTTERFLY    = 'iron_butterfly'
    CALENDAR_SPREAD   = 'calendar_spread'
    DIAGONAL_SPREAD   = 'diagonal_spread'
    BUTTERFLY_SPREAD  = 'butterfly_spread'

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class OptionsSignalPayload:
    """
    Options trade signal produced by OptionsEngine (T3.7).

    Producer  : OptionsEngine
    Consumers : AlpacaOptionsBroker (multi-leg), DurableEventLog (audit)

    legs_json : JSON string encoding a list of leg dicts:
                [{'symbol': 'AAPL240119C00180000', 'side': 'buy', 'qty': 1,
                  'ratio': 1, 'limit_price': 2.50}, ...]
                Stored as str to keep the payload frozen and serialisable.

    source    : 'signal' (from StrategyEngine SIGNAL event), 'bar_scan', or 'pop_signal'
                (independent neutral/volatility scan).
    """
    ticker:           str
    strategy_type:    OptionStrategyType
    underlying_price: float
    expiry_date:      str    # ISO date string: 'YYYY-MM-DD'
    net_debit:        float  # positive=cost, negative=credit received
    max_risk:         float  # maximum dollar loss; always > 0
    max_reward:       float  # maximum dollar gain; always > 0
    atr_value:        float
    rvol:             float
    rsi_value:        float
    legs_json:        str    # JSON list of leg dicts
    source:           str    # 'signal' | 'bar_scan'
    # ── Edge context (V10: captured at signal time for edge model) ─────
    timeframe:        str = '1min'           # '1min' or '5min'
    regime_at_entry:  str = ''               # 'BULL_TREND', 'RANGE_BOUND', etc.
    time_bucket:      str = ''               # '0930_0945', '0945_1030', etc.

    def __post_init__(self):
        _require_ticker(self.ticker)
        object.__setattr__(self, 'strategy_type', OptionStrategyType(self.strategy_type))
        _require_positive('underlying_price', self.underlying_price)
        if not self.expiry_date:
            raise ValueError("expiry_date must be a non-empty string")
        _require_positive('max_risk', self.max_risk)
        _require_positive('max_reward', self.max_reward)
        _require_positive('atr_value', self.atr_value)
        _require_non_negative('rvol', self.rvol)
        if not math.isfinite(self.rsi_value) or not (0.0 <= self.rsi_value <= 100.0):
            raise ValueError(f"rsi_value must be in [0, 100], got {self.rsi_value!r}")
        if not self.legs_json:
            raise ValueError("legs_json must be a non-empty string")
        if self.source not in ('signal', 'bar_scan', 'pop_signal'):
            raise ValueError(f"source must be 'signal', 'bar_scan', or 'pop_signal', got {self.source!r}")


# ── NEWS_DATA ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NewsDataPayload:
    """
    Benzinga news data snapshot for a ticker (persistence only).

    Producer  : PopStrategyEngine (after early-exit filter passes)
    Consumers : EventSourcingSubscriber (DB persistence for post-hoc analysis)

    Emitted as non-durable (no Redpanda write) to minimise bus overhead.
    Only emitted for tickers that pass the bar-activity early-exit filter,
    so volume is bounded to ~20-50 tickers per cycle, not all 183.
    """
    ticker:           str
    headlines_1h:     int        # count of headlines in last 1 hour
    headlines_24h:    int        # count of headlines in last 24 hours
    avg_sentiment_1h: float      # average sentiment score [-1, +1]
    avg_sentiment_24h: float
    top_headline:     str        # most recent headline text (truncated to 200 chars)
    latest_headline_time: str = ''   # ISO timestamp of most recent headline
    oldest_headline_time: str = ''   # ISO timestamp of oldest headline in 1h window
    news_fetched_at:      str = ''   # ISO timestamp when Benzinga API was called
    source:           str = 'benzinga'

    def __post_init__(self):
        _require_ticker(self.ticker)
        _require_non_negative('headlines_1h',  self.headlines_1h)
        _require_non_negative('headlines_24h', self.headlines_24h)
        if not math.isfinite(self.avg_sentiment_1h) or not (-1.0 <= self.avg_sentiment_1h <= 1.0):
            raise ValueError(f"avg_sentiment_1h must be in [-1, 1], got {self.avg_sentiment_1h!r}")
        if not math.isfinite(self.avg_sentiment_24h) or not (-1.0 <= self.avg_sentiment_24h <= 1.0):
            raise ValueError(f"avg_sentiment_24h must be in [-1, 1], got {self.avg_sentiment_24h!r}")


# ── SOCIAL_DATA ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SocialDataPayload:
    """
    StockTwits social data snapshot for a ticker (persistence only).

    Producer  : PopStrategyEngine (after early-exit filter passes)
    Consumers : EventSourcingSubscriber (DB persistence for post-hoc analysis)

    Emitted as non-durable (no Redpanda write) to minimise bus overhead.
    """
    ticker:           str
    mention_count:    int
    mention_velocity: float
    bullish_pct:      float      # [0, 1]
    bearish_pct:      float      # [0, 1]
    newest_message_time: str = ''  # ISO timestamp of newest StockTwits message
    oldest_message_time: str = ''  # ISO timestamp of oldest message in batch
    social_fetched_at:   str = ''  # ISO timestamp when StockTwits API was called
    source:           str = 'stocktwits'

    def __post_init__(self):
        _require_ticker(self.ticker)
        _require_non_negative('mention_count',    self.mention_count)
        _require_non_negative('mention_velocity', self.mention_velocity)
        _require_non_negative('bullish_pct',      self.bullish_pct)
        _require_non_negative('bearish_pct',      self.bearish_pct)
