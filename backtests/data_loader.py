"""
BarDataLoader — Load historical bar data for backtesting.

Primary source: yfinance (free, no auth, 60-day 1-min intraday limit).
Optional sources: Tradier, Alpaca.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd

log = logging.getLogger(__name__)

try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False
    log.warning("yfinance not installed; BarDataLoader will not work without it")


class BarDataLoader:
    """
    Load OHLCV bars for backtesting.

    Supports yfinance (default), Tradier, Alpaca.
    Normalizes to lowercase column names: open, high, low, close, volume.
    """

    def __init__(self, source: str = 'yfinance'):
        if source == 'yfinance' and not HAS_YFINANCE:
            raise RuntimeError("yfinance not installed. Install: pip install yfinance")
        self.source = source
        log.info(f"[BarDataLoader] Initialized with source={source}")

    def load_rvol_baseline(
        self,
        tickers: List[str],
        as_of_date: datetime | str,
        lookback_days: int = 14,
    ) -> Dict[str, pd.DataFrame]:
        """
        Load daily OHLCV for RVOL baseline computation (14-day rolling average).

        Args:
            tickers: list of symbols
            as_of_date: date to compute baseline as of
            lookback_days: number of days to include (default 14 for RVOL)

        Returns:
            dict[ticker, DataFrame with daily OHLCV]
        """
        if isinstance(as_of_date, str):
            as_of_date = datetime.fromisoformat(as_of_date)

        start_date = as_of_date - timedelta(days=lookback_days + 10)
        end_date = as_of_date

        result = {}
        if self.source == 'yfinance':
            result = self._load_yfinance_daily(tickers, start_date, end_date, lookback_days)
        else:
            raise NotImplementedError(f"source={self.source} not yet implemented")

        return result

    def load_intraday(
        self,
        tickers: List[str],
        start_date: datetime | str,
        end_date: datetime | str,
        interval: str = '1m',
    ) -> Dict[str, pd.DataFrame]:
        """
        Load intraday 1-minute bars.

        Args:
            tickers: list of symbols
            start_date: start of backtest period
            end_date: end of backtest period
            interval: '1m' (default), '5m', '15m', '1h'

        Returns:
            dict[ticker, DataFrame with 1-min OHLCV (timezone-aware index)]
        """
        if isinstance(start_date, str):
            start_date = datetime.fromisoformat(start_date)
        if isinstance(end_date, str):
            end_date = datetime.fromisoformat(end_date)

        result = {}
        if self.source == 'yfinance':
            result = self._load_yfinance_intraday(tickers, start_date, end_date, interval)
        else:
            raise NotImplementedError(f"source={self.source} not yet implemented")

        return result

    def get_prior_close(
        self,
        ticker: str,
        session_date: datetime | str,
        intraday_data: Dict[str, pd.DataFrame],
    ) -> float:
        """
        Get prior session's close price for gap calculation.

        Args:
            ticker: symbol
            session_date: date of current session
            intraday_data: dict from load_intraday()

        Returns:
            prior close price (0.0 if not found)
        """
        if isinstance(session_date, str):
            session_date = datetime.fromisoformat(session_date)

        if ticker not in intraday_data or len(intraday_data[ticker]) == 0:
            return 0.0

        df = intraday_data[ticker]
        # Find last bar before session_date
        mask = df.index.date < session_date.date()
        if mask.any():
            return float(df.loc[mask].iloc[-1]['close'])

        return 0.0

    def _load_yfinance_daily(
        self,
        tickers: List[str],
        start_date: datetime,
        end_date: datetime,
        lookback_days: int,
    ) -> Dict[str, pd.DataFrame]:
        """Load daily bars from yfinance for RVOL baseline."""
        result = {}
        from zoneinfo import ZoneInfo
        ET = ZoneInfo('America/New_York')

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
                    log.warning(f"[BarDataLoader] No daily data for {ticker}")
                    continue

                # Handle MultiIndex columns (yfinance >= 0.2.31)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [str(col).lower() for col in df.columns]
                if 'adj close' in df.columns:
                    df.drop(columns=['adj close'], inplace=True)

                # Keep last lookback_days
                if len(df) > lookback_days:
                    df = df.iloc[-lookback_days:]

                # Ensure timezone-aware
                if df.index.tz is None:
                    df.index = df.index.tz_localize(ET)
                else:
                    df.index = df.index.tz_convert(ET)

                result[ticker] = df
                log.debug(f"[BarDataLoader] Loaded {len(df)} daily bars for {ticker}")
            except Exception as e:
                log.warning(f"[BarDataLoader] Error loading daily data for {ticker}: {e}")
                continue

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
        from zoneinfo import ZoneInfo
        ET = ZoneInfo('America/New_York')

        # yfinance has 60-day limit for 1m data
        if (end_date - start_date).days > 60:
            log.warning(f"[BarDataLoader] yfinance 1m data limited to 60 days; requested {(end_date - start_date).days}")

        # yfinance limits 1m data to 7 days per request — batch into chunks
        chunk_days = 7

        for ticker in tickers:
            try:
                all_dfs = []
                chunk_start = start_date
                while chunk_start <= end_date:
                    chunk_end = min(chunk_start + timedelta(days=chunk_days), end_date + timedelta(days=1))
                    df = yf.download(
                        ticker,
                        start=chunk_start.date(),
                        end=chunk_end.date(),
                        interval=interval,
                        progress=False,
                    )
                    if not df.empty:
                        all_dfs.append(df)
                    chunk_start += timedelta(days=chunk_days)

                if not all_dfs:
                    log.warning(f"[BarDataLoader] No intraday data for {ticker}")
                    continue

                df = pd.concat(all_dfs)
                df = df[~df.index.duplicated(keep='first')]

                # Handle MultiIndex columns (yfinance >= 0.2.31)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [str(col).lower() for col in df.columns]
                if 'adj close' in df.columns:
                    df.drop(columns=['adj close'], inplace=True)

                # Ensure timezone-aware
                if df.index.tz is None:
                    df.index = df.index.tz_localize(ET)
                else:
                    df.index = df.index.tz_convert(ET)

                # Filter to session hours (9:30 - 16:00 ET)
                df = df.between_time('09:30', '16:00')

                result[ticker] = df
                log.info(f"[BarDataLoader] Loaded {len(df)} {interval} bars for {ticker}")
            except Exception as e:
                log.warning(f"[BarDataLoader] Error loading intraday data for {ticker}: {e}")
                continue

        return result

    # TODO: Implement Tradier backend
    # def _load_tradier_intraday(self, tickers, start_date, end_date): ...

    # TODO: Implement Alpaca backend
    # def _load_alpaca_intraday(self, tickers, start_date, end_date): ...
