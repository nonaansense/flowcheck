"""
targeted_strikes_backtest.py — Targeted strike/expiry stacking backtest runner.

Connects to Bullflow's backtesting SSE endpoint, replays a full trading day
through the targeted-strikes detection logic in isolated state (never
touches production _TARGETED), and reports results to Telegram when done.

Backtesting always tracks both calls and puts.

URL: https://api.bullflow.io/v1/streaming/backtesting?key={KEY}&date={DATE}&speed=60

Uses actual event timestamps from the stream (not real-clock + speed
scaling) for accurate same-day accumulation tracking, and the stream's
estTimestamp for the early-session (before 10:25am ET) check.

Telegram command: /targeted_backtest YYYY-MM-DD [detail]

For a date RANGE, call start_backtest_range(), which walks each trading
day sequentially (same approach as preset_backtest_range.py) and sends
one combined summary at the end.
"""
import os, re, time, json, threading, requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET    = ZoneInfo("America/New_York")
SPEED = 60   # Bullflow playback speed multiplier
MAX_RANGE_DAYS = 31   # matches preset_backtest_range.py — a full month of calendar days per run

TARGETED_FILTER  = os.environ.get("TARGETED_STRIKES_FILTER_NAME", "Targeted_Strikes_Expiry")
THRESHOLD        = int(os.environ.get("TARGETED_STRIKES_THRESHOLD", "4"))
EARLY_CUTOFF_STR = os.environ.get("TARGETED_STRIKES_EARLY_CUTOFF", "10:25")
SKIP_EARLY       = os.environ.get("TARGETED_STRIKES_SKIP_EARLY", "false").lower() in ("true","1","yes","on")
GATE_UNTIL_CUTOFF = os.environ.get("TARGETED_STRIKES_GATE_UNTIL_CUTOFF", "false").lower() in ("true","1","yes","on")


def _early_cutoff():
    try:
        hh, mm = EARLY_CUTOFF_STR.split(":")
        return int(hh), int(mm)
    except Exception:
        return 10, 25


def _is_early_str(est_str: str) -> bool:
    """est_str looks like '2026-06-01T09:42:11-04:00' or similar; fall back False."""
    try:
        hh_mm = est_str[11:16]
        hh, mm = int(hh_mm[:2]), int(hh_mm[3:5])
        cutoff_hh, cutoff_mm = _early_cutoff()
        return (hh, mm) < (cutoff_hh, cutoff_mm)
    except Exception:
        return False


def _build_url(api_key: str, date: str) -> str:
    return (f"https://api.bullflow.io/v1/streaming/backtesting"
            f"?key={api_key}&date={date}&speed={SPEED}")


def _parse_occ(symbol: str) -> dict | None:
    """Parse OCC option symbol: O:GOOGL260717C00370000"""
    if not symbol:
        return None
    m = re.search(r'O:([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d+)', symbol)
    if not m:
        return None
    ticker      = m.group(1)
    yy, mm, dd  = m.group(2), m.group(3), m.group(4)
    option_type = "call" if m.group(5) == "C" else "put"
    strike_raw  = int(m.group(6)) / 1000.0
    strike      = str(int(strike_raw)) if strike_raw == int(strike_raw) else f"{strike_raw:.1f}"
    expiry      = f"{mm}/{dd}/{yy}"
    try:
        exp_dt = datetime(int(f"20{yy}"), int(mm), int(dd), tzinfo=timezone.utc)
        dte    = max(0, (exp_dt - datetime.now(timezone.utc)).days)
    except Exception:
        dte = 0
    return {"ticker": ticker, "option_type": option_type,
            "strike": strike, "expiry": expiry, "dte": dte}


def _fmt_prem(p: float) -> str:
    return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"


