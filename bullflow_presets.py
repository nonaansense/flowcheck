"""
bullflow_presets.py — Bullflow pre-defined alert relay.

Relays Bullflow's own built-in alert types straight to Telegram, filtered
to high-conviction, near-dated flow. Unlike the other trackers, these
are Bullflow's canned signals — we don't recompute anything, we surface
the ones that clear the premium + DTE bar and enrich them with the
contract details, earnings date, and a chart link.

Tracked preset types (BULLFLOW_PRESET_TYPES, comma-separated):
  Discord Trade, Sizable Sweep, Urgent Repeater, Grenade Trade,
  Bullflow Repeater, Position Building Repeater

Filters:
  BULLFLOW_PRESET_MIN_PREMIUM = 500000   only >= $500K total premium
  BULLFLOW_PRESET_MAX_DTE     = 14       only expiring within 14 days

Config env vars:
  BULLFLOW_PRESET_TYPES        (defaults to the six types above)
  BULLFLOW_PRESET_MIN_PREMIUM  = 500000
  BULLFLOW_PRESET_MAX_DTE      = 14
"""
import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def _fetch_tradier_price(ticker: str) -> float:
    """
    Live last price from Tradier /markets/quotes (free tier).
    Used as a fallback when the fill payload has no stockPrice.
    Returns 0.0 on any failure.
    """
    token = os.environ.get("TRADIER_TOKEN", "")
    if not token or not ticker:
        return 0.0
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/quotes",
            params={"symbols": ticker.upper(), "greeks": "false"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=6,
        )
        if r.status_code == 200:
            q = (r.json().get("quotes") or {}).get("quote")
            if isinstance(q, list):
                q = q[0] if q else None
            if q:
                # last trade, then close, then midpoint of bid/ask
                px = q.get("last") or q.get("close")
                if not px:
                    bid, ask = q.get("bid") or 0, q.get("ask") or 0
                    px = (bid + ask) / 2 if (bid and ask) else 0
                return float(px or 0)
    except Exception as e:
        print(f"[PRESET] Tradier quote error {ticker}: {e}")
    return 0.0

_DEFAULT_TYPES = ("Discord Trade,Sizable Sweep,Urgent Repeater,"
                  "Grenade Trade,Bullflow Repeater,Position Building Repeater")

PRESET_TYPES = [t.strip() for t in
                os.environ.get("BULLFLOW_PRESET_TYPES", _DEFAULT_TYPES).split(",")
                if t.strip()]
MIN_PREMIUM  = float(os.environ.get("BULLFLOW_PRESET_MIN_PREMIUM", "500000"))
MAX_DTE      = int(os.environ.get("BULLFLOW_PRESET_MAX_DTE", "14"))

# ATM band: strike within this fraction of stock price counts as ATM (0.5%)
ATM_BAND_PCT       = float(os.environ.get("BULLFLOW_PRESET_ATM_BAND_PCT", "0.005"))
# Entry = this fraction below the flow trade price (20% → 0.20)
ENTRY_DISCOUNT_PCT = float(os.environ.get("BULLFLOW_PRESET_ENTRY_DISCOUNT_PCT", "0.20"))
# Trailing-stop offset = this fraction of the flow trade price (75% → 0.75)
TRAIL_OFFSET_PCT   = float(os.environ.get("BULLFLOW_PRESET_TRAIL_OFFSET_PCT", "0.75"))
# Whether to show ITM alerts. Set false to suppress in-the-money contracts
# (some traders only want OTM/ATM directional bets, not ITM).
SHOW_ITM = os.environ.get("BULLFLOW_PRESET_SHOW_ITM", "true").lower() not in ("false","0","no","off")

# Alerts before this ET hour are flagged for reversal risk (10.5 = 10:30am).
# Early-session flow often fades once the opening range resolves.
EARLY_CUTOFF_HOUR = float(os.environ.get("BULLFLOW_PRESET_EARLY_CUTOFF_HOUR", "10.5"))

