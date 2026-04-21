# 03 -- Broker Architecture Design

Source of truth: code in `monitor/smart_router.py`, `monitor/brokers.py`, `monitor/tradier_broker.py`, `monitor/portfolio_risk.py`, `options/broker.py`.

---

## Purpose

The broker layer handles all order execution across multiple brokerages (Alpaca, Tradier) for both equities and options. It provides:

- **Reliable execution** with retry logic, partial fill handling, and fill polling.
- **Multi-broker routing** with automatic failover and circuit breaking.
- **Portfolio-level risk enforcement** as a pre-trade gate before any order reaches a broker.
- **Separate options execution path** that avoids polluting the equity order channel.

## Design Principles

1. **Event-driven for equities, direct-call for options.** Equity brokers subscribe to `ORDER_REQ` on the EventBus and emit `FILL` / `ORDER_FAIL`. The options broker is called directly by `OptionsEngine` to keep the two execution paths cleanly separated.

2. **Sell orders always route to the opening broker.** A position opened at Alpaca must close at Alpaca. The SmartRouter maintains a persistent `position_broker_map` to enforce this invariant across restarts.

3. **Reduce-risk orders always pass.** Both `PortfolioRiskGate` and `SmartRouter` let SELL orders through unconditionally -- the priority is always to allow risk reduction.

4. **Fail loud, fail safe.** Circuit breakers disable brokers after consecutive failures. When all brokers are down, the system emits a CRITICAL alert and halts order routing rather than silently dropping orders.

5. **Thread safety everywhere.** Every component that touches shared state (health dicts, position maps, P&L tracking) uses `threading.Lock` to guard mutations.

6. **Idempotent fill emission.** Brokers tag every `FILL` event with `_routed_broker` so downstream consumers (SmartRouter health tracking, event sourcing) can attribute fills correctly without ambiguity.

---

## Component: PortfolioRiskGate

**File:** `monitor/portfolio_risk.py`

### Role

Pre-trade aggregate risk gate. Subscribes to `ORDER_REQ` at **priority 0** -- runs before any broker (priority 1) and before SmartRouter. Blocks BUY orders that would breach portfolio-level limits. SELL orders always pass.

### Design Choices

**Four independent limit checks, evaluated in order:**

| # | Check | Default Limit | Rationale |
|---|-------|---------------|-----------|
| 1 | Intraday drawdown (realized + unrealized) | -$5,000 | Hard circuit breaker. Once breached, sets `_halted = True` and blocks ALL subsequent buys for the session. Sends CRITICAL alert. |
| 2 | Notional exposure cap | $100,000 | Prevents over-concentration. Checks `current_notional + order_notional` against cap. |
| 3 | Buying power (aggregate across all accounts) | Sum of all accounts | Queries 3 Alpaca accounts (main, pop, options) + Tradier via REST with 3s timeout each. Returns `None` if no account responds, which causes the check to be skipped rather than blocking. |
| 4 | Portfolio Greeks (delta, gamma) | delta 5.0, gamma 1.0 | Prevents excessive directional or convexity risk from options positions. Sources data from `PortfolioGreeksTracker`. |

**Blocking mechanism:** When a check fails, the gate sets `event._portfolio_blocked = True` on the event object. Downstream brokers check this flag and skip execution. The gate also emits a `RISK_BLOCK` event for audit logging.

**State tracking:** The gate maintains its own `_positions` dict and `_realized_pnl` by listening to FILL and POSITION events. This is independent of the monitor's position tracking -- the gate needs its own consistent view for real-time limit enforcement.

**Daily reset:** `reset_day()` clears all tracking state. Must be called at session open.

**All limits are configurable** via environment variables (`MAX_INTRADAY_DRAWDOWN`, `MAX_NOTIONAL_EXPOSURE`, `MAX_PORTFOLIO_DELTA`, `MAX_PORTFOLIO_GAMMA`).

---

## Component: SmartRouter

**File:** `monitor/smart_router.py`

### Role

