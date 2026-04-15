"""
sector_map.py — GICS sector classification for the trading universe.

Provides ticker-to-sector mapping and sector concentration counting
for use by risk gates (RiskAdapter, PopExecutor).
"""
from __future__ import annotations

from typing import Dict, Set

# ── Ticker → GICS sector mapping ─────────────────────────────────────────────
SECTOR_MAP: Dict[str, str] = {
    # Mega-cap tech
    'AAPL': 'Technology',
    'MSFT': 'Technology',
    'GOOGL': 'Communication Services',
    'AMZN': 'Consumer Discretionary',
    'NVDA': 'Technology',
    'TSLA': 'Consumer Discretionary',
    'META': 'Communication Services',
    'AVGO': 'Technology',
    'ASML': 'Technology',
    'ORCL': 'Technology',

    # Semiconductors
    'AMD': 'Technology',
    'MU': 'Technology',
    'QCOM': 'Technology',
    'AMAT': 'Technology',
    'LRCX': 'Technology',
    'TXN': 'Technology',
    'ADI': 'Technology',
    'MRVL': 'Technology',
    'KLAC': 'Technology',
    'NXPI': 'Technology',
    'ON': 'Technology',
    'SWKS': 'Technology',

    # Software / cloud
    'ADBE': 'Technology',
    'CRM': 'Technology',
    'INTU': 'Technology',
    'SNPS': 'Technology',
    'CDNS': 'Technology',
    'NOW': 'Technology',
    'WDAY': 'Technology',
    'DDOG': 'Technology',
    'SNOW': 'Technology',
    'NET': 'Technology',
    'ZS': 'Technology',
    'OKTA': 'Technology',
    'TEAM': 'Technology',
    'MDB': 'Technology',
    'GTLB': 'Technology',
    'PATH': 'Technology',
    'HUBS': 'Technology',
    'ZM': 'Technology',
    'DOCU': 'Technology',

    # Internet / e-commerce
    'NFLX': 'Communication Services',
    'UBER': 'Technology',
    'LYFT': 'Technology',
    'ABNB': 'Consumer Discretionary',
    'DASH': 'Technology',
    'SHOP': 'Technology',
    'ETSY': 'Consumer Discretionary',
    'EBAY': 'Consumer Discretionary',
    'PINS': 'Communication Services',
    'SNAP': 'Communication Services',

    # Hardware / devices
    'CSCO': 'Technology',
    'IBM': 'Technology',
    'HPQ': 'Technology',
    'DELL': 'Technology',
    'NTAP': 'Technology',
    'STX': 'Technology',
    'WDC': 'Technology',

    # Fintech / payments
    'V': 'Financials',
    'MA': 'Financials',
    'PYPL': 'Financials',
    'SQ': 'Financials',
    'AFRM': 'Financials',
    'SOFI': 'Financials',
    'UPST': 'Financials',
    'NU': 'Financials',

    # Crypto-adjacent (classified as Financials)
    'COIN': 'Financials',
    'HOOD': 'Financials',
    'MSTR': 'Financials',
    'RIOT': 'Financials',
    'CLSK': 'Financials',
    'MARA': 'Financials',
    'HUT': 'Financials',

    # High-growth / speculative
    'PLTR': 'Technology',
    'RBLX': 'Communication Services',
    'ROKU': 'Communication Services',
    'TWLO': 'Technology',
    'BILL': 'Technology',
    'SMAR': 'Technology',

    # High-volatility / pop candidates
    'SMCI': 'Technology',
    'ARM': 'Technology',
    'CRWD': 'Technology',
    'PANW': 'Technology',
    'MNDY': 'Technology',
    'CELH': 'Consumer Staples',
    'RIVN': 'Consumer Discretionary',
    'LCID': 'Consumer Discretionary',
    'IONQ': 'Technology',
    'AI': 'Technology',
    'BIGB': 'Unknown',
    'CAVA': 'Consumer Discretionary',
    'DUOL': 'Technology',
    'GRAB': 'Technology',
    'SE': 'Technology',
    'BABA': 'Consumer Discretionary',
    'JD': 'Consumer Discretionary',
    'PDD': 'Consumer Discretionary',

    # Financials
    'JPM': 'Financials',
    'BAC': 'Financials',
    'WFC': 'Financials',
    'GS': 'Financials',
    'MS': 'Financials',
    'C': 'Financials',
    'AXP': 'Financials',
    'SCHW': 'Financials',
    'BLK': 'Financials',
    'KKR': 'Financials',
    'APO': 'Financials',
    'BX': 'Financials',
    'ICE': 'Financials',
    'CME': 'Financials',

    # Energy
    'XOM': 'Energy',
    'CVX': 'Energy',
    'COP': 'Energy',
    'EOG': 'Energy',
    'SLB': 'Energy',
    'HAL': 'Energy',
    'OXY': 'Energy',
    'MPC': 'Energy',
    'PSX': 'Energy',
    'VLO': 'Energy',

    # Healthcare / biotech
    'UNH': 'Healthcare',
    'LLY': 'Healthcare',
    'JNJ': 'Healthcare',
    'ABBV': 'Healthcare',
    'MRK': 'Healthcare',
    'PFE': 'Healthcare',
    'AMGN': 'Healthcare',
    'GILD': 'Healthcare',
    'REGN': 'Healthcare',
    'VRTX': 'Healthcare',
    'DXCM': 'Healthcare',
    'ISRG': 'Healthcare',
    'IDXX': 'Healthcare',
    'MTD': 'Healthcare',
    'VEEV': 'Healthcare',
    'INCY': 'Healthcare',

    # Consumer discretionary
    'HD': 'Consumer Discretionary',
    'MCD': 'Consumer Discretionary',
    'SBUX': 'Consumer Discretionary',
    'NKE': 'Consumer Discretionary',
    'TGT': 'Consumer Discretionary',
    'LOW': 'Consumer Discretionary',
    'BKNG': 'Consumer Discretionary',
    'CMG': 'Consumer Discretionary',
    'YUM': 'Consumer Discretionary',
    'NIO': 'Consumer Discretionary',

    # Consumer staples
    'WMT': 'Consumer Staples',
    'COST': 'Consumer Staples',
    'PG': 'Consumer Staples',
    'KO': 'Consumer Staples',
    'PEP': 'Consumer Staples',
    'MDLZ': 'Consumer Staples',
    'CL': 'Consumer Staples',

    # Industrials
    'CAT': 'Industrials',
    'DE': 'Industrials',
    'HON': 'Industrials',
    'GE': 'Industrials',
    'RTX': 'Industrials',
    'LMT': 'Industrials',
    'NOC': 'Industrials',
    'BA': 'Industrials',
    'UPS': 'Industrials',
    'FDX': 'Industrials',
    'BE': 'Industrials',

    # ETFs (market + sector + leveraged)
    'SPY': 'ETF',
    'QQQ': 'ETF',
    'IWM': 'ETF',
    'DIA': 'ETF',
    'XLK': 'ETF',
    'XLF': 'ETF',
    'XLE': 'ETF',
    'XLV': 'ETF',
    'XLY': 'ETF',
    'XLI': 'ETF',
    'XLC': 'ETF',
    'XLRE': 'ETF',
    'XLB': 'ETF',
    'XLU': 'ETF',
    'ARKK': 'ETF',
    'SOXS': 'ETF',
    'SOXL': 'ETF',
    'TQQQ': 'ETF',
    'SQQQ': 'ETF',
    'KWEB': 'ETF',
}


def get_sector(ticker: str) -> str:
    """Return the GICS sector for *ticker*, or 'Unknown' if unmapped."""
    return SECTOR_MAP.get(ticker, 'Unknown')


def is_etf(ticker: str) -> bool:
    """Return True if ticker is classified as an ETF."""
    return SECTOR_MAP.get(ticker) == 'ETF'


# ── ETF-specific RVOL thresholds ─────────────────────────────────────────
# ETFs are the most liquid instruments — they almost never reach 2x RVOL.
# A 1.3% move on QQQ at 0.8x RVOL is more significant than a 1.3% move
# on a mid-cap stock at 0.8x RVOL, because ETF volume is institutional.
ETF_RVOL_MIN = 0.7   # vs 2.0 for individual stocks


def count_sector_positions(held_tickers: Set[str]) -> Dict[str, int]:
    """
    Given a set of currently-held ticker strings, return a dict of
    {sector: count} showing how many positions are in each sector.
    """
    counts: Dict[str, int] = {}
    for ticker in held_tickers:
        sector = get_sector(ticker)
        counts[sector] = counts.get(sector, 0) + 1
    return counts
