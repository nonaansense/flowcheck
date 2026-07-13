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
from datetime import datetime, timezone, timedelta
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
_OPT_HIST_CACHE:   dict = {}   # occ_start_end → [daily option bars]

# P/L simulation can be disabled (it costs one Tradier call per alert)
PNL_ENABLED = os.environ.get("BULLFLOW_BACKTEST_PNL", "true").lower() not in ("false","0","no","off")


def _expiry_to_iso(expiry: str) -> str:
    """'07/18/26' → '2026-07-18'. Returns '' on failure."""
    try:
        mm, dd, yy = expiry.split("/")
        return f"20{yy}-{mm}-{dd}"
    except Exception:
        return ""


_OPT_INTRADAY_CACHE: dict = {}   # occ_date → [{ts, high, low, close}] sorted

# Check the entry limit against intraday bars AFTER the alert timestamp,
# rather than the whole alert-day low (which includes pre-alert trading).
INTRADAY_FILL = os.environ.get("BULLFLOW_BACKTEST_INTRADAY_FILL", "true").lower() not in ("false","0","no","off")


def _fetch_option_intraday(occ_symbol: str, date_str: str) -> list:
    """
    15-min intraday bars for an OPTION contract on one date via Tradier
    /markets/timesales. Returns [{"ts","high","low","close"}] sorted by time.
    Cached per contract+date. [] on failure.
    """
    sym = str(occ_symbol or "").replace("O:", "").strip().upper()
    if not sym or not date_str:
        return []
    key = f"{sym}_{date_str}"
    if key in _OPT_INTRADAY_CACHE:
        return _OPT_INTRADAY_CACHE[key]

    out = []
    token = os.environ.get("TRADIER_TOKEN", "")
    if token:
        try:
            r = requests.get(
                "https://api.tradier.com/v1/markets/timesales",
                params={"symbol": sym, "interval": "15min",
                        "start": f"{date_str} 09:30", "end": f"{date_str} 16:00",
                        "session_filter": "open"},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=10,
            )
            if r.status_code == 200:
                data = ((r.json().get("series") or {}).get("data"))
                if isinstance(data, dict):
                    data = [data]
                for b in (data or []):
                    try:
                        ts = b.get("timestamp")
                        if ts is None:
                            continue
                        out.append({
                            "ts":    float(ts),
                            "high":  float(b.get("high", 0) or 0),
                            "low":   float(b.get("low", 0) or 0),
                            "close": float(b.get("close", 0) or 0),
                        })
                    except Exception:
                        continue
                out.sort(key=lambda x: x["ts"])
        except Exception as e:
            print(f"[PRESET_BT] Tradier option timesales error {sym}: {e}")

    _OPT_INTRADAY_CACHE[key] = out
    return out


def _fetch_option_daily(occ_symbol: str, start_date: str, end_date: str) -> list:
    """
    Daily OHLC bars for an OPTION contract via Tradier /markets/history.
    Tradier expects the OCC symbol WITHOUT the 'O:' prefix
    (e.g. NVDA260718C00220000). Cached. Returns [] on failure.
    """
    sym = str(occ_symbol or "").replace("O:", "").strip().upper()
    if not sym or not start_date or not end_date:
        return []
    cache_key = f"{sym}_{start_date}_{end_date}"
    if cache_key in _OPT_HIST_CACHE:
        return _OPT_HIST_CACHE[cache_key]

    bars = []
    token = os.environ.get("TRADIER_TOKEN", "")
    if token:
        try:
            r = requests.get(
                "https://api.tradier.com/v1/markets/history",
                params={"symbol": sym, "interval": "daily",
                        "start": start_date, "end": end_date},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=10,
            )
            if r.status_code == 200:
                day = (r.json().get("history") or {}).get("day")
                if isinstance(day, dict):
                    day = [day]
                for b in (day or []):
                    try:
                        bars.append({
                            "date":  str(b.get("date", "")),
                            "open":  float(b.get("open", 0) or 0),
                            "high":  float(b.get("high", 0) or 0),
                            "low":   float(b.get("low", 0) or 0),
                            "close": float(b.get("close", 0) or 0),
                        })
                    except Exception:
                        continue
                bars.sort(key=lambda x: x["date"])
        except Exception as e:
            print(f"[PRESET_BT] Tradier option history error {sym}: {e}")

    _OPT_HIST_CACHE[cache_key] = bars
    return bars


