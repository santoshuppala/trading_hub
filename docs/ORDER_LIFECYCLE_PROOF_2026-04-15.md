# Order Lifecycle Proof Document — 2026-04-15

## Purpose
This document traces every order lifecycle path across Core, Pro, and Pop engines through both Alpaca and Tradier brokers. It serves as verification that all data flows are correctly wired, all fields are preserved, and all edge cases (partial fills, crashes, restarts) are handled.

## Architecture Overview

All three engines follow the **same execution path** through Core:

```
┌──────────┐     ┌──────────┐     ┌──────────┐
│   Pro    │     │   Pop    │     │   Core   │
│ (signal) │     │ (signal) │     │  (VWAP)  │
└────┬─────┘     └────┬─────┘     └────┬─────┘
     │ ORDER_REQ      │ ORDER_REQ      │ SIGNAL
     │ via IPC        │ via IPC        │
     ▼                ▼                ▼
┌─────────────────────────────────────────────┐
│              Core Process                    │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐ │
│  │RiskEngine│→ │SmartRouter│→ │Alpaca/    │ │
│  │          │  │(round-    │  │Tradier    │ │
│  │          │  │ robin BUY)│  │Broker     │ │
│  └──────────┘  └──────────┘  └───────────┘ │
│       ▲              │              │       │
│       │              │ BUY: save    │ FILL  │
│       │              │ broker map   │       │
│       │              ▼              ▼       │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐ │
│  │Strategy  │← │Position  │← │ FillPayload│ │
│  │Engine    │  │Manager   │  │(stop/tgt) │ │
│  │(exits)   │  │(bot_state│  └───────────┘ │
│  └──────────┘  │.json)    │                 │
│       │        └──────────┘                 │
│       │ SELL                                │
│       ▼                                     │
│  SmartRouter → same broker (from map)       │
└─────────────────────────────────────────────┘
```

## The 6 Paths

### PATH 1: Core VWAP → Alpaca

| Step | Component | What Happens | stop_price | target_price | broker |
|------|-----------|-------------|------------|--------------|--------|
| 1 | StrategyEngine._check_entry | Computes from ATR/candle | 193.35 | 193.74 | — |
| 2 | StrategyEngine → SIGNAL | Emitted with stop/target | 193.35 | 193.74 | — |
| 3 | RiskEngine → ORDER_REQ | Passed through | 193.35 | 193.74 | — |
| 4 | SmartRouter._select_broker | Round-robin → alpaca | 193.35 | 193.74 | alpaca |
| 5 | SmartRouter._execute_on_broker | Saves to position_broker_map.json | 193.35 | 193.74 | alpaca |
| 6 | AlpacaBroker._execute_buy | BRACKET order (stop + target) | 193.35 | 193.74 | alpaca |
| 7 | AlpacaBroker → FILL | FillPayload carries stop/target | 193.35 | 193.74 | alpaca |
| 8 | PositionManager._open_position | Uses FillPayload (NOT placeholders) | 193.35 | 193.74 | alpaca |
| 9 | bot_state.json | Persisted with correct stops | 193.35 | 193.74 | alpaca |
| 10 | StrategyEngine._check_exits | Reads from position dict | 193.35 | 193.74 | — |
| 11 | SmartRouter (SELL) | Routes to alpaca (from map) | — | — | alpaca |
| 12 | AlpacaBroker._execute_sell | Cancels bracket children first | — | — | alpaca |
| 13 | PositionManager._close_position | close_detail with broker tag | — | — | alpaca |

**Crash protection:** Alpaca bracket stop-loss order at $193.35
**Partial fill:** Cancel remainder + resubmit stop for filled qty
**Restart:** bot_state.json + position_broker_map.json

### PATH 2: Core VWAP → Tradier

| Step | Component | What Happens | stop_price | target_price | broker |
|------|-----------|-------------|------------|--------------|--------|
| 1-3 | Same as PATH 1 | Signal → RiskEngine → ORDER_REQ | 193.35 | 193.74 | — |
| 4 | SmartRouter._select_broker | Round-robin → tradier | 193.35 | 193.74 | tradier |
| 5 | SmartRouter._execute_on_broker | Saves to position_broker_map.json | 193.35 | 193.74 | tradier |
| 6 | TradierBroker._execute_buy | Simple LIMIT order | 193.35 | 193.74 | tradier |
| 7 | TradierBroker._submit_stop_order | Standalone STOP order at $193.35 | 193.35 | — | tradier |
| 8 | TradierBroker → FILL | FillPayload carries stop/target | 193.35 | 193.74 | tradier |
| 9-13 | Same as PATH 1 | Position → exits → sell via tradier | 193.35 | 193.74 | tradier |

