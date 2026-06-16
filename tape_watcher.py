"""
tape_watcher.py — Repeat buyer / tape watching detector for FlowCheck.

Monitors Bullflow "Testing-Tape-Watching" alert stream.
When the same ticker + strike + expiry appears 2+ times
with trade price same or higher → fires immediately to TRADE channel.

No technical confirmation required — the repeat buying IS the confirmation.
"""
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# In-memory tape store — resets on redeploy (intentional, tape watching is intraday)
# Key: "TICKER_STRIKE_EXPIRY"  Value: dict with flow history
_TAPE: dict = {}

# How long to track a position before resetting (default: same trading day)
TAPE_WINDOW_HOURS = float(os.environ.get("TAPE_WINDOW_HOURS", "8"))


def _tape_key(ticker: str, strike: str, expiry: str) -> str:
    return f"{ticker.upper()}_{strike}_{expiry}"


def _format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime("%-I:%M %p")


def _pct_change(first: float, latest: float) -> str:
    if not first or first == 0:
        return ""
    pct = (latest - first) / first * 100
    if pct > 0.05:   return f" ↑{pct:.1f}%"
    elif pct < -0.05: return f" ↓{abs(pct):.1f}%"
    else:             return " (same price)"


def process_tape(alert: dict) -> dict | None:
    """
    Process incoming Bullflow alert through tape watcher.

    Returns alert dict if repeat buyer detected, None otherwise.
    """
    ticker    = str(alert.get("ticker","") or "").upper()
    strike    = str(alert.get("strike","") or "")
    expiry    = str(alert.get("expiry","") or "")
    trade_px  = float(alert.get("trade_price") or alert.get("option_price") or
                      alert.get("averageFillPrice") or 0)
    premium   = float(alert.get("premium") or alert.get("alertPremium") or 0)
    fill_type = str(alert.get("fill_type","") or "")
    is_sweep  = bool(alert.get("is_sweep") or alert.get("sweep") or False)
    stock_px  = float(alert.get("stock_price") or alert.get("spotPrice") or 0)
    otm_pct   = float(alert.get("otm_pct") or alert.get("percentOtm") or 0)
    dte       = int(alert.get("dte") or 0)
    now       = time.time()

    if not ticker or not strike or not expiry:
        return None

    key = _tape_key(ticker, strike, expiry)

    # ── Prune stale entries ───────────────────────────────────────────
    stale = [k for k, v in _TAPE.items()
             if now - v["first_ts"] > TAPE_WINDOW_HOURS * 3600]
    for k in stale:
        del _TAPE[k]

    # ── First occurrence — just store ─────────────────────────────────
    if key not in _TAPE:
        _TAPE[key] = {
            "ticker":     ticker,
            "strike":     strike,
            "expiry":     expiry,
            "first_ts":   now,
            "first_price": trade_px,
            "flows": [{
                "price":    trade_px,
                "premium":  premium,
                "fill":     fill_type,
                "sweep":    is_sweep,
                "stock_px": stock_px,
                "otm_pct":  otm_pct,
                "ts":       now,
            }],
            "alert_count": 0,
        }
        print(f"[TAPE] 📋 First flow: {ticker} {strike} {expiry} @ ${trade_px:.2f}")
        return None

    entry = _TAPE[key]

    # ── Subsequent occurrence — check price condition ──────────────────
    first_price = entry["first_price"]

    if trade_px < first_price * 0.98:  # small tolerance for spread noise
        print(f"[TAPE] ⬇️  Price drop — {ticker} {strike} {expiry}: "
              f"${first_price:.2f} → ${trade_px:.2f} — not confirming")
        # Still record it but don't alert
        entry["flows"].append({
            "price": trade_px, "premium": premium, "fill": fill_type,
            "sweep": is_sweep, "stock_px": stock_px, "otm_pct": otm_pct, "ts": now,
        })
        return None

    # ── Repeat buyer confirmed ─────────────────────────────────────────
    entry["flows"].append({
        "price": trade_px, "premium": premium, "fill": fill_type,
        "sweep": is_sweep, "stock_px": stock_px, "otm_pct": otm_pct, "ts": now,
    })
    entry["alert_count"] += 1

    occurrence = len(entry["flows"])  # 2nd, 3rd, 4th etc.
    print(f"[TAPE] 🔥 Repeat buyer #{occurrence}: {ticker} {strike} {expiry} "
          f"${first_price:.2f} → ${trade_px:.2f}")

    return {
        "ticker":       ticker,
        "strike":       strike,
        "expiry":       expiry,
        "option_type":  alert.get("option_type","call"),
        "flows":        list(entry["flows"]),  # snapshot — not a live reference
        "first_price":  first_price,
        "latest_price": trade_px,
        "occurrence":   occurrence,
        "total_premium": sum(f["premium"] for f in entry["flows"]),
        "stock_px":     stock_px,
        "otm_pct":      otm_pct,
        "dte":          dte,
        "earnings_str": alert.get("earnings_str"),
        "iv_pct":       alert.get("iv_pct"),
        "iv_rank":      alert.get("iv_rank"),
        "iv_note":      alert.get("iv_note"),
    }


