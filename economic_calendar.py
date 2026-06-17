"""
Economic calendar — fetches live data from Finnhub API.
Falls back to hardcoded dates only if Finnhub is unavailable.
No more wrong hardcoded dates.
"""
import os, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

IMPACT_RANK = {"NONE":0,"LOW":1,"MEDIUM":2,"HIGH":3,"EXTREME":4}

# High-impact event keywords to filter from Finnhub
HIGH_IMPACT_KEYWORDS = [
    "nonfarm", "non-farm", "nfp", "jobs report",
    "cpi", "consumer price",
    "fomc", "fed decision", "interest rate decision",
    "gdp", "gross domestic",
    "pce", "personal consumption",
    "ppi", "producer price",
    "unemployment", "jobless claims",
    "retail sales",
    "ism manufacturing", "ism services",
]

# Hardcoded FOMC decision dates (day 2 of each 2-day meeting) — Finnhub's free
# tier calendar is unreliable for Fed events, so this guarantees we never miss one.
# Source: federalreserve.gov/monetarypolicy/fomccalendars.htm
FOMC_DECISION_DATES_2026 = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
FOMC_DECISION_DATES_2025 = [
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
]
FOMC_DECISION_DATES = FOMC_DECISION_DATES_2025 + FOMC_DECISION_DATES_2026

_cache: dict = {}
_cache_ttl = 3600  # 1 hour


def _fetch_finnhub_calendar(from_date: str, to_date: str) -> list:
    """Fetch economic events from Finnhub for a date range."""
    token = os.environ.get("FINNHUB_API_KEY","")
    if not token:
        return []
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/calendar/economic",
            params={"from": from_date, "to": to_date, "token": token},
            timeout=10
        )
        if r.status_code != 200:
            return []
        data = r.json()
        events = data.get("economicCalendar", []) or []
        return events
    except Exception as e:
        print(f"[CALENDAR] Finnhub error: {e}")
        return []


def _classify_event(event_name: str) -> dict:
    """Classify a Finnhub event into our impact/avoid system."""
    name_lower = event_name.lower()

    if any(k in name_lower for k in ["fomc", "fed decision", "interest rate decision"]):
        return {"impact": "EXTREME", "time_et": "2:00 PM", "avoid_until": "3:30 PM"}

    if any(k in name_lower for k in ["nonfarm", "non-farm", "nfp", "jobs report"]):
        return {"impact": "HIGH", "time_et": "8:30 AM", "avoid_until": "10:00 AM"}

    if any(k in name_lower for k in ["cpi", "consumer price index"]):
        return {"impact": "HIGH", "time_et": "8:30 AM", "avoid_until": "10:00 AM"}

    if any(k in name_lower for k in ["pce", "personal consumption expenditure"]):
        return {"impact": "HIGH", "time_et": "8:30 AM", "avoid_until": "10:00 AM"}

    if any(k in name_lower for k in ["gdp", "gross domestic"]):
        return {"impact": "HIGH", "time_et": "8:30 AM", "avoid_until": "10:00 AM"}

    if any(k in name_lower for k in ["ppi", "producer price"]):
        return {"impact": "HIGH", "time_et": "8:30 AM", "avoid_until": "10:00 AM"}

    if any(k in name_lower for k in ["retail sales"]):
        return {"impact": "HIGH", "time_et": "8:30 AM", "avoid_until": "10:00 AM"}

    return {"impact": "MEDIUM", "time_et": "8:30 AM", "avoid_until": "10:00 AM"}