def _stream_one_day(date: str, api_key: str, max_attempts: int = 4) -> tuple[list, int, int, dict]:
    """
    Streams a single day and returns (alerts_fired, event_count,
    targeted_events, seen_names). Isolated state — never touches
    production _TARGETED.

    Bullflow's backtest SSE endpoint replays a finite day and does NOT
    support resume-from-offset, so if the connection drops mid-stream we
    restart the whole day from scratch (state reset each attempt) rather
    than reconnecting — this avoids double-counting. Retries up to
    max_attempts times on premature drops / transient network errors.
    """
    url = _build_url(api_key, date)
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            return _stream_one_day_once(url, date)
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.HTTPError) as e:
            last_err = e
            if attempt < max_attempts:
                wait = 15 * attempt   # 15s, 30s, 45s backoff
                print(f"[TARGETED_BT] Stream dropped on attempt {attempt}/{max_attempts} "
                      f"({type(e).__name__}: {e}) — restarting day in {wait}s")
                time.sleep(wait)
            else:
                print(f"[TARGETED_BT] Stream failed after {max_attempts} attempts: {e}")
        except Exception as e:
            # Non-network error — don't retry blindly, surface it.
            raise
    # Exhausted retries — re-raise the last network error for the caller to report.
    raise last_err


def _stream_one_day_once(url: str, date: str) -> tuple[list, int, int, dict]:
    """One streaming attempt. Fresh state; raises on premature drop."""
    bt_state:        dict = {}   # ticker_direction → {"strike","expiry","fills":[...],"last_alerted_count"}
    alerts_fired:    list = []
    event_count      = 0
    targeted_events  = 0
    seen_names:      dict = {}

    resp = requests.get(url, stream=True, timeout=660,
                        headers={"Accept": "text/event-stream",
                                 "Cache-Control": "no-cache"})
    resp.raise_for_status()

    buffer = ""
    for chunk in resp.iter_content(chunk_size=2048, decode_unicode=True):
        if not chunk:
            continue
        buffer += chunk

        while "\n\n" in buffer:
            raw_event, buffer = buffer.split("\n\n", 1)
            data_lines = [l[6:] for l in raw_event.split("\n") if l.startswith("data: ")]
            if not data_lines:
                continue
            try:
                data = json.loads(data_lines[0])
            except Exception:
                continue

            event_count += 1
            inner      = data.get("data", data)
            alert_name = inner.get("alertName", "")
            seen_names[alert_name] = seen_names.get(alert_name, 0) + 1

            name_match = (alert_name == TARGETED_FILTER or
                          alert_name.lower() == TARGETED_FILTER.lower())
            if not name_match:
                continue
            targeted_events += 1

            symbol = inner.get("symbol", inner.get("ticker", ""))
            parsed = _parse_occ(symbol) if "O:" in str(symbol) else None
            if not parsed:
                raw_ticker = (inner.get("ticker", "") or "").upper()[:10]
                if not raw_ticker:
                    continue
                parsed = {
                    "ticker":      raw_ticker,
                    "option_type": inner.get("optionType", "call"),
                    "strike":      str(inner.get("strikePrice", "?")),
                    "expiry":      inner.get("expirationDate", "?"),
                    "dte":         0,
                }

            direction = "call" if "call" in str(parsed["option_type"]).lower() else "put"
            ticker    = parsed["ticker"]
            strike    = parsed["strike"]
            expiry    = parsed["expiry"]
            price     = float(inner.get("averageFillPrice") or inner.get("tradePrice") or 0)
            premium   = float(inner.get("alertPremium") or 0)
            is_sweep  = str(inner.get("alertFillType", "")).upper() in ("FULL_ASK", "AA")
            stock_px  = float(inner.get("stockPrice") or 0)

            event_ts = float(inner.get("timestamp") or time.time())
            est_str  = str(inner.get("estTimestamp", ""))
            time_str = est_str[11:19] if len(est_str) >= 19 else datetime.now(ET).strftime("%-I:%M:%S %p")
            early    = _is_early_str(est_str)

            if SKIP_EARLY and early:
                continue   # drop pre-cutoff fill entirely — not counted, not stored

            key = f"{ticker}_{direction}"   # streak is per ticker+direction
            fill = {
                "strike": strike, "expiry": expiry, "price": price,
                "premium": premium, "sweep": is_sweep, "dte": parsed["dte"],
                "stock_px": stock_px, "time": time_str, "ts": event_ts, "early": early,
            }

            streak = bt_state.get(key)
            same_contract = (streak is not None
                             and streak.get("strike") == strike
                             and streak.get("expiry") == expiry)

            if same_contract:
                # Continue the current consecutive run.
                streak["fills"].append(fill)
            else:
                # First fill for this ticker+dir, OR a same-direction fill at a
                # DIFFERENT strike/expiry -> previous run breaks; start fresh.
                # (Opposite direction / other tickers live under their own key,
                #  so they never reach here and never break this streak.)
                streak = {"strike": strike, "expiry": expiry,
                          "fills": [fill], "last_alerted_count": 0}
                bt_state[key] = streak

            fills        = streak["fills"]
            count        = len(fills)
            last_alerted = streak["last_alerted_count"]

            # GATE_UNTIL_CUTOFF: hold the alert while the triggering fill is
            # pre-cutoff (early=True); don't advance last_alerted so a later
            # at/after-cutoff fill fires with the full accumulated count.
            gated = GATE_UNTIL_CUTOFF and early

            if count >= THRESHOLD and count > last_alerted and not gated:
                streak["last_alerted_count"] = count
                alerts_fired.append({
                    "ticker":     ticker,
                    "strike":     strike,
                    "expiry":     expiry,
                    "direction":  direction,
                    "fills":      list(fills),
                    "count":      count,
                    "total_prem": sum(f["premium"] for f in fills),
                    "is_addon":   last_alerted > 0,
                    "early":      any(f.get("early") for f in fills),
                    "date":       date,
                    "time":       time_str,
                })

    return alerts_fired, event_count, targeted_events, seen_names