def _run_legs(window: list, entry: float, offset: float,
              t1: float, t2: float, use_trail: bool) -> dict:
    """
    Walk the price path with a 2-contract position:
      Leg 1 exits at TP1, Leg 2 exits at TP2.
      Any leg that never reaches its target exits on the trailing stop
      (if use_trail) or at the final close.

    Within a bar, targets are checked BEFORE the stop — a resting limit sell
    fills on the way up, before any later decline could trigger the stop.
    The trailing stop is NOT applied on the fill bar: that bar's low is what
    filled the entry, so it cannot also be a post-peak decline. Stop checking
    starts on the next bar. (Daily bars can't sequence intra-bar moves.)

    Returns per-leg exits + totals for 2 contracts (100 shares each).
    """
    legs = [{"target": t1, "open": True, "exit": 0.0, "reason": "", "label": "TP1"},
            {"target": t2, "open": True, "exit": 0.0, "reason": "", "label": "TP2"}]

    peak     = window[0]["high"] if window else entry
    exit_idx = 0

    for i, b in enumerate(window):
        peak = max(peak, b["high"])

        # 1) Profit targets (limit sells fill on the way up)
        for leg in legs:
            if leg["open"] and leg["target"] > 0 and b["high"] >= leg["target"]:
                leg["exit"]   = leg["target"]
                leg["reason"] = leg["label"]
                leg["open"]   = False
                exit_idx      = i

        # 2) Trailing stop takes out whatever is still open.
        #    NOT on the fill bar (i == 0): that bar's low is what FILLED us, so
        #    it can't also be a post-peak decline that stops us out. With daily
        #    bars the intra-bar sequence is unknowable, and treating the entry
        #    low as a stop trigger would be contradictory. Stop checking begins
        #    on the next bar, using the running peak.
        if use_trail and i > 0:
            stop = peak - offset
            if stop > 0 and b["low"] > 0 and b["low"] <= stop:
                for leg in legs:
                    if leg["open"]:
                        leg["exit"]   = max(stop, 0.0)
                        leg["reason"] = "trail stop"
                        leg["open"]   = False
                        exit_idx      = i

        if not any(l["open"] for l in legs):
            break

    # Anything still open rides to the last bar's close (expiry)
    if window:
        for leg in legs:
            if leg["open"]:
                leg["exit"]   = window[-1]["close"]
                leg["reason"] = "expiry"
                leg["open"]   = False
                exit_idx      = len(window) - 1

    cost   = entry * 2 * 100.0                       # 2 contracts x 100 shares
    proceeds = sum(l["exit"] for l in legs) * 100.0  # each leg = 1 contract
    pnl_usd  = round(proceeds - cost, 2)
    pnl_pct  = round((proceeds - cost) / cost * 100.0, 1) if cost > 0 else 0.0

    return {
        "leg1_exit":   round(legs[0]["exit"], 2),
        "leg1_reason": legs[0]["reason"],
        "leg2_exit":   round(legs[1]["exit"], 2),
        "leg2_reason": legs[1]["reason"],
        "pnl_usd":     pnl_usd,
        "pnl_pct":     pnl_pct,
        "exit_idx":    exit_idx,
    }


