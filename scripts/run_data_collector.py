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
    db_cleanup = None
    try:
        from scripts._db_helper import init_satellite_db
        db_cleanup = init_satellite_db('data_collector', bus=None)
        log.info("DB persistence initialized")
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

        # ── Finviz Short Float & Screener ─────────────────────────────
        try:
            finviz = collector._sources.get('finviz')
            if finviz:
                screener_data = {}
                for ticker in tickers[:20]:
                    try:
                        sd = finviz.screener_data(ticker)
                        if sd:
                            if hasattr(sd, '__dataclass_fields__'):
                                from dataclasses import asdict
                                screener_data[ticker] = asdict(sd)
                            elif hasattr(sd, '__dict__'):
                                screener_data[ticker] = sd.__dict__
                    except Exception:
                        pass
                if screener_data:
                    cache_data['finviz_screener'] = screener_data
                    log.info("[Collector] Finviz screener for %d tickers", len(screener_data))
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

        # ── Polygon Previous Day ──────────────────────────────────────
        try:
            polygon = collector._sources.get('polygon')
            if polygon:
                prev_data = {}
                for ticker in tickers[:20]:
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
                if prev_data:
                    cache_data['polygon_prev'] = prev_data
                    log.info("[Collector] Polygon prev-day for %d tickers", len(prev_data))
        except Exception as exc:
            log.debug("[Collector] Polygon failed: %s", exc)

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
        """Write data source snapshot to event_store."""
        try:
            from db.writer import get_writer
            writer = get_writer()
            if writer:
                import uuid
                row = {
                    'event_id': str(uuid.uuid4()),
                    'event_sequence': 0,
                    'event_type': 'DataSourceCollection',
                    'event_version': 1,
                    'event_time': datetime.now(ET).isoformat(),
                    'received_time': datetime.now(ET).isoformat(),
                    'processed_time': datetime.now(ET).isoformat(),
                    'aggregate_id': 'data_collector',
                    'aggregate_type': 'DataSource',
                    'aggregate_version': 1,
                    'event_payload': json.dumps({
                        'sources_collected': [k for k in cache_data
                                             if k not in ('collected_at', '_version',
                                                          '_timestamp', '_date')],
                        'fear_greed': cache_data.get('fear_greed', {}),
                        'macro_regime': cache_data.get('macro_regime', {}),
                        'discovered_count': len(cache_data.get('discovered_tickers', [])),
                        'earnings_count': len(cache_data.get('earnings', {})),
                        'finviz_count': len(cache_data.get('finviz_screener', {})),
                    }, default=str),
                    'source_system': 'data_collector',
                    'session_id': None,
                }
                writer.enqueue('event_store', row)
                log.debug("[DB] DataSourceCollection event persisted")
        except Exception as exc:
            log.debug("[DB] Persistence failed (non-fatal): %s", exc)

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

                # Initial discovery
                earnings_tickers = _discover_from_yahoo_earnings()
                discovered_tickers.update(earnings_tickers)
                polygon_tickers = _discover_from_polygon_movers()
                discovered_tickers.update(polygon_tickers)

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
                log.info("Discovery refresh")
                new_tickers = set()
                new_tickers.update(_discover_from_yahoo_earnings())
                new_tickers.update(_discover_from_polygon_movers())

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
