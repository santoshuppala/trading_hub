import logging
import requests as http
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)
ALPACA_DATA_URL = 'https://data.alpaca.markets'


class MomentumScreener:
    """
    Fetches high-momentum stocks from the Alpaca screener API and merges
    them into the active ticker list.

    Sources:
      - Top 50 most active stocks by volume
      - Top 20 gainers (movers)

    Filters:
      - Symbol is alpha-only, length <= 5 chars (no warrants/preferred)
      - Not already in base_tickers
      - 5-day relative strength vs SPY (must outperform)
    """

    def __init__(self, api_key, api_secret, data_client):
        """
        Parameters
        ----------
        api_key : str
        api_secret : str
        data_client : AlpacaDataClient
            Used for historical bar fetches (RS filter).
        """
        self._api_key = api_key
        self._api_secret = api_secret
        self._data_client = data_client

    def refresh(self, base_tickers, current_tickers, last_refresh):
        """
        Fetch and filter momentum stocks.

        Only runs during market hours. Throttled to 30-minute intervals.

        Parameters
        ----------
        base_tickers : list[str]
            The fixed watchlist — momentum additions are appended on top.
        current_tickers : list[str]
            The currently active ticker list.
        last_refresh : datetime or None
            Timestamp of the previous refresh. Pass None to force a refresh.

        Returns
        -------
        (new_tickers_list, refresh_time) : (list[str], datetime)
            new_tickers_list is base_tickers if no momentum stocks passed filters.
            refresh_time is the datetime of this refresh, or last_refresh if
            the refresh was skipped (throttled / outside market hours).
        """
        now = datetime.now(ET)

        # Only refresh during market hours
        if not ((now.hour == 9 and now.minute >= 30) or (10 <= now.hour <= 14)):
            return current_tickers, last_refresh

        # Throttle to 30-minute intervals
        if last_refresh is not None:
            elapsed = (now - last_refresh).total_seconds()
            if elapsed < 1800:
                return current_tickers, last_refresh

        headers = {
            'APCA-API-KEY-ID': self._api_key,
            'APCA-API-SECRET-KEY': self._api_secret,
        }
        candidates = set()

        # Most active by volume
        try:
            r = http.get(
                f'{ALPACA_DATA_URL}/v1beta1/screener/stocks/most-actives',
                params={'by': 'volume', 'top': 50},
                headers=headers,
                timeout=10,
            )
            if r.ok:
                for stock in r.json().get('most_actives', []):
                    candidates.add(stock['symbol'])
        except Exception as e:
            log.error(f"Screener most-actives error: {e}")

        # Top gainers
        try:
            r = http.get(
                f'{ALPACA_DATA_URL}/v1beta1/screener/stocks/movers',
                params={'market_type': 'stocks', 'top': 20},
                headers=headers,
                timeout=10,
            )
            if r.ok:
                for stock in r.json().get('gainers', []):
                    candidates.add(stock['symbol'])
        except Exception as e:
            log.error(f"Screener movers error: {e}")

        # Filter: valid US equity symbols only
        candidates = [
            s for s in candidates
            if s.isalpha() and len(s) <= 5 and s not in base_tickers
        ]

        # Relative Strength filter
        rs_passed = self._filter_by_relative_strength(candidates)

        if rs_passed:
            added = [s for s in rs_passed if s not in current_tickers]
            new_tickers = list(base_tickers) + rs_passed
            if added:
                log.info(f"[Momentum] Added {len(added)} RS-filtered tickers: {', '.join(added)}")
                log.info(f"[Momentum] Total scan list: {len(new_tickers)} tickers")
        else:
            new_tickers = list(base_tickers)

        return new_tickers, now

    def _filter_by_relative_strength(self, candidates):
        """
        Keep only candidates whose 5-day return > SPY's 5-day return.
        Uses daily bars to avoid noise from intraday data.
        Returns filtered list; returns all candidates if data unavailable.
        """
        if not candidates:
            return candidates
        try:
            symbols = list(set(candidates + ['SPY']))
            start = datetime.now(ET) - timedelta(days=8)  # 8 calendar days -> ~5 trading days
            req = StockBarsRequest(
                symbol_or_symbols=symbols,
                timeframe=TimeFrame.Day,
                start=start,
                feed='iex',
            )
            bars = self._data_client._client.get_stock_bars(req).df
            if bars.empty or not isinstance(bars.index, pd.MultiIndex):
                return candidates

            def five_day_return(sym):
                try:
                    df = bars.xs(sym, level='symbol')
                    if len(df) < 2:
                        return None
                    return (df['close'].iloc[-1] / df['close'].iloc[0]) - 1
                except Exception:
                    return None

            spy_ret = five_day_return('SPY')
            if spy_ret is None:
                return candidates  # can't compare, let all through

            passed = []
            for sym in candidates:
                ret = five_day_return(sym)
                if ret is None or ret > spy_ret:  # None = no data, let through
                    passed.append(sym)

            log.info(f"[RS Filter] SPY 5d: {spy_ret:+.2%} | {len(passed)}/{len(candidates)} passed")
            return passed
        except Exception as e:
            log.error(f"RS filter error: {e}")
            return candidates  # fail open