def _run_backtest_thread(date: str, bot_token: str, chat_id: str, detail: bool = False):
    from sms import send_telegram

    api_key = os.environ.get("BULLFLOW_API_KEY", "")
    if not api_key:
        send_telegram("❌ BULLFLOW_API_KEY not set", bot_token, chat_id)
        return

    try:
        alerts_fired, event_count, targeted_events, seen_names = _stream_one_day(date, api_key)
    except (requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout) as e:
        send_telegram(
            f"❌ Backtest for {date} failed: Bullflow stream kept dropping "
            f"after several retries ({type(e).__name__}). This is usually a "
            f"transient Bullflow/network issue — try again in a minute.",
            bot_token, chat_id)
        return
    except Exception as e:
        send_telegram(f"❌ Backtest error ({date}): {e}", bot_token, chat_id)
        return

    _report_results(date, alerts_fired, event_count, targeted_events, seen_names,
                     bot_token, chat_id, detail)


def _report_results(date: str, alerts_fired: list, event_count: int, targeted_events: int,
                     seen_names: dict, bot_token: str, chat_id: str, detail: bool):
    from sms import send_telegram

    if not alerts_fired:
        top_names = sorted(seen_names.items(), key=lambda x: -x[1])[:8]
        names_str = "\n".join(f"  {n!r}: {c}" for n, c in top_names) if top_names else "  (none)"
        send_telegram(
            f"🎯 Targeted Strikes Backtest: {date}\n"
            f"No alerts found for filter: {TARGETED_FILTER!r}\n"
            f"({targeted_events} matching events of {event_count} total)\n\n"
            f"Alert names seen in stream:\n{names_str}\n\n"
            f"If your filter name differs, set TARGETED_STRIKES_FILTER_NAME in Railway.",
            bot_token, chat_id)
        return

    # One line per unique key that crossed threshold — LATEST/highest count per key
    latest_per_key = {}
    for a in alerts_fired:
        latest_per_key[f"{a['ticker']}_{a['strike']}_{a['expiry']}_{a['direction']}"] = a
    early_count = sum(1 for a in alerts_fired if a["early"])

    summary = [
        f"🎯 Targeted Strikes Backtest: {date}",
        f"━━━ {len(latest_per_key)} strikes crossed {THRESHOLD}x "
        f"| {len(alerts_fired)} total alerts ({early_count} early-session) "
        f"| {targeted_events} events ━━━",
        "",
    ]
    for a in sorted(latest_per_key.values(), key=lambda x: -x["count"]):
        emoji  = "📈" if a["direction"] == "call" else "📉"
        otype  = "C" if a["direction"] == "call" else "P"
        early_s = " ⏰EARLY" if a["early"] else ""
        fills   = a.get("fills", [])
        # time the run first crossed THRESHOLD = the THRESHOLD-th fill's time
        cross_time = fills[THRESHOLD - 1]["time"] if len(fills) >= THRESHOLD else a["time"]
        latest_time = a["time"]
        if a["count"] > THRESHOLD:
            time_str = f"crossed {THRESHOLD}x @ {cross_time}, latest @ {latest_time}"
        else:
            time_str = f"@ {cross_time}"
        summary.append(
            f"{emoji} ${a['ticker']} {a['strike']}{otype} {a['expiry']}  "
            f"{a['count']}x  {_fmt_prem(a['total_prem'])}  {time_str}{early_s}"
        )
    send_telegram("\n".join(summary), bot_token, chat_id)

    if detail:
        from targeted_strikes_tracker import build_targeted_strikes_alert
        for a in latest_per_key.values():
            result = {
                "ticker": a["ticker"], "strike": a["strike"], "expiry": a["expiry"],
                "direction": a["direction"], "fills": a["fills"], "count": a["count"],
                "total_prem": a["total_prem"], "is_addon": a["is_addon"],
                "early": a["early"], "threshold": THRESHOLD,
            }
            send_telegram(build_targeted_strikes_alert(result), bot_token, chat_id)


