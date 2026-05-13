"""
Economic Calendar Module
========================
Fetches and caches high-impact macro events.

Schedule:
- 7:30 AM ET: fetch_and_cache_today() called by scheduler
- Pre-market SMS reads from cache — no delay at 8:00 AM

High-impact events tracked:
FOMC, CPI, PPI, NFP, PCE, GDP, Retail Sales, ISM, Fed speeches, Claims
"""

import os, json, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from anthropic import Anthropic

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Cache: {date_str: [events]}
_calendar_cache = {}

# ─────────────────────────────────────────
# EVENT DEFINITIONS
# ─────────────────────────────────────────
HIGH_IMPACT_EVENTS = {
    "fomc": {
        "names": ["FOMC", "Fed Rate Decision", "Federal Reserve Rate", "Fed Meeting"],
        "impact": "EXTREME",
        "avoid_until": "FULL DAY",
        "warning": "FOMC rate decision — avoid buying premium ALL DAY. IV spikes market-wide.",
        "emoji": "🚨"
    },
    "cpi": {
        "names": ["CPI", "Consumer Price Index"],
        "impact": "HIGH",
        "avoid_until": "10:00 AM",
        "warning": "CPI release at 8:30 AM — do not enter before 10:00 AM. Wait for print + digest.",
        "emoji": "🔴"
    },
    "ppi": {
        "names": ["PPI", "Producer Price Index"],
        "impact": "HIGH",
        "avoid_until": "10:00 AM",
        "warning": "PPI release at 8:30 AM — do not enter before 10:00 AM.",
        "emoji": "🔴"
    },
    "nfp": {
        "names": ["NFP", "Non-Farm Payroll", "Jobs Report", "Employment Situation", "Nonfarm"],
        "impact": "HIGH",
        "avoid_until": "10:00 AM",
        "warning": "Jobs Report at 8:30 AM — major market mover. Do not enter before 10:00 AM.",
        "emoji": "🔴"
    },
    "pce": {
        "names": ["PCE", "Personal Consumption", "Personal Income"],
        "impact": "HIGH",
        "avoid_until": "10:00 AM",
        "warning": "PCE release (Fed's preferred inflation gauge) — do not enter before 10:00 AM.",
        "emoji": "🔴"
    },
    "gdp": {
        "names": ["GDP", "Gross Domestic Product"],
        "impact": "HIGH",
        "avoid_until": "10:00 AM",
        "warning": "GDP release at 8:30 AM — wait until 10:00 AM for clean entry.",
        "emoji": "🔴"
    },
    "retail": {
        "names": ["Retail Sales"],
        "impact": "MEDIUM",
        "avoid_until": "10:00 AM",
        "warning": "Retail Sales at 8:30 AM — moderate volatility risk, wait for open to settle.",
        "emoji": "⚠️"
    },
    "ism": {
        "names": ["ISM", "PMI", "Manufacturing Index", "Services Index"],
        "impact": "MEDIUM",
        "avoid_until": "10:15 AM",
        "warning": "ISM/PMI release — typically 10:00 AM ET. Wait 15 min after for spreads to tighten.",
        "emoji": "⚠️"
    },
    "fed_speech": {
        "names": ["Powell", "Fed Chair", "FOMC Minutes", "Fed Minutes", "Beige Book"],
        "impact": "MEDIUM",
        "avoid_until": "VARIES",
        "warning": "Fed communication — surprise language can spike VIX. Check exact time.",
        "emoji": "⚠️"
    },
    "claims": {
        "names": ["Unemployment Claims", "Initial Claims", "Jobless Claims"],
        "impact": "LOW",
        "avoid_until": "9:45 AM",
        "warning": "Weekly claims at 8:30 AM — minor volatility, options market absorbs quickly.",
        "emoji": "📊"
    },
    "fomc_tomorrow": {
        "names": [],  # Handled programmatically
        "impact": "HIGH",
        "avoid_until": "3:00 PM",
        "warning": "FOMC decision TOMORROW — IV rises into close today. Avoid late-day premium buying.",
        "emoji": "🔴"
    },
}

