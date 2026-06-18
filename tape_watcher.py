"""
tape_watcher.py — Multi-day repeat buyer / tape watching detector.

Monitors Bullflow "Retail_Order_Flow" alert stream.
When the same ticker + strike + expiry appears 2+ times with trade price
same or higher → fires immediately to TRADE channel.

Persists to Supabase so multi-day accumulation is detected across
redeployments and overnight gaps.
"""
import os
import time
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ── Storage keys ──────────────────────────────────────────────────────
TAPE_STORAGE_KEY  = "tape_history"
TAPE_WINDOW_DAYS  = int(os.environ.get("TAPE_WINDOW_DAYS", "5"))
TAPE_WINDOW_HOURS = float(os.environ.get("TAPE_WINDOW_HOURS", "8"))  # kept for compat

# ── In-memory tape store — loaded from Supabase on first access ───────
_TAPE: dict = {}
_loaded = False


def _load_tape():
    """Load tape history from Supabase into memory."""
    global _TAPE, _loaded
    if _loaded:
        return
    try:
        from storage import db_get
        raw = db_get(TAPE_STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _TAPE = raw
            print(f"[TAPE] Loaded {len(_TAPE)} entries from Supabase")
        elif raw and isinstance(raw, str):
            _TAPE = json.loads(raw)
            print(f"[TAPE] Loaded {len(_TAPE)} entries from Supabase")
    except Exception as e:
        print(f"[TAPE] Load error: {e} — starting fresh")
    _loaded = True


def _save_tape():
    """Persist tape history to Supabase."""
    try:
        from storage import db_set
        db_set(TAPE_STORAGE_KEY, _TAPE)
    except Exception as e:
        print(f"[TAPE] Save error: {e}")


def _tape_key(ticker: str, strike: str, expiry: str) -> str:
    return f"{ticker.upper()}_{strike}_{expiry}"


def _format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime("%-I:%M %p")


def _format_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime("%b %-d")


def _pct_change(first: float, latest: float) -> str:
    if not first or first == 0:
        return ""
    pct = (latest - first) / first * 100
    if pct > 0.05:    return f" ↑{pct:.1f}%"
    elif pct < -0.05: return f" ↓{abs(pct):.1f}%"
    else:             return " (same price)"


def _ordinal(n: int) -> str:
    n = int(n)
    if 11 <= (n % 100) <= 13: return f"{n}th"
    return f"{n}{['th','st','nd','rd','th'][min(n % 10, 4)]}"


def _prune_stale():
    """Remove expired and too-old entries from the tape store."""
    now    = time.time()
    cutoff = now - TAPE_WINDOW_DAYS * 86400
    today  = datetime.now(ET).date()
    to_del = []
    for key, entry in _TAPE.items():
        # Remove if beyond window
        if entry.get("first_ts", 0) < cutoff:
            to_del.append(key)
            continue
        # Remove if option has expired
        expiry = entry.get("expiry", "")
        if expiry:
            try:
                parts = expiry.split("/")
                if len(parts) == 3:
                    m, d, y = parts
                    y = "20" + y if len(y) == 2 else y
                    exp_date = datetime.strptime(f"{y}-{m}-{d}", "%Y-%m-%d").date()
                    if exp_date < today:
                        to_del.append(key)
            except:
                pass
    for k in to_del:
        del _TAPE[k]


def process_tape(alert: dict) -> dict | None:
    """
    Process incoming Bullflow alert through tape watcher.
    Persists state to Supabase for multi-day detection.
    Returns alert dict if repeat buyer detected, None otherwise.
    """
    _load_tape()
    _prune_stale()

    ticker    = str(alert.get("ticker", "") or "").upper()
    strike    = str(alert.get("strike", "") or "")
    expiry    = str(alert.get("expiry", "") or "")
    trade_px  = float(alert.get("trade_price") or alert.get("option_price") or
                      alert.get("averageFillPrice") or 0)
    premium   = float(alert.get("premium") or alert.get("alertPremium") or 0)
    fill_type = str(alert.get("fill_type", "") or "")
    is_sweep  = bool(alert.get("is_sweep") or False)
    stock_px  = float(alert.get("stock_price") or 0)
    otm_pct   = float(alert.get("otm_pct") or 0)
    dte       = int(alert.get("dte") or 0)
    now       = time.time()

    if not ticker or not strike or not expiry:
        return None

    key = _tape_key(ticker, strike, expiry)
    today_str = datetime.now(ET).strftime("%b %-d")

    # ── First occurrence — store and persist ──────────────────────────
    if key not in _TAPE:
        _TAPE[key] = {
            "ticker":      ticker,
            "strike":      strike,
            "expiry":      expiry,
            "first_ts":    now,
            "first_price": trade_px,
            "flows": [{
                "price":    trade_px,
                "premium":  premium,
                "fill":     fill_type,
                "sweep":    is_sweep,
                "stock_px": stock_px,
                "otm_pct":  otm_pct,
                "ts":       now,
                "date":     today_str,
            }],
            "alert_count":    0,
            "last_alerted_ts": 0,
        }
        _save_tape()
        print(f"[TAPE] 📋 First flow: {ticker} {strike} {expiry} @ ${trade_px:.2f} "
              f"(persisted to Supabase)")
        return None

    entry       = _TAPE[key]
    first_price = entry["first_price"]

    # ── Price dropped >2% from first — record but don't alert ─────────
    if trade_px < first_price * 0.98:
        print(f"[TAPE] ⬇️  Price drop — {ticker} {strike} {expiry}: "
              f"${first_price:.2f} → ${trade_px:.2f} — not confirming")
        entry["flows"].append({
            "price": trade_px, "premium": premium, "fill": fill_type,
            "sweep": is_sweep, "stock_px": stock_px, "otm_pct": otm_pct,
            "ts": now, "date": today_str,
        })
        _save_tape()
        return None

    # ── Repeat buyer confirmed ─────────────────────────────────────────
    entry["flows"].append({
        "price": trade_px, "premium": premium, "fill": fill_type,
        "sweep": is_sweep, "stock_px": stock_px, "otm_pct": otm_pct,
        "ts": now, "date": today_str,
    })
    entry["alert_count"] += 1
    entry["last_alerted_ts"] = now
    _save_tape()

    occurrence = len(entry["flows"])
    # Count unique trading days
    unique_days = len(set(f.get("date", "") for f in entry["flows"]))
    multi_day   = unique_days > 1

    print(f"[TAPE] 🔥 Repeat buyer #{occurrence} over {unique_days}d: "
          f"{ticker} {strike} {expiry} ${first_price:.2f} → ${trade_px:.2f}")

    return {
        "ticker":        ticker,
        "strike":        strike,
        "expiry":        expiry,
        "option_type":   alert.get("option_type", "call"),
        "flows":         list(entry["flows"]),
        "first_price":   first_price,
        "latest_price":  trade_px,
        "occurrence":    occurrence,
        "unique_days":   unique_days,
        "multi_day":     multi_day,
        "total_premium": sum(f["premium"] for f in entry["flows"]),
        "stock_px":      (stock_px or
                          next((f["stock_px"] for f in reversed(entry["flows"])
                               if f.get("stock_px")), 0)),
        "otm_pct":       otm_pct,
        "dte":           dte,
        "earnings_str":  alert.get("earnings_str"),
        "iv_pct":        alert.get("iv_pct"),
        "iv_rank":       alert.get("iv_rank"),
        "iv_note":       alert.get("iv_note"),
        "news":          alert.get("news", []),
        "stn_risk":      alert.get("stn_risk"),
        "stn_note":      alert.get("stn_note"),
        "ipo_note":      alert.get("ipo_note"),
    }


def build_tape_alert(result: dict, alert_name: str) -> str:
    """Build the Telegram alert message for a confirmed repeat buyer."""
    ticker      = result["ticker"]
    strike      = result["strike"]
    expiry      = result["expiry"]
    flows       = result["flows"]
    occ         = result["occurrence"]
    stock_px    = result["stock_px"]
    otm_pct     = result["otm_pct"]
    dte         = result["dte"]
    total       = result["total_premium"]
    first_px    = result["first_price"]
    last_px     = result["latest_price"]
    unique_days = result.get("unique_days", 1)
    multi_day   = result.get("multi_day", False)
    earn_str    = result.get("earnings_str")
    iv_pct      = result.get("iv_pct")
    iv_rank     = result.get("iv_rank")
    news        = result.get("news", [])
    stn_note    = result.get("stn_note")
    ipo_note    = result.get("ipo_note")
    base_url    = os.environ.get("BASE_URL",
                  "https://web-production-19e44.up.railway.app").rstrip("/")

    opt_type  = result.get("option_type", "call")
    otype     = "C" if "call" in opt_type.lower() else "P"
    tot_str   = (f"${total/1_000_000:.1f}M" if total >= 1_000_000
                 else f"${total/1_000:.0f}K")
    total_chg = _pct_change(first_px, last_px)
    occ_label = _ordinal(occ)

    # Multi-day badge
    day_badge = f" \U0001f5d3\ufe0f {unique_days}-DAY ACCUMULATION" if multi_day else ""

    # Flow history lines
    flow_lines = []
    for i, f in enumerate(flows, 1):
        prem_str  = (f"${f['premium']/1_000_000:.1f}M" if f['premium'] >= 1_000_000
                     else f"${f['premium']/1_000:.0f}K")
        sweep_str = " \u26a1" if f.get("sweep") else ""
        fill_str  = f" {f['fill']}" if f.get("fill") else ""
        chg_str   = _pct_change(first_px, f["price"]) if i > 1 else ""
        time_str  = _format_time(f["ts"])
        date_str  = f" ({f.get('date','?')})" if multi_day else ""
        flow_lines.append(
            f"  {i}. ${f['price']:.2f}{chg_str} | "
            f"{prem_str}{fill_str}{sweep_str} | {time_str}{date_str}"
        )

    # Context line — use latest non-zero stock price from any fill
    if not stock_px:
        stock_px = next(
            (f["stock_px"] for f in reversed(flows) if f.get("stock_px")), 0
        )
    ctx_parts = []
    if stock_px:  ctx_parts.append(f"${stock_px:.2f}")
    if otm_pct:   ctx_parts.append(f"OTM {otm_pct:.1f}%")
    if dte:       ctx_parts.append(f"{dte}d DTE")
    ctx_line = " | ".join(ctx_parts) if ctx_parts else "\u2014"

    # Earnings line
    earn_line = f"\U0001f4c5 Earnings: {earn_str}" if earn_str else "\U0001f4c5 Earnings: unknown"

    # IV line
    iv_line = None
    if iv_pct and iv_rank is not None:
        iv_bar  = "\u2588" * int(iv_rank / 10) + "\u2591" * (10 - int(iv_rank / 10))
        iv_line = f"\U0001f4ca IV: {iv_pct:.1f}% | Rank {_ordinal(iv_rank)} [{iv_bar}]"
    elif iv_pct:
        iv_line = f"\U0001f4ca IV: {iv_pct:.1f}% (rank building)"

    # Analysis link — resolve from watchlist
    analysis_link = f"{base_url}/watchlist"
    try:
        from storage import get_watchlist as _gwl_ta
        _wl = _gwl_ta()
        if isinstance(_wl, dict):
            _entry = _wl.get(ticker.upper(), {})
            _aid   = str(_entry.get("analysis_id", "") or "")
            if _aid and _aid != "0":
                analysis_link = f"{base_url}/analysis/{_aid}"
    except:
        pass

    lines = [
        f"\U0001f3ac {alert_name}{day_badge}",
        f"\u2501\u2501\u2501 TAPE CONFIRMATION ({occ_label} fill) \u2501\u2501\u2501",
        f"\u2705 ${ticker} {strike}{otype} {expiry} \u2014 repeat buyer detected{total_chg}",
        f"",
        f"\U0001f4ca ALL FILLS ({len(flows)} total | {tot_str} deployed):",
    ] + flow_lines + [
        f"",
        f"Stock: {ctx_line}",
        earn_line,
    ]
    if iv_line:
        lines.append(iv_line)
    if stn_note:
        lines.append(stn_note)
    if ipo_note:
        lines.append(ipo_note)
    # News headlines
    if news:
        lines.append("")
        lines.append("\U0001f4f0 Recent news:")
        for _art in news[:3]:
            _hl = (_art.get("headline","") or "")[:70]
            _src = _art.get("source","") or ""
            _url = _art.get("url","") or ""
            _age_h = int((time.time() - _art.get("datetime",0)) / 3600)
            _age_str = f"{_age_h}h ago" if _age_h < 24 else f"{_age_h//24}d ago"
            lines.append(f"  • {_hl} ({_src}, {_age_str})")
            if _url:
                lines.append(f"    {_url}")

    lines += [
        f"",
        f"\U0001f4a1 Same strike/expiry bought {occ_label} time \u2014 accumulating position",
        f"\U0001f4c8 https://www.tradingview.com/chart/?symbol={ticker}",
        f"\U0001f4cb Full analysis \u2192 {analysis_link}",
    ]

    return "\n".join(lines)


def send_tape_eod_summary():
    """
    4:00 PM ET — send a summary of all tape watching alerts that fired today.
    Sends to TRADE channel.
    """
    _load_tape()

    ET_tz    = ZoneInfo("America/New_York")
    today_str = datetime.now(ET_tz).strftime("%b %-d")
    now      = time.time()

    # Collect entries that had any fills today
    today_alerts = []
    for key, entry in _TAPE.items():
        today_flows = [f for f in entry.get("flows", [])
                       if f.get("date","") == today_str]
        if not today_flows or entry.get("alert_count", 0) == 0:
            continue

        flows     = entry["flows"]
        all_today = len(today_flows)
        total_d   = len(set(f.get("date","") for f in flows))
        prem_tot  = sum(f["premium"] for f in flows)
        first_px  = entry.get("first_price", 0)
        last_px   = today_flows[-1]["price"]
        chg       = _pct_change(first_px, last_px).strip()
        multi     = f" 🗓️ {total_d}d" if total_d > 1 else ""

        prem_str  = (f"${prem_tot/1_000_000:.1f}M" if prem_tot >= 1_000_000
                     else f"${prem_tot/1_000:.0f}K")

        opt_type  = "C" if "call" in entry.get("option_type","call").lower() else "P"
        ticker    = entry["ticker"]
        strike    = entry["strike"]
        expiry    = entry["expiry"]
        fills     = len(flows)

        today_alerts.append({
            "ticker": ticker,
            "line":   (f"  ${ticker} {strike}{opt_type} {expiry} "
                       f"— {fills} fills | {prem_str} | ${first_px:.2f}→${last_px:.2f} {chg}{multi}"),
            "prem":   prem_tot,
        })

    # Sort by total premium descending
    today_alerts.sort(key=lambda x: x["prem"], reverse=True)

    bot  = os.environ.get("TELEGRAM_BOT_TOKEN","")
    chat = (os.environ.get("TELEGRAM_TRADE_CHAT_ID","") or
            os.environ.get("TELEGRAM_CHAT_ID",""))

    if not bot or not chat:
        print("[TAPE-EOD] No bot/chat configured")
        return

    if not today_alerts:
        msg = (f"🎬 Tape Watching EOD — {today_str}\n"
               f"No repeat buyer alerts fired today.")
    else:
        lines = [
            f"🎬 Tape Watching EOD — {today_str}",
            f"━━━ {len(today_alerts)} ticker(s) with repeat flow ━━━",
            "",
        ]
        for a in today_alerts:
            lines.append(a["line"])
        lines += ["", f"💡 Multi-day entries marked 🗓️ Nd"]
        msg = "\n".join(lines)

    try:
        from sms import send_telegram
        send_telegram(msg, bot, chat)
        print(f"[TAPE-EOD] Sent summary — {len(today_alerts)} tickers")
    except Exception as e:
        print(f"[TAPE-EOD] Send error: {e}")