**Crash protection:** Standalone stop order at Tradier ($193.35)
**Partial fill:** Cancel remainder in _poll_fill, return actual filled qty
**Sell routing:** _cancel_open_orders (cancels stop) before market sell

### PATH 3: Pro → IPC → Core → Alpaca

| Step | Component | What Happens | stop_price | target_price | reason |
|------|-----------|-------------|------------|--------------|--------|
| 1 | ProSetupEngine._on_bar | Detectors → classify → generate_stop | 193.35 | 193.74 | — |
| 2 | Engine-level stop floor | Enforces 0.3% minimum | 193.35 | 193.74 | — |
| 3 | RiskAdapter.validate_and_emit | 9 risk checks → ORDER_REQ | 193.35 | 193.74 | pro:sr_flip:T1:long |
| 4 | run_pro.py._forward_order | Publishes to Redpanda | 193.35 | 193.74 | pro:sr_flip:T1:long |
| 5 | run_core.py._on_remote_order | Reconstructs OrderRequestPayload | 193.35 | 193.74 | pro:sr_flip:T1:long |
| 6+ | Same as PATH 1 (steps 4-13) | SmartRouter → Alpaca bracket | 193.35 | 193.74 | pro:sr_flip:T1:long |

**Key difference:** Position dict has `strategy='pro:sr_flip'` (parsed from reason field)
**Who monitors exits:** Core's StrategyEngine (uses position dict's stop/target)

### PATH 4: Pro → IPC → Core → Tradier
Steps 1-5 same as PATH 3. Steps 6+ same as PATH 2.

### PATH 5: Pop → IPC → Core → Alpaca

| Step | Component | What Happens | stop_price | target_price | reason |
|------|-----------|-------------|------------|--------------|--------|
| 1 | PopExecutor.execute_entry | Risk checks → emit ORDER_REQ | 193.35 | 193.74 | pop:VWAP_RECLAIM:pop |
| 2 | run_pop.py._forward_order | Publishes to Redpanda | 193.35 | 193.74 | pop:VWAP_RECLAIM:pop |
| 3 | run_core.py._on_remote_order | Reconstructs OrderRequestPayload | 193.35 | 193.74 | pop:VWAP_RECLAIM:pop |
| 4+ | Same as PATH 1 (steps 4-13) | SmartRouter → Alpaca bracket | 193.35 | 193.74 | pop:VWAP_RECLAIM:pop |

**Key difference:** Position dict has `strategy='pop:VWAP_RECLAIM'`
**Pop no longer uses separate Alpaca account** — all execution through Core's SmartRouter

### PATH 6: Pop → IPC → Core → Tradier
Steps 1-3 same as PATH 5. Steps 4+ same as PATH 2.

---

## Critical Data Field Preservation

| Field | Signal Gen | RiskEngine | IPC (Redpanda) | Core Receive | Broker | FillPayload | PositionManager | bot_state.json | Exit Monitor |
|-------|-----------|------------|----------------|-------------|--------|-------------|-----------------|----------------|-------------|
| stop_price | COMPUTED | PASSED | SERIALIZED | RECONSTRUCTED | BRACKET/STOP | CARRIED | USED (not placeholder) | PERSISTED | READ |
| target_price | COMPUTED | PASSED | SERIALIZED | RECONSTRUCTED | BRACKET | CARRIED | USED (not placeholder) | PERSISTED | READ |
| reason/strategy | SET | SET | SERIALIZED | RECONSTRUCTED | N/A | CARRIED | PARSED → strategy field | PERSISTED | N/A |
| broker | N/A | N/A | N/A | N/A | SET | TAGGED | PROPAGATED | (via broker map) | N/A |
| qty | SIZED | SIZED | SERIALIZED | RECONSTRUCTED | ORDERED | ACTUAL FILLED | STORED | PERSISTED | READ |

**Every field is preserved end-to-end. No placeholders used when real values are available.**

---

## Partial Fill Handling

### BUY Partial Fill (ordered 5, got 2)

| Broker | Detection | Action | Stop Order | Position |
|--------|-----------|--------|------------|----------|
| **Alpaca** | `status='partially_filled'` in poll | Cancel remainder, re-check final qty | Cancel bracket children, resubmit standalone stop for 2 shares | Created with qty=2 |
| **Tradier** | `status='partially_filled'` in _poll_fill | Cancel remaining order, return actual filled qty | Submit stop for 2 shares (filled_qty) | Created with qty=2 |

