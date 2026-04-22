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
    # High-volatility / pop candidates
    'SMCI','ARM','CRWD','PANW','ZS','MNDY','CELH','RIVN','LCID','IONQ',
    'AI','BIGB','CAVA','DUOL','GRAB','SE','BABA','JD','PDD','KWEB',
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
STRATEGY       = 'vwap_reclaim'
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
GLOBAL_MAX_POSITIONS = int(os.getenv('GLOBAL_MAX_POSITIONS', 75))  # aggregate limit across ALL layers
MAX_DAILY_LOSS = float(os.getenv('MAX_DAILY_LOSS', -10000))  # kill switch: halt trading if daily P&L drops below this
PRO_MAX_DAILY_LOSS     = float(os.getenv('PRO_MAX_DAILY_LOSS', -2000))     # pro engine kill switch
POP_MAX_DAILY_LOSS     = float(os.getenv('POP_MAX_DAILY_LOSS', -2000))     # pop engine kill switch
OPTIONS_MAX_DAILY_LOSS = float(os.getenv('OPTIONS_MAX_DAILY_LOSS', -1000)) # V9: lowered from -3000 (paper trading)
ORDER_COOLDOWN = 300   # seconds between orders on same ticker
TRADE_BUDGET   = int(os.getenv('TRADE_BUDGET', 1000))  # dollars allocated per trade
OPEN_COST      = 0.0   # commission-free (Alpaca); slippage handled in OrderManager
CLOSE_COST     = 0.0

# ── Execution tuning ─────────────────────────────────────────────────
MAX_SLIPPAGE_PCT    = float(os.getenv('MAX_SLIPPAGE_PCT', 0.005))
DEFAULT_STOP_PCT    = float(os.getenv('DEFAULT_STOP_PCT', 0.005))
ORPHAN_STOP_PCT     = float(os.getenv('ORPHAN_STOP_PCT', 0.03))
ORPHAN_TARGET_PCT   = float(os.getenv('ORPHAN_TARGET_PCT', 0.05))
TRADE_START_TIME    = os.getenv('TRADE_START_TIME', '09:45')
FORCE_CLOSE_TIME    = os.getenv('FORCE_CLOSE_TIME', '15:00')
MIN_BARS_REQUIRED   = int(os.getenv('MIN_BARS_REQUIRED', 30))

# ── Credentials (read from environment / .env) ─────────────────────────────────
ALERT_EMAIL    = os.getenv('ALERT_EMAIL_TO', 'usantoshayyappa@yahoo.com')
ALPACA_API_KEY = os.getenv('APCA_API_KEY_ID')       # order execution — main VWAP strategy
ALPACA_SECRET  = os.getenv('APCA_API_SECRET_KEY')   # order execution — main VWAP strategy
TRADIER_TOKEN  = os.getenv('TRADIER_TOKEN')          # market data — required when DATA_SOURCE=tradier
PAPER_TRADING  = os.getenv('PAPER_TRADING', 'true').lower() == 'true'

# ── Pop-strategy dedicated Alpaca account ──────────────────────────────────────
# Uses a separate Alpaca account/sub-account for pop-strategy execution so that
# pop trades never touch the main VWAP strategy account capital or positions.
# If either key is missing, PopStrategyEngine falls back to PaperBroker mode.
ALPACA_POPUP_KEY          = os.getenv('APCA_POPUP_KEY')
ALPACA_POPUP_SECRET_KEY   = os.getenv('APCA_POPUP_SECRET_KEY')
POP_PAPER_TRADING         = os.getenv('POP_PAPER_TRADING', 'true').lower() == 'true'
POP_MAX_POSITIONS         = int(os.getenv('POP_MAX_POSITIONS', 15))   # max concurrent pop positions
POP_TRADE_BUDGET          = int(os.getenv('POP_TRADE_BUDGET', 2000))   # dollars per pop trade
POP_ORDER_COOLDOWN        = int(os.getenv('POP_ORDER_COOLDOWN', 300)) # seconds cooldown per ticker

