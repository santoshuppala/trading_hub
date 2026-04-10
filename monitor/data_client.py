"""
Data client abstraction layer.

BaseDataClient defines the interface every market-data provider must implement.
make_data_client() is the only place that knows about concrete implementations.

Strategy logic (signals.py, screener.py, monitor.py) only ever touches
BaseDataClient — swapping providers requires zero changes to strategy code.

To add a new provider:
  1. Create monitor/<name>_client.py implementing BaseDataClient.
  2. Add a branch in make_data_client() below.
  3. Set DATA_SOURCE=<name> in .env.
"""
from abc import ABC, abstractmethod


class BaseDataClient(ABC):
    """Abstract interface for market data. Order execution is always on Alpaca."""

    @abstractmethod
    def fetch_batch_bars(self, tickers):
        """
        Fetch today's 1-min bars and RVOL history for all tickers in one shot.

        Returns
        -------
        (bars_cache, rvol_cache)
          bars_cache : {ticker -> DataFrame of today's 1-min bars}
          rvol_cache : {ticker -> DataFrame of historical bars (daily or minute)
                        used by get_rvol() in signals.py}
        """

    @abstractmethod
    def get_bars(self, ticker, bars_cache, rvol_cache, calendar_days=2):
        """Return cached bars, falling back to a live per-ticker fetch."""

    @abstractmethod
    def check_spread(self, ticker):
        """
        Real-time Level 1 quote.
        Returns (spread_pct, ask_price) or (None, None) on error.
        """

    @abstractmethod
    def get_spy_vwap_bias(self, bars_cache):
        """Return True if SPY is currently above its cumulative intraday VWAP."""

    @abstractmethod
    def get_quotes(self, symbols):
        """
        Batch quote data used by MomentumScreener.

        Returns
        -------
        dict : {symbol -> {'volume': float, 'change_percentage': float, ...}}
        """

    @abstractmethod
    def get_daily_history(self, symbol, start, end):
        """
        Daily OHLCV bars — used by the RS filter and RVOL baseline.

        Returns
        -------
        pd.DataFrame with columns [open, high, low, close, volume]
        """


def make_data_client(source, tradier_token='', alpaca_api_key='', alpaca_secret=''):
    """
    Factory — returns the appropriate BaseDataClient subclass.

    Parameters
    ----------
    source         : 'tradier' | 'alpaca'
    tradier_token  : str  — required when source='tradier'
    alpaca_api_key : str  — required when source='alpaca'
    alpaca_secret  : str  — required when source='alpaca'
    """
    source = (source or 'tradier').lower().strip()
    if source == 'tradier':
        from .tradier_client import TradierDataClient
        return TradierDataClient(tradier_token)
    elif source == 'alpaca':
        from .alpaca_data_client import AlpacaDataClient
        return AlpacaDataClient(alpaca_api_key, alpaca_secret)
    else:
        raise ValueError(
            f"Unknown data source: {source!r}. "
            "Set DATA_SOURCE to 'tradier' or 'alpaca'."
        )
