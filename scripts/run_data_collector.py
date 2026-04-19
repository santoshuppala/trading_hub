#!/usr/bin/env python3
"""
V7.1 Data Collector Satellite — Alternative Data Sources.

Collects data from 6 alternative sources and shares discoveries with
all engines via:
  1. data/alt_data_cache.json (SafeStateFile) — market regime, earnings, insider
  2. Redpanda th-discovery topic — newly discovered tickers for Pop/Pro
  3. TimescaleDB event_store — DataSourceSnapshot events for audit

Sources collected:
  - Fear & Greed Index (market sentiment 0-100)
  - FRED Macro (VIX, yield curve, fed rate → market regime)
  - Finviz (short float, insider activity, top movers)
  - SEC EDGAR (recent filings, insider trades)
  - Polygon (previous day movers)
  - Yahoo Finance (earnings calendar, analyst consensus, beta)

Ticker Discovery:
  - Finviz top gainers/losers/most active → new tickers for Pop screener
  - Polygon prev-day big movers → gap candidates for next open
  - SEC 8-K filings → material event tickers
  - Yahoo earnings this week → options candidates

Schedule:
  - Session start (pre-market): full collection + discovery
  - Every 10 min: Fear & Greed + Finviz screener + discovery refresh
  - Session end: final snapshot + insider activity + analyst consensus

Auto-discovered by supervisor via V7 run_*.py pattern.
"""
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# Project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ET = ZoneInfo('America/New_York')
log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'logs', datetime.now().strftime('%Y%m%d'))
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s [data_collector] %(message)s',
    handlers=[logging.FileHandler(os.path.join(log_dir, 'data_collector.log'))],
)
log = logging.getLogger(__name__)

# Graceful shutdown
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))


