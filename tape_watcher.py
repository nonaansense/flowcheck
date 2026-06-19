"""
tape_watcher.py — Big-money-first tape watching detector.

Monitors both Bullflow filters and only fires when big money is present.

TWO rules — both require big money footprint:

  Rule A — Intraday conviction:
    1+ fill from Big_Money_Order_Flow
    + 1+ fill from Retail_Order_Flow
    Same ticker + direction, within the same trading day.
    Strike/expiry mix-and-match OK — signal is at ticker+direction level.

  Rule B — Multi-day big money accumulation:
    2+ fills from Big_Money_Order_Flow on the EXACT SAME strike+expiry
    Across different calendar days (same-day repeats don't count).

Pure retail flow (no big money) is IGNORED — no alert fired.

State persisted to Supabase:
  - Big money fills retained TAPE_BM_DAYS (default 7 calendar = 5 trading days)
  - Retail fills retained for current trading day only
"""
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

TAPE_STORAGE_KEY = "tape_history_v2"   # v2 = new structure (ticker+direction keys)
TAPE_BM_DAYS     = int(os.environ.get("TAPE_BM_MULTIDAY_DAYS", "7"))

BIG_MONEY_FILTER = os.environ.get("CONVICTION_BIG_MONEY_FILTER", "Big_Money_Order_Flow")
RETAIL_FILTER    = os.environ.get("CONVICTION_RETAIL_FILTER",    "Retail_Order_Flow")

_TAPE:   dict = {}
_loaded: bool = False


# ── Persistence ────────────────────────────────────────────────────────
def _load_tape():
    global _TAPE, _loaded
    if _loaded:
        return
    try:
        from storage import db_get
        raw = db_get(TAPE_STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _TAPE = raw
            print(f"[TAPE] Loaded {len(_TAPE)} entries from Supabase")
    except Exception as e:
        print(f"[TAPE] Load error: {e}")
    _loaded = True


def _save_tape():
    try:
        from storage import db_set
        db_set(TAPE_STORAGE_KEY, _TAPE)
    except Exception as e:
        print(f"[TAPE] Save error: {e}")


# ── Keys ───────────────────────────────────────────────────────────────
def _ticker_dir_key(ticker: str, option_type: str) -> str:
    d = "call" if "call" in str(option_type).lower() else "put"
    return f"{ticker.upper()}_{d}"


def _contract_key(strike: str, expiry: str) -> str:
    return f"{strike}_{expiry}"


# ── Formatting helpers ─────────────────────────────────────────────────
def _format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime("%-I:%M %p")


def _format_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime("%b %-d")


def _pct_change(first: float, latest: float) -> str:
    if not first:
        return ""
    pct = (latest - first) / first * 100
    if pct > 0.05:    return f" ↑{pct:.1f}%"
    elif pct < -0.05: return f" ↓{abs(pct):.1f}%"
    return " (same price)"


def _ordinal(n: int) -> str:
    n = int(n)
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}{['th','st','nd','rd','th'][min(n % 10, 4)]}"


def _fmt_prem(p: float) -> str:
    return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"


# ── Pruning ────────────────────────────────────────────────────────────
def _prune_stale():
    """
    Big money: prune fills older than TAPE_BM_DAYS (7 calendar days).
    Retail:    prune fills from previous trading days (keep today only).
    """
    now       = time.time()
    bm_cutoff = now - TAPE_BM_DAYS * 86400
    today_str = datetime.now(ET).strftime("%b %-d")

    for key in list(_TAPE.keys()):
        entry = _TAPE[key]
        if not isinstance(entry, dict) or "big_money" not in entry:
            del _TAPE[key]  # stale structure from old tape_watcher — remove
            continue

        entry["big_money"] = [f for f in entry["big_money"]
                               if f.get("ts", 0) >= bm_cutoff]
        entry["retail"]    = [f for f in entry["retail"]
                               if f.get("date") == today_str]

        # Clean up bm_contracts tracking for expired contracts
        active_contracts = {_contract_key(f["strike"], f["expiry"])
                            for f in entry["big_money"]}
        entry["alerted_bm_contracts"] = {
            k: v for k, v in entry.get("alerted_bm_contracts", {}).items()
            if k in active_contracts
        }

        if not entry["big_money"] and not entry["retail"]:
            del _TAPE[key]