Routes `ORDER_REQ` events to the best available broker. Provides failover, circuit breaking, and position-to-broker affinity tracking. Subscribes at **priority 1** (after PortfolioRiskGate at 0).

### Design Choices

**Broker selection strategy (evaluated in order):**

1. **SELL orders** -- look up the opening broker from `_position_broker` map. If not found, probe each broker's API for an open position in that ticker. Last resort: use default broker.
2. **Explicit source** -- if the order's `reason` field contains a broker name (e.g. "tradier"), route there.
3. **Health-based** -- if only one broker is healthy, use it.
4. **Round-robin** -- for BUY orders across healthy brokers, distribute via `_order_counter % len(healthy_brokers)`.

**Why round-robin instead of spread comparison:** The docstring mentions spread comparison, but the implementation uses round-robin. This is simpler and avoids the latency of fetching quotes from both brokers before every order. Spread comparison can be added later as a routing signal.

**Circuit breaker:** `BrokerHealth` dataclass tracks `consecutive_failures`. After 3 consecutive failures (configurable via `max_failures`), the broker is marked `available = False`. After 300 seconds (configurable `cooldown_seconds`), `_is_healthy()` auto-recovers the broker by checking elapsed time against `last_failure_time`. A single successful fill resets the failure counter immediately.

**Position broker map persistence:** `_position_broker` (ticker -> broker name) is persisted to `data/position_broker_map.json` using atomic write (write to `.tmp`, then `os.replace`). Loaded on startup. Updated on every BUY fill and POSITION CLOSED event.

**Startup seeding:** `seed_position_broker()` takes sets of tickers from both Alpaca and Tradier open positions and merges them into the map. This handles the case where the process restarts and the JSON file is stale or missing.

**Unsubscribe pattern:** On init, SmartRouter unsubscribes each broker's own `ORDER_REQ` handler to prevent double execution. It then calls broker handlers directly (`broker._on_order_request(event)` or `broker._on_order_req(event)`) when routing. This is a deliberate coupling -- the router owns the execution path.

**Failover on exception:** If `_execute_on_broker` catches an exception from the primary broker, it immediately tries the fallback. If both fail, it emits `ORDER_FAIL` with `reason='no healthy broker available'` and sends a CRITICAL alert (once, via `_all_brokers_down_alerted` flag).

---

## Component: AlpacaBroker

**File:** `monitor/brokers.py`

### Role

Executes equity orders via the Alpaca TradingClient (live or paper). Implements the `BaseBroker` abstract class.

### Design Choices

**BUY -- marketable limit with bracket:**

- Submits a `LimitOrderRequest` at the ask price. Uses `OrderClass.BRACKET` with `StopLossRequest` and `TakeProfitRequest` when stop/target prices are provided. This gives broker-side crash protection immediately on fill.
- Polls for fill every 0.5s with a 5s timeout (increased from an earlier 2s timeout that was too aggressive for illiquid names).
- On timeout: cancels the order, refreshes the ask via `quote_fn(ticker)`, and retries up to 2 times.
- **Slippage cap:** If the refreshed ask exceeds `original_ask * (1 + MAX_SLIPPAGE_PCT)`, the order is abandoned. This prevents chasing a fast-moving stock.
- **1.0s settle wait** after BUY fill before emitting the FILL event. This gives Alpaca time to fully settle the order, preventing race conditions where a subsequent SELL hits an incomplete position.

**Partial fill handling (BUY):**

- On `partially_filled` status: cancel the remainder, wait 0.5s, re-fetch the final filled qty.
- If the original order was a bracket, cancel all bracket children (`_cancel_bracket_children`) and resubmit a standalone stop order for the actual filled quantity. This prevents the bracket stop from trying to sell more shares than held.

**SELL -- market order with pre-cancellation:**