# Suppress early alerts entirely (rather than just flagging them).
SUPPRESS_EARLY = os.environ.get("BULLFLOW_PRESET_SUPPRESS_EARLY", "false").lower() in ("true","1","yes","on")

# For CALL alerts whose contract expires the SAME WEEK as the alert, suggest
# rolling out to the next week's expiry at the same strike (more time, less
# gamma/theta cliff into Friday).
ROLL_SUGGEST = os.environ.get("BULLFLOW_PRESET_ROLL_SUGGEST", "true").lower() not in ("false","0","no","off")


def _expiry_to_date(expiry: str):
    """'07/18/26' → date(2026, 7, 18). None on failure."""
    try:
        mm, dd, yy = expiry.split("/")
        return datetime(2000 + int(yy), int(mm), int(dd)).date()
    except Exception:
        return None


def _same_week(alert_d, expiry_d) -> bool:
    """True if both dates fall in the same Mon-Sun ISO week."""
    if not alert_d or not expiry_d:
        return False
    return alert_d.isocalendar()[:2] == expiry_d.isocalendar()[:2]


def _next_week_expiry(expiry_d):
    """Same weekday, one week later (weeklies expire Friday → next Friday)."""
    if not expiry_d:
        return None
    return expiry_d + timedelta(days=7)


def _build_occ(ticker: str, exp_d, otype: str, strike: float) -> str:
    """Build an OCC symbol, e.g. NVDA260718C00220000 (no 'O:' prefix)."""
    try:
        yy = exp_d.strftime("%y")
        mm = exp_d.strftime("%m")
        dd = exp_d.strftime("%d")
        cp = "C" if otype == "call" else "P"
        strike_int = int(round(float(strike) * 1000))
        return f"{ticker.upper()}{yy}{mm}{dd}{cp}{strike_int:08d}"
    except Exception:
        return ""


