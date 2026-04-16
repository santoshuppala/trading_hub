"""
V7.1 Market Regime & Ticker Conviction — Unified Alt Data Scoring.

Two layers:
  1. MarketRegime — system-wide score from Fear & Greed + FRED VIX
     Returns position_size_multiplier (0.5 to 1.0, never halt)

  2. TickerConviction — per-ticker score from Finviz + SEC + Yahoo
     Returns conviction boost (0.0 to +0.3, never negative)
     Negative signals logged as warnings, never block trades

Principle: Technical setup is king. Alt data is a tailwind, never a headwind.

Usage:
    from data_sources.market_regime import regime, conviction

    # System-wide (PortfolioRiskGate / RiskEngine)
    multiplier = regime.position_size_multiplier()  # 0.5 to 1.0
    qty = int(base_qty * multiplier)

    # Per-ticker (Pro RiskAdapter / Pop Executor)
    boost = conviction.score('AAPL')  # 0.0 to +0.3
    final_confidence = strategy_confidence + boost
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

# Cache alt_data reads (don't hit file every call)
_CACHE_TTL = 30.0  # seconds


class MarketRegime:
    """Unified market regime from Fear & Greed + FRED VIX.

    Score range: -1.0 (crisis) to +1.0 (euphoric)
    Position multiplier: 0.5 (crisis) to 1.0 (normal/aggressive)

    Never halts trading — the kill switch handles true emergencies.
    """

    def __init__(self):
        self._cached_score: Optional[float] = None
        self._cached_at: float = 0.0

    def score(self) -> float:
        """Market regime score: -1.0 (crisis) to +1.0 (euphoric).

        Combines Fear & Greed (sentiment) with VIX (volatility).
        Returns 0.0 if alt data unavailable (neutral — no impact).
        """
        now = time.monotonic()
        if self._cached_score is not None and now - self._cached_at < _CACHE_TTL:
            return self._cached_score

        try:
            from data_sources.alt_data_reader import alt_data

            fg_value = alt_data.fear_greed_value()  # 0-100 or None
            vix = alt_data.vix()  # float or None

            if fg_value is None and vix is None:
                self._cached_score = 0.0
                self._cached_at = now
                return 0.0

            # Fear & Greed component: map 0-100 to -0.5 to +0.5
            fg_score = 0.0
            if fg_value is not None:
                fg_score = (fg_value - 50) / 100  # -0.5 to +0.5

            # VIX component: low VIX = bullish, high VIX = bearish
            vix_score = 0.0
            if vix is not None:
                if vix > 40:
                    vix_score = -0.5  # crisis
                elif vix > 30:
                    vix_score = -0.3  # elevated
                elif vix > 25:
                    vix_score = -0.1  # above normal
                elif vix < 15:
                    vix_score = +0.2  # calm
                # 15-25 = normal → 0.0

            # Combined: weighted average (F&G 60%, VIX 40%)
            combined = fg_score * 0.6 + vix_score * 0.4
            # Clamp to [-1.0, +1.0]
            combined = max(-1.0, min(1.0, combined))

            self._cached_score = combined
            self._cached_at = now
            return combined

        except Exception:
            self._cached_score = 0.0
            self._cached_at = now
            return 0.0

    def position_size_multiplier(self) -> float:
        """Position size multiplier: 0.5 (crisis) to 1.0 (normal).

        Applied as: qty = int(base_qty * multiplier)
        Never returns 0 — always allows trading at reduced size.
        """
        s = self.score()

        if s <= -0.7:
            return 0.5   # crisis — half size
        elif s <= -0.4:
            return 0.7   # defensive — 70% size
        elif s <= -0.2:
            return 0.85  # cautious — 85% size
        else:
            return 1.0   # neutral/bullish — full size

    def summary(self) -> str:
        """Human-readable regime summary for logging."""
        s = self.score()
        m = self.position_size_multiplier()

        try:
            from data_sources.alt_data_reader import alt_data
            fg = alt_data.fear_greed_value()
            vix = alt_data.vix()
        except Exception:
            fg, vix = None, None

        if s <= -0.7:
            label = 'CRISIS'
        elif s <= -0.4:
            label = 'DEFENSIVE'
        elif s <= -0.2:
            label = 'CAUTIOUS'
        elif s <= 0.3:
            label = 'NEUTRAL'
        elif s <= 0.6:
            label = 'BULLISH'
        else:
            label = 'EUPHORIC'

        return (f"regime={label} score={s:+.2f} multiplier={m:.0%} "
                f"F&G={fg or '?'} VIX={vix or '?'}")


class TickerConviction:
    """Per-ticker conviction boost from Finviz + SEC + Yahoo.

    Score range: 0.0 to +0.3 (NEVER negative — only boosts, never blocks).
    Negative signals are logged as warnings for awareness.

    Applied as: final_confidence = strategy_confidence + conviction.score(ticker)
    """

    def __init__(self):
        self._cache: dict = {}  # {ticker: (score, monotonic_time)}

    def score(self, ticker: str) -> float:
        """Conviction boost for a ticker: 0.0 (no data) to +0.3 (strong conviction).

        Components:
          - Insider buying (SEC/Finviz): +0.05 to +0.10
          - Analyst consensus (Yahoo/Finviz): +0.03 to +0.08
          - Short squeeze potential (Finviz): +0.05
          - Earnings proximity (Yahoo): +0.02

        Negative signals → log warning, return 0.0 (don't penalize).
        """
        now = time.monotonic()
        cached = self._cache.get(ticker)
        if cached and now - cached[1] < _CACHE_TTL:
            return cached[0]

        total = 0.0
        warnings = []

        try:
            from data_sources.alt_data_reader import alt_data

            # ── Insider activity ──────────────────────────────────────
            finviz = alt_data.finviz_data(ticker)
            if finviz:
                # Insider signal from Finviz
                insider_signal = finviz.get('insider_signal', '')
                if insider_signal == 'bullish':
                    total += 0.08
                elif insider_signal == 'bearish':
                    warnings.append(f"insider selling cluster")

            # ── Analyst consensus ─────────────────────────────────────
            if finviz:
                try:
                    rating = float(finviz.get('analyst_rating', 3.0) or 3.0)
                    target = float(finviz.get('target_price', 0) or 0)
                    price = float(finviz.get('price', 0) or 0)

                    if rating <= 1.5 and price > 0:  # Strong Buy
                        total += 0.08
                    elif rating <= 2.0:  # Buy
                        total += 0.04
                    elif rating >= 4.0:  # Sell
                        warnings.append(f"analyst consensus bearish (rating={rating})")

                    if target > 0 and price > 0:
                        upside = (target - price) / price
                        if upside > 0.30:  # >30% upside to target
                            total += 0.05
                        elif upside < -0.10:  # >10% downside
                            warnings.append(f"analyst target below price ({upside:+.0%})")
                except (ValueError, TypeError):
                    pass

            # ── Short squeeze potential ────────────────────────────────
            short_float = alt_data.short_float(ticker)
            if short_float is not None and short_float > 15.0:
                total += 0.05  # high short interest = squeeze potential
                if short_float > 25.0:
                    total += 0.03  # very high = extra boost

            # ── Earnings proximity ────────────────────────────────────
            dte = alt_data.days_until_earnings(ticker)
            if dte is not None and 1 <= dte <= 5:
                total += 0.02  # earnings catalyst nearby

            # ── Unusual options flow ──────────────────────────────────
            uof = alt_data.unusual_options_flow(ticker)
            if uof:
                signal = uof.get('signal', '')
                conf = float(uof.get('confidence', 0))
                if signal == 'bullish' and conf > 0.3:
                    total += min(0.10, conf * 0.15)  # up to +0.10
                elif signal == 'bearish':
                    warnings.append(f"unusual bearish options flow (vol/oi={uof.get('volume_oi_ratio')}x)")

        except Exception:
            pass

        # Log warnings (awareness without blocking)
        for w in warnings:
            log.info("[Conviction] %s WARNING: %s (not blocking — awareness only)",
                     ticker, w)

        # Clamp to [0.0, 0.3] — never negative
        total = max(0.0, min(0.3, total))

        self._cache[ticker] = (total, now)
        return total

    def detail(self, ticker: str) -> str:
        """Human-readable conviction breakdown for logging."""
        s = self.score(ticker)
        if s == 0.0:
            return f"{ticker}: no alt data conviction"
        return f"{ticker}: conviction=+{s:.2f}"


# Module-level singletons
regime = MarketRegime()
conviction = TickerConviction()
