"""
pair_backtest.py — Pair flow backtest runner.

Connects to Bullflow backtesting SSE endpoint, replays a full trading day
through the pair flow detection logic in an isolated state (never touches
production _PAIRS), and reports all alerts to Telegram when complete.

URL: https://api.bullflow.io/v1/streaming/backtesting?key={KEY}={DATE}&speed=60

At speed=60: 1 real second = 1 market minute → full day streams in ~6 min.
The rolling window is scaled accordingly (5-min window = 5 real seconds).

Telegram command: /pair_backtest YYYY-MM-DD
"""
import os, re, time, json, threading, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET    = ZoneInfo("America/New_York")
SPEED = 60   # Bullflow playback speed multiplier

PAIR_FILTER       = os.environ.get("PAIR_FLOW_FILTER_NAME",          "Pair_of_3_in_5_mins")
WINDOW_MINS       = float(os.environ.get("PAIR_FLOW_WINDOW_MINS",     "5"))
MIN_COUNT         = int(os.environ.get("PAIR_FLOW_MIN_COUNT",         "3"))
PREMIUM_HIGHLIGHT = float(os.environ.get("PAIR_FLOW_PREMIUM_HIGHLIGHT","200000"))

# Scaled window: 5 market minutes = 5 real seconds at 60x speed
_REAL_WINDOW_SECS = WINDOW_MINS * 60.0 / SPEED


def _build_url(api_key: str, date: str) -> str:
    return (f"https://api.bullflow.io/v1/streaming/backtesting"
            f"?key={api_key}&date={date}&speed={SPEED}")


def _parse_occ(symbol: str) -> dict | None:
    """
    Parse OCC option symbol: O:TSLA260624C00415000
    Returns dict with ticker, option_type, strike, expiry, dte.
    """
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