# ── Core detector ──────────────────────────────────────────────────────
def process_tape(alert: dict, filter_name: str = "") -> dict | None:
    """
    Process a single fill. Returns alert dict when a rule fires, else None.

    Rule A: 1+ BM + 1+ retail, same ticker+direction, same trading day.
    Rule B: 2+ BM on exact same strike+expiry, different calendar days.
    Pure retail → always returns None.
    """
    _load_tape()
    _prune_stale()

    ticker     = str(alert.get("ticker", "") or "").upper()
    strike     = str(alert.get("strike", "") or "")
    expiry     = str(alert.get("expiry", "") or "")
    option_type = str(alert.get("option_type", "call") or "call")
    trade_px   = float(alert.get("option_price") or alert.get("trade_price") or 0)
    premium    = float(alert.get("premium") or 0)
    fill_type  = str(alert.get("fill_type", "") or "")
    is_sweep   = bool(alert.get("is_sweep") or False)
    stock_px   = float(alert.get("stock_price") or 0)
    otm_pct    = float(alert.get("otm_pct") or 0)
    dte        = int(alert.get("dte") or 0)
    now        = time.time()
    today_str  = datetime.now(ET).strftime("%b %-d")
    time_str   = datetime.now(ET).strftime("%-I:%M %p")

    if not ticker or not strike or not expiry:
        return None

    _retail_enabled = os.environ.get("RETAIL_FLOW_ENABLED","true").lower() not in ("false","0","no","off")
    is_big_money = (filter_name == BIG_MONEY_FILTER)
    is_retail    = (filter_name == RETAIL_FILTER) and _retail_enabled

    if not is_big_money and not is_retail:
        if filter_name == RETAIL_FILTER and not _retail_enabled:
            print(f"[TAPE] Retail flow disabled — skipping {alert.get('ticker','?')}")
        elif filter_name not in (BIG_MONEY_FILTER, RETAIL_FILTER):
            print(f"[TAPE] Unknown filter '{filter_name}' — skipping")
        return None

    # Pure retail with no big money in state yet — record but never fire
    key = _ticker_dir_key(ticker, option_type)
    if key not in _TAPE:
        _TAPE[key] = {
            "ticker": ticker, "direction": option_type,
            "big_money": [], "retail": [],
            "alerted_rule_a_date": None,
            "alerted_bm_contracts": {},
        }

    entry = _TAPE[key]
    fill = {
        "strike": strike, "expiry": expiry,
        "price": trade_px, "premium": premium,
        "fill": fill_type, "sweep": is_sweep,
        "stock_px": stock_px, "otm_pct": otm_pct,
        "dte": dte, "ts": now,
        "date": today_str, "time": time_str,
        "source": "big_money" if is_big_money else "retail",
    }

    if is_big_money:
        entry["big_money"].append(fill)
        print(f"[TAPE] 💰 Big money: {ticker} {option_type} "
              f"{strike} {expiry} {_fmt_prem(premium)}")
    else:
        entry["retail"].append(fill)
        print(f"[TAPE] 📊 Retail: {ticker} {option_type} "
              f"{strike} {expiry} {_fmt_prem(premium)}")

    _save_tape()

    # ── Rule A: intraday conviction (1+ BM + 1+ retail today) ─────────
    today_bm  = [f for f in entry["big_money"] if f["date"] == today_str]
    today_ret = [f for f in entry["retail"]    if f["date"] == today_str]

    if len(today_bm) >= 1 and len(today_ret) >= 1:
        last_date   = entry.get("alerted_rule_a_date")
        new_bm      = is_big_money and (last_date == today_str)
        not_alerted = (last_date != today_str)

        if not_alerted or new_bm:
            entry["alerted_rule_a_date"] = today_str
            _save_tape()

            total_bm  = sum(f["premium"] for f in today_bm)
            total_ret = sum(f["premium"] for f in today_ret)
            total_all = total_bm + total_ret
            best_stock = (stock_px or
                          next((f["stock_px"] for f in reversed(today_bm + today_ret)
                                if f.get("stock_px")), 0))

            print(f"[TAPE] 🔥 Rule A: {ticker} {option_type} — "
                  f"{len(today_bm)} BM + {len(today_ret)} retail | "
                  f"{_fmt_prem(total_all)}")

            return {
                "rule":         "A",
                "ticker":       ticker,
                "option_type":  option_type,
                "strike":       strike,
                "expiry":       expiry,
                "big_money":    today_bm,
                "retail":       today_ret,
                "total_bm":     total_bm,
                "total_ret":    total_ret,
                "total_all":    total_all,
                "stock_px":     best_stock,
                "dte":          dte,
                "new_bm":       new_bm,
                "earnings_str": alert.get("earnings_str"),
                "iv_pct":       alert.get("iv_pct"),
                "iv_rank":      alert.get("iv_rank"),
                "stn_note":     alert.get("stn_note"),
                "ipo_note":     alert.get("ipo_note"),
                "news":           alert.get("news", []),
                "float_shares":   alert.get("float_shares"),
                "short_interest": alert.get("short_interest"),
            }

    # ── Rule B: multi-day BM accumulation (same contract, diff days) ──
    if is_big_money:
        ckey         = _contract_key(strike, expiry)
        contract_bm  = [f for f in entry["big_money"]
                        if f["strike"] == strike and f["expiry"] == expiry]
        unique_dates = sorted(set(f["date"] for f in contract_bm))

        if len(unique_dates) >= 2:
            last_count = entry["alerted_bm_contracts"].get(ckey, 0)
            if len(contract_bm) > last_count:
                entry["alerted_bm_contracts"][ckey] = len(contract_bm)
                _save_tape()

                total_bm  = sum(f["premium"] for f in contract_bm)
                best_stock = (stock_px or
                              next((f["stock_px"] for f in reversed(contract_bm)
                                    if f.get("stock_px")), 0))

                print(f"[TAPE] 🗓️  Rule B: {ticker} {strike} {expiry} — "
                      f"{len(contract_bm)}x BM over {len(unique_dates)}d | "
                      f"{_fmt_prem(total_bm)}")

                return {
                    "rule":         "B",
                    "ticker":       ticker,
                    "option_type":  option_type,
                    "strike":       strike,
                    "expiry":       expiry,
                    "contract_bm":  contract_bm,
                    "unique_days":  len(unique_dates),
                    "day_labels":   unique_dates,
                    "total_bm":     total_bm,
                    "stock_px":     best_stock,
                    "dte":          dte,
                    "earnings_str": alert.get("earnings_str"),
                    "iv_pct":       alert.get("iv_pct"),
                    "iv_rank":      alert.get("iv_rank"),
                    "stn_note":     alert.get("stn_note"),
                    "ipo_note":     alert.get("ipo_note"),
                    "news":           alert.get("news", []),
                "float_shares":   alert.get("float_shares"),
                "short_interest": alert.get("short_interest"),
                }

    # Still building — log current state
    today_bm_c  = len(today_bm)
    today_ret_c = len(today_ret)
    print(f"[TAPE] {ticker} {option_type}: "
          f"{today_bm_c} BM + {today_ret_c} retail today — "
          f"{'need BM' if today_bm_c == 0 else 'need retail'}")
    return None