# ── Pro-setups subsystem (pro_setups/) ────────────────────────────────────────
# Uses APCA_API_KEY_ID / APCA_API_SECRET_KEY (same main account as VWAP strategy).
# Execution goes through the shared AlpacaBroker via ORDER_REQ events.
# RiskAdapter is the independent risk gate; existing RiskEngine is not used for
# pro-setup entries.
PRO_MAX_POSITIONS  = int(os.getenv('PRO_MAX_POSITIONS',   15))   # max concurrent pro positions
PRO_TRADE_BUDGET   = int(os.getenv('PRO_TRADE_BUDGET',  1000))   # dollars allocated per pro trade
PRO_ORDER_COOLDOWN = int(os.getenv('PRO_ORDER_COOLDOWN',  300))  # seconds cooldown per ticker

# ── Options engine (T3.7) — dedicated Alpaca account ────────────────────────────
# Uses separate APCA_OPTIONS_KEY / APCA_OPTIONS_SECRET credentials for options trading.
# Independent risk gate (OptionsRiskGate); does NOT route through existing RiskEngine.
# Starting budget: $20,000 total; $2,000 per trade max; 5 concurrent positions.
ALPACA_OPTIONS_KEY     = os.getenv('APCA_OPTIONS_KEY')
ALPACA_OPTIONS_SECRET  = os.getenv('APCA_OPTIONS_SECRET')
OPTIONS_PAPER_TRADING  = os.getenv('OPTIONS_PAPER_TRADING', 'true').lower() == 'true'
OPTIONS_MAX_POSITIONS  = int(os.getenv('OPTIONS_MAX_POSITIONS', 5))
OPTIONS_TRADE_BUDGET   = int(os.getenv('OPTIONS_TRADE_BUDGET', 2000))     # per trade
OPTIONS_TOTAL_BUDGET   = int(os.getenv('OPTIONS_TOTAL_BUDGET', 20000))    # $20K ceiling
OPTIONS_ORDER_COOLDOWN = int(os.getenv('OPTIONS_ORDER_COOLDOWN', 300))    # seconds per ticker
OPTIONS_MIN_DTE        = int(os.getenv('OPTIONS_MIN_DTE', 20))             # days to expiry
OPTIONS_MAX_DTE        = int(os.getenv('OPTIONS_MAX_DTE', 45))             # days to expiry
OPTIONS_LEAPS_DTE      = int(os.getenv('OPTIONS_LEAPS_DTE', 365))          # LEAPS leg DTE
# Exit management
OPTIONS_PROFIT_TARGET_CREDIT = float(os.getenv('OPTIONS_PROFIT_TARGET_CREDIT', 0.50))  # close credit at 50% of max reward
OPTIONS_PROFIT_TARGET_DEBIT  = float(os.getenv('OPTIONS_PROFIT_TARGET_DEBIT',  1.00))  # close debit at 100% of max reward
OPTIONS_STOP_LOSS_FRACTION   = float(os.getenv('OPTIONS_STOP_LOSS_FRACTION',   0.80))  # cut at 80% of max risk
OPTIONS_DTE_CLOSE            = int(os.getenv('OPTIONS_DTE_CLOSE', 7))                  # close at 7 DTE

# ── Portfolio-level risk limits ──────────────────────────────────────────────
MAX_INTRADAY_DRAWDOWN = float(os.getenv('MAX_INTRADAY_DRAWDOWN', -5000))
MAX_NOTIONAL_EXPOSURE = float(os.getenv('MAX_NOTIONAL_EXPOSURE', 100000))
MAX_PORTFOLIO_DELTA   = float(os.getenv('MAX_PORTFOLIO_DELTA', 5.0))
MAX_PORTFOLIO_GAMMA   = float(os.getenv('MAX_PORTFOLIO_GAMMA', 1.0))

# ── External data APIs ────────────────────────────────────────────────────────
BENZINGA_API_KEY    = os.getenv('BENZINGA_API_KEY') or os.getenv('BENZENGA_API_KEY', '')
STOCKTWITS_TOKEN    = os.getenv('STOCKTWITS_TOKEN', '')  # optional — public API works without token

