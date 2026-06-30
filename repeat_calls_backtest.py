"""
repeat_calls_backtest.py — Repeat flow activity backtest runner.

Connects to Bullflow backtesting SSE endpoint, replays a full trading day
through the repeat flow ratio detection logic in an isolated state (never
touches production _REPEAT), and reports results to Telegram when complete.

Backtesting is for research and always includes BOTH calls and puts,
regardless of the live REPEAT_PUTS_ENABLED toggle — that toggle only
controls what the live system tracks/alerts on, not historical analysis.

URL: https://api.bullflow.io/v1/streaming/backtesting?key={KEY}&date={DATE}&speed=60

Uses actual event timestamps from the stream (not real-clock + speed
scaling) for accurate same-day accumulation tracking.

Telegram command: /repeat_backtest YYYY-MM-DD [detail]
"""
import os, re, time, json, threading, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET    = ZoneInfo("America/New_York")
SPEED = 60   # Bullflow playback speed multiplier

REPEAT_FILTER   = (os.environ.get("REPEAT_FLOW_FILTER_NAME") or
                   os.environ.get("REPEAT_CALLS_FILTER_NAME") or
                   "Repeat_Flow_Tracker")
RATIO_THRESHOLD = float(os.environ.get("REPEAT_CALLS_RATIO_THRESHOLD", "50000"))


def _puts_enabled() -> bool:
    return os.environ.get("REPEAT_PUTS_ENABLED", "true").lower() not in ("false","0","no","off")


def _build_url(api_key: str, date: str) -> str:
    return (f"https://api.bullflow.io/v1/streaming/backtesting"
            f"?key={api_key}&date={date}&speed={SPEED}")


def _parse_occ(symbol: str) -> dict | None:
    """Parse OCC option symbol: O:TSLA260624C00415000"""
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


def _strike_val(strike: str) -> float:
    try:
        return float(strike)
    except Exception:
        return 0.0


_HIST_PRICE_CACHE: dict = {}   # f"{ticker}_{date}" → price (float)


