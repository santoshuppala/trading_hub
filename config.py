"""
Shared configuration for both the headless launcher (run_monitor.py)
and the Streamlit UI (realtime_gui.py).
Change values here and both modes pick them up automatically.
"""
import os

# ── Watchlist ──────────────────────────────────────────────────────────────────
TICKERS = [
    # Mega-cap tech
    'AAPL','MSFT','GOOGL','AMZN','NVDA','TSLA','META','AVGO','ASML','ORCL',
    # Semiconductors
    'AMD','MU','QCOM','AMAT','LRCX','TXN','ADI','MRVL','KLAC','NXPI','ON','SWKS',
    # Software / cloud
    'ADBE','CRM','INTU','SNPS','CDNS','NOW','WDAY','DDOG','SNOW','NET','ZS','OKTA',
    'TEAM','MDB','GTLB','PATH','HUBS','ZM','DOCU',
    # Internet / e-commerce
    'NFLX','UBER','LYFT','ABNB','DASH','SHOP','ETSY','EBAY','PINS','SNAP',
    # Hardware / devices
    'CSCO','IBM','HPQ','DELL','NTAP','STX','WDC',
    # Fintech / payments
    'V','MA','PYPL','SQ','AFRM','SOFI','UPST','NU',
    # Crypto-adjacent
    'COIN','HOOD','MSTR','RIOT','CLSK','MARA','HUT',
    # High-growth / speculative
    'PLTR','RBLX','ROKU','TWLO','BILL','SMAR',
    # Financials
    'JPM','BAC','WFC','GS','MS','C','AXP','SCHW','BLK','KKR','APO','BX','ICE','CME',
    # Energy
    'XOM','CVX','COP','EOG','SLB','HAL','OXY','MPC','PSX','VLO',
    # Healthcare / biotech
    'UNH','LLY','JNJ','ABBV','MRK','PFE','AMGN','GILD','REGN','VRTX',
    'DXCM','ISRG','IDXX','MTD','VEEV','INCY',
    # Consumer discretionary
    'AMZN','TSLA','HD','MCD','SBUX','NKE','TGT','LOW','BKNG','CMG','YUM',
    # Consumer staples
    'WMT','COST','PG','KO','PEP','MDLZ','CL',
    # Industrials
    'CAT','DE','HON','GE','RTX','LMT','NOC','BA','UPS','FDX',
    # ETFs (market + sector)
    'SPY','QQQ','IWM','DIA',
    'XLK','XLF','XLE','XLV','XLY','XLI','XLC','XLRE','XLB','XLU',
    'ARKK','SOXS','SOXL','TQQQ','SQQQ',
]

# Deduplicate while preserving order
TICKERS = list(dict.fromkeys(TICKERS))

# ── Strategy ───────────────────────────────────────────────────────────────────
STRATEGY       = 'confirmed_crossover'
STRATEGY_PARAMS = {
    'rsi_period':     14,
    'atr_period':     14,
    'atr_multiplier': 2.0,
    'volume_factor':  1.5,
    'min_stop_pct':   0.05,
    'rsi_overbought': 70,
}

# ── Risk / order settings ──────────────────────────────────────────────────────
MAX_POSITIONS  = 5
ORDER_COOLDOWN = 300   # seconds between orders on same ticker
TRADE_BUDGET   = int(os.getenv('TRADE_BUDGET', 1000))  # dollars allocated per trade
OPEN_COST      = 0.0   # commission-free (Alpaca); slippage handled in OrderManager
CLOSE_COST     = 0.0

# ── Credentials (read from environment / .env) ─────────────────────────────────
ALERT_EMAIL    = os.getenv('ALERT_EMAIL_TO', 'usantoshayyappa@yahoo.com')
ALPACA_API_KEY = os.getenv('APCA_API_KEY_ID')       # order execution (always Alpaca)
ALPACA_SECRET  = os.getenv('APCA_API_SECRET_KEY')   # order execution (always Alpaca)
TRADIER_TOKEN  = os.getenv('TRADIER_TOKEN')          # market data — required when DATA_SOURCE=tradier
PAPER_TRADING  = os.getenv('PAPER_TRADING', 'true').lower() == 'true'

# ── Data source ────────────────────────────────────────────────────────────────
# 'tradier' — Tradier REST API (recommended; commission-free data, no SDK)
# 'alpaca'  — Alpaca Data API (uses same key/secret as order execution)
DATA_SOURCE = os.getenv('DATA_SOURCE', 'tradier')

# ── Broker ─────────────────────────────────────────────────────────────────────
# 'alpaca' — live or paper execution via Alpaca TradingClient
# 'paper'  — local simulation; fills every order instantly, no API needed
BROKER = os.getenv('BROKER', 'alpaca')