# ── Database (TimescaleDB) ────────────────────────────────────────────────────
# Set DB_ENABLED=false to run without the database (all events still flow normally,
# just not persisted to TimescaleDB).
DB_ENABLED     = os.getenv('DB_ENABLED', 'true').lower() == 'true'
DATABASE_URL   = os.getenv('DATABASE_URL', 'postgresql://trading:trading_secret@localhost:5432/tradinghub')

# ── Data source ────────────────────────────────────────────────────────────────
# 'tradier' — Tradier REST API (recommended; commission-free data, no SDK)
# 'alpaca'  — Alpaca Data API (uses same key/secret as order execution)
DATA_SOURCE = os.getenv('DATA_SOURCE', 'tradier')

# ── Broker ─────────────────────────────────────────────────────────────────────
# 'alpaca' — live or paper execution via Alpaca TradingClient
# 'paper'  — local simulation; fills every order instantly, no API needed
BROKER = os.getenv('BROKER', 'alpaca')

# ── Tradier trading (secondary broker) ────────────────────────────────────
TRADIER_TRADING_TOKEN  = os.getenv('TRADIER_TRADING_TOKEN', '')     # trading API token
TRADIER_ACCOUNT_ID     = os.getenv('TRADIER_ACCOUNT_ID', '')        # account number
TRADIER_SANDBOX        = os.getenv('TRADIER_SANDBOX', 'true').lower() == 'true'
TRADIER_SANDBOX_TOKEN  = os.getenv('TRADIER_SANDBOX_TOKEN', '')     # sandbox token

# ── Smart routing ─────────────────────────────────────────────────────────
# 'smart' — route to best broker based on health/availability
# 'alpaca' — always use Alpaca (current behavior)
# 'tradier' — always use Tradier
BROKER_MODE = os.getenv('BROKER_MODE', 'alpaca')  # 'alpaca', 'tradier', or 'smart'

# ── Alternative data sources ──────────────────────────────────────────────
FRED_API_KEY        = os.getenv('FRED_API_KEY', '')
ALPHA_VANTAGE_KEY   = os.getenv('ALPHA_VANTAGE_KEY', '')
REDDIT_CLIENT_ID    = os.getenv('REDDIT_CLIENT_ID', '')
REDDIT_CLIENT_SECRET = os.getenv('REDDIT_CLIENT_SECRET', '')
REDDIT_USERNAME     = os.getenv('REDDIT_USERNAME', '')
REDDIT_PASSWORD     = os.getenv('REDDIT_PASSWORD', '')
POLYGON_API_KEY      = os.getenv('POLYGON_KEY', os.getenv('POLYGON_API_KEY', ''))
# UNUSUAL_WHALES_KEY — disabled for now (no account)
# REDDIT_* — disabled for now (signup pending)
SEC_EDGAR_EMAIL      = os.getenv('SEC_EDGAR_EMAIL', os.getenv('ALERT_EMAIL_TO', 'tradinghub@example.com'))


# ══════════════════════════════════════════════════════════════════════════════
# V7 P4-4: Configuration Validation
# ══════════════════════════════════════════════════════════════════════════════
# Fail-fast on startup if config values are invalid.
# Prevents silent propagation of bad env vars into trading logic.

_CONFIG_ERRORS = []


def _validate_positive(name, value):
    if value <= 0:
        _CONFIG_ERRORS.append(f"{name}={value} must be positive")


def _validate_non_negative(name, value):
    if value < 0:
        _CONFIG_ERRORS.append(f"{name}={value} must be non-negative")


def _validate_range(name, value, lo, hi):
    if not (lo <= value <= hi):
        _CONFIG_ERRORS.append(f"{name}={value} must be in [{lo}, {hi}]")


def _validate_choice(name, value, choices):
    if value not in choices:
        _CONFIG_ERRORS.append(f"{name}='{value}' must be one of {choices}")