# ── Alert builder ──────────────────────────────────────────────────────
def build_tape_alert(result: dict, alert_name: str) -> str:
    """Build Telegram alert for a confirmed tape signal (Rule A or B)."""
    rule        = result.get("rule", "A")
    ticker      = result["ticker"]
    option_type = result.get("option_type", "call")
    otype       = "C" if "call" in option_type.lower() else "P"
    direction   = "📈" if "call" in option_type.lower() else "📉"
    dte         = result.get("dte", 0)
    earn_str    = result.get("earnings_str")
    iv_pct      = result.get("iv_pct")
    iv_rank     = result.get("iv_rank")
    stn_note    = result.get("stn_note")
    ipo_note    = result.get("ipo_note")
    news        = result.get("news", [])
    base_url    = os.environ.get("BASE_URL",
                  "https://web-production-19e44.up.railway.app").rstrip("/")

    # Analysis link
    analysis_link = f"{base_url}/watchlist"
    try:
        from technical import get_watchlist
        wl    = get_watchlist()
        entry = wl.get(ticker.upper(), {}) if isinstance(wl, dict) else {}
        aid   = str(entry.get("analysis_id", "") or "")
        if aid and aid != "0":
            analysis_link = f"{base_url}/analysis/{aid}"
    except:
        pass

    def _fill_line(f, label):
        sweep   = " ⚡" if f.get("sweep") else ""
        date_s  = f" ({f['date']})" if rule == "B" else ""
        return (f"  [{label}] {f['strike']}{otype} {f['expiry']} | "
                f"${f['price']:.2f} | {_fmt_prem(f['premium'])}{sweep} | "
                f"{f['time']}{date_s}")

    if rule == "A":
        # Intraday: 1+ BM + 1+ retail same day
        bm_fills  = result["big_money"]
        ret_fills = result["retail"]
        total_bm  = result["total_bm"]
        total_ret = result["total_ret"]
        total_all = result["total_all"]
        stock_px  = result.get("stock_px", 0)
        new_bm    = result.get("new_bm", False)
        bm_pct    = int(total_bm / total_all * 10) if total_all else 10
        skew_bar  = "█" * bm_pct + "░" * (10 - bm_pct)
        header    = "🔥 NEW BIG MONEY" if new_bm else "🎬 TAPE CONVICTION"
        lines = [
            f"{header} — {alert_name}",
            f"━━━ {direction} INTRADAY: ${ticker} {bm_fills[0]['strike']}{otype} ━━━",
            f"",
            f"💰 BIG MONEY ({len(bm_fills)} fill{'s' if len(bm_fills)>1 else ''}"
            f" | {_fmt_prem(total_bm)}):",
        ] + [_fill_line(f, "BIG $") for f in bm_fills] + [
            f"",
            f"📊 RETAIL CONFIRM ({len(ret_fills)} fill{'s' if len(ret_fills)>1 else ''}"
            f" | {_fmt_prem(total_ret)}):",
        ] + [_fill_line(f, "RETAIL") for f in ret_fills] + [
            f"",
            f"💵 Total: {_fmt_prem(total_all)} | "
            f"Skew: {int(total_bm/total_all*100) if total_all else 100}% BM [{skew_bar}]",
        ]
        if stock_px:
            lines.append(f"Stock: ${stock_px:.2f}" +
                         (f" | {dte}d DTE" if dte else ""))

    elif rule == "B":
        # Multi-day: same contract, big money across days
        bm_fills    = result["contract_bm"]
        total_bm    = result["total_bm"]
        stock_px    = result.get("stock_px", 0)
        unique_days = result["unique_days"]
        day_labels  = result.get("day_labels", [])
        lines = [
            f"🗓️  TAPE ACCUMULATION — {alert_name}",
            f"━━━ {direction} {unique_days}-DAY BIG MONEY: ${ticker} "
            f"{bm_fills[0]['strike']}{otype} {bm_fills[0]['expiry']} ━━━",
            f"",
            f"💰 BIG MONEY ({len(bm_fills)} fills | {_fmt_prem(total_bm)}"
            f" | {unique_days} sessions):",
        ] + [_fill_line(f, "BIG $") for f in bm_fills] + [f""]
        if stock_px:
            lines.append(f"Stock: ${stock_px:.2f}" +
                         (f" | {dte}d DTE" if dte else ""))
        lines.append(f"📅 Sessions: {', '.join(day_labels)}")

    # Common context
    if earn_str:
        lines.append(f"📅 Earnings: {earn_str}")

    if iv_pct and iv_rank is not None:
        iv_bar  = "█" * int(iv_rank / 10) + "░" * (10 - int(iv_rank / 10))
        lines.append(f"📊 IV: {iv_pct:.1f}% | Rank {_ordinal(int(iv_rank))} [{iv_bar}]")
    elif iv_pct:
        lines.append(f"📊 IV: {iv_pct:.1f}% (rank building)")

    if stn_note:
        lines.append(stn_note)
    if ipo_note:
        lines.append(ipo_note)

    if news:
        lines += ["", "📰 Recent news:"]
        for art in news[:3]:
            hl    = (art.get("headline", "") or "")[:70]
            src   = art.get("source", "") or ""
            url   = art.get("url", "") or ""
            age_h = int((time.time() - art.get("datetime", 0)) / 3600)
            age_s = f"{age_h}h ago" if age_h < 24 else f"{age_h//24}d ago"
            lines.append(f"  • {hl} ({src}, {age_s})")
            if url:
                lines.append(f"    {url}")

    # Float/short interest context
    _float_sh  = result.get("float_shares")
    _short_int = result.get("short_interest")
    if _float_sh or _short_int:
        _ctx_parts = []
        if _float_sh:
            _f_m = _float_sh / 1_000_000
            _ctx_parts.append(f"Float: {_f_m:.1f}M shares")
        if _short_int and isinstance(_short_int, (int, float)) and _short_int > 0:
            _ctx_parts.append(f"Short int: {_short_int:.1f}%")
        if _ctx_parts:
            lines.append(f"📊 {' | '.join(_ctx_parts)}")

    rule_note = ("💡 Big money + retail same day = intraday conviction"
                 if rule == "A" else
                 "💡 Same contract bought multiple sessions = institutional accumulation")
    lines += [
        f"",
        rule_note,
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
        f"📋 Full analysis → {analysis_link}",
    ]

    return "\n".join(lines)


