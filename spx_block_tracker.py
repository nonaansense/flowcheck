"""
spx_block_tracker.py — SPX block trade repeat detector.

Monitors the SPX_Block_Trades Bullflow filter and fires when the same
contract (strike + expiry + call/put) appears more than once within a
session. Repeat block trades on the same SPX contract signal conviction.

Tracks trade price across fills to show whether the trader is:
  - Paying more (buying into strength / adding conviction)
  - Paying less (averaging down / rolling into weakness)

Config env vars:
  SPX_BLOCK_FILTER_NAME    = SPX_Block_Trades
  SPX_BLOCK_WINDOW_HOURS   = 24       rolling window (SPX trades 24/5)
  TELEGRAM_SPX_CHAT_ID                dedicated SPX plays channel
"""
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SPX_BLOCK_FILTER  = os.environ.get("SPX_BLOCK_FILTER_NAME", "SPX_Block_Trades")
WINDOW_HOURS      = float(os.environ.get("SPX_BLOCK_WINDOW_HOURS", "24"))
STORAGE_KEY       = "spx_block_history"

_BLOCKS: dict = {}   # contract_key → {"fills": [...], "alerted_count": int}
_loaded: bool = False


def _load():
    global _BLOCKS, _loaded
    if _loaded:
        return
    try:
        from storage import db_get
        raw = db_get(STORAGE_KEY)
        if raw and isinstance(raw, dict):
            _BLOCKS = raw
    except Exception as e:
        print(f"[SPX] Load error: {e}")
    _loaded = True


def _save():
    try:
        from storage import db_set
        db_set(STORAGE_KEY, _BLOCKS)
    except Exception as e:
        print(f"[SPX] Save error: {e}")


def _prune():
    cutoff = time.time() - WINDOW_HOURS * 3600
    for key in list(_BLOCKS.keys()):
        _BLOCKS[key]["fills"] = [
            f for f in _BLOCKS[key]["fills"] if f.get("ts", 0) >= cutoff
        ]
        if not _BLOCKS[key]["fills"]:
            del _BLOCKS[key]


def _contract_key(strike: str, expiry: str, option_type: str) -> str:
    otype = "C" if "call" in str(option_type).lower() else "P"
    return f"SPX_{strike}_{expiry}_{otype}"


def _fmt_prem(p: float) -> str:
    return f"${p/1_000_000:.1f}M" if p >= 1_000_000 else f"${p/1_000:.0f}K"


def _price_direction(fills: list) -> str:
    """Compare first fill price to latest fill price."""
    if len(fills) < 2:
        return ""
    first = fills[0].get("price", 0)
    last  = fills[-1].get("price", 0)
    if not first:
        return ""
    pct = (last - first) / first * 100
    if pct >= 2:
        return f"📈 Paying MORE (+{pct:.1f}%) — buying into strength"
    elif pct <= -2:
        return f"📉 Paying LESS ({pct:.1f}%) — averaging down / rolling"
    else:
        return f"➡️ Similar price ({pct:+.1f}%) — reloading same level"


def process_spx_block(alert: dict, filter_name: str) -> dict | None:
    """
    Track SPX block fills by contract. Fires on every repeat fill
    (2nd, 3rd, 4th...) on the same strike + expiry + call/put.
    """
    if filter_name != SPX_BLOCK_FILTER:
        return None

    _load()
    _prune()

    ticker      = str(alert.get("ticker", "") or "").upper()
    strike      = str(alert.get("strike", "") or "")
    expiry      = str(alert.get("expiry", "") or "")
    option_type = str(alert.get("option_type", "call") or "call")
    price       = float(alert.get("option_price") or alert.get("trade_price") or 0)
    premium     = float(alert.get("premium", 0) or 0)
    is_sweep    = bool(alert.get("is_sweep", False))
    dte         = int(alert.get("dte", 0) or 0)
    now         = time.time()
    time_str    = datetime.now(ET).strftime("%-I:%M %p")
    date_str    = datetime.now(ET).strftime("%b %-d")

    if not strike or not expiry:
        return None

    key = _contract_key(strike, expiry, option_type)

    if key not in _BLOCKS:
        _BLOCKS[key] = {"fills": [], "alerted_count": 0,
                        "ticker": ticker, "strike": strike,
                        "expiry": expiry, "option_type": option_type}

    fill = {
        "price":   price,
        "premium": premium,
        "sweep":   is_sweep,
        "time":    time_str,
        "date":    date_str,
        "ts":      now,
    }
    _BLOCKS[key]["fills"].append(fill)
    _save()

    fills       = _BLOCKS[key]["fills"]
    fill_count  = len(fills)
    last_alerted = _BLOCKS[key]["alerted_count"]

    print(f"[SPX] {key}: {fill_count} fill{'s' if fill_count > 1 else ''} "
          f"| ${price:.2f} | {_fmt_prem(premium)}")

    # Only fire on repeat (2nd fill onward), and on every new fill after that
    if fill_count < 2 or fill_count <= last_alerted:
        return None

    _BLOCKS[key]["alerted_count"] = fill_count
    _save()

    total_prem = sum(f["premium"] for f in fills)
    direction  = _price_direction(fills)
    otype      = "C" if "call" in option_type.lower() else "P"

    print(f"[SPX] 🔄 Repeat block: {key} — {fill_count} fills | {_fmt_prem(total_prem)}")

    return {
        "key":         key,
        "ticker":      ticker,
        "strike":      strike,
        "expiry":      expiry,
        "option_type": option_type,
        "otype":       otype,
        "fills":       fills,
        "fill_count":  fill_count,
        "total_prem":  total_prem,
        "direction":   direction,
        "dte":         dte,
    }


def build_spx_alert(result: dict) -> str:
    strike     = result["strike"]
    expiry     = result["expiry"]
    otype      = result["otype"]
    fills      = result["fills"]
    fill_count = result["fill_count"]
    total_prem = result["total_prem"]
    direction  = result["direction"]
    dte        = result["dte"]

    from tape_watcher import _ordinal
    ord_s    = _ordinal(fill_count)
    emoji    = "📈" if otype == "C" else "📉"
    contract = f"SPX {strike}{otype} {expiry}"

    fill_lines = []
    for i, f in enumerate(fills, 1):
        sweep_s = " ⚡" if f.get("sweep") else ""
        fill_lines.append(
            f"  #{i}  ${f['price']:.2f} | {_fmt_prem(f['premium'])}{sweep_s} | {f['time']}"
        )

    lines = [
        f"🔄 {ord_s.upper()} SPX BLOCK: {contract}",
        f"━━━ {emoji} REPEAT CONVICTION ━━━",
        f"",
        f"Fill history:",
    ] + fill_lines + [
        f"",
        f"💵 Total deployed: {_fmt_prem(total_prem)} across {fill_count} fills",
    ]

    if direction:
        lines.append(direction)
    if dte:
        lines.append(f"📅 {dte}d DTE")

    lines += [
        f"",
        f"💡 Same contract hit {fill_count}x — not a one-off, watching this level",
        f"📈 https://www.tradingview.com/chart/?symbol=SPX",
    ]

    return "\n".join(lines)
