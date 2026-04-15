"""
Advanced risk sizing — beta-adjusted, correlation-aware, volatility-scaled.

Provides position sizing adjustments that the basic RiskEngine doesn't cover:
  1. Beta-adjusted sizing: TQQQ (beta 3.0) gets 1/3 the position of SPY (beta 1.0)
  2. Correlation risk: warn/block if too many correlated positions
  3. Volatility-adjusted sizing: scale position size inversely with ATR

Usage:
    from monitor.risk_sizing import RiskSizer
    sizer = RiskSizer()
    adjusted_qty = sizer.adjust_size(ticker='TQQQ', base_qty=10, price=50.0)
    correlation_ok = sizer.check_correlation(ticker='AMD', current_positions={'NVDA', 'AVGO'})
"""
import logging
import os
import threading
import time
from typing import Dict, Set, Optional
from dataclasses import dataclass

log = logging.getLogger(__name__)

# ── Configurable limits ──────────────────────────────────────────────────────
MAX_CORRELATED_POSITIONS = int(os.getenv('MAX_CORRELATED_POSITIONS', 3))
TARGET_RISK_PER_TRADE    = float(os.getenv('TARGET_RISK_PER_TRADE', 100))  # $ risk per trade
MAX_BETA_EXPOSURE        = float(os.getenv('MAX_BETA_EXPOSURE', 200000))   # beta-weighted $ exposure


# ── Correlation groups (tickers that move together) ──────────────────────────
# More granular than GICS sectors — these are empirically correlated
CORRELATION_GROUPS = {
    'mega_tech':     {'AAPL', 'MSFT', 'GOOGL', 'META', 'AMZN'},
    'semiconductors': {'NVDA', 'AMD', 'AVGO', 'MU', 'QCOM', 'AMAT', 'LRCX', 'TXN',
                       'ADI', 'MRVL', 'KLAC', 'NXPI', 'ON', 'SWKS', 'SMCI', 'ARM'},
    'cloud_saas':    {'CRM', 'NOW', 'WDAY', 'DDOG', 'SNOW', 'NET', 'ZS', 'OKTA',
                      'TEAM', 'MDB', 'GTLB', 'PATH', 'HUBS', 'BILL', 'SMAR'},
    'ecommerce':     {'AMZN', 'SHOP', 'ETSY', 'EBAY', 'BABA', 'JD', 'PDD', 'SE'},
    'social_media':  {'META', 'SNAP', 'PINS', 'RBLX', 'ROKU'},
    'ev_auto':       {'TSLA', 'RIVN', 'LCID', 'NIO'},
    'crypto_adj':    {'COIN', 'HOOD', 'MSTR', 'RIOT', 'CLSK', 'MARA', 'HUT'},
    'fintech':       {'PYPL', 'SQ', 'AFRM', 'SOFI', 'UPST', 'NU'},
    'big_banks':     {'JPM', 'BAC', 'WFC', 'GS', 'MS', 'C'},
    'oil_gas':       {'XOM', 'CVX', 'COP', 'EOG', 'SLB', 'HAL', 'OXY', 'MPC', 'PSX', 'VLO'},
    'biotech':       {'AMGN', 'GILD', 'REGN', 'VRTX', 'INCY'},
    'defense':       {'RTX', 'LMT', 'NOC', 'BA', 'GE'},
    'leveraged_etf': {'TQQQ', 'SQQQ', 'SOXL', 'SOXS'},
    'index_etf':     {'SPY', 'QQQ', 'IWM', 'DIA'},
}

# Build reverse lookup: ticker → list of groups
_TICKER_GROUPS: Dict[str, list] = {}
for group_name, tickers in CORRELATION_GROUPS.items():
    for t in tickers:
        _TICKER_GROUPS.setdefault(t, []).append(group_name)

# ── Beta cache (static defaults, updated from Yahoo Finance) ─────────────────
# Leveraged ETFs have known betas; stocks default to 1.0 until fetched
_STATIC_BETAS = {
    'TQQQ': 3.0, 'SQQQ': -3.0, 'SOXL': 3.0, 'SOXS': -3.0,
    'SPY': 1.0, 'QQQ': 1.15, 'IWM': 1.2, 'DIA': 0.95,
    'ARKK': 1.5,
}


