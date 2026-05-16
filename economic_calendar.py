"""
Economic Calendar Module — Fixed lazy API key loading
"""
import os, json, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_calendar_cache = {}

HIGH_IMPACT_EVENTS = {
    "fomc": {
        "names": ["FOMC", "Fed Rate Decision", "Federal Reserve Rate", "Fed Meeting"],
        "impact": "EXTREME", "avoid_until": "FULL DAY",
        "warning": "FOMC rate decision — avoid buying premium ALL DAY. IV spikes market-wide.",
        "emoji": "🚨"
    },
    "cpi": {
        "names": ["CPI", "Consumer Price Index"],
        "impact": "HIGH", "avoid_until": "10:00 AM",
        "warning": "CPI release at 8:30 AM — do not enter before 10:00 AM ET.",
        "emoji": "🔴"
    },
    "ppi": {
        "names": ["PPI", "Producer Price Index"],
        "impact": "HIGH", "avoid_until": "10:00 AM",
        "warning": "PPI release at 8:30 AM — do not enter before 10:00 AM ET.",
        "emoji": "🔴"
    },
    "nfp": {
        "names": ["NFP", "Non-Farm Payroll", "Jobs Report", "Employment Situation", "Nonfarm"],
        "impact": "HIGH", "avoid_until": "10:00 AM",
        "warning": "Jobs Report at 8:30 AM — do not enter before 10:00 AM ET.",
        "emoji": "🔴"
    },
    "pce": {
        "names": ["PCE", "Personal Consumption", "Personal Income"],
        "impact": "HIGH", "avoid_until": "10:00 AM",
        "warning": "PCE release — do not enter before 10:00 AM ET.",
        "emoji": "🔴"
    },
    "gdp": {
        "names": ["GDP", "Gross Domestic Product"],
        "impact": "HIGH", "avoid_until": "10:00 AM",
        "warning": "GDP release at 8:30 AM — wait until 10:00 AM ET.",
        "emoji": "🔴"
    },
    "retail": {
        "names": ["Retail Sales"],
        "impact": "MEDIUM", "avoid_until": "10:00 AM",
        "warning": "Retail Sales at 8:30 AM — wait for open to settle.",
        "emoji": "⚠️"
    },
    "ism": {
        "names": ["ISM", "PMI", "Manufacturing Index", "Services Index"],
        "impact": "MEDIUM", "avoid_until": "10:15 AM",
        "warning": "ISM/PMI release — wait 15 min after for spreads to tighten.",
        "emoji": "⚠️"
    },
    "fed_speech": {
        "names": ["Powell", "Fed Chair", "FOMC Minutes", "Fed Minutes", "Beige Book"],
        "impact": "MEDIUM", "avoid_until": "VARIES",
        "warning": "Fed communication — surprise language can spike VIX.",
        "emoji": "⚠️"
    },
    "claims": {
        "names": ["Unemployment Claims", "Initial Claims", "Jobless Claims"],
        "impact": "LOW", "avoid_until": "9:45 AM",
        "warning": "Weekly claims at 8:30 AM — minor volatility.",
        "emoji": "📊"
    },
}

IMPACT_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "EXTREME": 4}


def get_client():
    """Lazy Anthropic client — reads key at call time."""
    from anthropic import Anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    return Anthropic(api_key=api_key)


def fetch_calendar_via_claude(date_str: str) -> list:
    try:
        client = get_client()
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_readable = dt.strftime("%A, %B %d, %Y")

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=600,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content":
                f"""Search for the economic calendar for {date_readable}.
List ONLY high-impact US events: FOMC, CPI, PPI, NFP, PCE, GDP, Retail Sales, ISM, Fed speeches, jobless claims.
Return ONLY a JSON array:
[{{"time_et": "8:30 AM", "event": "CPI", "impact": "HIGH", "detail": "Consumer Price Index"}}]
If no high-impact events, return: []
Return ONLY the JSON array, nothing else."""
            }]
        )

        text = "".join(b.text for b in response.content if hasattr(b, 'text'))
        text = re.sub(r"```json\s*|\s*```", "", text).strip()
        match = re.search(r'\[[\s\S]*?\]', text)
        if match:
            events = json.loads(match.group())
            print(f"[CALENDAR] Found {len(events)} events for {date_str}")
            return events
        return []
    except ValueError as e:
        print(f"[CALENDAR] API key error: {e}")
        return []
    except Exception as e:
        print(f"[CALENDAR] Fetch error: {e}")
        return []


