# Session Test Plan — 2026-04-15 (Tuesday)

**Changes deployed**: Pop screener RVOL/gap fix, Benzinga/StockTwits persistence, sentiment baselines, ETF thresholds, options↔pop integration

**Risk**: MEDIUM — pop screener was dead yesterday, these changes bring it online with real data for the first time

---

## Pre-Market Checks (Before 9:30 AM ET)

### 1. Verify preflight logs
```bash
tail -100 logs/monitor_2026-04-15.log | grep -i "preflight\|baseline\|equity\|PopStrategy\|SentimentBaseline"
```
**Expect**:
- [ ] `SESSION EQUITY BASELINE` block with previous close equity
- [ ] `PREFLIGHT POSITION & ORDER AUDIT` — clean slate or swing positions listed
- [ ] `PopStrategyEngine ready | paper=True | max_positions=25`
- [ ] `SentimentBaseline ready | headline_tickers=0 | social_tickers=0 | from_db=False` (first day, no history yet)
- [ ] NO `permanently failed` errors for PopStrategyEngine

### 2. Verify no startup crashes
```bash
grep -i "error\|failed\|traceback" logs/monitor_2026-04-15.log | head -20
```
**Expect**: No errors related to `rvol`, `MarketDataSlice`, `sector_map`, `sentiment_baseline`

---

## During Market Hours (9:30 AM - 4:00 PM ET)

### 3. Pop Screener — Is it alive?
**Check every 30 minutes:**
```bash
# Are BARs being processed (not crashing)?
grep "POP skip\|POP data\|POP features\|POP CANDIDATE\|POP no candidate" logs/monitor_2026-04-15.log | tail -10
```
**Expect**:
- [ ] `POP skip` messages with real RVOL values (NOT all 1.0) and real gap values (NOT all 0.0)
- [ ] Some `POP data` messages showing Benzinga/StockTwits fetches (headline counts, bull/bear %)
- [ ] Some `POP features` messages showing computed features
- [ ] `POP no candidate` is fine — means screener evaluated but thresholds not met
- [ ] `POP CANDIDATE` = a signal was generated (may or may not happen on a quiet day)

### 4. RVOL values — are they real?
```bash
grep "POP skip\|POP features" logs/monitor_2026-04-15.log | grep -o "rvol=[0-9.]*" | sort | uniq -c | sort -rn | head -10
```
**Expect**:
- [ ] Distribution of RVOL values (NOT all 1.0)
- [ ] Most values between 0.3 and 3.0
- [ ] Some ETFs (QQQ, SPY) showing values around 0.5-1.5

**RED FLAG**: If all rvol=1.0 → `payload.rvol_df` is empty or not being passed correctly

### 5. Gap size — is it real?
```bash
grep "POP skip\|POP features" logs/monitor_2026-04-15.log | grep -o "gap=[0-9.-]*" | sort | uniq -c | sort -rn | head -10
```
**Expect**:
- [ ] Variety of gap values (positive and negative)
- [ ] NOT all 0.0000

**RED FLAG**: If all gap=0.0 → `payload.rvol_df` has no close column or is empty

### 6. ETF RVOL thresholds — are ETFs passing through?
```bash
grep -E "\b(QQQ|SPY|IWM|TQQQ|XLK)\b" logs/monitor_2026-04-15.log | grep -v "chain\|permanently" | head -20
```
**Expect**:
- [ ] ETF BARs should NOT be blocked by RVOL (threshold is 0.7 not 2.0)
- [ ] Some ETF signals or at least `POP features` for ETFs
- [ ] If VWAP SIGNAL for an ETF: risk gate should pass RVOL check at 0.7

### 7. Benzinga/StockTwits data persistence
```bash
# Check if events are being written to DB
psql -d tradinghub -c "SELECT COUNT(*), event_type FROM event_store WHERE event_type IN ('NewsDataSnapshot', 'SocialDataSnapshot') AND event_time >= CURRENT_DATE GROUP BY event_type;"
```
**Expect**:
- [ ] `NewsDataSnapshot` count growing (should be 20-50+ by midday)
- [ ] `SocialDataSnapshot` count growing (same range)
- [ ] If both are 0 → check if DB is connected and EventSourcingSubscriber registered

### 8. Benzinga data quality
```bash
psql -d tradinghub -c "
SELECT event_payload->>'ticker' as ticker,
       event_payload->>'headlines_1h' as hl_1h,
       event_payload->>'avg_sentiment_1h' as sent,
       event_payload->>'top_headline' as headline,
       event_payload->>'news_fetched_at' as fetched,
       event_payload->>'latest_headline_time' as news_time
FROM event_store
WHERE event_type = 'NewsDataSnapshot'
  AND event_time >= CURRENT_DATE
ORDER BY event_time DESC LIMIT 10;
"
```
**Expect**:
- [ ] Real headline text (not empty)
- [ ] Sentiment scores between -1.0 and 1.0
- [ ] `news_fetched_at` timestamps present
- [ ] `latest_headline_time` timestamps present (for latency analysis)