- Before submitting the market sell, cancels all open orders for the ticker (both buy and sell sides). This prevents wash trade rejection from Alpaca and avoids the bracket stop firing simultaneously with the strategy exit.
- Verifies actual position qty at Alpaca via `get_open_position()` and uses the Alpaca qty if it differs from the monitor's qty. This handles orphaned positions and partial fill drift.
- Polls for fill confirmation (5s timeout). If not confirmed, emits FILL optimistically. If the submit itself throws an exception, polls by `client_order_id` to check if Alpaca actually filled it (handles network timeout after successful server-side execution).

**`_cancel_bracket_children`:** Fetches all open orders, filters for the ticker, and cancels any with `order_class='bracket'` or `side='sell'`. This is called before every SELL to prevent double-execution (strategy exit + bracket stop both firing).

**BaseBroker contract:** Abstract class defines `_execute_buy` and `_execute_sell`. `_on_order_request` dispatches based on `p.side`. Checks `event._portfolio_blocked` flag before executing (set by PortfolioRiskGate). `_emit_fill` tags every FILL event with `_routed_broker`.

**PaperBroker:** Fills every order instantly at the requested price. No API calls. Used for testing and CI.

---

## Component: TradierBroker

**File:** `monitor/tradier_broker.py`

### Role

Executes equity and option orders via Tradier's REST API. Supports sandbox and production modes.

### Design Choices