def get_recurring_events(date_str: str) -> list:
    """Fallback hardcoded recurring schedule."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except:
        return []
    events  = []
    weekday = dt.weekday()
    day     = dt.day
    if weekday == 4 and day <= 7:
        events.append({"time_et": "8:30 AM", "event": "NFP / Jobs Report",
                        "impact": "HIGH", "detail": "Non-Farm Payrolls — first Friday"})
    if weekday == 3:
        events.append({"time_et": "8:30 AM", "event": "Initial Jobless Claims",
                        "impact": "LOW", "detail": "Weekly unemployment claims"})
    return events


def fetch_and_cache_today():
    """Called at 7:30 AM by scheduler."""
    now_et   = datetime.now(ZoneInfo("America/New_York"))
    today    = now_et.strftime("%Y-%m-%d")
    weekday  = now_et.weekday()
    days_fwd = 3 if weekday == 4 else 1
    tomorrow = (now_et + timedelta(days=days_fwd)).strftime("%Y-%m-%d")
    print(f"[CALENDAR] Pre-fetching {today} and {tomorrow}...")
    get_economic_events(today, force_refresh=True)
    get_economic_events(tomorrow, force_refresh=True)
    print("[CALENDAR] Cache ready")


def get_economic_events(date_str: str = None, force_refresh: bool = False) -> list:
    if date_str is None:
        date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    if not force_refresh and date_str in _calendar_cache:
        return _calendar_cache[date_str]
    events = fetch_calendar_via_claude(date_str)
    if not events:
        events = get_recurring_events(date_str)
    _calendar_cache[date_str] = events
    return events


def classify_event(event_name: str) -> dict:
    name_upper = event_name.upper()
    for key, defn in HIGH_IMPACT_EVENTS.items():
        if any(n.upper() in name_upper or name_upper in n.upper()
               for n in defn.get("names", [])):
            return defn
    return {"impact": "LOW", "avoid_until": "9:45 AM",
            "warning": event_name, "emoji": "📊"}


def get_today_warnings() -> dict:
    now_et   = datetime.now(ZoneInfo("America/New_York"))
    today    = now_et.strftime("%Y-%m-%d")
    weekday  = now_et.weekday()
    days_fwd = 3 if weekday == 4 else 1
    tomorrow = (now_et + timedelta(days=days_fwd)).strftime("%Y-%m-%d")

    today_events    = get_economic_events(today)
    tomorrow_events = get_economic_events(tomorrow)

    warnings = {
        "today_events": today_events, "tomorrow_events": tomorrow_events,
        "max_impact": "NONE", "advisory": None, "advisory_emoji": "✅",
        "avoid_until": None, "avoid_buying": False,
        "events_summary": [], "week_events": {},
    }

    # Check tomorrow for FOMC
    for event in tomorrow_events:
        defn = classify_event(event.get("event", ""))
        if defn.get("impact") == "EXTREME":
            warnings["events_summary"].append(
                f"🔴 TOMORROW: {event.get('event','')} — "
                f"IV rising into today's close. Avoid premium after 3:00 PM."
            )
            if IMPACT_RANK.get(warnings["max_impact"], 0) < IMPACT_RANK["HIGH"]:
                warnings["max_impact"] = "HIGH"
                warnings["avoid_until"] = "3:00 PM (day before FOMC)"

    for event in today_events:
        defn   = classify_event(event.get("event", ""))
        impact = defn.get("impact", "LOW")
        if IMPACT_RANK.get(impact, 0) > IMPACT_RANK.get(warnings["max_impact"], 0):
            warnings["max_impact"] = impact
            warnings["avoid_until"] = defn.get("avoid_until")
        if impact == "EXTREME":
            warnings["avoid_buying"] = True
        emoji   = defn.get('emoji','📊') or '📊'
        time_et = str(event.get('time_et','') or '')
        name    = str(event.get('event','') or '')
        warning = str(defn.get('warning','') or '')
        warnings["events_summary"].append(
            f"{emoji} {time_et}: {name} — {warning}"
        )

    # Week ahead
    week = {}
    for i in range(1, 6):
        dt = now_et + timedelta(days=i)
        if dt.weekday() < 5:
            ds = dt.strftime("%Y-%m-%d")
            evs = get_economic_events(ds)
            high = [e for e in evs
                    if classify_event(e.get("event","")).get("impact") in ["HIGH","EXTREME"]]
            if high:
                week[ds] = high
    warnings["week_events"] = week

    mi = warnings["max_impact"]
    au = warnings["avoid_until"]
    if mi == "EXTREME":
        warnings["advisory"] = "🚨 FOMC today — avoid buying premium ALL DAY."
        warnings["advisory_emoji"] = "🚨"
        warnings["avoid_buying"]   = True
    elif mi == "HIGH":
        warnings["advisory"] = (
            f"🔴 High-impact data today — do NOT enter before {au or '10:00 AM ET'}. "
            f"Options open 9:30 AM, first clean window {au or '10:00 AM ET'}."
        )
        warnings["advisory_emoji"] = "🔴"
    elif mi == "MEDIUM":
        warnings["advisory"] = f"⚠️ Moderate macro event — wait until {au or '10:00 AM ET'}."
        warnings["advisory_emoji"] = "⚠️"
    elif mi == "LOW":
        warnings["advisory"] = f"📊 Minor data release — options normal by {au or '9:45 AM ET'}."
        warnings["advisory_emoji"] = "📊"
    else:
        warnings["advisory"] = "✅ No major macro events today — clean trading day."
        warnings["advisory_emoji"] = "✅"

    return warnings


def get_week_ahead_summary(warnings: dict) -> list:
    lines = []
    for date_str, events in warnings.get("week_events", {}).items():
        try:
            day_name = datetime.strptime(date_str, "%Y-%m-%d").strftime("%a %b %d")
        except:
            day_name = date_str
        for event in events:
            defn = classify_event(event.get("event", ""))
            lines.append(
                f"  {defn.get('emoji','📊')} {day_name}: {str(event.get('event','') or '')} "
                f"— avoid before {str(defn.get('avoid_until','10 AM ET') or '10 AM ET')}"
            )
    return lines
