"""
pop_screener/ingestion.py — Pluggable data-source adapters
==========================================================
Each source is a class that implements a well-defined interface.  The mock
implementations generate deterministic synthetic data so the entire pipeline
can run offline without any API keys.

How to swap a mock for a real API
----------------------------------
1. Subclass (or duck-type) the relevant mock class.
2. Implement the same public method(s) with the same signature.
3. Inject the new instance into PopStrategyEngine via its constructor.

Example — replacing NewsSentimentSource with a Benzinga adapter:

    class BenzingaNewsSentimentSource:
        def __init__(self, api_key: str): ...
        def get_news(self, symbol: str, window_hours: float) -> list[NewsData]:
            # call Benzinga REST API, parse, return NewsData objects
            ...

    engine = PopStrategyEngine(
        bus=bus,
        news_source=BenzingaNewsSentimentSource(api_key=os.getenv('BENZINGA_KEY')),
        ...
    )
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from pop_screener.models import (
    FloatCategory, MarketDataSlice, NewsData, OHLCVBar, SocialData,
)

ET = ZoneInfo('America/New_York')
_RNG = random.Random(42)   # seeded so mock outputs are reproducible


# ── News sentiment source ──────────────────────────────────────────────────────

class NewsSentimentSource:
    """
    Mock news sentiment source.

    Returns synthetic headlines with sentiment scores.  In production replace
    this with an adapter to Benzinga, Refinitiv, or any NLP pipeline that
    produces per-headline sentiment scores in [-1, +1].
    """

    # Realistic headline templates — {sym} is replaced with the ticker symbol
    _BULLISH_HEADLINES = [
        '{sym} smashes Q3 earnings estimates; raises full-year guidance',
        '{sym} announces $2B share buyback programme',
        'FDA grants breakthrough therapy designation to {sym}\'s lead asset',
        '{sym} wins landmark government contract worth $500M',
        'Analyst upgrades {sym} to Strong Buy with $150 price target',
    ]
    _BEARISH_HEADLINES = [
        '{sym} misses Q3 revenue estimates; cuts guidance',
        'DOJ launches antitrust investigation into {sym}',
        '{sym} CFO resigns amid accounting irregularities',
        'Short seller publishes 60-page report on {sym}',
    ]
    _NEUTRAL_HEADLINES = [
        '{sym} scheduled to present at healthcare conference next week',
        '{sym} files 10-Q with SEC',
    ]

    def get_news(self, symbol: str, window_hours: float = 24.0) -> List[NewsData]:
        """
        Return simulated news items for *symbol* within the last *window_hours*.

        Parameters
        ----------
        symbol       : ticker (e.g. 'AAPL')
        window_hours : how far back to look (e.g. 1.0 for 1-hour window)

        Returns
        -------
        List[NewsData] sorted oldest → newest
        """
        now = datetime.now(ET)
        results: List[NewsData] = []

        # Simulate 0–5 headlines per window
        n_headlines = _RNG.randint(0, 5)
        for _ in range(n_headlines):
            age_hours = _RNG.uniform(0, window_hours)
            ts = now - timedelta(hours=age_hours)

            bucket = _RNG.choices(
                ['bullish', 'bearish', 'neutral'],
                weights=[0.5, 0.25, 0.25],
            )[0]

            if bucket == 'bullish':
                raw = _RNG.choice(self._BULLISH_HEADLINES)
                score = round(_RNG.uniform(0.3, 1.0), 2)
            elif bucket == 'bearish':
                raw = _RNG.choice(self._BEARISH_HEADLINES)
                score = round(_RNG.uniform(-1.0, -0.3), 2)
            else:
                raw = _RNG.choice(self._NEUTRAL_HEADLINES)
                score = round(_RNG.uniform(-0.15, 0.15), 2)

            results.append(NewsData(
                timestamp=ts,
                headline=raw.format(sym=symbol),
                sentiment_score=score,
                source='mock',
            ))

        results.sort(key=lambda n: n.timestamp)
        return results


# ── Social sentiment source ────────────────────────────────────────────────────

class SocialSentimentSource:
    """
    Mock social sentiment source (Reddit / StockTwits / X).

    In production replace with an adapter to the StockTwits API, Reddit
    PushShift, or a custom social-listening pipeline.
    """

    def get_social(self, symbol: str, window_hours: float = 1.0) -> SocialData:
        """
        Return aggregated social metrics for *symbol* over the last *window_hours*.

        Parameters
        ----------
        symbol       : ticker
        window_hours : aggregation window

        Returns
        -------
        SocialData
        """
        # Seed by symbol so the same ticker always gets the same mock profile
        sym_seed = sum(ord(c) for c in symbol)
        rng = random.Random(sym_seed + int(window_hours * 10))

        mention_count    = rng.randint(10, 5000)
        mention_velocity = round(mention_count / max(window_hours, 0.1), 1)
        bullish_pct      = round(rng.uniform(0.20, 0.75), 2)
        bearish_pct      = round(rng.uniform(0.05, 1.0 - bullish_pct), 2)

        return SocialData(
            symbol=symbol,
            mention_count=mention_count,
            mention_velocity=mention_velocity,
            bullish_pct=bullish_pct,
            bearish_pct=bearish_pct,
        )


# ── Market behaviour source ────────────────────────────────────────────────────

class MarketBehaviorSource:
    """
    Mock intraday market-data source.

    Returns synthetic OHLCV bars with pre-computed VWAP and RVOL series.
    In production replace with calls to the existing TradierDataClient or
    AlpacaDataClient (already wired into the monitor loop) — just reformat
    their DataFrame output into the List[OHLCVBar] + series format.
    """

    def get_market_slice(
        self,
        symbol: str,
        window_bars: int = 60,
    ) -> MarketDataSlice:
        """
        Return intraday market data for *symbol* covering the last *window_bars*
        one-minute bars.

        Parameters
        ----------
        symbol      : ticker
        window_bars : number of 1-min bars to generate (default 60 = 1 hour)

        Returns
        -------
        MarketDataSlice
        """
        sym_seed = sum(ord(c) for c in symbol)
        rng = random.Random(sym_seed)

        # Simulate a base price between $5 and $500
        base_price = round(rng.uniform(5.0, 500.0), 2)
        prior_close = round(base_price * rng.uniform(0.92, 1.08), 2)
        today_open  = round(prior_close * rng.uniform(0.95, 1.12), 2)
        gap_size    = (today_open - prior_close) / prior_close

        float_shares = rng.randint(5_000_000, 200_000_000)
        float_cat    = FloatCategory.LOW_FLOAT if float_shares < 20_000_000 else FloatCategory.NORMAL
        earnings     = rng.random() < 0.10   # 10 % chance of earnings day

        bars: List[OHLCVBar] = []
        vwap_series: List[float] = []
        rvol_series: List[float] = []

        price   = today_open
        cum_tp  = 0.0   # for VWAP
        cum_vol = 0

        now = datetime.now(ET).replace(second=0, microsecond=0)
        start = now - timedelta(minutes=window_bars)

        for i in range(window_bars):
            ts = start + timedelta(minutes=i)

            # Random walk with slight upward drift
            ret   = rng.gauss(0.0002, 0.004)
            close = max(price * (1 + ret), 0.01)
            high  = close * rng.uniform(1.0, 1.005)
            low   = close * rng.uniform(0.995, 1.0)
            open_ = price   # previous close
            vol   = int(rng.uniform(10_000, 200_000))

            bar = OHLCVBar(
                timestamp=ts,
                open=round(open_, 4),
                high=round(high, 4),
                low=round(low, 4),
                close=round(close, 4),
                volume=vol,
            )
            bars.append(bar)

            # Incremental VWAP
            cum_tp  += ((high + low + close) / 3) * vol
            cum_vol += vol
            vwap_series.append(round(cum_tp / cum_vol, 4))

            # Mock RVOL: vary around 1.0 with occasional spikes
            rvol = round(max(rng.gauss(1.2, 0.6), 0.1), 2)
            rvol_series.append(rvol)

            price = close

        return MarketDataSlice(
            symbol=symbol,
            bars=bars,
            vwap_series=vwap_series,
            rvol_series=rvol_series,
            gap_size=round(gap_size, 4),
            float_category=float_cat,
            earnings_flag=earnings,
            prior_close=prior_close,
        )


# ── Momentum universe source ───────────────────────────────────────────────────

class MomentumSource:
    """
    Wraps or simulates the existing MomentumScreener to provide a universe of
    symbols exhibiting momentum.

    In production pass in the actual MomentumScreener instance from the monitor
    loop so the pop screener operates on the same symbol universe that the main
    strategy engine watches.

    Usage
    -----
        # Production
        momentum_src = MomentumSource(screener=existing_momentum_screener)

        # Development / testing
        momentum_src = MomentumSource(universe=['AAPL', 'NVDA', 'TSLA'])
    """

    def __init__(
        self,
        screener=None,
        universe: Optional[List[str]] = None,
    ):
        """
        Parameters
        ----------
        screener  : optional — an object with a .get_universe() → list[str] method
                    (the existing MomentumScreener or a compatible adapter).
        universe  : optional — explicit list of symbols; used when screener is None.
                    Defaults to a small hard-coded list.
        """
        self._screener = screener
        self._universe = universe or [
            'AAPL', 'NVDA', 'TSLA', 'AMD', 'META',
            'AMZN', 'MSFT', 'GOOGL', 'COIN', 'PLTR',
        ]

    def get_momentum_universe(self) -> List[str]:
        """
        Return the current momentum universe.

        Tries the injected screener first; falls back to the static list.
        """
        if self._screener is not None:
            try:
                return list(self._screener.get_universe())
            except Exception:
                pass   # screener unavailable — fall through to static list
        return list(self._universe)