**Does not extend BaseBroker.** TradierBroker is a standalone class with its own `_on_order_req` handler (note: different method name than AlpacaBroker's `_on_order_request`). SmartRouter handles both naming conventions when unsubscribing and dispatching.

**BUY -- simple limit (no brackets):**

- Submits limit buy orders via REST POST. Tradier does not support OTOCO brackets in the same way as Alpaca, so the strategy engine handles exit monitoring in-process.
- After a successful BUY fill, submits a standalone stop-loss order via `_submit_stop_order()` for crash protection. This stop lives independently and is easier to cancel before selling than OTOCO children.
- Retry logic: up to 2 attempts with cancel-and-resubmit on timeout.

**SELL -- market with pre-cancellation:**

- `_cancel_open_orders(ticker)` cancels any open sell/sell_short orders for the ticker before submitting the market sell. This prevents double-sells from standalone stop orders firing alongside strategy exits.
- Submits market sell via REST POST.

**API quirk handling:** Tradier's REST API returns `order_list` as either a dict (single order) or a list (multiple orders). Both `_cancel_open_orders`, `get_positions`, and `cancel_all_orders` handle this with `isinstance(order_list, dict)` checks and wrapping to a list.

**HTTP timeouts:** 10s for order submissions, 5s for status checks. These are more generous than Alpaca's SDK timeouts because Tradier's REST API has higher latency variability.

**Fill polling:** Same pattern as AlpacaBroker -- 5s timeout, 0.5s intervals. Handles partial fills by cancelling the remainder and re-fetching final qty.

**Options support:** Exposes `submit_option_order()` for single-leg and `submit_multileg_order()` for multi-leg options. These use Tradier's `class: option` and `class: multileg` order types. Parameters are indexed (`option_symbol[0]`, `side[0]`, etc.) for multi-leg. These methods return order IDs and do not emit bus events -- they are called directly by higher-level options logic.

**Session management:** Uses a persistent `requests.Session` with bearer token auth. The session is created once on init and reused for all API calls.

---

## Component: AlpacaOptionsBroker

**File:** `options/broker.py`

### Role

Executes single-leg and multi-leg options orders via Alpaca's options trading API. Called directly by `OptionsEngine` -- does NOT subscribe to `ORDER_REQ` on the EventBus.

### Design Choices

**Direct-call architecture (not event-driven):**

- `OptionsEngine` calls `broker.execute(trade_spec, source_event)` directly.
- This avoids polluting the equity ORDER_REQ channel with options orders and prevents equity brokers from accidentally picking up options orders.
- Uses its own `TradingClient` instance with separate credentials (`APCA_OPTIONS_KEY` / `APCA_OPTIONS_SECRET`), isolating options margin and buying power from equity accounts.

**Single-leg execution:**

- Submits a `LimitOrderRequest` with OCC symbol (alpaca-py recognizes these as options).
- Polls for fill every 1s with a 15s timeout (longer than equity because options markets are less liquid).
- On timeout: cancels and retries with $0.05 price widening (buy: increase limit, sell: decrease limit).
- Up to 1 retry (2 total attempts). Reduced from 2 retries to limit blocking time.

**Multi-leg execution:**

- Uses Alpaca's `order_class='mleg'` format via REST POST to `/v2/orders`.
- Each leg specifies `ratio_qty` (not `qty`) -- the top-level `qty` field represents the number of spreads.
- Entry orders use `type: limit` with net debit/credit price. Close orders use `type: market`.
- Submits via `TradingClient._request("POST", "/orders", body)` (internal method), falls back to `.post()`, then to raw `httpx` POST if neither is available. The `_request` method prepends `/v2` internally, so the path is `/orders` not `/v2/orders`.

**Cancel race detection:**

- If cancel fails on a multi-leg order, the broker re-checks order status. If it turns out the order was filled between the timeout and the cancel attempt, it returns success.
- If cancel failed AND the order is not filled, the broker aborts rather than retrying to avoid submitting duplicate orders.

**Close positions:**

- `close_position(ticker, legs)` reverses all leg sides (buy -> sell, sell -> buy) and submits as a market order.
- Uses the same single-leg or multi-leg path depending on the number of legs.

---

## Priority and Event Flow

```
ORDER_REQ event emitted by strategy engine
    |
    v
[Priority 0] PortfolioRiskGate._on_order_req
    |-- SELL? -> pass through
    |-- BUY?  -> check drawdown, notional, buying power, greeks
    |           -> BLOCKED? set _portfolio_blocked=True, emit RISK_BLOCK, return
    |           -> PASSED?  continue
    v
[Priority 1] SmartRouter._on_order_req
    |-- select broker (position affinity, health, round-robin)
    |-- execute on primary broker
    |   |-- success -> broker emits FILL
    |   |-- failure -> try fallback broker
    |       |-- success -> fallback emits FILL
    |       |-- failure -> emit ORDER_FAIL + CRITICAL alert
    v
[Priority 1] Broker._on_order_request (only if SmartRouter not present)
    |-- dispatched directly by SmartRouter in normal operation
    |-- standalone mode: subscribes to ORDER_REQ itself

FILL event
    |
    v
[Priority 0]  PortfolioRiskGate._on_fill  -> update P&L tracking
[Priority 10] SmartRouter._on_fill         -> record_success, reset circuit breaker
```

**Options flow (separate path):**

```
OptionsEngine detects trade opportunity
    |
    v
AlpacaOptionsBroker.execute(trade_spec)
    |-- single leg -> LimitOrderRequest
    |-- multi leg  -> mleg REST POST
    |-- poll for fill -> return True/False
    v
OptionsEngine handles result directly (no bus events from broker)
```

---

## Key Invariants

1. **A position is always closed on the broker that opened it.** Enforced by SmartRouter's `_position_broker` map with disk persistence and startup seeding.

2. **Bracket children are always cancelled before a strategy-initiated SELL.** Both AlpacaBroker and TradierBroker cancel open sell orders for a ticker before submitting their own SELL. Without this, both the bracket stop and the strategy exit could fire, creating a short position.

3. **Partial fills are handled atomically.** On partial fill: cancel remainder, wait for settlement, re-fetch final qty, then emit FILL for actual filled quantity only. For brackets, the stop is resubmitted at the actual filled quantity.

4. **PortfolioRiskGate blocks are sticky within a session.** Once intraday drawdown is breached, `_halted` stays True until `reset_day()` is called. Other checks (notional, buying power, Greeks) are evaluated per-order and can pass once conditions change.

5. **The options execution path never touches the equity EventBus ORDER_REQ channel.** AlpacaOptionsBroker takes direct method calls and returns bool results. This prevents cross-contamination between equity and options execution.
