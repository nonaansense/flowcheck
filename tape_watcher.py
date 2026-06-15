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
        "flows":        list(entry["flows"]),  # snapshot — not a live reference
        "first_price":  first_price,
        "latest_price": trade_px,
        "occurrence":   occurrence,
        "total_premium": sum(f["premium"] for f in entry["flows"]),
        "stock_px":     stock_px,
        "otm_pct":      otm_pct,
        "dte":          dte,
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

    otype    = "C"  # determined from option type if available
    otm_str  = f" | OTM {otm_pct:.1f}%" if otm_pct else ""
    dte_str  = f" | {dte}d DTE" if dte else ""
    tot_str  = (f"${total/1_000_000:.1f}M" if total >= 1_000_000
                else f"${total/1_000:.0f}K")

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

    lines = [
        f"🎬 {alert_name}",
        f"━━━ TAPE CONFIRMATION ({occ_label} fill) ━━━",
        f"✅ ${ticker} {strike}{otype} {expiry} — repeat buyer detected{total_chg}",
        f"",
        f"📊 ALL FILLS ({len(flows)} total | {tot_str} deployed):",
    ] + flow_lines + [
        f"",
        f"Stock: ${stock_px:.2f}{otm_str}{dte_str}",
        f"💡 Same strike/expiry bought {occ_label} time — accumulating position",
        f"📈 https://www.tradingview.com/chart/?symbol={ticker}",
    ]

    return "\n".join(lines)