def main():
    log.info("=" * 60)
    log.info("DATA COLLECTOR SATELLITE STARTING")
    log.info("=" * 60)

    from config import TICKERS, ALERT_EMAIL
    from lifecycle.safe_state import SafeStateFile
    from monitor.ipc import EventPublisher

    # ── IPC publisher for ticker discovery ─────────────────────────────
    publisher = EventPublisher(source_name='data_collector')
    TOPIC_DISCOVERY = 'th-discovery'

    # ── Shared alt-data cache (read by Core, Pop, Options) ─────────────
    ALT_CACHE_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'alt_data_cache.json')
    alt_cache = SafeStateFile(ALT_CACHE_PATH, max_age_seconds=900.0)  # 15 min

    # ── DB persistence ─────────────────────────────────────────────────
    # Data Collector has no EventBus — it writes snapshots directly via
    # DataSourceCollector's own DB calls.  init_satellite_db requires a bus
    # for EventSourcingSubscriber, so we just init the pool + writer directly.
    db_cleanup = None
    try:
        from config import DB_ENABLED, DATABASE_URL
        if DB_ENABLED:
            from db import init_db, close_db
            from db.writer import init_writer
            import asyncio, threading
            _db_loop = asyncio.new_event_loop()
            threading.Thread(target=_db_loop.run_forever, name="db-data_collector", daemon=True).start()
            asyncio.run_coroutine_threadsafe(init_db(DATABASE_URL), _db_loop).result(timeout=15)
            _db_writer = init_writer(_db_loop)
            asyncio.run_coroutine_threadsafe(_db_writer.start(), _db_loop).result(timeout=5)
            log.info("DB persistence initialized (pool + writer, no event subscriber)")
            def _db_cleanup():
                try:
                    asyncio.run_coroutine_threadsafe(_db_writer.stop(), _db_loop).result(timeout=5)
                    asyncio.run_coroutine_threadsafe(close_db(), _db_loop).result(timeout=5)
                    _db_loop.call_soon_threadsafe(_db_loop.stop)
                except Exception as exc:
                    log.warning("DB cleanup error: %s", exc)
            db_cleanup = _db_cleanup
    except Exception as exc:
        log.warning("DB init failed (non-fatal): %s", exc)

    # ── Initialize DataSourceCollector ─────────────────────────────────
    from data_sources.collector import DataSourceCollector
    collector = DataSourceCollector()
    log.info("DataSourceCollector initialized (sources: %s)",
             list(collector._sources.keys()))

    # ── Initialize unusual options flow scanner ──────────────────────────
    uof_scanner = None
    try:
        options_key = os.getenv('APCA_OPTIONS_KEY', '')
        options_secret = os.getenv('APCA_OPTIONS_SECRET', '')
        if options_key and options_secret:
            from options.chain import AlpacaOptionChainClient
            chain_client = AlpacaOptionChainClient(
                api_key=options_key, api_secret=options_secret)
            from pop_screener.unusual_options_flow import UnusualOptionsFlowScanner
            uof_scanner = UnusualOptionsFlowScanner(
                chain_client=chain_client, volume_oi_threshold=3.0)
            log.info("Unusual options flow scanner initialized")
    except Exception as exc:
        log.debug("Unusual options flow scanner unavailable: %s", exc)

    # ── Initialize ticker discovery sources ────────────────────────────
    discovered_tickers = set()

    def _discover_from_news():
        """V8: Discover tickers from MARKET-WIDE Benzinga headlines.
        1 API call → extracts all mentioned tickers from recent headlines.
        Best used during pre-market to find overnight movers."""
        tickers = set()
        try:
            if hasattr(_collect_and_cache, '_api_state') and _collect_and_cache._api_state.get('benz'):
                benz = _collect_and_cache._api_state['benz']
                trending = benz.discover_trending_tickers(hours=4.0, min_mentions=2)
                for ticker, info in trending.items():
                    if ticker not in TICKERS and len(ticker) <= 5 and ticker.isalpha():
                        tickers.add(ticker)
                if tickers:
                    log.info("[Discovery] Benzinga news: %d tickers mentioned in headlines: %s",
                             len(tickers), sorted(tickers)[:10])
        except Exception as exc:
            log.debug("[Discovery] Benzinga news discovery failed: %s", exc)
        return tickers

    def _discover_from_social_trending():
        """V8: Discover tickers from StockTwits trending. 1 API call → top 30."""
        tickers = set()
        try:
            if hasattr(_collect_and_cache, '_api_state') and _collect_and_cache._api_state.get('st'):
                st = _collect_and_cache._api_state['st']
                trending = st.discover_trending()
                for ticker in trending:
                    if ticker not in TICKERS and len(ticker) <= 5 and ticker.isalpha():
                        tickers.add(ticker)
                if tickers:
                    log.info("[Discovery] StockTwits trending: %d new tickers: %s",
                             len(tickers), sorted(tickers)[:10])
        except Exception as exc:
            log.debug("[Discovery] StockTwits trending failed: %s", exc)
        return tickers

    def _discover_from_finviz_intraday():
        """V8: Scrape Finviz for intraday movers — top gainers, unusual volume.
        1 page scrape, polite. Returns tickers with strong intraday moves."""
        tickers = set()
        try:
            finviz = collector._sources.get('finviz')
            if finviz and hasattr(finviz, 'top_gainers'):
                gainers = finviz.top_gainers(limit=10)
                if gainers:
                    for g in gainers:
                        sym = g.get('ticker', g.get('symbol', ''))
                        if sym and len(sym) <= 5 and sym.isalpha():
                            tickers.add(sym.upper())
                    log.info("[Discovery] Finviz gainers: %d tickers", len(tickers))
            elif finviz and hasattr(finviz, 'screener_scan'):
                # Fallback: use screener with volume filter
                results = finviz.screener_scan(
                    signal='unusual_volume', limit=10)
                if results:
                    for r in results:
                        sym = r.get('ticker', r.get('symbol', ''))
                        if sym and len(sym) <= 5 and sym.isalpha():
                            tickers.add(sym.upper())
                    log.info("[Discovery] Finviz unusual vol: %d tickers", len(tickers))
        except Exception as exc:
            log.debug("[Discovery] Finviz intraday failed: %s", exc)
        return tickers

    def _discover_from_yahoo_intraday():
        """V8: Check Yahoo for intraday upgrades/downgrades/earnings surprises.
        Free, no rate limit. Returns tickers with significant analyst actions."""
        tickers = set()
        try:
            yahoo = collector._sources.get('yahoo')
            if yahoo:
                # Check for upgrades/downgrades today
                if hasattr(yahoo, 'analyst_actions'):
                    actions = yahoo.analyst_actions()
                    if actions:
                        for a in actions[:15]:
                            sym = a.get('ticker', a.get('symbol', ''))
                            if sym and len(sym) <= 5 and sym.isalpha():
                                tickers.add(sym.upper())
                        log.info("[Discovery] Yahoo analyst actions: %d tickers", len(tickers))

                # Also re-check earnings (companies report intraday)
                if hasattr(yahoo, 'earnings_calendar'):
                    calendar = yahoo.earnings_calendar()
                    if calendar:
                        for entry in calendar[:10]:
                            sym = entry.get('ticker', entry.get('symbol', ''))
                            if sym and len(sym) <= 5 and sym.isalpha():
                                tickers.add(sym.upper())
        except Exception as exc:
            log.debug("[Discovery] Yahoo intraday failed: %s", exc)
        return tickers

    def _discover_from_polygon_intraday():
        """V8: Check Polygon for intraday volume spikes.
        Uses snapshot endpoint (1 call). Returns tickers with unusual intraday activity."""
        tickers = set()
        try:
            polygon = collector._sources.get('polygon')
            if polygon and hasattr(polygon, 'gainers_losers'):
                movers = polygon.gainers_losers()
                if movers:
                    for m in movers[:15]:
                        sym = m.get('ticker', m.get('symbol', ''))
                        change = abs(float(m.get('change_pct', m.get('todaysChangePerc', 0))))
                        if sym and change > 3.0 and len(sym) <= 5:
                            tickers.add(sym.upper())
                    log.info("[Discovery] Polygon intraday movers: %d tickers (>3%%)", len(tickers))
        except Exception as exc:
            log.debug("[Discovery] Polygon intraday failed: %s", exc)
        return tickers

    def _discover_from_polygon_movers():
        """Discover tickers with big previous-day moves NOT in watchlist."""
        polygon = collector._sources.get('polygon')
        if not polygon:
            return set()
        tickers = set()
        try:
            # Check a broad set of common high-volume tickers
            scan_candidates = [
                'CRWV', 'SMCI', 'IONQ', 'RGTI', 'QUBT', 'LUNR', 'RKLB',
                'APP', 'ALAB', 'VRT', 'OKLO', 'SMR', 'NNE', 'VST', 'CEG',
                'APLD', 'BTDR', 'CIFR', 'HIMS', 'RDDT', 'CART', 'TOST',
                'BIRK', 'CAVA', 'BROS', 'LMND', 'ROOT', 'MNSO', 'ONON',
            ]
            for ticker in scan_candidates:
                if ticker in TICKERS:
                    continue
                try:
                    prev = polygon.previous_close(ticker)
                    if prev and abs(prev.change_pct) > 3.0 and prev.volume > 500000:
                        tickers.add(ticker)
                except Exception:
                    pass
            if tickers:
                log.info("[Discovery] Polygon movers: %d tickers with >3%% move: %s",
                         len(tickers), sorted(tickers)[:10])
        except Exception as exc:
            log.debug("[Discovery] Polygon discovery failed: %s", exc)
        return tickers

    def _discover_from_yahoo_earnings():
        """Discover tickers with upcoming earnings."""
        source = collector._sources.get('yahoo')
        if not source:
            return set()
        tickers = set()
        try:
            calendar = source.earnings_calendar()
            if calendar:
                for entry in calendar[:30]:
                    ticker = entry.get('ticker') or entry.get('symbol', '')
                    if ticker and len(ticker) <= 5 and ticker.isalpha():
                        tickers.add(ticker.upper())
                log.info("[Discovery] Yahoo earnings calendar: %d tickers this week",
                         len(tickers))
        except Exception as exc:
            log.debug("[Discovery] Yahoo earnings failed: %s", exc)
        return tickers

    def _collect_and_cache(tickers):
        """Run full collection cycle and write to shared cache."""
        cache_data = {'collected_at': datetime.now(ET).isoformat()}

        # ── Fear & Greed ──────────────────────────────────────────────
        try:
            fg_source = collector._sources.get('fear_greed')
            if fg_source:
                fg = fg_source.get_current()
                if fg:
                    fg_dict = {}
                    if hasattr(fg, '__dataclass_fields__'):
                        from dataclasses import asdict
                        fg_dict = asdict(fg)
                    elif hasattr(fg, '__dict__'):
                        fg_dict = fg.__dict__
                    elif isinstance(fg, dict):
                        fg_dict = fg
                    cache_data['fear_greed'] = fg_dict
                    log.info("[Collector] Fear & Greed: %s",
                             fg_dict.get('value', fg_dict.get('score', '?')))
        except Exception as exc:
            log.debug("[Collector] Fear & Greed failed: %s", exc)

        # ── FRED Macro Regime ─────────────────────────────────────────
        try:
            fred_source = collector._sources.get('fred')
            if fred_source:
                regime = fred_source.macro_regime()
                if regime:
                    regime_dict = {}
                    if hasattr(regime, '__dataclass_fields__'):
                        from dataclasses import asdict
                        regime_dict = asdict(regime)
                    elif hasattr(regime, '__dict__'):
                        regime_dict = regime.__dict__
                    elif isinstance(regime, dict):
                        regime_dict = regime
                    cache_data['macro_regime'] = regime_dict
                    log.info("[Collector] FRED regime: %s VIX=%s",
                             regime_dict.get('regime', '?'),
                             regime_dict.get('vix', '?'))
        except Exception as exc:
            log.debug("[Collector] FRED failed: %s", exc)

        # ── Yahoo Earnings Calendar ───────────────────────────────────
        try:
            yahoo = collector._sources.get('yahoo')
            if yahoo:
                earnings = {}
                for ticker in tickers[:30]:
                    try:
                        info = yahoo.next_earnings(ticker)
                        if info:
                            if hasattr(info, '__dataclass_fields__'):
                                from dataclasses import asdict
                                earnings[ticker] = asdict(info)
                            elif hasattr(info, '__dict__'):
                                earnings[ticker] = info.__dict__
                    except Exception:
                        pass
                if earnings:
                    cache_data['earnings'] = earnings
                    log.info("[Collector] Earnings data for %d tickers", len(earnings))
        except Exception as exc:
            log.debug("[Collector] Yahoo earnings failed: %s", exc)

        # ── Finviz Short Float & Screener (web scraping — be polite) ───
        # V8: Rotate 5 tickers per cycle with 1s delay between requests.
        # Full coverage in 4 cycles (40 min). Avoids IP blocking.
        try:
            finviz = collector._sources.get('finviz')
            if finviz:
                if not hasattr(_collect_and_cache, '_finviz_cache'):
                    _collect_and_cache._finviz_cache = {}
                    _collect_and_cache._finviz_cycle = 0

                batch_start = (_collect_and_cache._finviz_cycle * 5) % max(len(tickers), 1)
                batch = tickers[batch_start:batch_start + 5]
                _collect_and_cache._finviz_cycle += 1

                for ticker in batch:
                    try:
                        sd = finviz.screener_data(ticker)
                        if sd:
                            if hasattr(sd, '__dataclass_fields__'):
                                from dataclasses import asdict
                                _collect_and_cache._finviz_cache[ticker] = asdict(sd)
                            elif hasattr(sd, '__dict__'):
                                _collect_and_cache._finviz_cache[ticker] = sd.__dict__
                        time.sleep(1)  # polite delay between scrapes
                    except Exception:
                        pass

                if _collect_and_cache._finviz_cache:
                    cache_data['finviz_screener'] = dict(_collect_and_cache._finviz_cache)
                    log.debug("[Collector] Finviz: batch of %d, total cache %d",
                              len(batch), len(_collect_and_cache._finviz_cache))
        except Exception as exc:
            log.debug("[Collector] Finviz failed: %s", exc)

        # ── SEC EDGAR Filing Counts ───────────────────────────────────
        try:
            edgar = collector._sources.get('edgar')
            if edgar:
                filing_data = {}
                for ticker in tickers[:15]:
                    try:
                        count = edgar.filing_count_recent(ticker, days=30)
                        if count and count > 0:
                            filing_data[ticker] = {
                                'count': count,
                                'days': 30,
                            }
                    except Exception:
                        pass
                if filing_data:
                    cache_data['sec_filings'] = filing_data
                    log.info("[Collector] SEC filings for %d tickers", len(filing_data))
        except Exception as exc:
            log.debug("[Collector] SEC EDGAR failed: %s", exc)

        # ── Polygon Previous Day (FREE: 5 calls/min) ─────────────────
        # V8: 5 tickers per cycle with cache. Full coverage in 4 cycles.
        try:
            polygon = collector._sources.get('polygon')
            if polygon:
                if not hasattr(_collect_and_cache, '_polygon_cache'):
                    _collect_and_cache._polygon_cache = {}
                    _collect_and_cache._polygon_cycle = 0

                batch_start = (_collect_and_cache._polygon_cycle * 5) % max(len(tickers), 1)
                batch = tickers[batch_start:batch_start + 5]
                _collect_and_cache._polygon_cycle += 1
                prev_data = dict(_collect_and_cache._polygon_cache)  # keep previous
                for ticker in batch:
                    try:
                        prev = polygon.previous_close(ticker)
                        if prev:
                            if hasattr(prev, '__dataclass_fields__'):
                                from dataclasses import asdict
                                prev_data[ticker] = asdict(prev)
                            elif hasattr(prev, '__dict__'):
                                prev_data[ticker] = prev.__dict__
                    except Exception:
                        pass
                _collect_and_cache._polygon_cache = prev_data
                if prev_data:
                    cache_data['polygon_prev'] = prev_data
                    log.debug("[Collector] Polygon: batch of %d, total cache %d",
                              len(batch), len(prev_data))
        except Exception as exc:
            log.debug("[Collector] Polygon failed: %s", exc)

        # ── Alpha Vantage (FREE: 25 calls/day) ────────────────────────
        # V8 AUDIT: No trading component reads Alpha Vantage data.
        # RiskSizer.get_beta() calls Yahoo directly, not alt_data_cache.
        # Disabled to save 25 API calls/day. Re-enable when a consumer exists.
        # If needed: av.company_overview(ticker) provides beta, PE, sector.
        pass

        # ── Unusual Options Flow ──────────────────────────────────────
        if uof_scanner:
            try:
                uof_results = uof_scanner.scan_universe(tickers, max_per_cycle=10)
                if uof_results:
                    cache_data['unusual_options_flow'] = {
                        r['ticker']: r for r in uof_results}
                    # Bullish unusual flow → add to discovery
                    for r in uof_results:
                        if r.get('signal') == 'bullish' and r.get('confidence', 0) > 0.5:
                            discovered_tickers.add(r['ticker'])
                    log.info("[Collector] Unusual options flow: %d tickers with activity",
                             len(uof_results))
            except Exception as exc:
                log.debug("[Collector] Unusual options flow failed: %s", exc)

        # ═══════════════════════════════════════════════════════════════
        # V8: News & Social — PRE-MARKET BURST + TRADING THROTTLE
        #
        # Pre-market (6:00-9:30 ET): BURST — use 60% of daily budget
        #   to scan max tickers. Picks up overnight news, earnings, movers.
        # Trading (9:30-4:00 ET): THROTTLE — use remaining 40% slowly.
        #
        # Benzinga:  50/day → 5/cycle pre-mkt (30 in 1hr) + 1/cycle trading (20)
        # StockTwits: 200/hr → 15/cycle pre-mkt + 5/cycle trading
        # ═══════════════════════════════════════════════════════════════

        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        _now_et = _dt.now(_ZI('America/New_York'))
        _is_premarket = _now_et.hour < 9 or (_now_et.hour == 9 and _now_et.minute < 30)

        # Persistent API state (survives across cycles)
        if not hasattr(_collect_and_cache, '_api_state'):
            _collect_and_cache._api_state = {
                'benz': None, 'benz_cache': {}, 'benz_calls': 0, 'benz_cycle': 0,
                'st': None, 'st_cache': {}, 'st_cycle': 0,
            }
            try:
                from pop_screener.benzinga_news import BenzingaNewsSentimentSource
                bk = os.getenv('BENZINGA_API_KEY', '')
                _collect_and_cache._api_state['benz'] = BenzingaNewsSentimentSource(api_key=bk) if bk else None
            except Exception:
                pass
            try:
                from pop_screener.stocktwits_social import StockTwitsSocialSource
                st = os.getenv('STOCKTWITS_TOKEN', '')
                _collect_and_cache._api_state['st'] = StockTwitsSocialSource(access_token=st or None)
            except Exception:
                pass

        _s = _collect_and_cache._api_state

        # ── Benzinga (50/day) ─────────────────────────────────────────
        benz = _s['benz']
        if benz and _s['benz_calls'] < 45:
            batch_size = 5 if (_is_premarket and _s['benz_calls'] < 30) else 1
            start = (_s['benz_cycle'] * batch_size) % len(tickers)
            batch = tickers[start:start + batch_size]
            _s['benz_cycle'] += 1

            for tk in batch:
                if _s['benz_calls'] >= 45:
                    break
                try:
                    news = benz.get_news(tk, window_hours=1)
                    _s['benz_calls'] += 1
                    if news:
                        avg = sum(n.sentiment_score for n in news) / len(news)
                        _s['benz_cache'][tk] = {
                            'headlines_1h': len(news), 'headlines_24h': 0,
                            'sentiment_delta': round(avg, 4),
                            'avg_sentiment_1h': round(avg, 4),
                            'avg_sentiment_24h': 0.0,
                            'headline_baseline': 2.0,
                        }
                except Exception:
                    pass

            phase = 'PRE-MARKET' if _is_premarket else 'TRADING'
            log.info("[Collector] Benzinga %s: batch=%d cached=%d calls=%d/50",
                     phase, len(batch), len(_s['benz_cache']), _s['benz_calls'])

        if _s.get('benz_cache'):
            cache_data['benzinga_news'] = dict(_s['benz_cache'])

        # ── StockTwits (200/hr) ───────────────────────────────────────
        st_inst = _s['st']
        if st_inst:
            batch_size = 15 if _is_premarket else 5
            start = (_s['st_cycle'] * batch_size) % len(tickers)
            batch = tickers[start:start + batch_size]
            _s['st_cycle'] += 1

            for tk in batch:
                try:
                    social = st_inst.get_social(tk, window_hours=1)
                    if social and social.mention_count > 0:
                        _s['st_cache'][tk] = {
                            'mention_count': social.mention_count,
                            'mention_velocity': round(social.mention_velocity, 4),
                            'bullish_pct': round(social.bullish_pct, 4),
                            'bearish_pct': round(social.bearish_pct, 4),
                            'baseline': 100.0,
                        }
                except Exception:
                    pass

            if _s['st_cache']:
                cache_data['stocktwits_social'] = dict(_s['st_cache'])

        # ── Discovered tickers ────────────────────────────────────────
        cache_data['discovered_tickers'] = list(discovered_tickers)

        # Write to shared cache
        alt_cache.write(cache_data)
        log.info("[Collector] Alt data cache updated (%d keys)",
                 len(cache_data))

        return cache_data

    def _publish_discoveries(new_tickers):
        """Publish newly discovered tickers to Redpanda for Pop/Pro to pick up."""
        if not new_tickers:
            return
        for ticker in new_tickers:
            publisher.publish(TOPIC_DISCOVERY, ticker, {
                'ticker': ticker,
                'source': 'data_collector',
                'discovered_at': datetime.now(ET).isoformat(),
            })
        publisher.flush(timeout=5.0)
        log.info("[Discovery] Published %d new tickers to %s: %s",
                 len(new_tickers), TOPIC_DISCOVERY,
                 sorted(new_tickers)[:10])

    def _persist_to_db(cache_data):
        """V8: Write ALL data source snapshots to data_source_snapshots table.

        Previously only wrote a summary to event_store (counts only).
        Now writes per-source per-ticker data so all 8 sources are queryable.
        """
        try:
            from db.writer import get_writer
            writer = get_writer()
            if not writer:
                return

            now_ts = datetime.now(ET)
            rows_written = 0

            # 1. Fear & Greed (market-wide, no ticker)
            fg = cache_data.get('fear_greed', {})
            if fg:
                writer.enqueue('data_source_snapshots', {
                    'ts': now_ts, 'source_name': 'fear_greed',
                    'ticker': None,
                    'data_payload': json.dumps(fg, default=str),
                    'ingested_at': now_ts,
                })
                rows_written += 1

            # 2. Macro Regime / FRED (market-wide)
            macro = cache_data.get('macro_regime', {})
            if macro:
                writer.enqueue('data_source_snapshots', {
                    'ts': now_ts, 'source_name': 'fred_macro',
                    'ticker': None,
                    'data_payload': json.dumps(macro, default=str),
                    'ingested_at': now_ts,
                })
                rows_written += 1

            # 3. Finviz screener (per-ticker)
            for ticker, data in cache_data.get('finviz_screener', {}).items():
                writer.enqueue('data_source_snapshots', {
                    'ts': now_ts, 'source_name': 'finviz',
                    'ticker': ticker,
                    'data_payload': json.dumps(data, default=str),
                    'ingested_at': now_ts,
                })
                rows_written += 1

            # 4. SEC EDGAR filings (per-ticker)
            for ticker, data in cache_data.get('sec_filings', {}).items():
                writer.enqueue('data_source_snapshots', {
                    'ts': now_ts, 'source_name': 'sec_edgar',
                    'ticker': ticker,
                    'data_payload': json.dumps(data, default=str),
                    'ingested_at': now_ts,
                })
                rows_written += 1

            # 5. Yahoo earnings calendar (per-ticker)
            for ticker, data in cache_data.get('earnings', {}).items():
                writer.enqueue('data_source_snapshots', {
                    'ts': now_ts, 'source_name': 'yahoo_earnings',
                    'ticker': ticker,
                    'data_payload': json.dumps(data, default=str),
                    'ingested_at': now_ts,
                })
                rows_written += 1

            # 6. Polygon prev-day movers (per-ticker)
            for ticker, data in cache_data.get('polygon_prev', {}).items():
                writer.enqueue('data_source_snapshots', {
                    'ts': now_ts, 'source_name': 'polygon',
                    'ticker': ticker,
                    'data_payload': json.dumps(data, default=str),
                    'ingested_at': now_ts,
                })
                rows_written += 1

            # 7. Unusual Options Flow (per-ticker, if collected)
            for ticker, data in cache_data.get('unusual_options_flow', {}).items():
                writer.enqueue('data_source_snapshots', {
                    'ts': now_ts, 'source_name': 'unusual_options_flow',
                    'ticker': ticker,
                    'data_payload': json.dumps(data, default=str),
                    'ingested_at': now_ts,
                })
                rows_written += 1

            # 8. Alpha Vantage technical data (per-ticker, if collected)
            for ticker, data in cache_data.get('alpha_vantage', {}).items():
                writer.enqueue('data_source_snapshots', {
                    'ts': now_ts, 'source_name': 'alpha_vantage',
                    'ticker': ticker,
                    'data_payload': json.dumps(data, default=str),
                    'ingested_at': now_ts,
                })
                rows_written += 1

            # 9. V8: Benzinga news sentiment (per-ticker)
            for ticker, data in cache_data.get('benzinga_news', {}).items():
                writer.enqueue('data_source_snapshots', {
                    'ts': now_ts, 'source_name': 'benzinga_news',
                    'ticker': ticker,
                    'data_payload': json.dumps(data, default=str),
                    'ingested_at': now_ts,
                })
                rows_written += 1

            # 10. V8: StockTwits social sentiment (per-ticker)
            for ticker, data in cache_data.get('stocktwits_social', {}).items():
                writer.enqueue('data_source_snapshots', {
                    'ts': now_ts, 'source_name': 'stocktwits_social',
                    'ticker': ticker,
                    'data_payload': json.dumps(data, default=str),
                    'ingested_at': now_ts,
                })
                rows_written += 1

            # Also write summary to event_store (backward compat)
            import uuid
            writer.enqueue('event_store', {
                'event_id': str(uuid.uuid4()),
                'event_sequence': 0,
                'event_type': 'DataSourceCollection',
                'event_version': 2,  # V8: version bump
                'event_time': now_ts,
                'received_time': now_ts,
                'processed_time': now_ts,
                'aggregate_id': 'data_collector',
                'aggregate_type': 'DataSource',
                'aggregate_version': 1,
                'event_payload': json.dumps({
                    'sources_collected': [k for k in cache_data
                                         if k not in ('collected_at', '_version',
                                                      '_timestamp', '_date')],
                    'rows_written': rows_written,
                    'fear_greed': cache_data.get('fear_greed', {}),
                    'macro_regime': cache_data.get('macro_regime', {}),
                }, default=str),
                'source_system': 'data_collector',
                'session_id': None,
            })

            log.info("[DB] Persisted %d data source snapshot rows", rows_written)
        except Exception as exc:
            log.warning("[DB] Data source persistence failed: %s", exc)

    # ══════════════════════════════════════════════════════════════════
    # MAIN LOOP
    # ══════════════════════════════════════════════════════════════════

    COLLECT_INTERVAL = 600   # 10 minutes
    DISCOVERY_INTERVAL = 1800  # 30 minutes
    last_collect = 0
    last_discovery = 0
    session_started = False

    log.info("Data collector running. Interval=%ds, Discovery=%ds",
             COLLECT_INTERVAL, DISCOVERY_INTERVAL)

    try:
        while True:
            now = datetime.now(ET)
            now_mono = time.monotonic()

            # EOD shutdown
            if now.hour >= 16:
                log.info("4:00 PM ET — stopping data collector.")
                break

            # ── Session start (once) ──────────────────────────────────
            if not session_started and now.hour >= 8:
                log.info("Session start collection (pre-market)")
                try:
                    collector.collect_session_start(TICKERS, max_tickers=30)
                except Exception as exc:
                    log.warning("Session start collection failed: %s", exc)

                # V8: Pre-market discovery burst — find new tickers from ALL sources
                # These 2 calls use market-wide endpoints (1 API call each)
                # to discover tickers we're NOT tracking but SHOULD be
                news_tickers = _discover_from_news()          # 1 Benzinga call
                social_tickers = _discover_from_social_trending()  # 1 StockTwits call
                discovered_tickers.update(news_tickers)
                discovered_tickers.update(social_tickers)

                # Existing discovery sources
                earnings_tickers = _discover_from_yahoo_earnings()
                discovered_tickers.update(earnings_tickers)
                polygon_tickers = _discover_from_polygon_movers()
                discovered_tickers.update(polygon_tickers)

                log.info("[Discovery] PRE-MARKET total: %d new tickers "
                         "(news=%d social=%d earnings=%d polygon=%d)",
                         len(discovered_tickers), len(news_tickers),
                         len(social_tickers), len(earnings_tickers),
                         len(polygon_tickers))

                # Full collection
                cache_data = _collect_and_cache(TICKERS)
                _persist_to_db(cache_data)

                if discovered_tickers:
                    new = discovered_tickers - set(TICKERS)
                    if new:
                        _publish_discoveries(new)

                session_started = True
                last_collect = now_mono
                last_discovery = now_mono

            # ── Periodic collection (every 10 min) ────────────────────
            if now_mono - last_collect >= COLLECT_INTERVAL:
                log.info("Periodic collection cycle")
                try:
                    cache_data = _collect_and_cache(TICKERS)
                    _persist_to_db(cache_data)
                except Exception as exc:
                    log.warning("Periodic collection failed: %s", exc)
                last_collect = now_mono

            # ── Discovery refresh (every 30 min) ──────────────────────
            if now_mono - last_discovery >= DISCOVERY_INTERVAL:
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo as _ZI
                _now_et = _dt.now(_ZI('America/New_York'))
                _is_trading = 9 <= _now_et.hour < 16

                log.info("Discovery refresh (%s)",
                         'TRADING' if _is_trading else 'PRE-MARKET')
                new_tickers = set()

                # Always: news + social trending (1 API call each, cheap)
                new_tickers.update(_discover_from_news())
                new_tickers.update(_discover_from_social_trending())

                # Always: earnings + prev-day movers
                new_tickers.update(_discover_from_yahoo_earnings())
                new_tickers.update(_discover_from_polygon_movers())

                # V8: During trading hours — also scan for intraday movers
                # These are free/cheap and catch stocks moving NOW
                if _is_trading:
                    new_tickers.update(_discover_from_finviz_intraday())
                    new_tickers.update(_discover_from_yahoo_intraday())
                    new_tickers.update(_discover_from_polygon_intraday())

                truly_new = new_tickers - discovered_tickers - set(TICKERS)
                if truly_new:
                    discovered_tickers.update(truly_new)
                    _publish_discoveries(truly_new)
                    log.info("[Discovery] %d new tickers found: %s",
                             len(truly_new), sorted(truly_new)[:10])

                last_discovery = now_mono

            time.sleep(30)  # Check every 30 seconds

    except KeyboardInterrupt:
        log.info("Data collector interrupted.")
    finally:
        # Session end collection
        try:
            log.info("Session end collection")
            collector.collect_session_end(TICKERS, max_tickers=30)
            cache_data = _collect_and_cache(TICKERS)
            _persist_to_db(cache_data)
        except Exception:
            pass

        publisher.stop()
        if db_cleanup:
            try:
                db_cleanup()
            except Exception:
                pass

        log.info("Data collector stopped. Discovered %d tickers total.",
                 len(discovered_tickers))


if __name__ == '__main__':
    main()
