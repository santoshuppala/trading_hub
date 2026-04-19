"""
BarDataLoader — Load historical bar data for backtesting.

Sources:
  - yfinance: Free, 60-day 1-min limit (default)
  - tradier: Production API ($10/month), 20-day 1-min / 40-day 5-min
  - db: PostgreSQL market_bars table (grows daily from live trading)

Option C (hybrid): fetch from Tradier, persist to DB, future runs load from DB.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

log = logging.getLogger(__name__)

try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')


class BarDataLoader:
    """
    Load OHLCV bars for backtesting.

    Supports yfinance (default), Tradier, DB (market_bars table).
    Normalizes to lowercase column names: open, high, low, close, volume.
    """

    # Tradier lookback limits (calendar days, with margin for weekends)
    TRADIER_1M_LOOKBACK_DAYS = 28    # ~20 trading days
    TRADIER_5M_LOOKBACK_DAYS = 56    # ~40 trading days

    def __init__(
        self,
        source: str = 'yfinance',
        tradier_token: str = None,
        db_dsn: str = None,
    ):
        self.source = source
        self._tradier_token = tradier_token or os.getenv('TRADIER_TOKEN', '')
        self._db_dsn = db_dsn or os.getenv(
            'DATABASE_URL',
            'postgresql://trading:trading_secret@localhost:5432/tradinghub',
        )

        if source == 'yfinance' and not HAS_YFINANCE:
            raise RuntimeError("yfinance not installed. Install: pip install yfinance")
        if source == 'tradier' and not self._tradier_token:
            raise RuntimeError("TRADIER_TOKEN not set. Set env var or pass tradier_token=")

        log.info(f"[BarDataLoader] Initialized with source={source}")

    # ══════════════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════════════

    def load_rvol_baseline(
        self,
        tickers: List[str],
        as_of_date: datetime | str,
        lookback_days: int = 14,
    ) -> Dict[str, pd.DataFrame]:
        """Load daily OHLCV for RVOL baseline (14-day rolling average)."""
        if isinstance(as_of_date, str):
            as_of_date = datetime.fromisoformat(as_of_date)

        start_date = as_of_date - timedelta(days=lookback_days + 10)
        end_date = as_of_date

        if self.source == 'tradier':
            return self._load_tradier_daily(tickers, start_date, end_date, lookback_days)
        elif self.source == 'db':
            return self._load_daily_from_db(tickers, start_date, end_date, lookback_days)
        elif self.source == 'yfinance':
            return self._load_yfinance_daily(tickers, start_date, end_date, lookback_days)
        else:
            raise NotImplementedError(f"source={self.source}")

    def load_intraday(
        self,
        tickers: List[str],
        start_date: datetime | str,
        end_date: datetime | str,
        interval: str = '1m',
    ) -> Dict[str, pd.DataFrame]:
        """
        Load intraday bars.

        Args:
            tickers: list of symbols
            start_date: start of backtest period
            end_date: end of backtest period
            interval: '1m' or '5m'

        Returns:
            dict[ticker, DataFrame with OHLCV (timezone-aware ET index)]
        """
        if isinstance(start_date, str):
            start_date = datetime.fromisoformat(start_date)
        if isinstance(end_date, str):
            end_date = datetime.fromisoformat(end_date)

        if self.source == 'tradier':
            return self._load_tradier_intraday(tickers, start_date, end_date, interval)
        elif self.source == 'db':
            return self._load_from_db(tickers, start_date, end_date, interval)
        elif self.source == 'yfinance':
            return self._load_yfinance_intraday(tickers, start_date, end_date, interval)
        else:
            raise NotImplementedError(f"source={self.source}")

    def save_to_db(
        self,
        data: Dict[str, pd.DataFrame],
        interval: str = '1m',
    ) -> int:
        """
        Persist bar data to market_bars table via psql.

        Args:
            data: dict[ticker, DataFrame with OHLCV + tz-aware index]
            interval: '1m' or '5m'

        Returns:
            number of rows inserted
        """
        if not data:
            return 0
        return self._save_to_db_psql(data, interval)

    def get_prior_close(
        self,
        ticker: str,
        session_date: datetime | str,
        intraday_data: Dict[str, pd.DataFrame],
    ) -> float:
        """Get prior session's close price for gap calculation."""
        if isinstance(session_date, str):
            session_date = datetime.fromisoformat(session_date)

        if ticker not in intraday_data or len(intraday_data[ticker]) == 0:
            return 0.0

        df = intraday_data[ticker]
        mask = df.index.date < session_date.date()
        if mask.any():
            return float(df.loc[mask].iloc[-1]['close'])
        return 0.0

    # ══════════════════════════════════════════════════════════════════════
    # Tradier backend
    # ══════════════════════════════════════════════════════════════════════

    def _load_tradier_intraday(
        self,
        tickers: List[str],
        start_date: datetime,
        end_date: datetime,
        interval: str = '1m',
    ) -> Dict[str, pd.DataFrame]:
        """
        Load intraday bars from Tradier timesales API.

        Rate limit: 120 req/min. Uses parallel fetching with throttling.
        """
        from monitor.tradier_client import TradierDataClient
        client = TradierDataClient(self._tradier_token)

        # Tradier interval mapping
        tradier_interval = '5min' if interval == '5m' else '1min'

        # Clamp to Tradier lookback limits
        max_lookback = (self.TRADIER_5M_LOOKBACK_DAYS if interval == '5m'
                        else self.TRADIER_1M_LOOKBACK_DAYS)
        earliest = datetime.now(ET) - timedelta(days=max_lookback)
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=ET)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=ET)
        effective_start = max(start_date, earliest)

        # Market hours for API call
        api_start = effective_start.replace(hour=9, minute=30, second=0, microsecond=0)
        api_end = end_date.replace(hour=16, minute=0, second=0, microsecond=0)

        log.info(
            "[BarDataLoader] Fetching %s bars from Tradier: %d tickers, %s → %s",
            interval, len(tickers),
            effective_start.strftime('%Y-%m-%d'),
            end_date.strftime('%Y-%m-%d'),
        )

        result: Dict[str, pd.DataFrame] = {}
        batch_size = 30  # stay well under 120/min with parallel workers
        total_batches = (len(tickers) + batch_size - 1) // batch_size

        for batch_idx in range(total_batches):
            batch_start = batch_idx * batch_size
            batch = tickers[batch_start:batch_start + batch_size]

            with ThreadPoolExecutor(max_workers=20) as pool:
                futures = {
                    pool.submit(
                        client._fetch_timesales, ticker,
                        api_start, api_end, tradier_interval,
                    ): ticker
                    for ticker in batch
                }
                for future in as_completed(futures):
                    ticker = futures[future]
                    try:
                        df = future.result()
                        if df is not None and not df.empty:
                            # Filter to market hours
                            df = df.between_time('09:30', '16:00')
                            if not df.empty:
                                result[ticker] = df
                    except Exception as e:
                        log.warning("[BarDataLoader] Tradier %s failed for %s: %s",
                                    interval, ticker, e)

            loaded = len(result)
            log.info(
                "[BarDataLoader] Batch %d/%d done — %d/%d tickers loaded so far",
                batch_idx + 1, total_batches, loaded, len(tickers),
            )

            # Rate-limit pause between batches (30 calls / 15s = 120/min)
            if batch_idx < total_batches - 1:
                time.sleep(15)

        log.info("[BarDataLoader] Tradier %s: loaded %d/%d tickers",
                 interval, len(result), len(tickers))
        return result

    def _load_tradier_daily(
        self,
        tickers: List[str],
        start_date: datetime,
        end_date: datetime,
        lookback_days: int,
    ) -> Dict[str, pd.DataFrame]:
        """Load daily OHLCV from Tradier history API."""
        from monitor.tradier_client import TradierDataClient
        client = TradierDataClient(self._tradier_token)

        result: Dict[str, pd.DataFrame] = {}

        for ticker in tickers:
            try:
                df = client.get_daily_history(ticker, start_date, end_date)
                if df is not None and not df.empty:
                    if len(df) > lookback_days:
                        df = df.iloc[-lookback_days:]
                    result[ticker] = df
            except Exception as e:
                log.warning("[BarDataLoader] Tradier daily failed for %s: %s", ticker, e)

        log.info("[BarDataLoader] Tradier daily: loaded %d/%d tickers", len(result), len(tickers))
        return result

    # ══════════════════════════════════════════════════════════════════════
    # DB backend (market_bars table via psql subprocess)
    # ══════════════════════════════════════════════════════════════════════

    def _load_from_db(
        self,
        tickers: List[str],
        start_date: datetime,
        end_date: datetime,
        interval: str = '1m',
    ) -> Dict[str, pd.DataFrame]:
        """Load intraday bars from market_bars table via psql."""
        result: Dict[str, pd.DataFrame] = {}

        # Format dates for SQL
        start_str = start_date.strftime('%Y-%m-%d %H:%M:%S')
        end_str = end_date.strftime('%Y-%m-%d %H:%M:%S')
        ticker_list = "','".join(tickers)

        query = (
            f"COPY (SELECT ticker, bar_time, open, high, low, close, volume "
            f"FROM trading.market_bars "
            f"WHERE ticker IN ('{ticker_list}') "
            f"AND bar_time BETWEEN '{start_str}' AND '{end_str}' "
            f"AND bar_interval = '{interval}' "
            f"ORDER BY ticker, bar_time) TO STDOUT WITH CSV HEADER"
        )

        try:
            proc = subprocess.run(
                ['psql', '-d', 'tradinghub', '-c', query],
                capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                log.warning("[BarDataLoader] psql query failed: %s", proc.stderr.strip())
                return {}

            if not proc.stdout.strip():
                log.info("[BarDataLoader] DB %s: no data found", interval)
                return {}

            df_all = pd.read_csv(io.StringIO(proc.stdout))
            if df_all.empty:
                return {}

            df_all['bar_time'] = pd.to_datetime(df_all['bar_time'])
            if df_all['bar_time'].dt.tz is None:
                df_all['bar_time'] = df_all['bar_time'].dt.tz_localize(ET)

            for ticker, group in df_all.groupby('ticker'):
                df = group[['bar_time', 'open', 'high', 'low', 'close', 'volume']].copy()
                df = df.set_index('bar_time')
                df = df.between_time('09:30', '16:00')
                for col in ['open', 'high', 'low', 'close']:
                    df[col] = df[col].astype(float)
                df['volume'] = df['volume'].astype(float)
                if not df.empty:
                    result[ticker] = df

            log.info("[BarDataLoader] DB %s: loaded %d/%d tickers",
                     interval, len(result), len(tickers))
        except FileNotFoundError:
            log.warning("[BarDataLoader] psql not found — cannot load from DB")
        except subprocess.TimeoutExpired:
            log.warning("[BarDataLoader] psql query timed out")
        except Exception as e:
            log.warning("[BarDataLoader] DB load error: %s", e)

        return result

    def _load_daily_from_db(
        self,
        tickers: List[str],
        start_date: datetime,
        end_date: datetime,
        lookback_days: int,
    ) -> Dict[str, pd.DataFrame]:
        """Load daily bars from market_bars where bar_interval='1d'."""
        return self._load_from_db(tickers, start_date, end_date, interval='1d')

    def _save_to_db_psql(
        self,
        data: Dict[str, pd.DataFrame],
        interval: str,
    ) -> int:
        """Insert bars into market_bars via psql \\copy (file-based, no arg limit)."""
        if not data:
            return 0

        import tempfile

        # Write CSV to temp file
        csv_path = None
        total_rows = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', suffix='.csv', delete=False, prefix='bars_',
            ) as f:
                csv_path = f.name
                writer = csv.writer(f)
                for ticker, df in data.items():
                    if df.empty:
                        continue
                    for idx, row in df.iterrows():
                        ts = idx
                        if hasattr(ts, 'to_pydatetime'):
                            ts = ts.to_pydatetime()
                        if hasattr(ts, 'tzinfo') and ts.tzinfo is not None:
                            ts = ts.replace(tzinfo=None)
                        writer.writerow([
                            ticker,
                            ts.strftime('%Y-%m-%d %H:%M:%S'),
                            f"{float(row['open']):.4f}",
                            f"{float(row['high']):.4f}",
                            f"{float(row['low']):.4f}",
                            f"{float(row['close']):.4f}",
                            int(row['volume']),
                            interval,
                        ])
                        total_rows += 1

            if total_rows == 0:
                return 0

            # Build psql script: create temp table, \copy from CSV, merge
            sql_path = csv_path + '.sql'
            with open(sql_path, 'w') as f:
                f.write("CREATE TEMP TABLE _bars_import (\n")
                f.write("  ticker TEXT, bar_time TIMESTAMP, open NUMERIC(10,2),\n")
                f.write("  high NUMERIC(10,2), low NUMERIC(10,2), close NUMERIC(10,2),\n")
                f.write("  volume BIGINT, bar_interval VARCHAR(3)\n")
                f.write(");\n")
                f.write(f"\\copy _bars_import FROM '{csv_path}' WITH CSV\n")
                f.write(
                    "INSERT INTO trading.market_bars "
                    "(ticker, bar_time, open, high, low, close, volume, bar_interval) "
                    "SELECT * FROM _bars_import "
                    "ON CONFLICT DO NOTHING;\n"
                )
                f.write("DROP TABLE _bars_import;\n")

            proc = subprocess.run(
                ['psql', '-d', 'tradinghub', '-f', sql_path],
                capture_output=True, text=True, timeout=300,
            )
            if proc.returncode != 0:
                log.warning("[BarDataLoader] DB save failed: %s",
                            proc.stderr.strip()[:300])
                return 0

            log.info("[BarDataLoader] DB save: %d rows (%s)", total_rows, interval)
            return total_rows

        except Exception as e:
            log.warning("[BarDataLoader] DB save error: %s", e)
            return 0
        finally:
            if csv_path and os.path.exists(csv_path):
                os.unlink(csv_path)
            sql_tmp = (csv_path or '') + '.sql'
            if os.path.exists(sql_tmp):
                os.unlink(sql_tmp)

    # ══════════════════════════════════════════════════════════════════════
    # yfinance backend (original)
    # ══════════════════════════════════════════════════════════════════════

    def _load_yfinance_daily(
        self,
        tickers: List[str],
        start_date: datetime,
        end_date: datetime,
        lookback_days: int,
    ) -> Dict[str, pd.DataFrame]:
        """Load daily bars from yfinance for RVOL baseline."""
        result = {}
        for ticker in tickers:
            try:
                df = yf.download(
                    ticker,
                    start=start_date.date(),
                    end=end_date.date(),
                    interval='1d',
                    progress=False,
                )
                if df.empty:
                    continue

                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [str(col).lower() for col in df.columns]
                if 'adj close' in df.columns:
                    df.drop(columns=['adj close'], inplace=True)

                if len(df) > lookback_days:
                    df = df.iloc[-lookback_days:]

                if df.index.tz is None:
                    df.index = df.index.tz_localize(ET)
                else:
                    df.index = df.index.tz_convert(ET)

                result[ticker] = df
            except Exception as e:
                log.warning("[BarDataLoader] yfinance daily error for %s: %s", ticker, e)

        return result

    def _load_yfinance_intraday(
        self,
        tickers: List[str],
        start_date: datetime,
        end_date: datetime,
        interval: str = '1m',
    ) -> Dict[str, pd.DataFrame]:
        """Load intraday bars from yfinance."""
        result = {}
        yf_interval = interval.replace('m', 'm')  # '1m' or '5m' already valid

        if interval == '1m' and (end_date - start_date).days > 60:
            log.warning("[BarDataLoader] yfinance 1m limited to 60 days; "
                        "requested %d", (end_date - start_date).days)

        chunk_days = 7

        for ticker in tickers:
            try:
                all_dfs = []
                chunk_start = start_date
                while chunk_start <= end_date:
                    chunk_end = min(chunk_start + timedelta(days=chunk_days),
                                   end_date + timedelta(days=1))
                    df = yf.download(
                        ticker,
                        start=chunk_start.date(),
                        end=chunk_end.date(),
                        interval=yf_interval,
                        progress=False,
                    )
                    if not df.empty:
                        all_dfs.append(df)
                    chunk_start += timedelta(days=chunk_days)

                if not all_dfs:
                    continue

                df = pd.concat(all_dfs)
                df = df[~df.index.duplicated(keep='first')]

                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [str(col).lower() for col in df.columns]
                if 'adj close' in df.columns:
                    df.drop(columns=['adj close'], inplace=True)

                if df.index.tz is None:
                    df.index = df.index.tz_localize(ET)
                else:
                    df.index = df.index.tz_convert(ET)

                df = df.between_time('09:30', '16:00')
                result[ticker] = df
                log.debug("[BarDataLoader] Loaded %d %s bars for %s",
                          len(df), interval, ticker)
            except Exception as e:
                log.warning("[BarDataLoader] yfinance %s error for %s: %s",
                            interval, ticker, e)

        return result
