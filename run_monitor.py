"""
Standalone monitor launcher — runs without Streamlit UI.
Scheduled daily at 6:00 AM PST (9:00 AM ET, 30 min before open).
Stops automatically at 3:15 PM ET after all positions are force-closed.
"""
import os
import sys
import time
import logging
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))
from realtime_main import RealTimeMonitor

# ── Configuration ─────────────────────────────────────────────────────────────
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

STRATEGY      = 'confirmed_crossover'
OPEN_COST     = 0.0    # Alpaca is commission-free; slippage modelled in RealTimeMonitor
CLOSE_COST    = 0.0
MAX_POSITIONS = 5
ORDER_COOLDOWN = 300  # seconds between orders on same ticker

STRATEGY_PARAMS = {
    'rsi_period':      14,
    'atr_period':      14,
    'atr_multiplier':  2.0,
    'volume_factor':   1.5,
    'min_stop_pct':    0.05,
    'rsi_overbought':  70,
}

# Set these via environment variables or fill in directly
ALERT_EMAIL     = os.getenv('ALERT_EMAIL_TO', 'usantoshayyappa@yahoo.com')
ALPACA_API_KEY  = os.getenv('APCA_API_KEY_ID')
ALPACA_SECRET   = os.getenv('APCA_API_SECRET_KEY')
PAPER_TRADING   = os.getenv('PAPER_TRADING', 'true').lower() == 'true'

# ── Logging ───────────────────────────────────────────────────────────────────
log_dir = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"monitor_{datetime.now().strftime('%Y-%m-%d')}.log")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info(f"Starting monitor | Strategy: {STRATEGY} | Tickers: {len(TICKERS)} | Paper: {PAPER_TRADING}")

    monitor = RealTimeMonitor(
        tickers=TICKERS,
        strategy_name=STRATEGY,
        strategy_params=STRATEGY_PARAMS,
        open_cost=OPEN_COST,
        close_cost=CLOSE_COST,
        alert_email=ALERT_EMAIL,
        alpaca_api_key=ALPACA_API_KEY,
        alpaca_secret_key=ALPACA_SECRET,
        paper=PAPER_TRADING,
        max_positions=MAX_POSITIONS,
        order_cooldown=ORDER_COOLDOWN,
    )

    monitor.start()
    log.info("Monitor running. Will stop at 3:15 PM ET.")

    try:
        while True:
            now = datetime.now()
            # Stop at 3:15 PM ET (after 3 PM force-close has fired)
            if now.hour == 15 and now.minute >= 15:
                log.info("3:15 PM ET reached — stopping monitor.")
                break
            # Print daily summary every hour
            if now.minute == 0:
                trades = monitor.trade_log
                if trades:
                    wins  = sum(1 for t in trades if t['is_win'])
                    total_pnl = sum(t['pnl'] for t in trades)
                    log.info(f"Hourly summary: {len(trades)} trades | {wins} wins | PnL: ${total_pnl:+.2f}")
            time.sleep(30)
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
    finally:
        monitor.stop()
        trades = monitor.trade_log
        W = 60
        bar = "=" * W

        log.info(bar)
        log.info(f"  EOD SUMMARY — {datetime.now().strftime('%A, %Y-%m-%d')}")
        log.info(bar)

        if not trades:
            log.info("  No trades executed today.")
            log.info(bar)
        else:
            wins       = [t for t in trades if t['is_win']]
            losses     = [t for t in trades if not t['is_win']]
            total_pnl  = sum(t['pnl'] for t in trades)
            win_rate   = len(wins) / len(trades) * 100
            avg_win    = sum(t['pnl'] for t in wins)   / len(wins)   if wins   else 0
            avg_loss   = sum(t['pnl'] for t in losses) / len(losses) if losses else 0
            gross_win  = sum(t['pnl'] for t in wins)
            gross_loss = abs(sum(t['pnl'] for t in losses))
            pf         = gross_win / gross_loss if gross_loss > 0 else float('inf')
            best       = max(trades, key=lambda t: t['pnl'])
            worst      = min(trades, key=lambda t: t['pnl'])

            # ── Overall ───────────────────────────────────────────────
            log.info(f"  {'Total Trades':<20} {len(trades)}")
            log.info(f"  {'Wins / Losses':<20} {len(wins)} / {len(losses)}")
            log.info(f"  {'Win Rate':<20} {win_rate:.1f}%")
            log.info(f"  {'Total PnL':<20} ${total_pnl:+.2f}")
            log.info(f"  {'Avg Win':<20} ${avg_win:+.2f}")
            log.info(f"  {'Avg Loss':<20} ${avg_loss:+.2f}")
            log.info(f"  {'Profit Factor':<20} {pf:.2f}x")
            log.info(f"  {'Best Trade':<20} {best['ticker']} ${best['pnl']:+.2f} ({best['reason']})")
            log.info(f"  {'Worst Trade':<20} {worst['ticker']} ${worst['pnl']:+.2f} ({worst['reason']})")

            # ── Exit reason breakdown ─────────────────────────────────
            log.info("")
            log.info("  Exit Reason Breakdown:")
            reasons = {}
            for t in trades:
                r = t['reason']
                reasons.setdefault(r, {'count': 0, 'pnl': 0.0})
                reasons[r]['count'] += 1
                reasons[r]['pnl']   += t['pnl']
            for reason, stats in sorted(reasons.items(), key=lambda x: -x[1]['pnl']):
                log.info(f"    {reason:<25} {stats['count']:>3} trades  PnL: ${stats['pnl']:+.2f}")

            # ── Per-ticker breakdown ──────────────────────────────────
            log.info("")
            log.info("  Per-Ticker Breakdown:")
            tickers_seen = {}
            for t in trades:
                tk = t['ticker']
                tickers_seen.setdefault(tk, {'count': 0, 'pnl': 0.0, 'wins': 0})
                tickers_seen[tk]['count'] += 1
                tickers_seen[tk]['pnl']   += t['pnl']
                tickers_seen[tk]['wins']  += 1 if t['is_win'] else 0
            for tk, stats in sorted(tickers_seen.items(), key=lambda x: -x[1]['pnl']):
                wr = stats['wins'] / stats['count'] * 100
                log.info(f"    {tk:<6}  {stats['count']:>2} trades  {wr:>5.1f}% win  PnL: ${stats['pnl']:+.2f}")

            # ── Trade-by-trade log ────────────────────────────────────
            log.info("")
            log.info("  Trade Log:")
            log.info(f"  {'Entry':>8}  {'Exit':>8}  {'Ticker':<6}  {'Qty':>3}  {'Entry $':>8}  {'Exit $':>8}  {'PnL':>8}  Reason")
            log.info("  " + "-" * (W - 2))
            for t in trades:
                flag = "✓" if t['is_win'] else "✗"
                log.info(
                    f"  {t.get('entry_time','?'):>8}  {t['time']:>8}  {t['ticker']:<6}  "
                    f"{t['qty']:>3}  ${t['entry_price']:>7.2f}  ${t['exit_price']:>7.2f}  "
                    f"${t['pnl']:>+7.2f}  {flag} {t['reason']}"
                )

        log.info(bar)


if __name__ == '__main__':
    main()