def _build_range_workbook(all_alerts: list, start_date: str, end_date: str) -> str:
    """One row per alert (including add-ons), sorted by date/time. Returns .xlsx path."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Targeted Strikes"

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F3864")
    center      = Alignment(horizontal="center")

    from openpyxl.utils import get_column_letter

    cols = ["Date", f"Crossed {THRESHOLD}x Time", "Fill Time", "Ticker", "Direction",
            "Strike", "Expiry", "Count", "Add-On", "Early Session", "Combined Premium"]
    ws.append(cols)
    for c in range(1, len(cols) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = header_font; cell.fill = header_fill; cell.alignment = center

    for a in sorted(all_alerts, key=lambda x: (x["date"], x["time"])):
        fills = a.get("fills", [])
        cross_time = fills[THRESHOLD - 1]["time"] if len(fills) >= THRESHOLD else a["time"]
        ws.append([
            a["date"], cross_time, a["time"], a["ticker"], a["direction"].upper(),
            a["strike"], a["expiry"], a["count"],
            "YES" if a["is_addon"] else "",
            "YES" if a["early"] else "",
            round(a["total_prem"], 2),
        ])

    for i, col in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = max(12, len(col) + 2)

    path = f"/tmp/targeted_strikes_{start_date}_to_{end_date}.xlsx"
    wb.save(path)
    return path


def _run_range_thread(start_date: str, end_date: str, bot_token: str, chat_id: str, detail: bool = False):
    from sms import send_telegram

    api_key = os.environ.get("BULLFLOW_API_KEY", "")
    if not api_key:
        send_telegram("❌ BULLFLOW_API_KEY not set", bot_token, chat_id)
        return

    start = datetime.strptime(start_date, "%Y-%m-%d")
    end   = datetime.strptime(end_date, "%Y-%m-%d")
    if end < start:
        send_telegram("❌ End date is before start date", bot_token, chat_id)
        return
    span = (end - start).days
    if span > MAX_RANGE_DAYS:
        send_telegram(f"❌ Range too large ({span} days). Max is {MAX_RANGE_DAYS} days (a full month).",
                       bot_token, chat_id)
        return

    all_alerts, total_events, total_targeted = [], 0, 0
    day = start
    days_run = 0
    while day <= end:
        if day.weekday() < 5:  # skip weekends
            date_str = day.strftime("%Y-%m-%d")
            try:
                alerts, events, targeted, _ = _stream_one_day(date_str, api_key)
                all_alerts.extend(alerts)
                total_events += events
                total_targeted += targeted
                days_run += 1
            except Exception as e:
                send_telegram(f"⚠️ Skipped {date_str}: {e}", bot_token, chat_id)
        day += timedelta(days=1)

    range_label = f"{start_date} → {end_date} ({days_run} trading days)"
    if not all_alerts:
        send_telegram(
            f"🎯 Targeted Strikes Range Backtest: {range_label}\n"
            f"No alerts found for filter {TARGETED_FILTER!r} "
            f"({total_targeted} matching events of {total_events} total).",
            bot_token, chat_id)
        return

    latest_per_key = {}
    for a in all_alerts:
        latest_per_key[f"{a['date']}_{a['ticker']}_{a['strike']}_{a['expiry']}_{a['direction']}"] = a
    early_count = sum(1 for a in all_alerts if a["early"])
    by_ticker: dict = {}
    for a in latest_per_key.values():
        by_ticker[a["ticker"]] = by_ticker.get(a["ticker"], 0) + 1

    top_tickers = sorted(by_ticker.items(), key=lambda x: -x[1])[:10]
    summary = [
        f"🎯 Targeted Strikes Range Backtest: {range_label}",
        f"━━━ {len(latest_per_key)} distinct strikes crossed {THRESHOLD}x "
        f"| {len(all_alerts)} total alerts ({early_count} early-session) ━━━",
        "",
        "Top tickers: " + ", ".join(f"{t}({c})" for t, c in top_tickers),
        "",
    ]
    for a in sorted(latest_per_key.values(), key=lambda x: (x["date"], -x["count"]))[:40]:
        emoji  = "📈" if a["direction"] == "call" else "📉"
        otype  = "C" if a["direction"] == "call" else "P"
        early_s = " ⏰" if a["early"] else ""
        fills   = a.get("fills", [])
        cross_time = fills[THRESHOLD - 1]["time"] if len(fills) >= THRESHOLD else a["time"]
        if a["count"] > THRESHOLD:
            time_str = f"{THRESHOLD}x@{cross_time} → {a['count']}x@{a['time']}"
        else:
            time_str = f"@{cross_time}"
        summary.append(
            f"{a['date']} {emoji} ${a['ticker']} {a['strike']}{otype} {a['expiry']}  "
            f"{a['count']}x  {_fmt_prem(a['total_prem'])}  {time_str}{early_s}"
        )
    if len(latest_per_key) > 40:
        summary.append(f"... and {len(latest_per_key) - 40} more — see attached workbook")

    send_telegram("\n".join(summary), bot_token, chat_id)

    try:
        from sms import send_telegram_document
        xlsx_path = _build_range_workbook(all_alerts, start_date, end_date)
        send_telegram_document(
            xlsx_path, bot_token, chat_id,
            caption=f"Targeted Strikes: {range_label} — every alert, one row each"
        )
    except Exception as e:
        send_telegram(f"⚠️ Could not build/send workbook: {e}", bot_token, chat_id)


def start_backtest(date: str, bot_token: str, chat_id: str, detail: bool = False) -> bool:
    """Validate date format and launch a single-day backtest in a background thread."""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return False
    t = threading.Thread(
        target=_run_backtest_thread,
        args=(date, bot_token, chat_id, detail),
        daemon=True,
        name=f"targeted_strikes_backtest_{date}",
    )
    t.start()
    return True


def start_backtest_range(start_date: str, end_date: str, bot_token: str, chat_id: str, detail: bool = False) -> bool:
    """Validate date formats and launch a multi-day backtest in a background thread."""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', start_date) or not re.match(r'^\d{4}-\d{2}-\d{2}$', end_date):
        return False
    t = threading.Thread(
        target=_run_range_thread,
        args=(start_date, end_date, bot_token, chat_id, detail),
        daemon=True,
        name=f"targeted_strikes_range_{start_date}_{end_date}",
    )
    t.start()
    return True
