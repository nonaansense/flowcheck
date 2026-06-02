"""
Economic calendar — hardcoded recurring high-impact US events.
Claude web search disabled to avoid rate limits and None-type errors.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

RECURRING_EVENTS = [
    {"month":1, "day":29,"event":"FOMC Decision","time_et":"2:00 PM","impact":"EXTREME","avoid_until":"3:30 PM"},
    {"month":3, "day":19,"event":"FOMC Decision","time_et":"2:00 PM","impact":"EXTREME","avoid_until":"3:30 PM"},
    {"month":5, "day":7, "event":"FOMC Decision","time_et":"2:00 PM","impact":"EXTREME","avoid_until":"3:30 PM"},
    {"month":6, "day":18,"event":"FOMC Decision","time_et":"2:00 PM","impact":"EXTREME","avoid_until":"3:30 PM"},
    {"month":7, "day":30,"event":"FOMC Decision","time_et":"2:00 PM","impact":"EXTREME","avoid_until":"3:30 PM"},
    {"month":9, "day":17,"event":"FOMC Decision","time_et":"2:00 PM","impact":"EXTREME","avoid_until":"3:30 PM"},
    {"month":11,"day":5, "event":"FOMC Decision","time_et":"2:00 PM","impact":"EXTREME","avoid_until":"3:30 PM"},
    {"month":12,"day":10,"event":"FOMC Decision","time_et":"2:00 PM","impact":"EXTREME","avoid_until":"3:30 PM"},
    {"month":1, "day":15,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":2, "day":12,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":3, "day":12,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":4, "day":10,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":5, "day":13,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":6, "day":11,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":7, "day":15,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":8, "day":13,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":9, "day":10,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":10,"day":15,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":11,"day":12,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":12,"day":10,"event":"CPI Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":1, "day":10,"event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":2, "day":7, "event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":3, "day":7, "event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":4, "day":4, "event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":5, "day":2, "event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":6, "day":6, "event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":7, "day":3, "event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":8, "day":1, "event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":9, "day":5, "event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":10,"day":3, "event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":11,"day":7, "event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
    {"month":12,"day":5, "event":"NFP Jobs Report","time_et":"8:30 AM","impact":"HIGH","avoid_until":"10:00 AM"},
]

IMPACT_RANK = {"NONE":0,"LOW":1,"MEDIUM":2,"HIGH":3,"EXTREME":4}
_calendar_cache: dict = {}

def get_economic_events(date_str: str = None) -> list:
    if not date_str:
        date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    if date_str in _calendar_cache:
        return _calendar_cache[date_str]
    try:
        dt     = datetime.strptime(date_str, "%Y-%m-%d")
        events = [e for e in RECURRING_EVENTS
                  if e["month"]==dt.month and e["day"]==dt.day]
        _calendar_cache[date_str] = events
        return events
    except Exception as e:
        print(f"[CALENDAR] Error: {e}")
        return []

def get_today_warnings() -> dict:
    warnings = {"events_today":[],"events_summary":[],"max_impact":"NONE",
                "avoid_buying":False,"avoid_until":None}
    events = get_economic_events()
    for e in events:
        try:
            impact  = str(e.get("impact") or "LOW")
            time_et = str(e.get("time_et") or "")
            name    = str(e.get("event") or "")
            avoid   = str(e.get("avoid_until") or "10:00 AM")
            if IMPACT_RANK.get(impact,0) > IMPACT_RANK.get(warnings["max_impact"],0):
                warnings["max_impact"]  = impact
                warnings["avoid_until"] = avoid
            if impact in ("HIGH","EXTREME"):
                warnings["avoid_buying"] = True
            emoji = "🚨" if impact=="EXTREME" else "⚠️"
            warnings["events_today"].append(e)
            warnings["events_summary"].append(f"{emoji} {time_et}: {name} — avoid before {avoid}")
        except Exception as ex:
            print(f"[CALENDAR] Event error: {ex}")
    return warnings

def get_week_ahead() -> list:
    lines = []
    now   = datetime.now(ZoneInfo("America/New_York"))
    for i in range(1,6):
        day    = now + timedelta(days=i)
        events = get_economic_events(day.strftime("%Y-%m-%d"))
        for e in events:
            if e.get("impact") in ("HIGH","EXTREME"):
                emoji = "🚨" if e["impact"]=="EXTREME" else "⚠️"
                lines.append(f"  {emoji} {day.strftime('%A')}: {e.get('event','')} — avoid before {e.get('avoid_until','10AM')}")
    return lines