@dataclass
class SizingResult:
    """Result of position size adjustment."""
    original_qty: int
    adjusted_qty: int
    beta: float
    atr_risk: float           # $ risk per share (ATR-based)
    adjustment_reason: str    # why size was changed


class RiskSizer:
    """Advanced position sizing with beta, correlation, and volatility adjustments."""

    def __init__(self):
        self._beta_cache: Dict[str, float] = dict(_STATIC_BETAS)
        # Pre-seed cache times for static betas so they're always valid
        self._beta_cache_time: Dict[str, float] = {k: time.time() for k in _STATIC_BETAS}
        self._lock = threading.Lock()

    def get_beta(self, ticker: str) -> float:
        """Get beta for a ticker. Uses cache, falls back to Yahoo Finance."""
        with self._lock:
            # Check cache (valid for 24 hours)
            if ticker in self._beta_cache:
                cache_time = self._beta_cache_time.get(ticker, 0)
                if time.time() - cache_time < 86400:  # 24 hours
                    return self._beta_cache[ticker]

        # Try Yahoo Finance
        try:
            from data_sources.yahoo_finance import YahooFinanceSource
            yf = YahooFinanceSource()
            fund = yf.fundamentals(ticker)
            if fund and fund.beta and fund.beta != 0:
                with self._lock:
                    self._beta_cache[ticker] = fund.beta
                    self._beta_cache_time[ticker] = time.time()
                return fund.beta
        except Exception:
            pass

        return 1.0  # default beta

    def adjust_size(
        self,
        ticker: str,
        base_qty: int,
        price: float,
        atr_value: float = 0.0,
        trade_budget: float = 1000.0,
    ) -> SizingResult:
        """Adjust position size based on beta and volatility.

        Beta adjustment: qty = base_qty / abs(beta)
          - TQQQ (beta 3.0): 10 shares → 3 shares (same market exposure)
          - SPY (beta 1.0): 10 shares → 10 shares (no change)

        Volatility adjustment: qty = target_risk / (ATR * price_factor)
          - High ATR stock: fewer shares (same $ risk)
          - Low ATR stock: more shares (same $ risk)

        Returns the lower of beta-adjusted and volatility-adjusted.
        """
        beta = self.get_beta(ticker)
        abs_beta = max(abs(beta), 0.3)  # floor at 0.3 to prevent huge positions

        # Beta-adjusted quantity
        beta_qty = max(1, int(base_qty / abs_beta))

        # Volatility-adjusted quantity (if ATR available)
        vol_qty = base_qty
        atr_risk = 0.0
        if atr_value > 0 and price > 0:
            # Risk per share = 2 * ATR (typical stop distance)
            atr_risk = atr_value * 2.0
            if atr_risk > 0:
                vol_qty = max(1, int(TARGET_RISK_PER_TRADE / atr_risk))

        # Take the more conservative (smaller) quantity
        adjusted_qty = min(beta_qty, vol_qty)

        # Cap at budget
        max_by_budget = int(trade_budget / price) if price > 0 else base_qty
        adjusted_qty = min(adjusted_qty, max_by_budget)
        adjusted_qty = max(1, adjusted_qty)

        reason_parts = []
        if adjusted_qty != base_qty:
            if beta_qty < base_qty:
                reason_parts.append(f"beta={beta:.2f}")
            if vol_qty < base_qty:
                reason_parts.append(f"atr_risk=${atr_risk:.2f}")

        return SizingResult(
            original_qty=base_qty,
            adjusted_qty=adjusted_qty,
            beta=beta,
            atr_risk=atr_risk,
            adjustment_reason=', '.join(reason_parts) if reason_parts else 'no_adjustment',
        )

    def check_correlation(self, ticker: str, current_positions: Set[str],
                          news_context: dict = None) -> tuple:
        """Check if adding ticker would create too much correlation risk.

        News-aware correlation:
          - If the catalyst is TICKER-SPECIFIC (e.g., "AAPL beats earnings"),
            allow entry even if correlated positions are held.
          - If the catalyst is SECTOR-WIDE (e.g., "tech sells off on rates"),
            enforce the correlation limit strictly.
          - If no news context, default to strict enforcement.

        Args:
            ticker: ticker to check
            current_positions: set of currently held tickers
            news_context: optional dict with:
                'headlines_1h': int — headline count for this ticker
                'sentiment_delta': float — sentiment change
                'is_ticker_specific': bool — True if catalyst is ticker-only

        Returns (allowed: bool, reason: str)
        """
        ticker_groups = _TICKER_GROUPS.get(ticker, [])

        if not ticker_groups:
            return True, 'no_correlation_group'

        for group in ticker_groups:
            group_tickers = CORRELATION_GROUPS[group]
            overlap = current_positions & group_tickers

            if len(overlap) >= MAX_CORRELATED_POSITIONS:
                # Check if this is a ticker-specific catalyst
                if news_context and self._is_ticker_specific_catalyst(
                    ticker, group, news_context
                ):
                    log.info(
                        "[RiskSizer] Correlation override for %s: ticker-specific catalyst "
                        "(headlines=%d, sentiment_delta=%.2f) — allowing despite %d in '%s'",
                        ticker, news_context.get('headlines_1h', 0),
                        news_context.get('sentiment_delta', 0), len(overlap), group,
                    )
                    return True, f'correlation_override: ticker_specific_catalyst_for_{ticker}'

                return False, (
                    f"correlation_block: {ticker} in group '{group}' "
                    f"with {len(overlap)} existing ({', '.join(sorted(overlap))}), "
                    f"max={MAX_CORRELATED_POSITIONS}"
                )

        return True, 'correlation_ok'

    def _is_ticker_specific_catalyst(self, ticker: str, group: str,
                                      news_context: dict) -> bool:
        """Determine if the catalyst is ticker-specific vs sector-wide.

        Heuristics:
          1. If caller already marked it as ticker-specific → trust it
          2. If this ticker has headlines but group peers don't → ticker-specific
          3. If sentiment_delta is strong (>0.3) → likely a direct catalyst
          4. If headline count > 3 in 1 hour → significant ticker attention
        """
        # Explicit flag from caller
        if news_context.get('is_ticker_specific'):
            return True

        headlines = news_context.get('headlines_1h', 0)
        sentiment_delta = abs(news_context.get('sentiment_delta', 0))

        # Strong sentiment shift + multiple headlines = ticker-specific catalyst
        if headlines >= 3 and sentiment_delta >= 0.3:
            return True

        # Check if peer tickers in the group also have headlines
        # (if they do, it's a sector-wide event, not ticker-specific)
        peer_headlines = news_context.get('peer_headlines', {})
        if peer_headlines:
            group_tickers = CORRELATION_GROUPS.get(group, set())
            peers_with_news = sum(
                1 for peer in group_tickers
                if peer != ticker and peer_headlines.get(peer, 0) >= 2
            )
            # If most peers also have news → sector-wide
            if peers_with_news >= 2:
                return False
            # Only this ticker has news → ticker-specific
            if peers_with_news == 0 and headlines >= 2:
                return True

        # Moderate catalyst (some headlines, some sentiment) — allow with caution
        if headlines >= 2 and sentiment_delta >= 0.2:
            return True

        return False

    def beta_weighted_exposure(self, positions: Dict[str, dict]) -> float:
        """Calculate total beta-weighted notional exposure.

        $10K in TQQQ (beta 3) = $30K beta-weighted exposure.
        $10K in SPY (beta 1) = $10K beta-weighted exposure.
        """
        total = 0.0
        for ticker, pos in positions.items():
            qty = pos.get('qty', pos.get('quantity', 0))
            price = pos.get('current_price', pos.get('entry_price', 0))
            beta = self.get_beta(ticker)
            total += abs(qty * price * beta)
        return round(total, 2)

    def check_beta_exposure(self, positions: Dict[str, dict],
                            new_ticker: str, new_qty: int, new_price: float) -> tuple:
        """Check if adding a new position would exceed beta-weighted exposure limit.

        Returns (allowed: bool, reason: str, current_exposure: float)
        """
        current = self.beta_weighted_exposure(positions)
        new_beta = self.get_beta(new_ticker)
        additional = abs(new_qty * new_price * new_beta)

        if current + additional > MAX_BETA_EXPOSURE:
            return False, (
                f"beta_exposure_block: current ${current:,.0f} + "
                f"new ${additional:,.0f} (beta={new_beta:.1f}) = "
                f"${current + additional:,.0f} > limit ${MAX_BETA_EXPOSURE:,.0f}"
            ), current

        return True, 'beta_exposure_ok', current