def get_economic_events(date_str: str = None) -> list:
    """Get high-impact US economic events for a given date."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if not date_str:
        date_str = now_et.strftime("%Y-%m-%d")

    cache_key = f"events_{date_str}"
    if cache_key in _cache:
        cached, ts = _cache[cache_key]
        if (datetime.now().timestamp() - ts) < _cache_ttl:
            return cached

    # Fetch from Finnhub — request +/- 1 day window to ensure we catch it
    try:
        dt       = datetime.strptime(date_str, "%Y-%m-%d")
        from_dt  = (dt - timedelta(days=1)).strftime("%Y-%m-%d")
        to_dt    = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
        raw      = _fetch_finnhub_calendar(from_dt, to_dt)
    except Exception:
        raw = []

    events = []
    for e in raw:
        ev_date = str(e.get("time","") or "")[:10]
        if ev_date != date_str:
            continue
        country = str(e.get("country","") or "").upper()
        if country and country != "US":
            continue
        name = str(e.get("event","") or "")
        name_lower = name.lower()
        if not any(k in name_lower for k in HIGH_IMPACT_KEYWORDS):
            continue
        meta = _classify_event(name)
        events.append({
            "event":       name,
            "impact":      meta["impact"],
            "time_et":     meta["time_et"],
            "avoid_until": meta["avoid_until"],
            "month":       dt.month,
            "day":         dt.day,
        })

    # Cache result
    _cache[cache_key] = (events, datetime.now().timestamp())

    if not events:
        print(f"[CALENDAR] No high-impact US events on {date_str} (Finnhub)")
    else:
        print(f"[CALENDAR] {len(events)} events on {date_str}: {[e['event'] for e in events]}")

    return events


def get_today_warnings() -> dict:
    warnings = {
        "events_today":  [],
        "events_summary":[],
        "max_impact":    "NONE",
        "avoid_buying":  False,
        "avoid_until":   None,
    }
    events = get_economic_events()

    # Hardcoded FOMC override — Finnhub's calendar is unreliable for Fed events
    today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    if today_str in FOMC_DECISION_DATES:
        already_has_fomc = any("fomc" in str(e.get("event","")).lower() or
                                "fed" in str(e.get("event","")).lower()
                                for e in events)
        if not already_has_fomc:
            events.append({
                "event":       "FOMC Rate Decision",
                "impact":      "EXTREME",
                "time_et":     "2:00 PM",
                "avoid_until": "2:30 PM",
            })
            print(f"[CALENDAR] FOMC decision day detected (hardcoded override): {today_str}")
    for e in events:
        impact  = str(e.get("impact","LOW"))
        time_et = str(e.get("time_et",""))
        name    = str(e.get("event",""))
        avoid   = str(e.get("avoid_until","10:00 AM"))
        if IMPACT_RANK.get(impact,0) > IMPACT_RANK.get(warnings["max_impact"],0):
            warnings["max_impact"]  = impact
            warnings["avoid_until"] = avoid
        if impact in ("HIGH","EXTREME"):
            warnings["avoid_buying"] = True
        emoji = "🚨" if impact == "EXTREME" else "⚠️"
        warnings["events_today"].append(e)
        warnings["events_summary"].append(
            f"{emoji} {time_et}: {name} — avoid before {avoid}"
        )
    return warnings


def get_week_ahead() -> list:
    lines = []
    now   = datetime.now(ZoneInfo("America/New_York"))

    # Fetch entire week in one API call
    from_dt = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    to_dt   = (now + timedelta(days=7)).strftime("%Y-%m-%d")
    raw     = _fetch_finnhub_calendar(from_dt, to_dt)

    # Group by date
    from collections import defaultdict
    by_date = defaultdict(list)
    for e in raw:
        ev_date = str(e.get("time","") or "")[:10]
        country = str(e.get("country","") or "").upper()
        if country and country != "US":
            continue
        name = str(e.get("event","") or "")
        if not any(k in name.lower() for k in HIGH_IMPACT_KEYWORDS):
            continue
        by_date[ev_date].append(name)

    for i in range(1, 8):
        day     = now + timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        for name in by_date.get(day_str, []):
            meta  = _classify_event(name)
            emoji = "🚨" if meta["impact"] == "EXTREME" else "⚠️"
            lines.append(
                f"  {emoji} {day.strftime('%A')}: {name} — avoid before {meta['avoid_until']}"
            )

    return lines