### 9. StockTwits data quality
```bash
psql -d tradinghub -c "
SELECT event_payload->>'ticker' as ticker,
       event_payload->>'mention_count' as mentions,
       event_payload->>'mention_velocity' as velocity,
       event_payload->>'bullish_pct' as bull,
       event_payload->>'bearish_pct' as bear,
       event_payload->>'social_fetched_at' as fetched
FROM event_store
WHERE event_type = 'SocialDataSnapshot'
  AND event_time >= CURRENT_DATE
ORDER BY event_time DESC LIMIT 10;
"
```
**Expect**:
- [ ] Mention counts > 0 for popular tickers
- [ ] Bullish/bearish percentages that aren't all 0.50/0.50
- [ ] `social_fetched_at` timestamps present

### 10. Sentiment baseline building (intraday)
```bash
grep "SentimentBaseline\|baseline" logs/monitor_2026-04-15.log | tail -5
```
**Expect**:
- [ ] Initial load shows 0 tickers (first day)
- [ ] No errors from baseline engine

### 11. Options engine — POP_SIGNAL integration
```bash
grep "OptionsEngine.*POP_SIGNAL\|OPTIONS_SIGNAL.*pop_signal" logs/monitor_2026-04-15.log | head -10
```
**Expect**:
- [ ] If any pop candidates are found, options engine should log `POP_SIGNAL → TICKER strategy_type`
- [ ] If no pop candidates today, this will be empty (that's OK)

### 12. Intraday reconciliation
```bash
grep "Intraday reconciliation" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] Runs at :30 of each hour (10:30, 11:30, 12:30, etc.)
- [ ] `Intraday reconciliation OK: N positions match`
- [ ] If MISMATCH appears → investigate immediately

### 13. Hourly summary — equity P&L included
```bash
grep "Hourly summary" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] Shows both trade P&L and Equity PnL (Alpaca)
- [ ] Format: `Hourly summary: N trades | WW/LL | PnL: $X.XX | Equity PnL: $Y.YY (Alpaca)`

### 14. Kill switch — immediate on FILL
```bash
grep "KillSwitchGuard\|KILL SWITCH" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] No kill switch activation (hopefully!)
- [ ] If it fires, should show `KILL SWITCH (immediate)` not just heartbeat check

---

## After Market Close (4:00 PM ET)

### 15. EOD P&L reconciliation
```bash
grep -A 20 "P&L RECONCILIATION" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] `Ending Equity` populated
- [ ] `Prev Close Equity` matches yesterday's close ($1,005,527.43)
- [ ] `Trade-log PnL` — sum of closed trades
- [ ] `Alpaca Fees/Reg` — regulatory fees (may be $0 on paper)
- [ ] `Fee-Adjusted PnL` = trades - fees
- [ ] `Equity-Based PnL` = ending - prev close (Alpaca ground truth)
- [ ] `Discrepancy` — ideally within $1.00
- [ ] If discrepancy > $1: `P&L DISCREPANCY` warning with causes listed

### 16. Orphaned trade audit
```bash
grep -A 15 "ORPHANED.*TRADE AUDIT" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] `Alpaca filled orders today: N`
- [ ] `Monitor trade_log entries: M`
- [ ] `No orphaned or missed trades` (ideal)
- [ ] If MISSED/PHANTOM trades found → note for investigation

### 17. Pop screener summary — did it work?
```bash
echo "=== Pop activity ==="
grep -c "POP CANDIDATE" logs/monitor_2026-04-15.log
echo "=== Pop signals ==="
grep -c "POP_SIGNAL" logs/monitor_2026-04-15.log
echo "=== Pop errors ==="
grep -c "permanently failed.*Pop" logs/monitor_2026-04-15.log
echo "=== Pop skips (early exit) ==="
grep -c "POP skip" logs/monitor_2026-04-15.log
echo "=== Benzinga fetches ==="
grep -c "POP data" logs/monitor_2026-04-15.log
```
**Expect**:
- [ ] Pop errors = 0 (CRITICAL — if > 0, the fix didn't work)
- [ ] Pop skips > 0 (normal — most bars are quiet)
- [ ] Benzinga fetches > 0 (some tickers passed early exit and fetched data)
- [ ] Pop candidates >= 0 (may be 0 on a quiet day, but should fire on volatile days)

### 18. Sector concentration blocks
```bash
grep "sector.*already has\|BLOCKED.*sector" logs/monitor_2026-04-15.log | head -10
```
**Expect**:
- [ ] Sector blocks firing when 2+ positions in same sector

### 19. DB event counts
```bash
psql -d tradinghub -c "
SELECT event_type, COUNT(*) as cnt
FROM event_store
WHERE event_time >= CURRENT_DATE
GROUP BY event_type
ORDER BY cnt DESC;
"
```
**Expect**:
- [ ] `NewsDataSnapshot` — 50-200+ (depends on how many tickers passed early exit)
- [ ] `SocialDataSnapshot` — similar count
- [ ] `BarReceived` — thousands (every ticker every ~10s)
- [ ] Other event types as usual

### 20. Baseline data for tomorrow
```bash
psql -d tradinghub -c "
SELECT event_payload->>'ticker' as ticker,
       COUNT(*) as snapshots,
       AVG((event_payload->>'headlines_1h')::float) as avg_hl,
       AVG((event_payload->>'mention_velocity')::float) as avg_vel