def _fetch_option_quote(occ_symbol: str) -> float:
    """
    Last/mid price for an option contract via Tradier /markets/quotes.
    Returns 0.0 if the contract doesn't exist or the call fails.
    """
    token = os.environ.get("TRADIER_TOKEN", "")
    if not token or not occ_symbol:
        return 0.0
    try:
        r = requests.get(
            "https://api.tradier.com/v1/markets/quotes",
            params={"symbols": occ_symbol, "greeks": "false"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=6,
        )
        if r.status_code == 200:
            q = (r.json().get("quotes") or {}).get("quote")
            if isinstance(q, list):
                q = q[0] if q else None
            if q:
                px = q.get("last") or q.get("close")
                if not px:
                    bid, ask = q.get("bid") or 0, q.get("ask") or 0
                    px = (bid + ask) / 2 if (bid and ask) else 0
                return float(px or 0)
    except Exception as e:
        print(f"[PRESET] Tradier option quote error {occ_symbol}: {e}")
    return 0.0

# Preset types to play as 30M trend REVERSAL (all others → 30M trend FOLLOW).
_DEFAULT_REVERSAL_TYPES = "Grenade Trade"
REVERSAL_TYPES = [t.strip().lower() for t in
                  os.environ.get("BULLFLOW_PRESET_REVERSAL_TYPES",
                                 _DEFAULT_REVERSAL_TYPES).split(",")
                  if t.strip()]

# Case-insensitive lookup set for matching incoming alert names
_PRESET_LOWER = {t.lower() for t in PRESET_TYPES}


def _round_up_tenth(value: float) -> float:
    """
    Round UP to the nearest $0.10.  2.44 → 2.50,  2.40 → 2.40,  1.61 → 1.70.

    Float-safe: 2.40 * 10 is 24.000000000000004 in binary floating point, so a
    naive ceil() would wrongly bump it to 2.50. Rounding to 6dp first snaps
    that back to 24.0 before the ceiling is applied.
    """
    import math
    if value <= 0:
        return 0.0
    return math.ceil(round(value * 10, 6)) / 10.0


def _fmt_prem(p: float) -> str:
    return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"


def is_preset(alert_name: str) -> bool:
    return str(alert_name or "").strip().lower() in _PRESET_LOWER


def process_preset(alert: dict, filter_name: str) -> dict | None:
    """
    Evaluate a Bullflow pre-defined alert against the premium + DTE filters.
    Returns an enriched result dict when it qualifies, else None.
    """
    if not is_preset(filter_name):
        return None

    ticker  = str(alert.get("ticker", "") or "").upper()
    strike  = str(alert.get("strike", "") or "")
    expiry  = str(alert.get("expiry", "") or "")
    otype   = str(alert.get("option_type", "call") or "call")
    price   = float(alert.get("option_price") or 0)
    premium = float(alert.get("premium", 0) or 0)
    dte     = int(alert.get("dte", 0) or 0)
    sweep   = bool(alert.get("is_sweep", False))
    stock_px = float(alert.get("stock_price") or 0)

    if not ticker or not strike or not expiry:
        return None

    # Alert timestamp — prefer Bullflow's est_timestamp string, then epoch,
    # then fall back to now. Displayed in ET as HH:MM:SS AM/PM.
    # Also capture alert_hour (float ET) for the early-session check, and
    # alert_date (date obj) for the same-week expiry roll suggestion.
    time_str   = ""
    alert_hour = None
    alert_date = None
    est = str(alert.get("est_timestamp", "") or "")
    if len(est) >= 19:
        # e.g. "2026-06-05 09:32:26 EST" → "9:32:26 AM"
        try:
            dt = datetime.strptime(est[:19], "%Y-%m-%d %H:%M:%S")
            time_str   = dt.strftime("%-I:%M:%S %p")
            alert_hour = dt.hour + dt.minute / 60.0
            alert_date = dt.date()
        except Exception:
            time_str = est[11:19]
    if not time_str or alert_hour is None or alert_date is None:
        epoch = alert.get("timestamp")
        try:
            if epoch:
                _dt = datetime.fromtimestamp(float(epoch), ET)
                if not time_str:
                    time_str = _dt.strftime("%-I:%M:%S %p")
                if alert_hour is None:
                    alert_hour = _dt.hour + _dt.minute / 60.0
                if alert_date is None:
                    alert_date = _dt.date()
        except Exception:
            pass
    if not time_str:
        _now = datetime.now(ET)
        time_str = _now.strftime("%-I:%M:%S %p")
        if alert_hour is None:
            alert_hour = _now.hour + _now.minute / 60.0
    if alert_date is None:
        alert_date = datetime.now(ET).date()

    # Fall back to a live Tradier quote if the fill carried no stock price
    if not stock_px:
        stock_px = _fetch_tradier_price(ticker)

    # ── Filters ──
    if premium < MIN_PREMIUM:
        print(f"[PRESET] {filter_name} {ticker}: {_fmt_prem(premium)} < "
              f"{_fmt_prem(MIN_PREMIUM)} min — skipping")
        return None
    if dte > MAX_DTE:
        print(f"[PRESET] {filter_name} {ticker}: {dte}d DTE > {MAX_DTE}d max — skipping")
        return None

    direction = "call" if "call" in otype.lower() else "put"

    # Earnings enrichment (best-effort)
    earnings_str = None
    earnings_flag = ""
    try:
        from fetcher import fetch_earnings_date
        e_str, e_dt, e_past, e_timing = fetch_earnings_date(ticker)
        if e_str and not e_past:
            earnings_str = f"{e_str}{' ' + e_timing if e_timing else ''}"
            if e_dt:
                days_to = (e_dt.date() - datetime.now().date()).days
                if 0 <= days_to <= dte:
                    earnings_flag = f"⚠️ earnings {earnings_str} — inside contract window"
                else:
                    earnings_flag = f"📅 earnings {earnings_str}"
    except Exception as _ee:
        print(f"[PRESET] earnings fetch error {ticker}: {_ee}")

    # ── Moneyness (ITM / ATM / OTM) relative to stock price ──
    # ATM band = within ATM_BAND_PCT of the strike (default 0.5%).
    moneyness = ""
    try:
        strike_f = float(strike)
    except Exception:
        strike_f = 0.0
    if stock_px > 0 and strike_f > 0:
        diff_pct = abs(stock_px - strike_f) / stock_px
        if diff_pct <= ATM_BAND_PCT:
            moneyness = "ATM"
        elif direction == "call":
            moneyness = "ITM" if stock_px > strike_f else "OTM"
        else:  # put
            moneyness = "ITM" if stock_px < strike_f else "OTM"

    # Suppress ITM alerts when disabled
    if moneyness == "ITM" and not SHOW_ITM:
        print(f"[PRESET] {filter_name} {ticker}: ITM suppressed (SHOW_ITM off)")
        return None

    # ── Trade size in # of contracts ──
    # Each contract controls 100 shares, so cost per contract = price * 100.
    # contracts = total premium / (price per contract * 100).
    contracts = 0
    if price > 0:
        contracts = int(round(premium / (price * 100)))

    # ── Suggested entry + trailing stop, derived from flow trade price ──
    # Entry = 20% below flow trade price. Trail stop OFFSET = 75% of trade
    # price (e.g. $2.00 → entry $1.60, trail offset $1.50).
    # Entry = 20% below flow trade price, rounded UP to the nearest $0.10
    # (a slightly higher limit is more likely to actually fill).
    entry_price  = _round_up_tenth(price * (1 - ENTRY_DISCOUNT_PCT)) if price > 0 else 0.0
    trail_offset = round(price * TRAIL_OFFSET_PCT, 2)                if price > 0 else 0.0

    # ── Same-week CALL → suggest rolling to next week's expiry, same strike ──
    # A call expiring the same week as the alert faces a hard theta/gamma cliff
    # into Friday. The next weekly at the same strike keeps the thesis with
    # more time. Only for calls; only when the alert's own expiry is this week.
    roll = None
    if ROLL_SUGGEST and direction == "call":
        exp_d = _expiry_to_date(expiry)
        if exp_d and _same_week(alert_date, exp_d):
            next_d = _next_week_expiry(exp_d)
            if next_d:
                next_occ = _build_occ(ticker, next_d, "call", strike_f)
                next_px  = _fetch_option_quote(next_occ) if next_occ else 0.0
                roll = {
                    "expiry":   next_d.strftime("%m/%d/%y"),
                    "strike":   strike,
                    "dte":      max(0, (next_d - alert_date).days),
                    "price":    next_px,
                    "occ":      next_occ,
                    "available": next_px > 0,
                }

    # ── Early-session reversal risk ──
    # Flow printed before EARLY_CUTOFF_HOUR (10:30am ET default) lands while the
    # opening range is still resolving and frequently fades — flag it.
    is_early = alert_hour is not None and alert_hour < EARLY_CUTOFF_HOUR

    if is_early and SUPPRESS_EARLY:
        print(f"[PRESET] {filter_name} {ticker}: pre-{EARLY_CUTOFF_HOUR:.2f} ET "
              f"suppressed (SUPPRESS_EARLY on)")
        return None

    # ── 30M playbook ──
    # Grenade Trades (and any other configured type) are played as 30M trend
    # REVERSALS; every other preset type is played as 30M trend FOLLOWING.
    is_reversal = str(filter_name).strip().lower() in REVERSAL_TYPES
    playbook    = "reversal" if is_reversal else "follow"

    print(f"[PRESET] 🎯 {filter_name}: {ticker} {strike}{'C' if direction=='call' else 'P'} "
          f"{expiry} | {_fmt_prem(premium)} | {dte}d | {moneyness} | {contracts} contracts "
          f"| 30M {playbook}{' | EARLY' if is_early else ''}")

    return {
        "preset_type":  filter_name,
        "ticker":       ticker,
        "strike":       strike,
        "expiry":       expiry,
        "direction":    direction,
        "price":        price,
        "premium":      premium,
        "dte":          dte,
        "sweep":        sweep,
        "stock_px":     stock_px,
        "moneyness":    moneyness,
        "contracts":    contracts,
        "entry_price":  entry_price,
        "trail_offset": trail_offset,
        "earnings_str": earnings_str,
        "earnings_flag": earnings_flag,
        "time_str":     time_str,
        "alert_hour":   alert_hour,
        "alert_date":   alert_date.strftime("%Y-%m-%d") if alert_date else "",
        "is_early":     is_early,
        "playbook":     playbook,
        "roll":         roll,
    }


def build_preset_alert(result: dict) -> str:
    ptype   = result["preset_type"]
    ticker  = result["ticker"]
    strike  = result["strike"]
    expiry  = result["expiry"]
    direction = result["direction"]
    price   = result["price"]
    premium = result["premium"]
    dte     = result["dte"]
    sweep   = result["sweep"]
    stock_px = result["stock_px"]
    eflag   = result["earnings_flag"]
    time_str = result.get("time_str", "")
    moneyness    = result.get("moneyness", "")
    contracts    = result.get("contracts", 0)
    entry_price  = result.get("entry_price", 0)
    trail_offset = result.get("trail_offset", 0)
    is_early     = result.get("is_early", False)
    playbook     = result.get("playbook", "follow")

    otype = "C" if direction == "call" else "P"
    emoji = "📈" if direction == "call" else "📉"
    sweep_s = " ⚡ SWEEP" if sweep else ""
    money_s = f"  [{moneyness}]" if moneyness else ""

    lines = [
        f"🔔 BULLFLOW: {ptype}",
        f"━━━ {emoji} {direction.upper()} {ticker} ━━━",
        "",
        f"Contract: {strike}{otype} {expiry}  ({dte}d DTE){sweep_s}",
        f"💵 Total premium: {_fmt_prem(premium)}",
        f"💲 Trade price: ${price:.2f}{money_s}",
    ]
    if contracts:
        lines.append(f"📦 Trade size: {contracts:,} contracts")
    if stock_px:
        lines.append(f"📊 Stock: ${stock_px:.2f}")
    if time_str:
        lines.append(f"🕐 Alert time: {time_str} ET")
    if eflag:
        lines.append(eflag)

    # ── Playbook + timing warnings ──
    lines.append("")
    if playbook == "reversal":
        lines.append("🔄 PLAY: watch 30M for TREND REVERSAL")
    else:
        lines.append("➡️ PLAY: watch 30M for TREND CONTINUATION")
    if is_early:
        _cut_h = int(EARLY_CUTOFF_HOUR)
        _cut_m = int(round((EARLY_CUTOFF_HOUR - _cut_h) * 60))
        _cut_s = f"{(_cut_h - 12) if _cut_h > 12 else _cut_h}:{_cut_m:02d}{'pm' if _cut_h >= 12 else 'am'}"
        lines.append(f"⚠️ EARLY (pre-{_cut_s}) — elevated reversal risk, "
                     "let the opening range resolve")

    if entry_price:
        lines += [
            "",
            f"🎯 Entry: ${entry_price:.2f}  ({ENTRY_DISCOUNT_PCT*100:.0f}% below flow)",
            f"🛑 Trail stop offset: -${trail_offset:.2f}  "
            f"({TRAIL_OFFSET_PCT*100:.0f}% of flow price)",
        ]

    # ── Roll suggestion: same-week call → next week, same strike ──
    roll = result.get("roll")
    if roll:
        otype_r = "C"
        if roll.get("available") and roll.get("price"):
            lines += [
                "",
                f"🔁 EXPIRES THIS WEEK — consider next week instead:",
                f"   {roll['strike']}{otype_r} {roll['expiry']}  "
                f"({roll['dte']}d DTE)  @ ${roll['price']:.2f}",
            ]
        else:
            lines += [
                "",
                f"🔁 EXPIRES THIS WEEK — consider next week instead:",
                f"   {roll['strike']}{otype_r} {roll['expiry']}  "
                f"({roll['dte']}d DTE)  (quote unavailable)",
            ]

    lines += [
        "",
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
    ]
    return "\n".join(lines)
