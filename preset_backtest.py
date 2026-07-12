"""
preset_backtest.py — Bullflow pre-defined alert backtest runner.

Connects to the Bullflow backtesting SSE endpoint, replays a full trading
day, and reports every pre-defined preset alert (Discord Trade, Sizable
Sweep, Urgent Repeater, Grenade Trade, Bullflow Repeater, Position
Building Repeater) that clears the premium + DTE filters — enriched with
moneyness, contract count, suggested entry, and trailing-stop offset.

URL: https://api.bullflow.io/v1/streaming/backtesting?key={KEY}&date={DATE}&speed=60

Uses actual event timestamps from the stream (not real-clock + speed
scaling). Stock price for moneyness is matched to each alert's timestamp
using Tradier 15-min intraday candles (the backtest stream has no
stockPrice field), so a 9:32 AM alert and a 3:43 PM alert on the same
ticker use their own point-in-time prices — not a single daily close.
Falls back to the daily close if intraday data is unavailable.

Telegram command: /preset_backtest YYYY-MM-DD [detail]
"""
import os, re, time, json, threading, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET    = ZoneInfo("America/New_York")
SPEED = 60


def _build_url(api_key: str, date: str) -> str:
    return (f"https://api.bullflow.io/v1/streaming/backtesting"
            f"?key={api_key}&date={date}&speed={SPEED}")