def build_tape_alert(result: dict, alert_name: str) -> str:
    """Build the Telegram alert message for a confirmed repeat buyer."""
    ticker   = result["ticker"]
    strike   = result["strike"]
    expiry   = result["expiry"]
    flows    = result["flows"]
    occ      = result["occurrence"]
    stock_px = result["stock_px"]
    otm_pct  = result["otm_pct"]
    dte      = result["dte"]
    total    = result["total_premium"]
    first_px = result["first_price"]
    last_px  = result["latest_price"]

    otype    = "C" if result.get("flows",[{}])[0].get("price",0) else "C"
    opt_type = result.get("option_type", "call")
    otype    = "C" if "call" in opt_type.lower() else "P"
    otm_str  = f" | OTM {otm_pct:.1f}%" if otm_pct else ""
    dte_str  = f" | {dte}d DTE" if dte else ""
    tot_str  = (f"${total/1_000_000:.1f}M" if total >= 1_000_000
                else f"${total/1_000:.0f}K")
    earn_str = result.get("earnings_str")
    iv_pct   = result.get("iv_pct")
    iv_rank  = result.get("iv_rank")
    base_url = os.environ.get("BASE_URL","https://web-production-19e44.up.railway.app").rstrip("/")

    # Flow history lines
    flow_lines = []
    for i, f in enumerate(flows, 1):
        prem_str = (f"${f['premium']/1_000_000:.1f}M" if f['premium'] >= 1_000_000
                    else f"${f['premium']/1_000:.0f}K")
        sweep_str = " ⚡" if f.get("sweep") else ""
        fill_str  = f" {f['fill']}" if f.get("fill") else ""
        chg_str   = _pct_change(first_px, f["price"]) if i > 1 else ""
        time_str  = _format_time(f["ts"])
        flow_lines.append(
            f"  {i}. ${f['price']:.2f}{chg_str} | "
            f"{prem_str}{fill_str}{sweep_str} | {time_str}"
        )

    occ_label = {2:"2nd",3:"3rd"}.get(occ,f"{occ}th")
    total_chg = _pct_change(first_px, last_px)

    # Build context lines
    ctx_parts = []
    if stock_px:  ctx_parts.append(f"${stock_px:.2f}")
    if otm_str:   ctx_parts.append(otm_str.strip(" |"))
    if dte_str:   ctx_parts.append(dte_str.strip(" |"))
    ctx_line = " | ".join(ctx_parts) if ctx_parts else "—"

    # Earnings line
    earn_line = f"📅 Earnings: {earn_str}" if earn_str else "📅 Earnings: unknown"

    # IV line
    def _ordinal(n):
        n = int(n)
        if 11 <= (n % 100) <= 13: return f"{n}th"
        return f"{n}{['th','st','nd','rd','th'][min(n%10,4)]}"

    if iv_pct and iv_rank is not None:
        iv_bar = "█" * int(iv_rank / 10) + "░" * (10 - int(iv_rank / 10))
        iv_line = f"📊 IV: {iv_pct:.1f}% | Rank {_ordinal(iv_rank)} [{iv_bar}]"
    elif iv_pct:
        iv_line = f"📊 IV: {iv_pct:.1f}% (rank building)"
    else:
        iv_line = None

    lines = [
        f"🎬 {alert_name}",
        f"━━━ TAPE CONFIRMATION ({occ_label} fill) ━━━",
        f"✅ ${ticker} {strike}{otype} {expiry} — repeat buyer detected{total_chg}",
        f"",
        f"📊 ALL FILLS ({len(flows)} total | {tot_str} deployed):",
    ] + flow_lines + [
        f"",
        f"Stock: {ctx_line}",
        earn_line,
    ]
    if iv_line:
        lines.append(iv_line)
    lines += [
        f"💡 Same strike/expiry bought {occ_label} time — accumulating position",
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
        f"📋 {base_url}/analysis/latest?ticker={ticker}",
    ]

    return "\n".join(lines)