def _run_backtest_thread(date: str, bot_token: str, chat_id: str):
    """
    Background thread. Streams the full day, applies isolated pair flow
    detection, sends summary + detailed alerts to Telegram when complete.
    """
    from sms import send_telegram

    api_key = os.environ.get("BULLFLOW_API_KEY", "")
    if not api_key:
        send_telegram("❌ BULLFLOW_API_KEY not set", bot_token, chat_id)
        return

    url = _build_url(api_key, date)

    # Isolated state — never touches production _PAIRS dict
    bt_state:     dict = {}   # ticker_dir → {fills, last_alerted_count}
    alerts_fired: list = []
    event_count   = 0
    pair_events   = 0
    seen_names:   dict = {}   # alertName → count (for debugging)

    try:
        resp = requests.get(url, stream=True, timeout=660,
                            headers={"Accept":        "text/event-stream",
                                     "Cache-Control": "no-cache"})
        resp.raise_for_status()

        buffer = ""
        for chunk in resp.iter_content(chunk_size=2048, decode_unicode=True):
            if not chunk:
                continue
            buffer += chunk

            while "\n\n" in buffer:
                raw_event, buffer = buffer.split("\n\n", 1)
                data_lines = [l[6:] for l in raw_event.split("\n")
                              if l.startswith("data: ")]
                if not data_lines:
                    continue
                try:
                    data = json.loads(data_lines[0])
                except Exception:
                    continue

                event_count += 1

                # Unwrap nested structure: {"event":..., "data":{...actual fields...}}
                inner      = data.get("data", data)
                alert_name = inner.get("alertName", "")
                seen_names[alert_name] = seen_names.get(alert_name, 0) + 1

                # Match filter name — try exact then case-insensitive
                name_match = (alert_name == PAIR_FILTER or
                              alert_name.lower() == PAIR_FILTER.lower())
                if not name_match:
                    continue
                pair_events += 1

                # Parse contract from OCC symbol or raw fields
                symbol = inner.get("symbol", inner.get("ticker", ""))
                parsed = _parse_occ(symbol) if "O:" in str(symbol) else None
                if not parsed:
                    raw_ticker = (inner.get("ticker","") or "").upper()[:10]
                    if not raw_ticker:
                        continue
                    parsed = {
                        "ticker":      raw_ticker,
                        "option_type": inner.get("optionType","call"),
                        "strike":      str(inner.get("strikePrice","?")),
                        "expiry":      inner.get("expirationDate","?"),
                        "dte":         0,
                    }

                ticker      = parsed["ticker"]
                option_type = parsed["option_type"]
                strike      = parsed["strike"]
                expiry      = parsed["expiry"]
                dte         = parsed["dte"]
                price       = float(inner.get("tradePrice") or
                                    inner.get("alertPrice") or 0)
                premium     = float(inner.get("alertPremium") or 0)
                is_sweep    = str(inner.get("alertFillType","")).upper() in ("FULL_ASK","AA")
                stock_px    = float(inner.get("stockPrice") or 0)

                # Use actual market timestamp for accurate rolling window
                event_ts = float(inner.get("timestamp") or time.time())

                # Human-readable time from estTimestamp e.g. "2026-06-05 09:32:26 EST"
                est_str  = str(inner.get("estTimestamp",""))
                time_str = est_str[11:19] if len(est_str) >= 19 else datetime.now(ET).strftime("%-I:%M:%S %p")

                direction = "call" if "call" in option_type.lower() else "put"
                key       = f"{ticker}_{direction}"

                if key not in bt_state:
                    bt_state[key] = {"fills": [], "last_alerted_count": 0}

                fill = {
                    "strike": strike, "expiry": expiry, "price": price,
                    "premium": premium, "sweep": is_sweep, "dte": dte,
                    "stock_px": stock_px, "time": time_str, "ts": event_ts,
                }
                bt_state[key]["fills"].append(fill)

                # Use actual market timestamps for rolling window — no speed scaling needed
                cutoff       = event_ts - WINDOW_MINS * 60
                window_fills = [f for f in bt_state[key]["fills"] if f["ts"] >= cutoff]
                bt_state[key]["fills"] = window_fills
                count        = len(window_fills)
                last_alerted = bt_state[key]["last_alerted_count"]

                if count >= MIN_COUNT and count > last_alerted:
                    bt_state[key]["last_alerted_count"] = count
                    total_prem = sum(f["premium"] for f in window_fills)
                    above      = total_prem >= PREMIUM_HIGHLIGHT

                    # Span in actual market time
                    if len(window_fills) >= 2:
                        market_secs = event_ts - min(f["ts"] for f in window_fills)
                        span_str    = (f"{market_secs/60:.1f}min"
                                       if market_secs >= 60 else f"{market_secs:.0f}s")
                    else:
                        span_str = "0s"

                    alerts_fired.append({
                        "ticker":     ticker,
                        "direction":  direction,
                        "count":      count,
                        "total_prem": total_prem,
                        "above":      above,
                        "time":       time_str,
                        "fills":      list(window_fills),
                        "span":       span_str,
                    })

    except Exception as e:
        send_telegram(f"❌ Backtest error ({date}): {e}", bot_token, chat_id)
        return

    # ── Report results ─────────────────────────────────────────────────────
    if not alerts_fired:
        # Build a useful debug message showing what filter names were seen
        top_names = sorted(seen_names.items(), key=lambda x: -x[1])[:8]
        names_str = "\n".join(f"  {n!r}: {c}" for n, c in top_names) if top_names else "  (none)"
        send_telegram(
            f"📊 Pair Flow Backtest: {date}\n"
            f"No alerts found for filter: {PAIR_FILTER!r}\n"
            f"({event_count} total events)\n\n"
            f"Alert names seen in stream:\n{names_str}\n\n"
            f"If your filter name differs, set PAIR_FLOW_FILTER_NAME in Railway.",
            bot_token, chat_id)
        return

    # Summary
    summary = [
        f"📊 Pair Flow Backtest: {date}",
        f"━━━ {len(alerts_fired)} alerts fired | {pair_events} pair events ━━━",
        "",
    ]
    for a in alerts_fired:
        emoji = "📈" if a["direction"] == "call" else "📉"
        hl    = " ✅" if a["above"] else ""
        summary.append(
            f"{emoji} ${a['ticker']}  "
            f"{a['count']}x {a['direction']}  "
            f"{_fmt_prem(a['total_prem'])}{hl}  "
            f"@ {a['time']}"
        )
    send_telegram("\n".join(summary), bot_token, chat_id)

    # Detailed alert for every signal above the premium threshold
    from pair_flow_tracker import build_pair_alert
    for a in alerts_fired:
        if a["above"]:
            result = {
                "ticker":            a["ticker"],
                "direction":         a["direction"],
                "fills":             a["fills"],
                "count":             a["count"],
                "total_prem":        a["total_prem"],
                "above_highlight":   a["above"],
                "span_str":          a["span"],
                "window_mins":       WINDOW_MINS,
                "min_count":         MIN_COUNT,
                "premium_highlight": PREMIUM_HIGHLIGHT,
            }
            send_telegram(build_pair_alert(result), bot_token, chat_id)


def start_backtest(date: str, bot_token: str, chat_id: str) -> bool:
    """
    Validate date format and launch backtest in a background thread.
    Returns True if launched, False if date format is invalid.
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return False
    t = threading.Thread(
        target=_run_backtest_thread,
        args=(date, bot_token, chat_id),
        daemon=True,
        name=f"pair_backtest_{date}",
    )
    t.start()
    return True