### SELL Partial Fill (selling 5, got 3)

| Broker | Detection | Action | Position |
|--------|-----------|--------|----------|
| **Alpaca** | Re-check after 5s timeout | Emit FILL for actual 3 (not optimistic 5) | PositionManager does partial close (remaining=2) |
| **Tradier** | `status='partially_filled'` in _poll_fill | Cancel remaining, return actual qty | PositionManager does partial close |

### Late Fill (fills after poll timeout)

| Broker | Risk | Mitigation |
|--------|------|-----------|
| **Alpaca** | Remainder cancelled during poll → no late fills | Cancel in poll loop before retry |
| **Tradier** | Remainder cancelled in _poll_fill → no late fills | _cancel_order called on partial |

---

## Crash Recovery

### What's Protected

| Component | Protection | File |
|-----------|-----------|------|
| Position state | bot_state.json (atomic write every mutation) | monitor/state.py |
| Broker mapping | position_broker_map.json (saved every BUY) | monitor/smart_router.py |
| Broker stop-loss | Alpaca bracket child OR Tradier standalone stop | Lives at broker |
| Event history | event_store (TimescaleDB) | db/event_sourcing_subscriber.py |
| Position registry | data/position_registry.json (fcntl locked) | monitor/distributed_registry.py |

### Restart Sequence

1. Load bot_state.json → positions with correct stop/target/strategy
2. Load position_broker_map.json → know which broker holds each position
3. Reconcile with Alpaca (qty mismatches, orphaned positions, stale local)
4. Reconcile with Tradier (same)
5. Remove stale local positions not at any broker
6. StrategyEngine monitors exits using correct stops from position dict
7. Broker stop orders still active (bracket or standalone)

### Worst Case: Double Protection

If both Core AND broker stop trigger simultaneously:
- Broker stop fires → sells at broker price
- Core SELL → SmartRouter → _cancel_bracket_children → bracket already fired
- Cancel fails → recheck status → sees "already filled" → treats as success
- No short created (cancel-before-sell pattern)

---

## What Changed Today (Fixes Applied)

| Before | After | Impact |
|--------|-------|--------|
| PositionManager used placeholders (0.5%/1%) | Uses FillPayload.stop_price/target_price | Correct stops on ALL positions |
| No `strategy` field on positions | Parsed from reason ("pro:sr_flip") | Can distinguish Pro/Pop/VWAP positions |
| Pop used separate Alpaca account | Pop sends ORDER_REQ via IPC → Core executes | Same broker for BUY and SELL |
| No stop order on Tradier | Standalone stop after BUY fill | Crash protection on Tradier |
| Bracket children not cancelled before SELL | _cancel_bracket_children called first | No double-sell → no shorts |
| Partial fills created qty mismatches | Cancel remainder, resubmit stop for actual qty | Correct qty tracking |
| position_broker_map lost on restart | Persisted to data/position_broker_map.json | SELL goes to correct broker after restart |
| No engine-level stop floor | 0.3% minimum enforced after generate_stop() | No micro-stops from strategy overrides |

---

## Verification Commands

```bash
# 1. Check position_broker_map persistence
cat data/position_broker_map.json

# 2. Check bot_state.json has correct stops
python3 -c "import json; d=json.load(open('bot_state.json')); [print(f'{t}: stop={p[\"stop_price\"]:.2f} target={p[\"target_price\"]:.2f} strategy={p.get(\"strategy\",\"?\")}') for t,p in d.get('positions',{}).items()]"

# 3. Check DB has broker tags on fills
psql -U trading -d tradinghub -c "SELECT event_payload->>'ticker', event_payload->>'broker', event_payload->>'bracket_order', event_payload->>'stop_price' FROM event_store WHERE event_type='FillExecuted' ORDER BY event_time DESC LIMIT 5"

# 4. Check no unintended shorts
python3 -c "from alpaca.trading.client import TradingClient; c=TradingClient('key','secret',paper=True); print([f'{p.symbol} qty={p.qty}' for p in c.get_all_positions() if int(float(p.qty))<0])"

# 5. Check Tradier stop orders exist
curl -s -H "Authorization: Bearer $TRADIER_SANDBOX_TOKEN" "https://sandbox.tradier.com/v1/accounts/$TRADIER_ACCOUNT_ID/orders?filter=open" | python3 -m json.tool
```

---

*Document generated 2026-04-15. Covers 35+ files, 6 execution paths, all edge cases.*
