"""
preset_backtest_range.py — Multi-day Bullflow preset backtest → Excel.

Iterates every trading weekday in a date range, runs the same per-day
preset backtest logic (preset_backtest.collect_day), and writes results
to an .xlsx with ONE TAB PER DATE, delivered to Telegram as a document.

Weekends are skipped automatically (the options market is closed, so a
weekend replay returns an empty stream). Range is capped at 31 calendar
days per run (a full month).

Each date tab has one row per qualifying alert with columns:
  Time, Preset Type, Ticker, Direction, Strike, Expiry, DTE, Moneyness,
  Total Premium, Contracts, Trade Price, Entry, Trail Offset, Stock @ Alert,
  Earnings, Sweep

A leading Summary tab lists per-date counts + total premium.

Telegram command: /preset_backtest_range YYYY-MM-DD YYYY-MM-DD
"""
import os, re, time, threading
from datetime import datetime, timedelta

MAX_RANGE_DAYS = 31   # a full month of calendar days per run


def _weekdays_in_range(start: str, end: str) -> list:
    """Return YYYY-MM-DD strings for each Mon-Fri in [start, end] inclusive."""
    d0 = datetime.strptime(start, "%Y-%m-%d").date()
    d1 = datetime.strptime(end,   "%Y-%m-%d").date()
    out = []
    cur = d0
    while cur <= d1:
        if cur.weekday() < 5:   # 0=Mon .. 4=Fri
            out.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return out


# Column layout for each per-date sheet
_COLUMNS = [
    ("Time",          lambda a: a.get("time_str", "")),
    ("Preset Type",   lambda a: a.get("preset_type", "")),
    ("Ticker",        lambda a: a.get("ticker", "")),
    ("Direction",     lambda a: a.get("direction", "").upper()),
    ("Strike",        lambda a: a.get("strike", "")),
    ("Expiry",        lambda a: a.get("expiry", "")),
    ("DTE",           lambda a: a.get("dte", 0)),
    ("Moneyness",     lambda a: a.get("moneyness", "")),
    ("Total Premium", lambda a: round(a.get("premium", 0), 2)),
    ("Contracts",     lambda a: a.get("contracts", 0)),
    ("Trade Price",   lambda a: round(a.get("price", 0), 2)),
    ("Entry",         lambda a: round(a.get("entry_price", 0), 2)),
    ("Trail Offset",  lambda a: -round(a.get("trail_offset", 0), 2)),
    ("Stock @ Alert", lambda a: round(a.get("stock_px", 0), 2)),
    ("Earnings",      lambda a: a.get("earnings_str") or ""),
    ("Sweep",         lambda a: "YES" if a.get("sweep") else ""),
    ("30M Play",      lambda a: "REVERSAL" if a.get("playbook") == "reversal" else "FOLLOW"),
    ("Early <10:30",  lambda a: "YES — reversal risk" if a.get("is_early") else ""),
]


def _safe_sheet_title(date_str: str) -> str:
    # Excel sheet titles: <=31 chars, no : \ / ? * [ ]. Date is already safe.
    return date_str[:31]


