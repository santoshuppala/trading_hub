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
import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


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


class FailoverDataClient:
    """Wraps primary (Tradier $10) + secondary (Alpaca free) data client.

    Alpaca free tier = IEX only (~10% of trades, lower bar quality).
    Used ONLY as emergency fallback when Tradier is completely down.
    Logs CRITICAL alert on failover so operator is aware.

    Auto-recovers to primary after 5 minutes of secondary use.
    """

    def __init__(self, primary, secondary=None, alert_fn=None):
        self._primary = primary
        self._secondary = secondary
        self._alert_fn = alert_fn
        self._consecutive_failures = 0
        self._using_secondary = False
        self._secondary_since = 0.0
        self._FAILOVER_THRESHOLD = 3      # failures before switching
        self._RECOVERY_INTERVAL = 300.0    # 5 min — try primary again

    def fetch_batch_bars(self, tickers):
        import time as _time

        # Auto-recover: try primary again after 5 min on secondary
        if self._using_secondary and self._secondary:
            if (_time.monotonic() - self._secondary_since) > self._RECOVERY_INTERVAL:
                log.info("[Failover] Attempting recovery to primary data source")
                self._using_secondary = False
                self._consecutive_failures = 0

        client = self._secondary if self._using_secondary else self._primary
        try:
            bars, rvol = client.fetch_batch_bars(tickers)
            if len(bars) < len(tickers) * 0.5:
                raise ValueError(f"Low coverage: {len(bars)}/{len(tickers)}")
            self._consecutive_failures = 0
            return bars, rvol
        except Exception as exc:
            self._consecutive_failures += 1
            if (self._consecutive_failures >= self._FAILOVER_THRESHOLD
                    and self._secondary and not self._using_secondary):
                log.error(
                    "[Failover] Primary data source failed %dx — switching to "
                    "secondary (IEX only, DEGRADED quality). MONITOR CLOSELY.",
                    self._consecutive_failures,
                )
                if self._alert_fn:
                    try:
                        self._alert_fn(
                            f"DATA FAILOVER: Primary data source failed {self._consecutive_failures}x. "
                            f"Switched to secondary (Alpaca IEX — degraded quality). "
                            f"Last error: {exc}"
                        )
                    except Exception:
                        pass
                self._using_secondary = True
                self._secondary_since = _time.monotonic()
                try:
                    return self._secondary.fetch_batch_bars(tickers)
                except Exception:
                    pass
            raise

    # Delegate other methods to active client
    def check_spread(self, ticker):
        client = self._secondary if self._using_secondary else self._primary
        return client.check_spread(ticker)

    def get_daily_history(self, symbol, start, end):
        client = self._secondary if self._using_secondary else self._primary
        return client.get_daily_history(symbol, start, end)

    def __getattr__(self, name):
        """Forward unknown attributes to active client."""
        client = self._secondary if self._using_secondary else self._primary
        return getattr(client, name)


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