# ── Validate risk parameters ──────────────────────────────────────────────
_validate_positive('TRADE_BUDGET', TRADE_BUDGET)
_validate_positive('MAX_POSITIONS', MAX_POSITIONS)
_validate_positive('GLOBAL_MAX_POSITIONS', GLOBAL_MAX_POSITIONS)
_validate_positive('ORDER_COOLDOWN', ORDER_COOLDOWN)
_validate_range('MAX_SLIPPAGE_PCT', MAX_SLIPPAGE_PCT, 0.0, 0.10)
_validate_range('DEFAULT_STOP_PCT', DEFAULT_STOP_PCT, 0.0, 0.50)
_validate_positive('MIN_BARS_REQUIRED', MIN_BARS_REQUIRED)

# ── Validate kill switches (must be negative) ────────────────────────────
if MAX_DAILY_LOSS >= 0:
    _CONFIG_ERRORS.append(f"MAX_DAILY_LOSS={MAX_DAILY_LOSS} must be negative")
if PRO_MAX_DAILY_LOSS >= 0:
    _CONFIG_ERRORS.append(f"PRO_MAX_DAILY_LOSS={PRO_MAX_DAILY_LOSS} must be negative")
if POP_MAX_DAILY_LOSS >= 0:
    _CONFIG_ERRORS.append(f"POP_MAX_DAILY_LOSS={POP_MAX_DAILY_LOSS} must be negative")
if OPTIONS_MAX_DAILY_LOSS >= 0:
    _CONFIG_ERRORS.append(f"OPTIONS_MAX_DAILY_LOSS={OPTIONS_MAX_DAILY_LOSS} must be negative")

# ── Validate per-engine budgets ────────────────────────────────────────────
_validate_positive('POP_TRADE_BUDGET', POP_TRADE_BUDGET)
_validate_positive('PRO_TRADE_BUDGET', PRO_TRADE_BUDGET)
_validate_positive('OPTIONS_TRADE_BUDGET', OPTIONS_TRADE_BUDGET)
_validate_positive('OPTIONS_TOTAL_BUDGET', OPTIONS_TOTAL_BUDGET)
_validate_positive('POP_MAX_POSITIONS', POP_MAX_POSITIONS)
_validate_positive('PRO_MAX_POSITIONS', PRO_MAX_POSITIONS)
_validate_positive('OPTIONS_MAX_POSITIONS', OPTIONS_MAX_POSITIONS)

# ── Validate options DTE ──────────────────────────────────────────────────
if OPTIONS_MIN_DTE >= OPTIONS_MAX_DTE:
    _CONFIG_ERRORS.append(
        f"OPTIONS_MIN_DTE={OPTIONS_MIN_DTE} must be < OPTIONS_MAX_DTE={OPTIONS_MAX_DTE}")

# ── Validate broker/data source choices ────────────────────────────────────
_validate_choice('BROKER', BROKER, ('alpaca', 'paper'))
_validate_choice('DATA_SOURCE', DATA_SOURCE, ('tradier', 'alpaca'))
_validate_choice('BROKER_MODE', BROKER_MODE, ('alpaca', 'tradier', 'smart'))

# ── Validate portfolio limits ──────────────────────────────────────────────
_validate_positive('MAX_NOTIONAL_EXPOSURE', MAX_NOTIONAL_EXPOSURE)
_validate_positive('MAX_PORTFOLIO_DELTA', MAX_PORTFOLIO_DELTA)
_validate_positive('MAX_PORTFOLIO_GAMMA', MAX_PORTFOLIO_GAMMA)
if MAX_INTRADAY_DRAWDOWN >= 0:
    _CONFIG_ERRORS.append(f"MAX_INTRADAY_DRAWDOWN={MAX_INTRADAY_DRAWDOWN} must be negative")

# ── Report errors ─────────────────────────────────────────────────────────
if _CONFIG_ERRORS:
    import logging as _logging
    _log = _logging.getLogger('config')
    for err in _CONFIG_ERRORS:
        _log.error("[CONFIG VALIDATION] %s", err)
    # Fail-fast: raise on startup so bad config never reaches trading logic
    raise ValueError(
        f"Configuration validation failed ({len(_CONFIG_ERRORS)} error(s)):\n"
        + "\n".join(f"  - {e}" for e in _CONFIG_ERRORS)
    )