def _fetch_historical_stock_price(ticker: str, date_str: str) -> float:
    """
    Fetch the historical daily close for ticker on date_str (YYYY-MM-DD).
    The Bullflow backtest stream does NOT include a stock price field on
    any fill, so this is required to compute a meaningful premium/price
    ratio for a historical date — fetcher.fetch_price() only returns
    TODAY's live price, which would be wrong for backtests.

    Uses Tradier's /v1/markets/history endpoint (free tier, already proven
    working elsewhere in this codebase for intraday data). Finnhub's
    /stock/candle endpoint is premium-only and returns 403 on free plans,
    so it is intentionally NOT used here.

    Cached per ticker+date within the process so a backtest with
    thousands of fills only costs one Tradier call per unique ticker.
    """
    cache_key = f"{ticker}_{date_str}"
    if cache_key in _HIST_PRICE_CACHE:
        return _HIST_PRICE_CACHE[cache_key]

    price = 0.0
    token = os.environ.get("TRADIER_TOKEN", "")
    if token:
        try:
            r = requests.get(
                "https://api.tradier.com/v1/markets/history",
                params={"symbol": ticker.upper(), "interval": "daily",
                        "start": date_str, "end": date_str},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                day  = (data.get("history") or {}).get("day")
                if isinstance(day, list) and day:
                    price = float(day[0].get("close", 0) or 0)
                elif isinstance(day, dict):
                    price = float(day.get("close", 0) or 0)
            elif r.status_code == 429:
                time.sleep(1)
        except Exception as e:
            print(f"[REPEAT_BT] Tradier history error for {ticker} {date_str}: {e}")
    else:
        print(f"[REPEAT_BT] No TRADIER_TOKEN — cannot fetch historical price for {ticker}")

    _HIST_PRICE_CACHE[cache_key] = price
    return price


def _furthest_out_fill(fills: list) -> dict:
    return sorted(
        fills,
        key=lambda f: (f.get("dte", 0), _strike_val(f.get("strike", "0"))),
        reverse=True,
    )[0]


def _run_backtest_thread(date: str, bot_token: str, chat_id: str, detail: bool = False):
    """
    Background thread. Streams the full day, applies isolated repeat-flow
    detection (same-day accumulation, calls + optional puts), reports to
    Telegram when complete. Never touches production _REPEAT state.
    """
    from sms import send_telegram

    api_key = os.environ.get("BULLFLOW_API_KEY", "")
    if not api_key:
        send_telegram("❌ BULLFLOW_API_KEY not set", bot_token, chat_id)
        return

    url = _build_url(api_key, date)

    # Isolated state — never touches production _REPEAT dict
    bt_state:     dict = {}   # ticker_direction → {"fills": [...], "last_alerted_ratio": 0}
    alerts_fired: list = []
    event_count   = 0
    repeat_events = 0
    seen_names:   dict = {}
    # Backtest always tracks both calls and puts — independent of the
    # live REPEAT_PUTS_ENABLED toggle, which only affects live alerting

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

                name_match = (alert_name == REPEAT_FILTER or
                              alert_name.lower() == REPEAT_FILTER.lower())
                if not name_match:
                    continue
                repeat_events += 1

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

                direction = "call" if "call" in str(parsed["option_type"]).lower() else "put"
                # Always track both directions in backtest — no puts gate

                ticker   = parsed["ticker"]
                strike   = parsed["strike"]
                expiry   = parsed["expiry"]
                dte      = parsed["dte"]
                price    = float(inner.get("averageFillPrice") or
                                 inner.get("tradePrice")        or 0)
                premium  = float(inner.get("alertPremium") or 0)
                is_sweep = str(inner.get("alertFillType","")).upper() in ("FULL_ASK","AA")
                stock_px = float(inner.get("stockPrice") or 0)
                if not stock_px:
                    stock_px = _fetch_historical_stock_price(ticker, date)

                event_ts = float(inner.get("timestamp") or time.time())
                est_str  = str(inner.get("estTimestamp",""))
                time_str = est_str[11:19] if len(est_str) >= 19 else datetime.now(ET).strftime("%-I:%M:%S %p")

                key = f"{ticker}_{direction}"
                if key not in bt_state:
                    bt_state[key] = {"fills": [], "last_alerted_ratio": 0}

                bt_state[key]["fills"].append({
                    "strike": strike, "expiry": expiry, "price": price,
                    "premium": premium, "sweep": is_sweep, "dte": dte,
                    "stock_px": stock_px, "time": time_str, "ts": event_ts,
                })

                fills      = bt_state[key]["fills"]
                total_prem = sum(f["premium"] for f in fills)
                px_samples = [f["stock_px"] for f in fills if f.get("stock_px", 0) > 0]
                avg_px     = sum(px_samples) / len(px_samples) if px_samples else 0
                ratio      = (total_prem / avg_px) if avg_px > 0 else 0
                last_alerted = bt_state[key]["last_alerted_ratio"]

                if avg_px > 0 and ratio >= RATIO_THRESHOLD and ratio > last_alerted:
                    bt_state[key]["last_alerted_ratio"] = ratio
                    furthest = _furthest_out_fill(fills)
                    alerts_fired.append({
                        "ticker":     ticker,
                        "direction":  direction,
                        "fills":      list(fills),
                        "fill_count": len(fills),
                        "total_prem": total_prem,
                        "avg_px":     avg_px,
                        "ratio":      ratio,
                        "furthest":   furthest,
                        "time":       time_str,
                    })

    except Exception as e:
        send_telegram(f"❌ Backtest error ({date}): {e}", bot_token, chat_id)
        return

    # ── Report results ─────────────────────────────────────────────────────
    if not alerts_fired:
        top_names = sorted(seen_names.items(), key=lambda x: -x[1])[:8]
        names_str = "\n".join(f"  {n!r}: {c}" for n, c in top_names) if top_names else "  (none)"
        send_telegram(
            f"🔁 Repeat Flow Backtest: {date}\n"
            f"No alerts found for filter: {REPEAT_FILTER!r}\n"
            f"({repeat_events} matching events of {event_count} total, "
            f"calls + puts both checked)\n\n"
            f"Alert names seen in stream:\n{names_str}\n\n"
            f"If your filter name differs, set REPEAT_FLOW_FILTER_NAME in Railway.",
            bot_token, chat_id)
        return

    # Summary — one line per unique ticker+direction that crossed threshold
    # (may re-fire multiple times; show the LATEST/highest ratio per key)
    latest_per_key = {}
    for a in alerts_fired:
        latest_per_key[f"{a['ticker']}_{a['direction']}"] = a   # last write = highest ratio

    summary = [
        f"🔁 Repeat Flow Backtest: {date}",
        f"━━━ {len(latest_per_key)} signals crossed {RATIO_THRESHOLD:,.0f} ratio "
        f"| {repeat_events} events | calls + puts ━━━",
        "",
    ]
    for a in sorted(latest_per_key.values(), key=lambda x: -x["ratio"]):
        emoji = "📈" if a["direction"] == "call" else "📉"
        summary.append(
            f"{emoji} ${a['ticker']} {a['direction']}  "
            f"{a['fill_count']} fills  "
            f"{_fmt_prem(a['total_prem'])}  "
            f"ratio {a['ratio']:,.0f}  "
            f"@ {a['time']}"
        )
    send_telegram("\n".join(summary), bot_token, chat_id)

    # Detailed alert per signal — only if requested
    if detail:
        from repeat_calls_tracker import build_repeat_calls_alert
        for a in latest_per_key.values():
            result = {
                "ticker":     a["ticker"],
                "direction":  a["direction"],
                "fills":      a["fills"],
                "fill_count": a["fill_count"],
                "total_prem": a["total_prem"],
                "avg_px":     a["avg_px"],
                "ratio":      a["ratio"],
                "furthest":   a["furthest"],
                "threshold":  RATIO_THRESHOLD,
            }
            send_telegram(build_repeat_calls_alert(result), bot_token, chat_id)


def start_backtest(date: str, bot_token: str, chat_id: str, detail: bool = False) -> bool:
    """
    Validate date format and launch backtest in a background thread.
    Returns True if launched, False if date format is invalid.
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return False
    t = threading.Thread(
        target=_run_backtest_thread,
        args=(date, bot_token, chat_id, detail),
        daemon=True,
        name=f"repeat_calls_backtest_{date}",
    )
    t.start()
    return True