IMPACT_RANK = {"NONE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "EXTREME": 4}


# ─────────────────────────────────────────
# CLAUDE WEB SEARCH FETCH
# ─────────────────────────────────────────
def fetch_calendar_via_claude(date_str: str) -> list:
    """Use Claude web search to fetch economic events for a date."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        date_readable = dt.strftime("%A, %B %d, %Y")

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": f"""Search MarketWatch economic calendar or Investing.com for economic events on {date_readable}.

List ONLY high-impact events: FOMC, CPI, PPI, NFP/Jobs, PCE, GDP, Retail Sales, ISM/PMI, Fed speeches, unemployment claims.

Return ONLY a JSON array:
[
  {{"time_et": "8:30 AM", "event": "CPI", "impact": "HIGH", "detail": "Consumer Price Index MoM"}},
  {{"time_et": "2:00 PM", "event": "FOMC Rate Decision", "impact": "EXTREME", "detail": "Federal Reserve interest rate announcement"}}
]

If no high-impact events, return: []
Return ONLY the JSON array."""
            }]
        )

        text = "".join(b.text for b in response.content if hasattr(b, 'text'))
        text = re.sub(r"```json\s*|\s*```", "", text).strip()
        match = re.search(r'\[[\s\S]*?\]', text)
        if match:
            events = json.loads(match.group())
            print(f"[CALENDAR] Claude found {len(events)} events for {date_str}")
            return events
        return []

    except Exception as e:
        print(f"[CALENDAR] Claude fetch error: {e}")
        return []


# ─────────────────────────────────────────
# HARDCODED RECURRING FALLBACK
# ─────────────────────────────────────────
def get_recurring_events(date_str: str) -> list:
    """Fallback: known recurring schedule patterns."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except:
        return []

    events = []
    weekday = dt.weekday()  # 0=Mon, 4=Fri
    day = dt.day

    # NFP — first Friday of each month
    if weekday == 4 and day <= 7:
        events.append({
            "time_et": "8:30 AM", "event": "NFP / Jobs Report",
            "impact": "HIGH", "detail": "Non-Farm Payrolls — first Friday of month"
        })

    # Weekly unemployment claims — every Thursday
    if weekday == 3:
        events.append({
            "time_et": "8:30 AM", "event": "Initial Jobless Claims",
            "impact": "LOW", "detail": "Weekly unemployment claims"
        })

    # CPI — usually 2nd or 3rd Wednesday/Thursday mid-month
    if 8 <= day <= 17 and weekday in [1, 2, 3]:
        events.append({
            "time_et": "8:30 AM", "event": "CPI (possible — verify)",
            "impact": "HIGH", "detail": "CPI typically mid-month — confirm exact date"
        })

    return events


# ─────────────────────────────────────────
# CACHE MANAGEMENT
# ─────────────────────────────────────────
def fetch_and_cache_today():
    """
    Called at 7:30 AM ET by scheduler.
    Pre-fetches today + tomorrow so pre-market SMS is instant.
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))
    today  = now_et.strftime("%Y-%m-%d")
    tomorrow = (now_et + timedelta(days=1)).strftime("%Y-%m-%d")
    # Skip weekend for tomorrow
    if now_et.weekday() == 4:  # Friday → next is Monday
        tomorrow = (now_et + timedelta(days=3)).strftime("%Y-%m-%d")

    print(f"[CALENDAR] Pre-fetching calendar for {today} and {tomorrow}...")
    get_economic_events(today, force_refresh=True)
    get_economic_events(tomorrow, force_refresh=True)
    print(f"[CALENDAR] Calendar cache ready")


def get_economic_events(date_str: str = None, force_refresh: bool = False) -> list:
    """Get events for a date. Uses cache unless force_refresh=True."""
    if date_str is None:
        date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    if not force_refresh and date_str in _calendar_cache:
        return _calendar_cache[date_str]

    # Try Claude web search first
    events = fetch_calendar_via_claude(date_str)

    # Fall back to hardcoded
    if not events:
        events = get_recurring_events(date_str)

    _calendar_cache[date_str] = events
    return events


# ─────────────────────────────────────────
# EVENT CLASSIFICATION
# ─────────────────────────────────────────
def classify_event(event_name: str) -> dict:
    """Match event name to high-impact definition."""
    name_upper = event_name.upper()
    for key, defn in HIGH_IMPACT_EVENTS.items():
        if key == "fomc_tomorrow":
            continue
        if any(n.upper() in name_upper or name_upper in n.upper()
               for n in defn["names"]):
            return defn
    return {
        "impact": "LOW", "avoid_until": "9:45 AM",
        "warning": event_name, "emoji": "📊"
    }


# ─────────────────────────────────────────
# MAIN WARNING BUILDER
# ─────────────────────────────────────────
def get_today_warnings() -> dict:
    """
    Build today's macro warnings for use in SMS and scoring.
    Called at alert time — reads from cache (populated at 7:30 AM).
    """
    now_et   = datetime.now(ZoneInfo("America/New_York"))
    today    = now_et.strftime("%Y-%m-%d")
    tomorrow = (now_et + timedelta(days=1)).strftime("%Y-%m-%d")
    if now_et.weekday() == 4:
        tomorrow = (now_et + timedelta(days=3)).strftime("%Y-%m-%d")

    today_events    = get_economic_events(today)
    tomorrow_events = get_economic_events(tomorrow)

    warnings = {
        "today_events":    today_events,
        "tomorrow_events": tomorrow_events,
        "max_impact":      "NONE",
        "advisory":        None,
        "advisory_emoji":  "✅",
        "avoid_until":     None,
        "avoid_buying":    False,
        "events_summary":  [],
        "week_events":     {},
    }

    # Check tomorrow for FOMC (raises today's IV into close)
    for event in tomorrow_events:
        defn = classify_event(event.get("event", ""))
        if defn.get("impact") == "EXTREME":
            warnings["events_summary"].append(
                f"🔴 TOMORROW {event.get('event','')} — "
                f"IV rising into today's close. Avoid buying premium after 3:00 PM today."
            )
            # Elevate today's impact
            if IMPACT_RANK.get(warnings["max_impact"], 0) < IMPACT_RANK["HIGH"]:
                warnings["max_impact"] = "HIGH"
                warnings["avoid_until"] = "3:00 PM (today, day before FOMC)"

    # Process today's events
    for event in today_events:
        defn   = classify_event(event.get("event", ""))
        impact = defn.get("impact", "LOW")

        if IMPACT_RANK.get(impact, 0) > IMPACT_RANK.get(warnings["max_impact"], 0):
            warnings["max_impact"] = impact
            warnings["avoid_until"] = defn.get("avoid_until")

        if impact == "EXTREME":
            warnings["avoid_buying"] = True

        time_et = event.get("time_et", "")
        warnings["events_summary"].append(
            f"{defn['emoji']} {time_et}: {event.get('event','')} — {defn['warning']}"
        )

    # Get week ahead (next 4 trading days)
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

    # Build advisory text
    mi = warnings["max_impact"]
    au = warnings["avoid_until"]

    if mi == "EXTREME":
        warnings["advisory"] = f"🚨 FOMC today — avoid buying premium ALL DAY."
        warnings["advisory_emoji"] = "🚨"
        warnings["avoid_buying"]   = True
    elif mi == "HIGH":
        warnings["advisory"] = (
            f"🔴 High-impact data today — "
            f"do NOT enter new positions before {au or '10:00 AM ET'}. "
            f"Options market opens 9:30 AM, first clean window {au or '10:00 AM ET'}."
        )
        warnings["advisory_emoji"] = "🔴"
    elif mi == "MEDIUM":
        warnings["advisory"] = (
            f"⚠️ Moderate macro event today — "
            f"wait until {au or '10:00 AM ET'} for spreads to normalize after open."
        )
        warnings["advisory_emoji"] = "⚠️"
    elif mi == "LOW":
        warnings["advisory"] = (
            f"📊 Minor data release today — "
            f"small volatility at 8:30 AM, options market normal by {au or '9:45 AM ET'}."
        )
        warnings["advisory_emoji"] = "📊"
    else:
        warnings["advisory"] = "✅ No major macro events today — clean trading day."
        warnings["advisory_emoji"] = "✅"

    return warnings


def get_week_ahead_summary(warnings: dict) -> list:
    """Format week-ahead events as text lines."""
    lines = []
    week  = warnings.get("week_events", {})
    for date_str, events in week.items():
        try:
            dt       = datetime.strptime(date_str, "%Y-%m-%d")
            day_name = dt.strftime("%a %b %d")
        except:
            day_name = date_str
        for event in events:
            defn = classify_event(event.get("event", ""))
            lines.append(
                f"  {defn['emoji']} {day_name}: {event.get('event','')} "
                f"[{defn['impact']}] — avoid before {defn.get('avoid_until','10 AM')}"
            )
    return lines
