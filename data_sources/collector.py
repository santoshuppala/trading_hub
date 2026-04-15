"""
Data Source Collector — fetches all data sources and persists snapshots.

Called periodically from the trading session (e.g., every 10 minutes)
or at session start/end. Uses SmartPersistence to avoid DB flooding.

Usage:
    from data_sources.collector import DataSourceCollector
    collector = DataSourceCollector(bus=monitor._bus)
    collector.collect_session_start(tickers)  # pre-market
    collector.collect_periodic(tickers)       # every 10 min during trading
    collector.collect_session_end(tickers)    # EOD
"""
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

from data_sources.persistence import (
    SmartPersistence, CHANGE_DETECTORS,
)

log = logging.getLogger(__name__)


class DataSourceCollector:
    """Orchestrates data fetching + smart persistence for all sources."""

    def __init__(self, bus=None, db_writer_fn=None):
        """
        Args:
            bus: EventBus (optional — for emitting DataSourceSnapshot events)
            db_writer_fn: direct DB writer function, OR uses direct DB write if None
        """
        self._bus = bus

        # Build the writer function
        if db_writer_fn:
            writer = db_writer_fn
        else:
            writer = self._write_to_db

        self._persistence = SmartPersistence(writer_fn=writer)

        # Circuit breaker state
        self._source_failures: Dict[str, int] = {}  # {source_name: consecutive_failures}
        self._source_disabled_until: Dict[str, float] = {}  # {source_name: monotonic_time}

        # Lazy-init sources (avoid import errors if libraries missing)
        self._sources = {}
        self._init_sources()

    def _init_sources(self):
        """Initialize available data sources (non-fatal per source)."""
        # Yahoo Finance (no key)
        try:
            from data_sources.yahoo_finance import YahooFinanceSource
            self._sources['yahoo'] = YahooFinanceSource()
            log.info("[Collector] Yahoo Finance loaded")
        except Exception as exc:
            log.debug("[Collector] Yahoo Finance unavailable: %s", exc)

        # Fear & Greed (no key)
        try:
            from data_sources.fear_greed import FearGreedSource
            self._sources['fear_greed'] = FearGreedSource()
            log.info("[Collector] Fear & Greed loaded")
        except Exception as exc:
            log.debug("[Collector] Fear & Greed unavailable: %s", exc)

        # FRED (needs key)
        try:
            from data_sources.fred_macro import FREDMacroSource
            source = FREDMacroSource()
            if source._api_key:
                self._sources['fred'] = source
                log.info("[Collector] FRED loaded")
        except Exception as exc:
            log.debug("[Collector] FRED unavailable: %s", exc)

        # Alpha Vantage (needs key, 25/day)
        try:
            from data_sources.alpha_vantage import AlphaVantageSource
            source = AlphaVantageSource()
            if source._api_key:
                self._sources['alpha_vantage'] = source
                log.info("[Collector] Alpha Vantage loaded (%d req/day)", source._daily_limit)
        except Exception as exc:
            log.debug("[Collector] Alpha Vantage unavailable: %s", exc)

        # Finviz (no key)
        try:
            from data_sources.finviz_data import FinvizSource
            self._sources['finviz'] = FinvizSource()
            log.info("[Collector] Finviz loaded")
        except Exception as exc:
            log.debug("[Collector] Finviz unavailable: %s", exc)

        # SEC EDGAR (no key)
        try:
            from data_sources.sec_edgar import SECEdgarSource
            self._sources['edgar'] = SECEdgarSource()
            log.info("[Collector] SEC EDGAR loaded")
        except Exception as exc:
            log.debug("[Collector] SEC EDGAR unavailable: %s", exc)

        # Polygon (needs key)
        try:
            from data_sources.polygon_data import PolygonSource
            source = PolygonSource()
            if source._api_key:
                self._sources['polygon'] = source
                log.info("[Collector] Polygon loaded")
        except Exception as exc:
            log.debug("[Collector] Polygon unavailable: %s", exc)

    def _is_source_healthy(self, source_name: str) -> bool:
        """Check if a source is healthy (circuit breaker)."""
        disabled_until = self._source_disabled_until.get(source_name, 0)
        if time.monotonic() < disabled_until:
            return False
        if time.monotonic() >= disabled_until and disabled_until > 0:
            # Cooldown expired — re-enable
            self._source_failures[source_name] = 0
            self._source_disabled_until[source_name] = 0
        return True

    def _record_source_failure(self, source_name: str):
        count = self._source_failures.get(source_name, 0) + 1
        self._source_failures[source_name] = count
        if count >= 3:
            self._source_disabled_until[source_name] = time.monotonic() + 1800  # 30 min
            log.warning("[Collector] %s disabled for 30min after %d failures", source_name, count)

    def _record_source_success(self, source_name: str):
        self._source_failures[source_name] = 0

    def collect_session_start(self, tickers: list, max_tickers: int = 20):
        """Collect all data at session start. Runs once pre-market.

        Fetches market-wide data (Fear & Greed, FRED) for all,
        per-ticker data for top N tickers only (rate limits).
        """
        log.info("[Collector] Session start collection for %d tickers", len(tickers))

        # Market-wide sources
        self._collect_fear_greed()
        self._collect_fred()

        # Per-ticker (limited batch)
        batch = tickers[:max_tickers]
        for ticker in batch:
            self._collect_yahoo_earnings(ticker)
            self._collect_polygon_prev(ticker)
            self._collect_edgar_filings(ticker)

        stats = self._persistence.stats()
        log.info("[Collector] Session start done: persisted=%d skipped=%d",
                 stats['persisted'], stats['skipped'])

    def collect_periodic(self, tickers: list, max_tickers: int = 10):
        """Collect during trading session. Called every ~10 minutes.

        Only fetches fast, rate-limit-safe sources.
        """
        self._collect_fear_greed()

        # Rotate through tickers — don't fetch all every cycle
        batch = tickers[:max_tickers]
        for ticker in batch:
            self._collect_finviz_screener(ticker)

    def collect_session_end(self, tickers: list, max_tickers: int = 30):
        """Collect at session end. Final snapshot for the day."""
        log.info("[Collector] Session end collection")

        self._collect_fear_greed()
        self._collect_fred()

        batch = tickers[:max_tickers]
        for ticker in batch:
            self._collect_yahoo_analyst(ticker)
            self._collect_finviz_insider(ticker)

        stats = self._persistence.stats()
        log.info("[Collector] Session end done: total persisted=%d skipped=%d tracked=%d",
                 stats['persisted'], stats['skipped'], stats['tracked_keys'])

    # -- Per-source collectors -------------------------------------------------

    def _collect_fear_greed(self):
        if not self._is_source_healthy('fear_greed'):
            return
        source = self._sources.get('fear_greed')
        if not source:
            return
        try:
            data = source.get_current()
            if data:
                self._persistence.persist_if_changed(
                    'fear_greed', 'MARKET', data,
                    CHANGE_DETECTORS['fear_greed'],
                )
                self._record_source_success('fear_greed')
        except Exception as exc:
            self._record_source_failure('fear_greed')
            log.debug("[Collector] Fear & Greed failed: %s", exc)

    def _collect_fred(self):
        if not self._is_source_healthy('fred'):
            return
        source = self._sources.get('fred')
        if not source:
            return
        try:
            regime = source.macro_regime()
            data = asdict(regime) if hasattr(regime, '__dataclass_fields__') else {
                'regime': regime.regime, 'fed_rate': regime.fed_rate,
                'fed_trend': regime.fed_trend, 'yield_curve': regime.yield_curve,
                'yield_status': regime.yield_status, 'vix': regime.vix,
                'vix_regime': regime.vix_regime, 'updated_at': regime.updated_at,
            }
            self._persistence.persist_if_changed(
                'fred_macro', 'MARKET', data,
                CHANGE_DETECTORS['fred_macro'],
            )
            self._record_source_success('fred')
        except Exception as exc:
            self._record_source_failure('fred')
            log.debug("[Collector] FRED failed: %s", exc)

    def _collect_yahoo_earnings(self, ticker: str):
        if not self._is_source_healthy('yahoo_earnings'):
            return
        source = self._sources.get('yahoo')
        if not source:
            return
        try:
            info = source.next_earnings(ticker)
            if info:
                data = asdict(info) if hasattr(info, '__dataclass_fields__') else {
                    'ticker': info.ticker,
                    'next_earnings_date': info.next_earnings_date,
                    'days_until_earnings': info.days_until_earnings,
                }
                self._persistence.persist_if_changed(
                    'yahoo_earnings', ticker, data,
                    CHANGE_DETECTORS['yahoo_earnings'],
                )
            self._record_source_success('yahoo_earnings')
        except Exception as exc:
            self._record_source_failure('yahoo_earnings')
            log.debug("[Collector] Yahoo earnings %s: %s", ticker, exc)

    def _collect_yahoo_analyst(self, ticker: str):
        if not self._is_source_healthy('yahoo_analyst'):
            return
        source = self._sources.get('yahoo')
        if not source:
            return
        try:
            consensus = source.analyst_consensus(ticker)
            if consensus:
                data = asdict(consensus) if hasattr(consensus, '__dataclass_fields__') else {
                    'ticker': consensus.ticker,
                    'target_mean': consensus.target_mean,
                    'recommendation': consensus.recommendation,
                    'upside_pct': consensus.upside_pct,
                }
                self._persistence.persist_if_changed(
                    'yahoo_analyst', ticker, data,
                    CHANGE_DETECTORS['yahoo_analyst'],
                )
            self._record_source_success('yahoo_analyst')
        except Exception as exc:
            self._record_source_failure('yahoo_analyst')
            log.debug("[Collector] Yahoo analyst %s: %s", ticker, exc)

    def _collect_finviz_screener(self, ticker: str):
        if not self._is_source_healthy('finviz_screener'):
            return
        source = self._sources.get('finviz')
        if not source:
            return
        try:
            screener = source.screener_data(ticker)
            if screener:
                data = asdict(screener) if hasattr(screener, '__dataclass_fields__') else {
                    'ticker': screener.ticker, 'price': screener.price,
                    'short_float': screener.short_float, 'change_pct': screener.change_pct,
                }
                self._persistence.persist_if_changed(
                    'finviz_screener', ticker, data,
                    CHANGE_DETECTORS['finviz_screener'],
                )
            self._record_source_success('finviz_screener')
        except Exception as exc:
            self._record_source_failure('finviz_screener')
            log.debug("[Collector] Finviz screener %s: %s", ticker, exc)

    def _collect_finviz_insider(self, ticker: str):
        if not self._is_source_healthy('finviz_insider'):
            return
        source = self._sources.get('finviz')
        if not source:
            return
        try:
            insider = source.insider_activity(ticker)
            if insider:
                data = asdict(insider) if hasattr(insider, '__dataclass_fields__') else {
                    'ticker': insider.ticker,
                    'net_insider_buys': insider.net_insider_buys,
                    'insider_signal': insider.insider_signal,
                }
                self._persistence.persist_if_changed(
                    'finviz_insider', ticker, data,
                    CHANGE_DETECTORS['finviz_insider'],
                )
            self._record_source_success('finviz_insider')
        except Exception as exc:
            self._record_source_failure('finviz_insider')
            log.debug("[Collector] Finviz insider %s: %s", ticker, exc)

    def _collect_edgar_filings(self, ticker: str):
        if not self._is_source_healthy('edgar_filings'):
            return
        source = self._sources.get('edgar')
        if not source:
            return
        try:
            count = source.filing_count_recent(ticker, days=30)
            data = {'ticker': ticker, 'filing_count': count, 'period_days': 30}
            self._persistence.persist_if_changed(
                'sec_edgar_filings', ticker, data,
                CHANGE_DETECTORS['sec_edgar_filings'],
            )
            self._record_source_success('edgar_filings')
        except Exception as exc:
            self._record_source_failure('edgar_filings')
            log.debug("[Collector] EDGAR %s: %s", ticker, exc)

    def _collect_polygon_prev(self, ticker: str):
        if not self._is_source_healthy('polygon_prev'):
            return
        source = self._sources.get('polygon')
        if not source:
            return
        try:
            prev = source.previous_close(ticker)
            if prev:
                data = asdict(prev) if hasattr(prev, '__dataclass_fields__') else {
                    'ticker': prev.ticker, 'close': prev.close,
                    'volume': prev.volume, 'vwap': prev.vwap,
                    'change_pct': prev.change_pct, 'date': prev.date,
                }
                self._persistence.persist_if_changed(
                    'polygon_prev_close', ticker, data,
                    CHANGE_DETECTORS['polygon_prev_close'],
                )
            self._record_source_success('polygon_prev')
        except Exception as exc:
            self._record_source_failure('polygon_prev')
            log.debug("[Collector] Polygon %s: %s", ticker, exc)

    # -- Direct DB writer ------------------------------------------------------

    def _write_to_db(self, snapshot_type: str, ticker: str, data: dict, reason: str):
        """Write directly to event_store table."""
        try:
            import psycopg2
            from config import DATABASE_URL
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
            cur = conn.cursor()

            now = datetime.now(timezone.utc)

            cur.execute("""
                INSERT INTO event_store (
                    event_id, event_sequence, event_type, event_version,
                    event_time, received_time, processed_time,
                    aggregate_id, aggregate_type,
                    event_payload, source_system, session_id
                ) VALUES (
                    gen_random_uuid(), 0, %s, 1,
                    %s, %s, %s,
                    %s, 'DataSource',
                    %s, %s, 'collector'
                )
            """, (
                f'DataSource_{snapshot_type}',
                now, now, now,
                f'{snapshot_type}_{ticker}_{now.isoformat()}',
                json.dumps({
                    'ticker': ticker,
                    'snapshot_type': snapshot_type,
                    'change_reason': reason,
                    **data,
                }),
                snapshot_type,
            ))
            conn.commit()
            conn.close()
        except Exception as exc:
            log.debug("[Collector] DB write failed for %s/%s: %s", snapshot_type, ticker, exc)
