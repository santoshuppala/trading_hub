import logging
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)


class MomentumScreener:
    """
    Discovers high-momentum stocks using Tradier batch quotes and daily bars.

    Sources:
      - Top 50 most active by today's volume  (from batch quotes)
      - Top 20 gainers by % change today      (from batch quotes)

    Filter:
      - Symbol is alpha-only, length <= 5 (no warrants/preferred)
      - Not already in base_tickers
      - 5-day relative strength > SPY (using Tradier daily history)

    Uses the TradierDataClient internally — no separate screener API needed.
    """

    # Universe of symbols to screen beyond our base list.
    # These are liquid, well-known names that might appear as momentum movers.
    _SCREEN_UNIVERSE = [
        'AAPL','MSFT','GOOGL','AMZN','NVDA','TSLA','META','AVGO','AMD','MU',
        'QCOM','NFLX','ADBE','CRM','INTU','NOW','SNOW','DDOG','PLTR','COIN',
        'HOOD','MSTR','RIOT','MARA','CLSK','HUT','RBLX','ROKU','UBER','LYFT',
        'ABNB','DASH','SHOP','SOFI','UPST','NU','SQ','PYPL','AFRM','BILL',
        'SMCI','CRWD','PANW','FTNT','ZS','NET','OKTA','CYBR','S','VRNS',
        'ENPH','FSLR','RUN','PLUG','BE','CHPT','BLNK','LCID','RIVN','NIO',
        'BABA','JD','PDD','BIDU','KWEB','GS','MS','JPM','BAC','WFC','C',
        'SCHW','HOOD','IBKR','MDB','GTLB','PATH','HUBS','DKNG','MGM','WYNN',
        'LVS','CZR','PENN','SPY','QQQ','IWM','SOXL','SOXS','TQQQ','SQQQ',
        'GLD','SLV','TLT','HYG','XLK','XLF','XLE','XLV','ARKK',
    ]

    def __init__(self, data_client):
        """
        Parameters
        ----------
        data_client : TradierDataClient
        """
        self._data_client = data_client

    def refresh(self, base_tickers, current_tickers, last_refresh):
        """
        Fetch and filter momentum stocks.

        Only runs during market hours. Throttled to 30-minute intervals.

        Returns
        -------
        (new_tickers_list, refresh_time)
        """
        now = datetime.now(ET)

        # Only refresh during market hours
        if not ((now.hour == 9 and now.minute >= 30) or (10 <= now.hour <= 14)):
            return current_tickers, last_refresh

        # Throttle to 30-minute intervals
        if last_refresh is not None:
            if (now - last_refresh).total_seconds() < 1800:
                return current_tickers, last_refresh

        candidates = self._fetch_momentum_candidates(base_tickers)

        # Filter: valid US equity symbols only
        candidates = [
            s for s in candidates
            if s.isalpha() and len(s) <= 5 and s not in base_tickers
        ]

        rs_passed = self._filter_by_relative_strength(candidates)

        if rs_passed:
            added = [s for s in rs_passed if s not in current_tickers]
            new_tickers = list(base_tickers) + rs_passed
            if added:
                log.info(
                    f"[Momentum] Added {len(added)} RS-filtered tickers: "
                    f"{', '.join(added)}"
                )
                log.info(f"[Momentum] Total scan list: {len(new_tickers)} tickers")
        else:
            new_tickers = list(base_tickers)

        return new_tickers, now

    def _fetch_momentum_candidates(self, base_tickers):
        """
        Use Tradier batch quotes to find top movers in the screen universe.
        Returns a set of symbols.
        """
        universe = list(set(self._SCREEN_UNIVERSE) | set(base_tickers))
        try:
            quotes = self._data_client._fetch_quotes(universe)
            if not quotes:
                return set()

            # Top 50 by volume
            by_vol = sorted(
                quotes.values(),
                key=lambda q: float(q.get('volume') or 0),
                reverse=True,
            )
            most_active = {q['symbol'] for q in by_vol[:50]}

            # Top 20 gainers by % change
            by_chg = sorted(
                [q for q in quotes.values() if q.get('change_percentage') is not None],
                key=lambda q: float(q.get('change_percentage') or 0),
                reverse=True,
            )
            top_gainers = {q['symbol'] for q in by_chg[:20]}

            candidates = most_active | top_gainers
            log.info(
                f"[Screener] {len(most_active)} most-active + "
                f"{len(top_gainers)} gainers = {len(candidates)} candidates"
            )
            return candidates

        except Exception as e:
            log.error(f"Momentum screener error: {e}")
            return set()

    def _filter_by_relative_strength(self, candidates):
        """
        Keep only candidates whose 5-day return > SPY's 5-day return.
        Uses Tradier daily history bars. Returns all candidates if data unavailable.
        """
        if not candidates:
            return list(candidates)

        symbols = list(set(list(candidates) + ['SPY']))
        start = datetime.now(ET) - timedelta(days=8)
        end   = datetime.now(ET)

        bars = {}

        def _fetch(sym):
            return sym, self._data_client._fetch_daily_history(sym, start, end)

        with ThreadPoolExecutor(max_workers=10) as ex:
            futures = {ex.submit(_fetch, s): s for s in symbols}
            for f in as_completed(futures):
                try:
                    sym, df = f.result()
                    if not df.empty:
                        bars[sym] = df
                except Exception:
                    pass

        spy_df = bars.get('SPY')
        if spy_df is None or len(spy_df) < 2:
            return list(candidates)

        spy_ret = (spy_df['close'].iloc[-1] / spy_df['close'].iloc[0]) - 1

        passed = []
        for sym in candidates:
            df = bars.get(sym)
            if df is None or len(df) < 2:
                passed.append(sym)   # no data → let through
                continue
            ret = (df['close'].iloc[-1] / df['close'].iloc[0]) - 1
            if ret > spy_ret:
                passed.append(sym)

        log.info(
            f"[RS Filter] SPY 5d: {spy_ret:+.2%} | "
            f"{len(passed)}/{len(candidates)} passed"
        )
        return passed
