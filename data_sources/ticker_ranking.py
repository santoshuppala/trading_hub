"""
V8 TickerRankingEngine — Hedge-fund-grade ticker conviction scoring.

Combines ALL data sources into a ranked list with:
  - Conviction score (0.0-1.0)
  - Recommended strategy type
  - Entry urgency
  - Position size multiplier
  - Catalyst classification

This is how institutional desks use alternative data:
  1. NEWS is an EVENT TRIGGER, not a passive score
  2. SOCIAL SENTIMENT is a CONTRARIAN indicator at extremes
  3. INSIDER FILINGS are the strongest directional signal
  4. OPTIONS FLOW reveals institutional positioning
  5. MACRO REGIME determines portfolio-level risk appetite
  6. EARNINGS PROXIMITY drives strategy selection (equity vs options)
  7. RELATIVE VOLUME confirms institutional participation
  8. PRICE ACTION (prev-day) sets today's key levels

Usage:
    from data_sources.ticker_ranking import TickerRankingEngine, rank_tickers
    rankings = rank_tickers(tickers)
    for r in rankings[:10]:  # top 10 by conviction
        print(f"{r.ticker}: conviction={r.conviction:.2f} strategy={r.strategy}")
"""
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
log = logging.getLogger(__name__)


@dataclass
class TickerRanking:
    """Ranked ticker with conviction score and trade recommendation."""
    ticker: str
    conviction: float           # 0.0-1.0 composite score
    strategy: str               # 'momentum', 'swing', 'mean_reversion', 'earnings_play', 'squeeze'
    entry_urgency: str          # 'immediate', 'wait_for_setup', 'watch_only'
    size_multiplier: float      # 0.5-2.0 (applied to base budget)
    risk_level: str             # 'normal', 'elevated', 'high'
    catalyst: str               # 'earnings', 'fda', 'upgrade', 'insider', 'momentum', 'squeeze', 'none'
    signals: Dict[str, float]   # per-source conviction breakdown
    reasoning: str              # human-readable explanation