def _parse_occ(symbol: str, ref_date: str = None) -> dict | None:
    """Parse OCC option symbol. DTE is measured from ref_date (the backtest
    date, YYYY-MM-DD) when provided, NOT from today — otherwise a backtest
    of a past date would compute wrong DTE and let long-dated contracts
    slip through the DTE filter."""
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
        if ref_date:
            base_dt = datetime.strptime(ref_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            base_dt = datetime.now(timezone.utc)
        dte    = max(0, (exp_dt - base_dt).days)
    except Exception:
        dte = 0
    return {"ticker": ticker, "option_type": option_type,
            "strike": strike, "expiry": expiry, "dte": dte}


_HIST_PRICE_CACHE: dict = {}   # ticker_date → daily close (fallback)
_INTRADAY_CACHE:   dict = {}   # ticker_date → [(epoch, price), ...] sorted by time


def _fetch_intraday_series(ticker: str, date_str: str) -> list:
    """
    Fetch the backtest day's 15-min candles via Tradier /markets/timesales
    and return a sorted list of (epoch_seconds, close_price) tuples so an
    alert's timestamp can be matched to the price AT THAT TIME (not the
    end-of-day close). Cached per ticker+date. Returns [] on failure.
    """
    cache_key = f"{ticker}_{date_str}"
    if cache_key in _INTRADAY_CACHE:
        return _INTRADAY_CACHE[cache_key]

    series_out = []
    token = os.environ.get("TRADIER_TOKEN", "")
    if token:
        try:
            r = requests.get(
                "https://api.tradier.com/v1/markets/timesales",
                params={"symbol": ticker.upper(), "interval": "15min",
                        "start": f"{date_str} 09:30", "end": f"{date_str} 16:00",
                        "session_filter": "open"},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=10,
            )
            if r.status_code == 200:
                data = ((r.json().get("series") or {}).get("data"))
                if isinstance(data, dict):
                    data = [data]
                for bar in (data or []):
                    # Each bar has 'time' (ET ISO string) and 'close'; 'timestamp' is epoch
                    epoch = bar.get("timestamp")
                    close = bar.get("close")
                    if epoch is not None and close:
                        series_out.append((float(epoch), float(close)))
                series_out.sort(key=lambda x: x[0])
        except Exception as e:
            print(f"[PRESET_BT] Tradier timesales error {ticker} {date_str}: {e}")

    _INTRADAY_CACHE[cache_key] = series_out
    return series_out


def _price_at_time(ticker: str, date_str: str, event_epoch) -> float:
    """
    Return the stock price at the alert's timestamp using intraday candles:
    the close of the most recent 15-min bar at or before the alert time
    (or the nearest bar if the alert precedes the first candle). Falls back
    to the daily close if intraday data is unavailable.
    """
    series = _fetch_intraday_series(ticker, date_str)
    if series and event_epoch:
        try:
            ev = float(event_epoch)
            # Most recent bar at or before the event
            chosen = None
            for epoch, close in series:
                if epoch <= ev:
                    chosen = close
                else:
                    break
            # Alert before first candle → use the first available bar
            if chosen is None:
                chosen = series[0][1]
            return chosen
        except Exception:
            pass
    # Fallback: daily close
    return _fetch_historical_stock_price(ticker, date_str)


def _fetch_historical_stock_price(ticker: str, date_str: str) -> float:
    """Historical daily close via Tradier /markets/history (free tier),
    cached per ticker+date. Fallback when intraday candles are unavailable."""
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
                day = (r.json().get("history") or {}).get("day")
                if isinstance(day, list) and day:
                    price = float(day[0].get("close", 0) or 0)
                elif isinstance(day, dict):
                    price = float(day.get("close", 0) or 0)
        except Exception as e:
            print(f"[PRESET_BT] Tradier history error {ticker} {date_str}: {e}")
    _HIST_PRICE_CACHE[cache_key] = price
    return price


def collect_day(date: str) -> dict:
    """
    Stream one trading day and return its preset results WITHOUT sending to
    Telegram. Used by both the single-day backtest and the range export.

    Returns {"date","alerts":[result,...],"preset_events","event_count",
             "seen_names","error"(optional)}.
    """
    import bullflow_presets as bp

    api_key = os.environ.get("BULLFLOW_API_KEY", "")
    if not api_key:
        return {"date": date, "alerts": [], "preset_events": 0,
                "event_count": 0, "seen_names": {}, "error": "BULLFLOW_API_KEY not set"}

    url = _build_url(api_key, date)
    alerts_fired: list = []
    event_count   = 0
    preset_events = 0
    seen_names:   dict = {}

    try:
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
                data_lines = [l[6:] for l in raw_event.split("\n")
                              if l.startswith("data: ")]
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

                if not bp.is_preset(alert_name):
                    continue
                preset_events += 1

                symbol = inner.get("symbol", inner.get("ticker", ""))
                parsed = _parse_occ(symbol, ref_date=date) if "O:" in str(symbol) else None
                if not parsed:
                    continue

                ticker   = parsed["ticker"]
                price    = float(inner.get("averageFillPrice") or
                                 inner.get("tradePrice")        or 0)
                premium  = float(inner.get("alertPremium") or 0)
                is_sweep = str(inner.get("alertFillType","")).upper() in ("FULL_ASK","AA")
                stock_px = float(inner.get("stockPrice") or 0)
                if not stock_px:
                    stock_px = _price_at_time(ticker, date, inner.get("timestamp"))

                fill = {
                    "ticker":       ticker,
                    "option_type":  parsed["option_type"],
                    "strike":       parsed["strike"],
                    "expiry":       parsed["expiry"],
                    "dte":          parsed["dte"],
                    "option_price": price,
                    "premium":      premium,
                    "is_sweep":     is_sweep,
                    "stock_price":  stock_px,
                    "timestamp":    inner.get("timestamp"),
                    "est_timestamp": inner.get("estTimestamp", ""),
                }
                result = bp.process_preset(fill, alert_name)
                if result:
                    alerts_fired.append(result)

    except Exception as e:
        return {"date": date, "alerts": alerts_fired, "preset_events": preset_events,
                "event_count": event_count, "seen_names": seen_names, "error": str(e)}

    return {"date": date, "alerts": alerts_fired, "preset_events": preset_events,
            "event_count": event_count, "seen_names": seen_names}


def _run_backtest_thread(date: str, bot_token: str, chat_id: str, detail: bool = False):
    from sms import send_telegram
    import bullflow_presets as bp

    if not os.environ.get("BULLFLOW_API_KEY", ""):
        send_telegram("❌ BULLFLOW_API_KEY not set", bot_token, chat_id)
        return

    day = collect_day(date)
    if day.get("error"):
        send_telegram(f"❌ Backtest error ({date}): {day['error']}", bot_token, chat_id)
        return

    alerts_fired = day["alerts"]
    preset_events = day["preset_events"]
    event_count   = day["event_count"]
    seen_names    = day["seen_names"]

    # ── Report ──
    if not alerts_fired:
        top_names = sorted(seen_names.items(), key=lambda x: -x[1])[:8]
        names_str = "\n".join(f"  {n!r}: {c}" for n, c in top_names) if top_names else "  (none)"
        send_telegram(
            f"🔔 Preset Backtest: {date}\n"
            f"No preset alerts cleared filters "
            f"(≥{bp._fmt_prem(bp.MIN_PREMIUM)}, ≤{bp.MAX_DTE}d DTE)\n"
            f"({preset_events} preset events of {event_count} total)\n\n"
            f"Alert names seen in stream:\n{names_str}",
            bot_token, chat_id)
        return

    # Summary — one line each
    summary = [
        f"🔔 Preset Backtest: {date}",
        f"━━━ {len(alerts_fired)} alerts | {preset_events} preset events ━━━",
        "",
    ]
    for a in alerts_fired:
        emoji = "📈" if a["direction"] == "call" else "📉"
        otype = "C" if a["direction"] == "call" else "P"
        money = f" [{a['moneyness']}]" if a.get("moneyness") else ""
        play  = "🔄REV" if a.get("playbook") == "reversal" else "➡️FOLLOW"
        early = " ⚠️EARLY" if a.get("is_early") else ""
        summary.append(
            f"{emoji} {a['preset_type']}: ${a['ticker']} "
            f"{a['strike']}{otype} {a['expiry']}{money}  "
            f"{bp._fmt_prem(a['premium'])} | {a['contracts']:,}x | "
            f"@ {a.get('time_str','')} | {play}{early}"
        )
    send_telegram("\n".join(summary), bot_token, chat_id)

    # Detail — full alert card each
    if detail:
        for a in alerts_fired:
            send_telegram(bp.build_preset_alert(a), bot_token, chat_id)


def start_backtest(date: str, bot_token: str, chat_id: str, detail: bool = False) -> bool:
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return False
    t = threading.Thread(
        target=_run_backtest_thread,
        args=(date, bot_token, chat_id, detail),
        daemon=True,
        name=f"preset_backtest_{date}",
    )
    t.start()
    return True