FROM event_store
WHERE event_type IN ('NewsDataSnapshot', 'SocialDataSnapshot')
  AND event_time >= CURRENT_DATE
GROUP BY event_payload->>'ticker'
ORDER BY snapshots DESC
LIMIT 20;
"
```
**Expect**:
- [ ] Multiple tickers with 5+ snapshots each (enough for baselines tomorrow)
- [ ] Avg headline and velocity values that look reasonable
- [ ] Tomorrow's session should log: `SentimentBaseline ready | headline_tickers=N | from_db=True`

---

## Things to Watch For (Red Flags)

| Red Flag | What it Means | Action |
|---|---|---|
| `permanently failed.*rvol` | RVOL bug not fixed | Check `_bar_payload_to_slice` — is `payload.rvol_df` populated? |
| All `rvol=1.0` in POP logs | `rvol_df` empty or not passed | Check Tradier `fetch_batch_bars` returns rvol_cache |
| All `gap=0.0` in POP logs | Prior close not extracted | Check `rvol_df['close'].iloc[-1]` |
| `NewsDataSnapshot` count = 0 | DB persistence not working | Check EventSourcingSubscriber registered NEWS_DATA |
| `SocialDataSnapshot` count = 0 | Same | Check SOCIAL_DATA registration |
| `[StockTwits] Rate limit` | Hitting 200/hr limit | Normal if many tickers pass early exit — rate limiter handles it |
| RVOL block on QQQ/SPY | ETF threshold not applied | Check `is_etf()` returns True for the ticker |
| Pop candidates but no trades | Executor risk gate blocking | Check cooldown, max positions, sector limits |
| P&L discrepancy > $5 | Missed trades or fees | Review orphaned trade audit section |
| `KILL SWITCH (immediate)` | Daily loss limit breached | Review recent trades for pattern |

---

## Success Criteria

**Minimum bar for "pop screener is working":**
- [ ] Zero `permanently failed` errors for PopStrategyEngine
- [ ] RVOL values in logs are NOT all 1.0
- [ ] Gap values are NOT all 0.0
- [ ] At least 10 `POP data` messages (Benzinga/StockTwits fetched)
- [ ] At least 1 `NewsDataSnapshot` and `SocialDataSnapshot` in event_store

**Bonus (may not happen on a quiet day):**
- [ ] At least 1 `POP CANDIDATE` found
- [ ] At least 1 `POP_SIGNAL` emitted
- [ ] Options engine logs `POP_SIGNAL →` for a pop candidate
- [ ] ETF (QQQ/SPY) passes RVOL threshold somewhere in the pipeline

---

## Changes Deployed in This Session

| Change | Files | What |
|---|---|---|
| Pop RVOL fix | `pop_strategy_engine.py` | Real RVOL from `payload.rvol_df` (was always 1.0) |
| Pop gap_size fix | `pop_strategy_engine.py` | Real gap from prior close (was always 0.0) |
| Pop observability | `pop_strategy_engine.py` | Debug logging at 6 decision points |
| Benzinga/StockTwits persistence | `events.py`, `event_bus.py`, `event_sourcing_subscriber.py` | NEWS_DATA + SOCIAL_DATA events to DB |
| News/social timestamps | `events.py`, `pop_strategy_engine.py`, `stocktwits_social.py`, `models.py` | Full timing chain for latency analysis |
| Sentiment baselines | `pop_screener/sentiment_baseline.py` (NEW) | Per-ticker baselines from DB history |
| ETF RVOL thresholds | `sector_map.py`, `risk_engine.py`, `selector.py`, `screener.py`, `vwap_reclaim_engine.py`, `pop_strategy_engine.py`, `options/engine.py` | Lower thresholds for 21 ETFs |
| Options ↔ Pop integration | `options/engine.py`, `events.py` | POP_SIGNAL → options entries |