class TickerRankingEngine:
    """
    Institutional-grade ticker ranking using all available data sources.

    Key principles (how quant desks use this data):
    1. News is a TRIGGER — classify by type, don't just count headlines
    2. Social sentiment is CONTRARIAN at extremes — 90% bullish = warning
    3. Insider filing TYPE matters — CEO buying ≠ routine Form 4
    4. Options flow PATTERN matters — OTM sweeps ≠ ATM hedging
    5. Macro regime drives ALLOCATION — not just position sizing
    6. Earnings proximity determines STRATEGY TYPE — not just blocking
    7. Volume confirms CONVICTION — high RVOL validates other signals
    """

    # Weights for composite conviction score
    WEIGHTS = {
        'news_catalyst':    0.20,  # Breaking news, analyst actions
        'social_contrarian': 0.10,  # Social sentiment (contrarian at extremes)
        'insider_activity':  0.15,  # SEC filings, insider trades
        'options_flow':      0.25,  # UOF — strongest institutional signal
        'volume_momentum':   0.15,  # RVOL + prev-day momentum
        'earnings_catalyst': 0.10,  # Earnings proximity + surprise potential
        'technical_setup':   0.05,  # Analyst consensus alignment
    }

    def __init__(self):
        self._cache: Dict[str, Tuple[float, TickerRanking]] = {}
        self._cache_ttl = 300  # 5 min cache

    def rank(self, tickers: List[str]) -> List[TickerRanking]:
        """
        Rank all tickers by conviction. Returns sorted list (highest first).

        Rate-limit safe: reads from alt_data_cache.json (no API calls).
        All data already collected by Data Collector.
        """
        try:
            from data_sources.alt_data_reader import alt_data
        except ImportError:
            return []

        rankings = []
        for ticker in tickers:
            # Check cache
            cached = self._cache.get(ticker)
            if cached and (time.monotonic() - cached[0]) < self._cache_ttl:
                rankings.append(cached[1])
                continue

            ranking = self._score_ticker(ticker, alt_data)
            self._cache[ticker] = (time.monotonic(), ranking)
            rankings.append(ranking)

        # Sort by conviction (highest first)
        rankings.sort(key=lambda r: r.conviction, reverse=True)
        return rankings

    def _score_ticker(self, ticker: str, alt_data) -> TickerRanking:
        """Score a single ticker across all data dimensions."""
        signals = {}
        reasoning_parts = []

        # ── 1. NEWS CATALYST (Benzinga) ──────────────────────────────
        # HF approach: classify news by TYPE, not just count headlines
        news_score = 0.0
        catalyst = 'none'
        try:
            news = alt_data.news_sentiment(ticker)
            if news:
                headlines = news.get('headlines_1h', 0)
                sentiment = news.get('sentiment_delta', 0)

                if headlines >= 5:
                    # 5+ headlines in 1 hour = major event
                    news_score = min(0.9, headlines / 8.0)
                    catalyst = self._classify_catalyst(news)
                    reasoning_parts.append(
                        f"BREAKING: {headlines} headlines (sentiment {sentiment:+.2f})")
                elif headlines >= 2:
                    news_score = min(0.5, headlines / 6.0)
                    if abs(sentiment) > 0.3:
                        catalyst = 'upgrade' if sentiment > 0 else 'downgrade'
                        reasoning_parts.append(
                            f"News activity: {headlines} headlines ({catalyst})")
                elif abs(sentiment) > 0.4:
                    news_score = 0.3
                    reasoning_parts.append(f"Strong sentiment shift: {sentiment:+.2f}")
        except Exception:
            pass
        signals['news_catalyst'] = news_score

        # ── 2. SOCIAL SENTIMENT — CONTRARIAN at extremes ─────────────
        # HF approach: 90% bullish retail = likely local top
        #              90% bearish retail = likely local bottom
        #              Divergence from price action = strongest signal
        social_score = 0.0
        try:
            social = alt_data.social_sentiment(ticker)
            if social:
                velocity = social.get('mention_velocity', 0)
                baseline = max(social.get('baseline', 100), 1)
                rel_velocity = velocity / baseline
                bullish = social.get('bullish_pct', 0.5)

                if rel_velocity > 3.0:
                    # Very high social attention
                    if bullish > 0.85:
                        # CONTRARIAN: extreme bullish = warning
                        social_score = 0.3  # reduced, not boosted
                        reasoning_parts.append(
                            f"CONTRARIAN WARNING: {bullish:.0%} bullish on StockTwits "
                            f"({rel_velocity:.1f}x velocity) — retail euphoria")
                    elif bullish < 0.25:
                        # CONTRARIAN: extreme bearish = opportunity
                        social_score = 0.7
                        reasoning_parts.append(
                            f"CONTRARIAN BUY: {bullish:.0%} bullish ({rel_velocity:.1f}x velocity) "
                            f"— retail panic, potential reversal")
                    else:
                        # Moderate sentiment with high attention = momentum confirmation
                        social_score = 0.5
                        reasoning_parts.append(
                            f"Social momentum: {rel_velocity:.1f}x velocity, "
                            f"{bullish:.0%} bullish")
                elif rel_velocity > 1.5:
                    social_score = 0.3
        except Exception:
            pass
        signals['social_contrarian'] = social_score

        # ── 3. INSIDER / FILING ACTIVITY (SEC EDGAR + Finviz) ────────
        # HF approach: CEO buying own stock = strongest bullish signal
        #              High filing count = corporate event incoming
        insider_score = 0.0
        try:
            sec = alt_data.sec_filings(ticker) if hasattr(alt_data, 'sec_filings') else None
            if sec:
                count = int(sec.get('count', 0) or 0)
                if count > 10:
                    insider_score = 0.8
                    catalyst = catalyst or 'insider'
                    reasoning_parts.append(
                        f"HIGH SEC activity: {count} filings in 30 days")
                elif count > 5:
                    insider_score = 0.5
                    reasoning_parts.append(f"Elevated SEC filings: {count}")

            # Finviz: analyst rating + short float
            finviz = alt_data.finviz_data(ticker)
            if finviz:
                rating = float(finviz.get('analyst_rating', 3.0) or 3.0)
                short_float = float(finviz.get('short_float', 0) or 0)
                rvol = float(finviz.get('relative_volume', 1.0) or 1.0)

                # Strong buy consensus (< 2.0) with high RVOL = institutional accumulation
                if rating < 2.0 and rvol > 1.5:
                    insider_score = max(insider_score, 0.6)
                    reasoning_parts.append(
                        f"Strong Buy consensus ({rating:.1f}) + high RVOL ({rvol:.1f}x)")

                # Short squeeze potential: high short float + rising + high volume
                if short_float > 15.0 and rvol > 2.0:
                    insider_score = max(insider_score, 0.7)
                    catalyst = 'squeeze'
                    reasoning_parts.append(
                        f"SQUEEZE POTENTIAL: short float {short_float:.1f}% + "
                        f"RVOL {rvol:.1f}x")
        except Exception:
            pass
        signals['insider_activity'] = insider_score

        # ── 4. OPTIONS FLOW (UOF) ───────────────────────────────────
        # HF approach: institutional sweeps at OTM = directional bet
        #              Repeat sweeps = high conviction
        #              Premium relative to daily volume = significance
        options_score = 0.0
        try:
            uof = alt_data.unusual_options_flow(ticker)
            if uof:
                premium = uof.get('total_premium', 0)
                daily_opt = uof.get('avg_daily_options_premium', 0)
                daily_stock = uof.get('avg_daily_dollar_volume', 0)

                # Relative significance
                if daily_opt > 0:
                    significance = premium / daily_opt
                elif daily_stock > 0:
                    significance = premium / (daily_stock * 0.03)
                else:
                    significance = 0

                repeats = uof.get('repeat_sweeps', 0)
                call_ratio = uof.get('call_ratio', 0.5)

                if significance > 0.1 and repeats >= 2:
                    options_score = min(0.9, significance * 2)
                    direction = 'bullish' if call_ratio > 0.6 else 'bearish' if call_ratio < 0.4 else 'neutral'
                    reasoning_parts.append(
                        f"INSTITUTIONAL FLOW: {direction} sweeps ({repeats}x repeat), "
                        f"{significance:.1%} of daily volume")
                elif significance > 0.05:
                    options_score = min(0.5, significance * 3)
                    reasoning_parts.append(
                        f"Options flow: {significance:.1%} of daily volume")
        except Exception:
            pass
        signals['options_flow'] = options_score

        # ── 5. VOLUME + MOMENTUM (Polygon prev-day + Finviz RVOL) ────
        # HF approach: prev-day gap + high volume + follow-through = momentum
        volume_score = 0.0
        try:
            polygon = alt_data.polygon_prev_day(ticker) if hasattr(alt_data, 'polygon_prev_day') else None
            if polygon:
                change = abs(float(polygon.get('change_pct', 0) or 0))
                volume = float(polygon.get('volume', 0) or 0)

                if change > 5.0 and volume > 1_000_000:
                    volume_score = min(0.8, change / 10.0)
                    catalyst = catalyst or 'momentum'
                    reasoning_parts.append(
                        f"MOMENTUM: {change:+.1f}% prev-day on {volume/1e6:.1f}M volume")
                elif change > 2.0 and volume > 500_000:
                    volume_score = min(0.5, change / 8.0)

            # Add current RVOL from Finviz
            finviz = alt_data.finviz_data(ticker)
            if finviz:
                rvol = float(finviz.get('relative_volume', 1.0) or 1.0)
                if rvol > 3.0:
                    volume_score = max(volume_score, 0.7)
                    reasoning_parts.append(f"UNUSUAL VOLUME: {rvol:.1f}x relative volume")
                elif rvol > 2.0:
                    volume_score = max(volume_score, 0.4)
        except Exception:
            pass
        signals['volume_momentum'] = volume_score

        # ── 6. EARNINGS CATALYST (Yahoo) ─────────────────────────────
        # HF approach: earnings proximity → strategy type selection
        #   Pre-earnings (2-7 days): options plays (straddles, butterflies)
        #   Earnings today: equity momentum play on reaction
        #   Post-earnings (1-3 days): momentum continuation
        earnings_score = 0.0
        try:
            earnings = alt_data.earnings(ticker)
            if earnings:
                days = earnings.get('days_until_earnings')
                if days is not None and isinstance(days, (int, float)):
                    if days == 0:
                        earnings_score = 0.9
                        catalyst = 'earnings'
                        reasoning_parts.append("EARNINGS TODAY — high vol expected")
                    elif days <= 2:
                        earnings_score = 0.7
                        catalyst = 'earnings'
                        reasoning_parts.append(
                            f"Earnings in {int(days)} day(s) — options straddle opportunity")
                    elif days <= 7:
                        earnings_score = 0.4
                        reasoning_parts.append(
                            f"Earnings approaching ({int(days)} days)")
        except Exception:
            pass
        signals['earnings_catalyst'] = earnings_score

        # ── 7. TECHNICAL CONSENSUS (Finviz analyst rating) ───────────
        tech_score = 0.0
        try:
            finviz = alt_data.finviz_data(ticker)
            if finviz:
                rating = float(finviz.get('analyst_rating', 3.0) or 3.0)
                target = float(finviz.get('target_price', 0) or 0)
                price = float(finviz.get('price', 0) or 0)

                # Analyst consensus alignment
                if rating <= 2.0:
                    tech_score = 0.6
                elif rating <= 2.5:
                    tech_score = 0.3

                # Upside to target
                if price > 0 and target > price:
                    upside = (target - price) / price
                    if upside > 0.20:
                        tech_score = max(tech_score, 0.5)
                        reasoning_parts.append(
                            f"Analyst target ${target:.0f} ({upside:.0%} upside)")
        except Exception:
            pass
        signals['technical_setup'] = tech_score

        # ── COMPOSITE CONVICTION ─────────────────────────────────────
        conviction = sum(
            signals.get(k, 0) * w
            for k, w in self.WEIGHTS.items()
        )

        # Apply F&G multiplier (macro regime)
        try:
            fg = alt_data.fear_greed()
            if fg:
                fg_val = fg.get('value', 50) / 100.0
                # Extreme fear = boost conviction (contrarian buying)
                # Extreme greed = dampen conviction (risk of reversal)
                if fg_val < 0.25:
                    conviction *= 1.2  # extreme fear = opportunity
                    reasoning_parts.append("MACRO: Extreme fear — contrarian opportunity")
                elif fg_val > 0.75:
                    conviction *= 0.8  # extreme greed = caution
                    reasoning_parts.append("MACRO: Extreme greed — elevated risk")
        except Exception:
            pass

        conviction = min(conviction, 1.0)

        # ── STRATEGY SELECTION ───────────────────────────────────────
        strategy = self._select_strategy(signals, catalyst, ticker)

        # ── ENTRY URGENCY ────────────────────────────────────────────
        if conviction > 0.7 and (catalyst in ('earnings', 'fda', 'squeeze') or
                                 signals.get('news_catalyst', 0) > 0.7):
            urgency = 'immediate'
        elif conviction > 0.4:
            urgency = 'wait_for_setup'
        else:
            urgency = 'watch_only'

        # ── SIZE MULTIPLIER ──────────────────────────────────────────
        # Higher conviction = larger position (up to 2x base budget)
        if conviction > 0.7:
            size_mult = 1.5 + conviction * 0.5  # 1.85-2.0x
        elif conviction > 0.4:
            size_mult = 1.0 + conviction * 0.5  # 1.2-1.35x
        else:
            size_mult = 0.5 + conviction       # 0.5-0.9x
        size_mult = round(min(size_mult, 2.0), 2)

        # ── RISK LEVEL ───────────────────────────────────────────────
        if catalyst in ('earnings', 'fda', 'squeeze'):
            risk = 'high'
        elif conviction > 0.6 or signals.get('options_flow', 0) > 0.5:
            risk = 'elevated'
        else:
            risk = 'normal'

        reasoning = ' | '.join(reasoning_parts) if reasoning_parts else 'No significant signals'

        return TickerRanking(
            ticker=ticker,
            conviction=round(conviction, 4),
            strategy=strategy,
            entry_urgency=urgency,
            size_multiplier=size_mult,
            risk_level=risk,
            catalyst=catalyst,
            signals=signals,
            reasoning=reasoning,
        )

    def _select_strategy(self, signals: dict, catalyst: str, ticker: str) -> str:
        """Select optimal strategy based on data signals and catalyst type."""
        if catalyst == 'earnings':
            return 'earnings_play'
        elif catalyst == 'squeeze':
            return 'momentum'  # short squeezes are momentum plays
        elif catalyst in ('fda', 'insider'):
            return 'swing'  # event-driven plays need multi-day holding
        elif signals.get('volume_momentum', 0) > 0.5:
            return 'momentum'
        elif signals.get('options_flow', 0) > 0.5:
            return 'swing'  # follow institutional positioning
        elif signals.get('social_contrarian', 0) > 0.5:
            return 'mean_reversion'  # contrarian = reversal play
        else:
            return 'momentum'  # default to momentum for high-conviction

    def _classify_catalyst(self, news: dict) -> str:
        """Classify news catalyst type from headline content."""
        # This is a simple heuristic — a real system would use NLP
        sentiment = news.get('sentiment_delta', 0)
        headlines = news.get('headlines_1h', 0)

        if headlines > 8:
            return 'fda'  # massive headline count = binary event
        elif sentiment > 0.5:
            return 'upgrade'
        elif sentiment < -0.5:
            return 'downgrade'
        elif headlines > 3:
            return 'momentum'
        return 'none'


