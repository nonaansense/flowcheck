"""
Market holiday calendar for FlowCheck.
Prevents alerts, pre-market summaries, and schedulers
from firing on days when markets are closed.

NYSE/NASDAQ holidays 2025-2027.
Early close days (1:00 PM ET) also tracked.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

# ── Full Market Holidays ───────────────────────────────────────────────
# Markets closed all day

MARKET_HOLIDAYS = {
    # 2025
    date(2025, 1, 1),   # New Year's Day
    date(2025, 1, 20),  # MLK Day
    date(2025, 2, 17),  # Presidents Day
    date(2025, 4, 18),  # Good Friday
    date(2025, 5, 26),  # Memorial Day
    date(2025, 6, 19),  # Juneteenth
    date(2025, 7, 4),   # Independence Day
    date(2025, 9, 1),   # Labor Day
    date(2025, 11, 27), # Thanksgiving
    date(2025, 12, 25), # Christmas

    # 2026
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day  ← TODAY
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas

    # 2027
    date(2027, 1, 1),   # New Year's Day
    date(2027, 1, 18),  # MLK Day
    date(2027, 2, 15),  # Presidents Day
    date(2027, 3, 26),  # Good Friday
    date(2027, 5, 31),  # Memorial Day
    date(2027, 6, 18),  # Juneteenth (observed)
    date(2027, 7, 5),   # Independence Day (observed)
    date(2027, 9, 6),   # Labor Day
    date(2027, 11, 25), # Thanksgiving
    date(2027, 12, 24), # Christmas (observed)
}

# ── Early Close Days (1:00 PM ET) ─────────────────────────────────────
EARLY_CLOSE_DAYS = {
    # 2025
    date(2025, 7, 3),   # Day before Independence Day
    date(2025, 11, 28), # Day after Thanksgiving
    date(2025, 12, 24), # Christmas Eve

    # 2026
    date(2026, 7, 2),   # Day before Independence Day (observed)
    date(2026, 11, 27), # Day after Thanksgiving
    date(2026, 12, 24), # Christmas Eve

    # 2027
    date(2027, 11, 26), # Day after Thanksgiving
    date(2027, 12, 23), # Christmas Eve (observed)
}

# ── Holiday Names ──────────────────────────────────────────────────────
HOLIDAY_NAMES = {
    date(2026, 1, 1):   "New Year's Day",
    date(2026, 1, 19):  "MLK Day",
    date(2026, 2, 16):  "Presidents Day",
    date(2026, 4, 3):   "Good Friday",
    date(2026, 5, 25):  "Memorial Day",
    date(2026, 6, 19):  "Juneteenth",
    date(2026, 7, 3):   "Independence Day",
    date(2026, 9, 7):   "Labor Day",
    date(2026, 11, 26): "Thanksgiving",
    date(2026, 12, 25): "Christmas",
    date(2025, 1, 1):   "New Year's Day",
    date(2025, 1, 20):  "MLK Day",
    date(2025, 2, 17):  "Presidents Day",
    date(2025, 4, 18):  "Good Friday",
    date(2025, 5, 26):  "Memorial Day",
    date(2025, 6, 19):  "Juneteenth",
    date(2025, 7, 4):   "Independence Day",
    date(2025, 9, 1):   "Labor Day",
    date(2025, 11, 27): "Thanksgiving",
    date(2025, 12, 25): "Christmas",
    date(2027, 1, 1):   "New Year's Day",
    date(2027, 5, 31):  "Memorial Day",
    date(2027, 11, 25): "Thanksgiving",
    date(2027, 12, 24): "Christmas",
}

# ── Core Functions ─────────────────────────────────────────────────────

def today_et() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()

def is_market_holiday(dt: date = None) -> bool:
    """Returns True if the date is a market holiday."""
    if dt is None:
        dt = today_et()
    return dt in MARKET_HOLIDAYS

def is_early_close(dt: date = None) -> bool:
    """Returns True if markets close early (1 PM ET) on this date."""
    if dt is None:
        dt = today_et()
    return dt in EARLY_CLOSE_DAYS

def is_market_open(dt: date = None) -> bool:
    """Returns True if markets are open today (not holiday, not weekend)."""
    if dt is None:
        dt = today_et()
    if dt.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return dt not in MARKET_HOLIDAYS

def get_holiday_name(dt: date = None) -> str | None:
    """Returns holiday name if today is a holiday, else None."""
    if dt is None:
        dt = today_et()
    return HOLIDAY_NAMES.get(dt)

def market_status() -> dict:
    """
    Returns current market status dict.
    Used in pre-market summary and scheduled jobs.
    """
    now_et  = datetime.now(ZoneInfo("America/New_York"))
    today   = now_et.date()
    weekday = today.weekday()  # 0=Mon, 6=Sun

    if weekday >= 5:
        return {
            "is_open":      False,
            "reason":       "Weekend",
            "label":        "Markets closed — Weekend",
            "emoji":        "📅",
            "early_close":  False,
        }

    if today in MARKET_HOLIDAYS:
        name = HOLIDAY_NAMES.get(today, "Market Holiday")
        return {
            "is_open":      False,
            "reason":       name,
            "label":        f"Markets closed — {name}",
            "emoji":        "🏖️",
            "early_close":  False,
        }

    if today in EARLY_CLOSE_DAYS:
        return {
            "is_open":      True,
            "reason":       "Early close",
            "label":        "Early close today — markets close 1:00 PM ET",
            "emoji":        "⏰",
            "early_close":  True,
            "close_time":   "1:00 PM ET",
        }

    return {
        "is_open":      True,
        "reason":       "Normal session",
        "label":        "Normal trading day",
        "emoji":        "✅",
        "early_close":  False,
        "close_time":   "4:00 PM ET",
    }

def next_market_day(from_date: date = None) -> date:
    """Returns the next trading day."""
    from datetime import timedelta
    if from_date is None:
        from_date = today_et()
    dt = from_date + timedelta(days=1)
    while dt.weekday() >= 5 or dt in MARKET_HOLIDAYS:
        dt += timedelta(days=1)
    return dt
