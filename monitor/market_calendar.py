"""
US Market Holiday Calendar — NYSE/NASDAQ closures and early closes.

Used by watchdog to skip trading on holidays, and by strategy engine
to avoid entries on half-days.

Covers 2026-2028. Update annually (takes 2 minutes, dates are published
by NYSE every September for the following year).

Sources:
  https://www.nyse.com/markets/hours-calendars
  https://www.nasdaq.com/market-activity/stock-market-holiday-schedule
"""
from datetime import date


# Full market closures (no trading)
MARKET_HOLIDAYS: set[date] = {
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas

    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # MLK Day
    date(2027, 2, 15),   # Presidents' Day
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed, Fri)
    date(2027, 7, 5),    # Independence Day (observed, Mon)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (observed, Fri)

    # 2028
    date(2028, 1, 17),   # MLK Day
    date(2028, 2, 21),   # Presidents' Day
    date(2028, 4, 14),   # Good Friday
    date(2028, 5, 29),   # Memorial Day
    date(2028, 6, 19),   # Juneteenth
    date(2028, 7, 4),    # Independence Day
    date(2028, 9, 4),    # Labor Day
    date(2028, 11, 23),  # Thanksgiving
    date(2028, 12, 25),  # Christmas
}

# Early close days (market closes at 1:00 PM ET)
EARLY_CLOSE_DAYS: set[date] = {
    # 2026
    date(2026, 7, 2),    # Day before Independence Day
    date(2026, 11, 27),  # Day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve

    # 2027
    date(2027, 11, 26),  # Day after Thanksgiving

    # 2028
    date(2028, 7, 3),    # Day before Independence Day
    date(2028, 11, 24),  # Day after Thanksgiving
    date(2028, 12, 22),  # Friday before Christmas (if applicable)
}


def is_market_holiday(d: date = None) -> bool:
    """True if the given date is a full market closure."""
    if d is None:
        d = date.today()
    return d in MARKET_HOLIDAYS


def is_early_close(d: date = None) -> bool:
    """True if the market closes early (1:00 PM ET) on this date."""
    if d is None:
        d = date.today()
    return d in EARLY_CLOSE_DAYS


def early_close_hour() -> int:
    """Returns the close hour on early close days (13 = 1 PM ET)."""
    return 13


def is_trading_day(d: date = None) -> bool:
    """True if this is a regular or early-close trading day (not weekend, not holiday)."""
    if d is None:
        d = date.today()
    if d.weekday() >= 5:  # Saturday, Sunday
        return False
    if d in MARKET_HOLIDAYS:
        return False
    return True