def simulate_trade(result: dict, occ_symbol: str, alert_date: str,
                   alert_epoch=None) -> dict:
    """
    Simulate a 2-CONTRACT position against the option's real price path:

      Entry — limit at entry_price (20% below the flow's trade price). On the
              ALERT DAY the limit is checked only against intraday bars AFTER
              the alert timestamp, so a fill can't be credited to price action
              that happened before the alert fired. Later days use daily bars.
      Exit  — Leg 1 sells at TP1 (+101% from entry), Leg 2 sells at TP2 (+201%).
              Any leg that never reaches its target exits on the trailing stop
              (offset = 75% of flow price below the running peak), or at the
              final close if the stop never triggers.

    Reports P/L BOTH WITH and WITHOUT the trailing stop, so the cost of the
    stop is visible. Also reports peak (MFE), heat (MAE), and max drawdown.

    Daily bars can't sequence intra-bar moves — targets are assumed to fill
    before the stop within the same bar. Directional evidence, not a
    fill-accurate backtest.
    """
    out = {"entry_filled": False, "fill_price": 0.0, "bars": 0,
           "exit_reason": "", "pnl_pct": None, "pnl_per_contract": None,
           "exit_price": 0.0,
           "max_price": None, "min_price": None, "mfe_pct": None,
           "mae_pct": None, "max_dd_pct": None, "days_held": 0,
           "fill_time": "", "fill_source": "",
           "max_profit_pct": None, "max_profit_per_contract": None,
           "target1": 0.0, "target2": 0.0, "t1_hit": False, "t2_hit": False,
           "leg1_exit": None, "leg1_reason": "", "leg2_exit": None, "leg2_reason": "",
           "pnl_usd_trail": None, "pnl_pct_trail": None,
           "pnl_usd_notrail": None, "pnl_pct_notrail": None,
           "trail_cost_usd": None}

    entry  = float(result.get("entry_price") or 0)
    offset = float(result.get("trail_offset") or 0)
    t1     = float(result.get("target1") or 0)
    t2     = float(result.get("target2") or 0)
    if entry <= 0 or offset <= 0:
        out["exit_reason"] = "no rules"
        return out

    exp_iso = _expiry_to_iso(result.get("expiry", ""))
    if not exp_iso:
        out["exit_reason"] = "bad expiry"
        return out

    # ── Build the bar path: post-alert intraday (day 0) + daily (day 1..expiry) ──
    path = []
    day0_intraday = []
    used_intraday = False

    if INTRADAY_FILL and alert_epoch:
        raw = _fetch_option_intraday(occ_symbol, alert_date)
        try:
            ae = float(alert_epoch)
            day0_intraday = [b for b in raw if b["ts"] >= ae]
        except Exception:
            day0_intraday = []
        if day0_intraday:
            used_intraday = True
            path.extend(day0_intraday)

    daily_start = alert_date
    if used_intraday:
        try:
            d0 = datetime.strptime(alert_date, "%Y-%m-%d").date()
            daily_start = (d0 + timedelta(days=1)).strftime("%Y-%m-%d")
        except Exception:
            daily_start = alert_date

    path.extend(_fetch_option_daily(occ_symbol, daily_start, exp_iso))

    out["bars"] = len(path)
    if not path:
        out["exit_reason"] = "no option data"
        return out

    # ── Entry: first bar whose low touches the limit ──
    fill_idx = None
    for i, b in enumerate(path):
        if b["low"] > 0 and b["low"] <= entry:
            fill_idx = i
            break

    if fill_idx is None:
        out["exit_reason"] = "never filled"
        return out

    out["entry_filled"] = True
    out["fill_price"]   = entry
    out["target1"]      = t1
    out["target2"]      = t2

    if used_intraday and fill_idx < len(day0_intraday):
        out["fill_source"] = "intraday (post-alert)"
        try:
            out["fill_time"] = datetime.fromtimestamp(
                day0_intraday[fill_idx]["ts"], ET).strftime("%-I:%M %p")
        except Exception:
            out["fill_time"] = ""
    else:
        out["fill_source"] = "daily bar"

    window = path[fill_idx:]

    # ── Run both scenarios ──
    with_trail = _run_legs(window, entry, offset, t1, t2, use_trail=True)
    no_trail   = _run_legs(window, entry, offset, t1, t2, use_trail=False)

    out["leg1_exit"]   = with_trail["leg1_exit"]
    out["leg1_reason"] = with_trail["leg1_reason"]
    out["leg2_exit"]   = with_trail["leg2_exit"]
    out["leg2_reason"] = with_trail["leg2_reason"]

    out["pnl_usd_trail"]   = with_trail["pnl_usd"]
    out["pnl_pct_trail"]   = with_trail["pnl_pct"]
    out["pnl_usd_notrail"] = no_trail["pnl_usd"]
    out["pnl_pct_notrail"] = no_trail["pnl_pct"]
    # Positive = the trailing stop COST you money vs just holding to targets
    out["trail_cost_usd"]  = round(no_trail["pnl_usd"] - with_trail["pnl_usd"], 2)

    # Headline P/L = the WITH-trail scenario (what your rules actually do)
    out["pnl_pct"]          = with_trail["pnl_pct"]
    out["pnl_per_contract"] = round(with_trail["pnl_usd"] / 2.0, 2)
    out["exit_price"]       = round((with_trail["leg1_exit"] + with_trail["leg2_exit"]) / 2.0, 2)
    _reasons = {with_trail["leg1_reason"], with_trail["leg2_reason"]}
    out["exit_reason"] = " + ".join(sorted(_reasons))

    # ── Excursions over the life of the trade (fill → last leg closed) ──
    life = window[: with_trail["exit_idx"] + 1] or window[:1]
    highs = [b["high"] for b in life if b["high"] > 0]
    lows  = [b["low"]  for b in life if b["low"]  > 0]

    max_price = max(highs) if highs else entry
    min_price = min(lows)  if lows  else entry

    out["max_price"]  = round(max_price, 2)
    out["min_price"]  = round(min_price, 2)
    out["mfe_pct"]    = round((max_price - entry) / entry * 100.0, 1)
    out["mae_pct"]    = round((min_price - entry) / entry * 100.0, 1)
    out["days_held"]  = len(life)

    run_peak, max_dd = 0.0, 0.0
    for b in life:
        run_peak = max(run_peak, b["high"])
        if run_peak > 0 and b["low"] > 0:
            max_dd = max(max_dd, (run_peak - b["low"]) / run_peak * 100.0)
    out["max_dd_pct"] = round(max_dd, 1)

    out["max_profit_pct"]          = out["mfe_pct"]
    out["max_profit_per_contract"] = round((max_price - entry) * 100.0, 2)

    out["t1_hit"] = bool(t1 > 0 and max_price >= t1)
    out["t2_hit"] = bool(t2 > 0 and max_price >= t2)
    return out


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
                    # Simulate the alert's own entry + trail-stop rules against
                    # the option's actual price path to produce P/L
                    if PNL_ENABLED:
                        try:
                            result["pnl"] = simulate_trade(result, symbol, date,
                                                             alert_epoch=inner.get("timestamp"))
                        except Exception as _pe:
                            print(f"[PRESET_BT] P/L sim error {ticker}: {_pe}")
                            result["pnl"] = {}
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
        _p    = a.get("pnl") or {}
        if _p.get("pnl_usd_trail") is not None:
            _sign = "🟢" if _p["pnl_usd_trail"] >= 0 else "🔴"
            pnl_s = (f" | {_sign} ${_p['pnl_usd_trail']:+,.0f} "
                     f"({_p['pnl_pct_trail']:+.0f}%)")
            _l1 = _p.get("leg1_reason", "")
            _l2 = _p.get("leg2_reason", "")
            if _l1 or _l2:
                pnl_s += f" [{_l1}/{_l2}]"
            if _p.get("pnl_usd_notrail") is not None:
                pnl_s += f" | no-trail ${_p['pnl_usd_notrail']:+,.0f}"
            if _p.get("mfe_pct") is not None:
                pnl_s += f" | peak {_p['mfe_pct']:+.0f}%"
        elif _p.get("exit_reason"):
            pnl_s = f" | ({_p['exit_reason']})"
        else:
            pnl_s = ""
        _r    = a.get("roll") or {}
        roll_s = f" | 🔁→{_r['expiry']}" if _r.get("expiry") else ""
        _ef, _es = a.get("ema_fast", 0), a.get("ema_slow", 0)
        if _ef and _es:
            ema_s = f" | 📊{'5>12' if _ef > _es else '5<12'}"
        else:
            ema_s = " | 📊no-EMA"
        summary.append(
            f"{emoji} {a['preset_type']}: ${a['ticker']} "
            f"{a['strike']}{otype} {a['expiry']}{money}  "
            f"{bp._fmt_prem(a['premium'])} | {a['contracts']:,}x | "
            f"@ {a.get('time_str','')} | {play}{ema_s}{early}{pnl_s}{roll_s}"
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