def _build_workbook(day_results: list, start: str, end: str) -> str:
    """day_results: list of collect_day() dicts (in date order).
    Returns the path to the written .xlsx."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3864")
    center      = Alignment(horizontal="center")

    # ── Summary tab (first) ──
    summary = wb.active
    summary.title = "Summary"
    summary.append(["Date", "Alerts", "Total Premium", "Preset Events", "Note"])
    for c in range(1, 6):
        cell = summary.cell(row=1, column=c)
        cell.font = header_font; cell.fill = header_fill; cell.alignment = center

    for day in day_results:
        alerts = day["alerts"]
        total_prem = sum(a.get("premium", 0) for a in alerts)
        note = day.get("error", "") or ("no alerts" if not alerts else "")
        summary.append([day["date"], len(alerts), round(total_prem, 2),
                        day.get("preset_events", 0), note])

    for col, width in zip("ABCDE", (12, 8, 16, 14, 24)):
        summary.column_dimensions[col].width = width

    # ── One tab per date ──
    for day in day_results:
        ws = wb.create_sheet(title=_safe_sheet_title(day["date"]))
        ws.append([c[0] for c in _COLUMNS])
        for i in range(1, len(_COLUMNS) + 1):
            cell = ws.cell(row=1, column=i)
            cell.font = header_font; cell.fill = header_fill; cell.alignment = center

        # Sort alerts by time for readability
        for a in sorted(day["alerts"], key=lambda x: x.get("timestamp") or 0):
            ws.append([fn(a) for _, fn in _COLUMNS])

        # Reasonable column widths
        widths = [11, 22, 8, 10, 9, 10, 6, 10, 14, 10, 11, 9, 12, 13, 16, 7, 11, 20]
        for idx, w in enumerate(widths, start=1):
            ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = w
        ws.freeze_panes = "A2"

    out_dir = "/tmp"
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"preset_backtest_{start}_to_{end}.xlsx")
    wb.save(path)
    return path


def _run_range_thread(start: str, end: str, bot_token: str, chat_id: str):
    from sms import send_telegram, send_telegram_document
    from preset_backtest import collect_day
    import bullflow_presets as bp

    days = _weekdays_in_range(start, end)
    if not days:
        send_telegram(f"No weekdays in range {start} → {end}.", bot_token, chat_id)
        return

    send_telegram(
        f"📊 Preset range backtest: {start} → {end}\n"
        f"{len(days)} trading days | filters ≥{bp._fmt_prem(bp.MIN_PREMIUM)}, "
        f"≤{bp.MAX_DTE}d DTE\n"
        f"~{len(days)*6} min at 60x. I'll send the Excel when done.",
        bot_token, chat_id)

    day_results = []
    total_alerts = 0
    for i, date in enumerate(days, 1):
        try:
            day = collect_day(date)
        except Exception as e:
            day = {"date": date, "alerts": [], "preset_events": 0,
                   "event_count": 0, "seen_names": {}, "error": str(e)}
        day_results.append(day)
        total_alerts += len(day["alerts"])
        # Progress ping every 5 days
        if i % 5 == 0 and i < len(days):
            send_telegram(f"… {i}/{len(days)} days processed "
                          f"({total_alerts} alerts so far)", bot_token, chat_id)

    try:
        path = _build_workbook(day_results, start, end)
    except Exception as e:
        send_telegram(f"❌ Excel build error: {e}", bot_token, chat_id)
        return

    ok = send_telegram_document(
        path, bot_token, chat_id,
        caption=f"Preset backtest {start} → {end}: {total_alerts} alerts "
                f"across {len(days)} trading days (tab per date).")
    if not ok:
        send_telegram("❌ Could not send the Excel file to Telegram.", bot_token, chat_id)
    try:
        os.remove(path)
    except Exception:
        pass


def start_range_backtest(start: str, end: str, bot_token: str, chat_id: str):
    """
    Validate the range and launch in a background thread.
    Returns (ok: bool, message: str) — message explains any rejection.
    """
    if not (re.match(r'^\d{4}-\d{2}-\d{2}$', start) and re.match(r'^\d{4}-\d{2}-\d{2}$', end)):
        return False, "Both dates must be YYYY-MM-DD. e.g. /preset_backtest_range 2026-06-01 2026-06-30"
    try:
        d0 = datetime.strptime(start, "%Y-%m-%d").date()
        d1 = datetime.strptime(end,   "%Y-%m-%d").date()
    except Exception:
        return False, "Invalid date(s). Use YYYY-MM-DD."
    if d1 < d0:
        return False, "End date is before start date."
    span = (d1 - d0).days + 1
    if span > MAX_RANGE_DAYS:
        return False, f"Range too large ({span} days). Max is {MAX_RANGE_DAYS} days (a full month)."

    t = threading.Thread(target=_run_range_thread,
                         args=(start, end, bot_token, chat_id),
                         daemon=True, name=f"preset_range_{start}_{end}")
    t.start()
    return True, ""
