# Release Plan v1 — Risk Hardening & Edge Case Fixes

**Date**: 2026-04-14
**Status**: COMPLETED
**Commit**: `148fb28` on `tradinghub_options`

---

## Critical Fixes — ALL DONE

- [x] **Fix 1**: Sector concentration limit (max 2 per GICS sector) — `monitor/sector_map.py`, `risk_adapter.py`, `pop_strategy_engine.py`
- [x] **Fix 2**: Overnight position reduction (halve Pro/Pop swings at 3 PM) — `strategy_engine.py`
- [x] **Fix 3**: Kill switch on every FILL event (ms latency) — `run_monitor.py` KillSwitchGuard
- [x] **Fix 4**: Reconciler ATR-based stops (replaces fixed 3%) — `monitor.py`
- [x] **Fix 5**: StockTwits rate limit monitoring (warn 180, stop 195) — `stocktwits_social.py`

## Enhancements — ALL DONE

- [x] **Enhancement 1**: DB circuit breaker alerting — `db/writer.py`
- [x] **Enhancement 2**: Preflight position & order audit — `run_monitor.py`
- [ ] **Enhancement 3**: Options paper validation — manual testing procedure (not code)

## Also Delivered (not in original plan)

- [x] P&L reconciliation vs Alpaca equity ground truth (fees, orphaned trades)
- [x] Session equity baseline logging (start + EOD)
- [x] Hourly equity-based P&L in heartbeat