# ── Module-level convenience function ─────────────────────────────────

_engine = TickerRankingEngine()


def rank_tickers(tickers: List[str]) -> List[TickerRanking]:
    """Rank tickers by conviction. Returns sorted list (highest first)."""
    return _engine.rank(tickers)


def get_top_tickers(tickers: List[str], n: int = 20,
                    min_conviction: float = 0.3) -> List[TickerRanking]:
    """Get top N tickers above minimum conviction threshold."""
    rankings = _engine.rank(tickers)
    return [r for r in rankings if r.conviction >= min_conviction][:n]


def print_rankings(tickers: List[str], n: int = 20):
    """Print formatted ranking table (for debugging/logging)."""
    rankings = _engine.rank(tickers)[:n]
    print(f"\n{'='*90}")
    print(f"  TICKER CONVICTION RANKINGS (top {n})")
    print(f"{'='*90}")
    print(f"  {'Ticker':<8} {'Conv':>5} {'Strategy':<16} {'Urgency':<16} "
          f"{'Size':>5} {'Risk':<10} {'Catalyst':<10}")
    print(f"  {'-'*80}")
    for r in rankings:
        print(f"  {r.ticker:<8} {r.conviction:>5.2f} {r.strategy:<16} {r.entry_urgency:<16} "
              f"{r.size_multiplier:>5.2f} {r.risk_level:<10} {r.catalyst:<10}")
        if r.reasoning and r.conviction > 0.2:
            print(f"           {r.reasoning[:75]}")
    print(f"{'='*90}\n")