# ── EOD summary ────────────────────────────────────────────────────────
def send_tape_eod_summary():
    """4:00 PM ET — summarise all tape signals that fired today."""
    _load_tape()

    ET_tz     = ZoneInfo("America/New_York")
    today_str = datetime.now(ET_tz).strftime("%b %-d")
    bot       = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat      = (os.environ.get("TELEGRAM_TRADE_CHAT_ID", "") or
                 os.environ.get("TELEGRAM_CHAT_ID", ""))

    if not bot or not chat:
        return

    rule_a = []
    rule_b = []

    for key, entry in _TAPE.items():
        if not isinstance(entry, dict) or "big_money" not in entry:
            continue
        ticker = entry.get("ticker", key.split("_")[0])
        direct = "call" if "_call" in key else "put"
        otype  = "C" if direct == "call" else "P"

        # Rule A fired today
        if entry.get("alerted_rule_a_date") == today_str:
            today_bm  = [f for f in entry["big_money"] if f["date"] == today_str]
            today_ret = [f for f in entry["retail"]    if f["date"] == today_str]
            total     = sum(f["premium"] for f in today_bm + today_ret)
            rule_a.append({
                "line": (f"  🎬 ${ticker} — {len(today_bm)} BM + {len(today_ret)} retail "
                         f"| {_fmt_prem(total)}"),
                "total": total,
            })

        # Rule B — any multi-day BM contracts
        for ckey, alerted_count in entry.get("alerted_bm_contracts", {}).items():
            if alerted_count >= 2:
                strike, expiry = ckey.split("_", 1)
                contract_bm = [f for f in entry["big_money"]
                               if f["strike"] == strike and f["expiry"] == expiry]
                unique_d = len(set(f["date"] for f in contract_bm))
                total    = sum(f["premium"] for f in contract_bm)
                rule_b.append({
                    "line": (f"  🗓️  ${ticker} {strike}{otype} {expiry} — "
                             f"{len(contract_bm)} BM fills over {unique_d}d "
                             f"| {_fmt_prem(total)}"),
                    "total": total,
                })

    rule_a.sort(key=lambda x: x["total"], reverse=True)
    rule_b.sort(key=lambda x: x["total"], reverse=True)

    if not rule_a and not rule_b:
        msg = f"🎬 Tape EOD — {today_str}\nNo big-money tape signals today."
    else:
        lines = [f"🎬 Tape Watching EOD — {today_str}", ""]
        if rule_a:
            lines.append(f"📊 INTRADAY CONVICTION ({len(rule_a)}):")
            lines += [a["line"] for a in rule_a]
        if rule_b:
            if rule_a:
                lines.append("")
            lines.append(f"🗓️  MULTI-DAY ACCUMULATION ({len(rule_b)}):")
            lines += [b["line"] for b in rule_b]
        msg = "\n".join(lines)

    try:
        from sms import send_telegram
        send_telegram(msg, bot, chat)
        print(f"[TAPE-EOD] Sent — {len(rule_a)} intraday + {len(rule_b)} multi-day")
    except Exception as e:
        print(f"[TAPE-EOD] Send error: {e}")
