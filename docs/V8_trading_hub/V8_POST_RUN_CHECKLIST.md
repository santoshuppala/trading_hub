# V8 Post-Run Checklist

Run these checks after the first V8 trading session (2026-04-17).

## Priority 1: Verify Core Functionality

### 1. Pro signals firing in Core process
```bash
grep "PRO_SIGNAL" logs/$(date +%Y%m%d)/core.log | head -10
# Expected: PRO_SIGNAL entries at INFO level for various tickers
```

### 2. No duplicate processes
```bash
ps aux | grep "run_pro\|run_pop" | grep -v grep
# Expected: EMPTY (Pro/Pop should NOT be running as separate processes)
```

### 3. Single bot_state.json tracks all positions
```bash
python3 -c "import json; d=json.load(open('bot_state.json')); print(f'Positions: {len(d.get(\"positions\",{}))}'); [print(f'  {t}: strategy={p.get(\"strategy\")}') for t,p in d.get('positions',{}).items()]"
# Expected: Mix of 'vwap_reclaim' and 'pro:...' strategies in same file
```

### 4. Stale orders cancelled on startup
```bash
grep "Cancelled stale" logs/$(date +%Y%m%d)/core.log
# Expected: Log entries showing stale order cancellation
```

### 5. Kill switch active
```bash
grep "PerStrategyKillSwitch" logs/$(date +%Y%m%d)/core.log
# Expected: "PerStrategyKillSwitch active | vwap=$-10000 pro=$-2000"
```

## Priority 2: Verify Bug Fixes

### 6. RVOL not double-counted
```bash
grep "rvol=" logs/$(date +%Y%m%d)/core.log | grep "SIGNAL\|PRO_SIGNAL" | head -5
# Check: same ticker should show same rvol for VWAP and Pro signals
```

### 7. Unrealized P&L tracked (not always $0)
```bash
grep "PortfolioRisk" logs/$(date +%Y%m%d)/core.log | grep "pnl\|drawdown" | tail -5
# Expected: Non-zero unrealized P&L values during trading hours
```

### 8. No phantom sell storm
```bash
grep "phantom" logs/$(date +%Y%m%d)/core.log
# Expected: Either no entries (good) or "Removing phantom" (cleanup working)
```

### 9. Heartbeat stable (no HUNG detection)
```bash
grep "HUNG" logs/$(date +%Y%m%d)/supervisor.log
# Expected: No HUNG entries (background heartbeat should prevent false positives)
```

### 10. Alert delivery working
```bash
grep "Alert sent\|Alert delivery" logs/$(date +%Y%m%d)/core.log | tail -5
# Expected: "Alert sent:" entries. If "permanently disabled" appears, check SMTP config.
```

## Priority 3: Verify New Features

### 11. SentimentDetector firing
```bash
grep "sentiment\|SentimentDetector" logs/$(date +%Y%m%d)/core.log | head -5
# Expected: Detector output in PRO_SIGNAL entries
```

### 12. Tier-based EOD behavior
```bash
grep "eod_close\|partial_sell\|_get_pro_tier" logs/$(date +%Y%m%d)/core.log | tail -10
# Check: T1 positions should full-close, T2/T3 should partial-sell
```

### 13. Trailing stops logging R levels
```bash
grep "\[Trail\]" logs/$(date +%Y%m%d)/core.log
# Expected: Trail entries with tier, R level, old/new stop prices
```

### 14. Pop scanner adding tickers
```bash
grep "PopScan" logs/$(date +%Y%m%d)/core.log
# Expected: "Added N momentum tickers" or "Breaking news: TICKER"
```

### 15. Discovery consumer working
```bash
grep "Discovery.*Added" logs/$(date +%Y%m%d)/core.log
# Expected: Discovered tickers added to scan universe
```

## Priority 4: Verify Data Persistence

### 16. All 8 sources in data_source_snapshots table
```sql
SELECT source_name, COUNT(*), MAX(ts) as latest
FROM trading.data_source_snapshots
WHERE ts::date = CURRENT_DATE
GROUP BY source_name
ORDER BY source_name;
-- Expected: 8 rows (fear_greed, fred_macro, finviz, sec_edgar, yahoo_earnings, polygon, unusual_options_flow, alpha_vantage)
```

### 17. Pro trades recorded with correct strategy
```sql
SELECT event_payload->>'strategy' AS strategy, COUNT(*)
FROM trading.event_store
WHERE event_type = 'PositionOpened' AND event_time::date = CURRENT_DATE
GROUP BY 1;
-- Expected: Mix of 'vwap_reclaim' and 'pro:...' strategies
```

### 18. EOD summary emailed
```bash
grep "EOD SUMMARY\|Alert sent.*EOD" logs/$(date +%Y%m%d)/core.log
# Expected: EOD summary logged AND emailed
```

## Priority 5: Performance Monitoring

### 19. Handler latency
```bash
grep "slow-handler" logs/$(date +%Y%m%d)/core.log | wc -l
# Compare to V7 (should be similar or lower with precomputed indicators)
```

### 20. Data coverage
```bash
grep "DataQuality" logs/$(date +%Y%m%d)/core.log
# Expected: No "DataQuality" warnings (>90% coverage)
```

### 21. Buying power check timing
```bash
grep "PortfolioRisk.*BLOCKED\|buying power" logs/$(date +%Y%m%d)/core.log | tail -5
# Check: No excessive blocking from buying power checks (cache should prevent)
```

## Residual Uncertainties (Monitor Weekly)

- **EventBus handler exception propagation** — search for `handler.*exception` on ORDER_REQ events
- **RVOL dedup timestamp precision** — compare RVOL values for same ticker in VWAP vs Pro signals
- **Trailing stop R-level calibration** — collect [Trail] log data for 2-3 weeks, analyze optimal thresholds
- **SentimentDetector weight calibration** — after 1 month, correlate composite score with trade P&L
- **Quick filter false negatives** — run full analysis on ALL tickers for 1 week, compare to filtered signals
