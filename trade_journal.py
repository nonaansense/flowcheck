"""
Trade Journal for FlowCheck.
Comprehensive trade tracking with full holding period analytics.
Supports both intraday and multi-day swing trades.

Commands:
/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE
/exit TICKER PRICE
/journal — open + recent closed trades
/pnl — full P&L summary with analytics
"""
import json, os, requests, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JOURNAL_FILE = "/tmp/flowcheck_journal.json"

def poly_key():
    return os.environ.get("POLYGON_API_KEY")

def load_journal() -> dict:
    try:
        with open(JOURNAL_FILE) as f:
            return json.load(f)
    except:
        return {"trades": [], "closed": []}

def save_journal(data: dict):
    try:
        with open(JOURNAL_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[JOURNAL] Save error: {e}")

# ── Option ticker builder ──────────────────────────────────────────────

def build_opt_ticker(ticker: str, strike: str, opt_type: str, expiry: str) -> str | None:
    """
    Build Polygon option ticker.
    expiry format: MM/DD/YY or MM/DD/YYYY
    """
    try:
        parts = expiry.replace("-","/").split("/")
        if len(parts) == 3:
            m, d, y = parts
            y = "20"+y if len(y)==2 else y
            exp_str    = f"{y}{m.zfill(2)}{d.zfill(2)}"
            cp         = "C" if opt_type.upper() in ("C","CALL") else "P"
            strike_int = int(float(strike) * 1000)
            return f"O:{ticker.upper()}{exp_str}{cp}{strike_int:08d}"
    except Exception as e:
        print(f"[JOURNAL] Ticker build error: {e}")
    return None

# ── Historical option price fetcher ───────────────────────────────────

def fetch_option_price_at(opt_ticker: str, dt: datetime) -> float | None:
    """Fetch option price at a specific datetime from Polygon."""
    key = poly_key()
    if not key or not opt_ticker:
        return None
    try:
        ts_ms = int(dt.timestamp() * 1000)
        from_ms = ts_ms - 10 * 60 * 1000   # 10 min before
        to_ms   = ts_ms + 10 * 60 * 1000   # 10 min after

        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{opt_ticker}/range/1/minute/{from_ms}/{to_ms}",
            params={"adjusted":"true","sort":"asc","limit":20,"apiKey":key},
            timeout=10
        )
        if r.status_code == 200 and r.json().get("results"):
            bars    = r.json()["results"]
            closest = min(bars, key=lambda b: abs(b["t"] - ts_ms))
            return round(float(closest["c"]), 2)
    except Exception as e:
        print(f"[JOURNAL] Option price at time error: {e}")
    return None

def fetch_option_history(opt_ticker: str, from_dt: datetime,
                          to_dt: datetime) -> list:
    """
    Fetch full option price history for holding period.
    Returns list of {timestamp, open, high, low, close, volume}.
    """
    key = poly_key()
    if not key or not opt_ticker:
        return []
    try:
        from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%S")
        to_str   = to_dt.strftime("%Y-%m-%dT%H:%M:%S")
        r = requests.get(
            f"https://api.polygon.io/v2/aggs/ticker/{opt_ticker}/range/5/minute/{from_str}/{to_str}",
            params={"adjusted":"true","sort":"asc","limit":500,"apiKey":key},
            timeout=15
        )
        if r.status_code == 200 and r.json().get("results"):
            return r.json()["results"]
    except Exception as e:
        print(f"[JOURNAL] Option history error: {e}")
    return []

def calc_holding_analytics(bars: list, entry_price: float,
                             exit_price: float) -> dict:
    """
    Calculate max drawdown, highest value, and key stats
    from option price bars during holding period.
    """
    if not bars or not entry_price:
        return {}

    closes = [float(b["c"]) for b in bars if b.get("c")]
    highs  = [float(b["h"]) for b in bars if b.get("h")]
    lows   = [float(b["l"]) for b in bars if b.get("l")]

    if not closes:
        return {}

    peak_price    = max(highs) if highs else max(closes)
    trough_price  = min(lows) if lows else min(closes)

    peak_pct      = round(((peak_price - entry_price) / entry_price) * 100, 1)
    trough_pct    = round(((trough_price - entry_price) / entry_price) * 100, 1)

    # Max drawdown from peak
    running_peak  = entry_price
    max_drawdown  = 0.0
    for h in highs:
        running_peak = max(running_peak, h)
    for l in lows:
        dd = ((running_peak - l) / running_peak) * 100
        max_drawdown = max(max_drawdown, dd)

    # Time at peak (approximate)
    peak_bar_idx = highs.index(peak_price) if highs else 0
    peak_ts      = bars[peak_bar_idx].get("t", 0) if peak_bar_idx < len(bars) else 0
    peak_time    = datetime.fromtimestamp(peak_ts/1000).strftime("%m/%d %H:%M") if peak_ts else "?"

    return {
        "peak_price":   round(peak_price, 2),
        "peak_pct":     peak_pct,
        "peak_time":    peak_time,
        "trough_price": round(trough_price, 2),
        "trough_pct":   trough_pct,
        "max_drawdown": round(max_drawdown, 1),
        "bars_count":   len(bars),
        "left_on_table": round(peak_pct - ((exit_price - entry_price)/entry_price*100), 1) if exit_price else None,
    }

# ── Trade Entry / Exit ─────────────────────────────────────────────────

def add_entry(ticker: str, strike: str, opt_type: str, expiry: str,
              contracts: int, price: float,
              entry_date: str = None, entry_time: str = None) -> dict:
    """
    Record a new trade entry.
    entry_date + entry_time: when trade was actually entered (not when logged).
    If omitted, uses current time — only accurate if logging immediately.
    """
    journal = load_journal()
    now_et  = datetime.now(ZoneInfo("America/New_York"))
    opt_ticker = build_opt_ticker(ticker, strike, opt_type, expiry)

    # Parse entry datetime
    auto_filled = False
    if entry_date and entry_time:
        try:
            entry_dt   = parse_exit_datetime(entry_date, entry_time)
            auto_filled = False
            print(f"[JOURNAL] Entry datetime: {entry_dt.strftime('%Y-%m-%d %H:%M ET')}")
        except Exception as e:
            print(f"[JOURNAL] Date parse error: {e} — using current time")
            entry_dt   = now_et
            auto_filled = True
    elif entry_date:
        try:
            entry_dt   = parse_exit_datetime(entry_date, "09:30")
            auto_filled = False
            print(f"[JOURNAL] Entry date only — assuming 9:30 AM ET open")
        except:
            entry_dt   = now_et
            auto_filled = True
    else:
        entry_dt   = now_et
        auto_filled = True
        print("[JOURNAL] No entry time provided — auto-filling current time")

    # Find matching FlowCheck alert
    fc_score = fc_verdict = None
    try:
        from main import analyses
        today   = now_et.strftime("%Y-%m-%d")
        matches = [
            a for a in analyses
            if a.get("trade",{}).get("ticker","").upper() == ticker.upper()
            and a.get("date") == today
        ]
        if matches:
            latest     = matches[-1]
            fc_score   = latest.get("result",{}).get("final_score")
            fc_verdict = latest.get("result",{}).get("verdict")
    except:
        pass

    trade = {
        "id":             len(journal["trades"]) + len(journal["closed"]),
        "ticker":         ticker.upper(),
        "strike":         strike,
        "option_type":    "call" if opt_type.upper() in ("C","CALL") else "put",
        "expiry":         expiry,
        "opt_ticker":     opt_ticker,
        "contracts":      int(contracts),
        "entry_price":    float(price),
        "entry_datetime": entry_dt.isoformat(),
        "entry_date":     entry_dt.strftime("%Y-%m-%d"),
        "entry_time":     entry_dt.strftime("%H:%M"),
        "total_cost":     round(float(price) * int(contracts) * 100, 2),
        "fc_score":       fc_score,
        "fc_verdict":     fc_verdict,
        "entry_auto_filled": auto_filled,  # True = time was auto-filled, not user-provided
        "status":         "OPEN",
        # Exit fields
        "exit_price":     None,
        "exit_datetime":  None,
        "exit_date":      None,
        "exit_time":      None,
        # P&L fields
        "pnl_per_contract": None,
        "pnl_total":      None,
        "pnl_pct":        None,
        # Holding period analytics
        "peak_price":     None,
        "peak_pct":       None,
        "peak_time":      None,
        "trough_price":   None,
        "trough_pct":     None,
        "max_drawdown":   None,
        "left_on_table":  None,
        "holding_days":   None,
        "holding_hours":  None,
    }

    journal["trades"].append(trade)
    save_journal(journal)
    print(f"[JOURNAL] Entry: {ticker} {strike}{opt_type} x{contracts} @ ${price} at {now_et.strftime('%m/%d %H:%M')}")
    return trade

def parse_exit_datetime(date_str: str, time_str: str) -> datetime:
    """
    Parse exit date and time into ET datetime.
    date_str: YYYY-MM-DD or MM/DD/YY or MM/DD/YYYY
    time_str: HH:MM (24h) or HH:MMam/pm
    """
    # Normalize date
    date_str = date_str.replace("/","-")
    parts = date_str.split("-")
    if len(parts[0]) == 4:
        # YYYY-MM-DD
        dt_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    elif len(parts[2]) == 4:
        # MM-DD-YYYY
        dt_date = datetime.strptime(date_str, "%m-%d-%Y").date()
    else:
        # MM-DD-YY
        dt_date = datetime.strptime(date_str, "%m-%d-%y").date()

    # Normalize time
    time_str = time_str.upper().replace(" ","")
    if "AM" in time_str or "PM" in time_str:
        try:
            dt_time = datetime.strptime(time_str, "%I:%M%p").time()
        except:
            dt_time = datetime.strptime(time_str, "%H:%M").time()
    else:
        dt_time = datetime.strptime(time_str, "%H:%M").time()

    return datetime(
        dt_date.year, dt_date.month, dt_date.day,
        dt_time.hour, dt_time.minute,
        tzinfo=ZoneInfo("America/New_York")
    )

def add_exit(ticker: str, exit_price: float,
             exit_date: str = None, exit_time: str = None,
             contracts_to_close: int = None) -> dict | None:
    """
    Record full or partial exit with explicit date+time.

    exit_date:           date of exit (required for swing trades)
    exit_time:           time of exit ET (required for accurate analytics)
    contracts_to_close:  number of contracts to close (None = close all)

    Supports multiple partial exits on same trade.
    Each partial exit is recorded separately.
    When all contracts are closed, trade moves to closed list.
    """
    journal = load_journal()
    now_et  = datetime.now(ZoneInfo("America/New_York"))

    open_trades = [
        t for t in journal["trades"]
        if t.get("ticker","").upper() == ticker.upper()
        and t.get("status") == "OPEN"
    ]
    if not open_trades:
        return None

    trade     = open_trades[-1]
    ep        = float(exit_price)
    entry     = float(trade["entry_price"])
    total_open = int(trade.get("contracts_remaining", trade["contracts"]))

    # Determine how many contracts to close
    if contracts_to_close is None:
        closing = total_open  # Close all remaining
    else:
        closing = min(int(contracts_to_close), total_open)

    remaining = total_open - closing

    # Parse exit datetime
    if exit_date and exit_time:
        try:
            exit_dt = parse_exit_datetime(exit_date, exit_time)
        except Exception as e:
            print(f"[JOURNAL] Date parse error: {e} — using current time")
            exit_dt = now_et
    elif exit_date:
        try:
            exit_dt = parse_exit_datetime(exit_date, "16:00")
        except:
            exit_dt = now_et
    else:
        exit_dt = now_et

    # P&L for this exit
    pnl_per   = round((ep - entry) * 100, 2)
    pnl_total = round(pnl_per * closing, 2)
    pnl_pct   = round(((ep - entry) / entry) * 100, 1)

    # Holding period
    entry_dt    = datetime.fromisoformat(trade["entry_datetime"])
    holding_td  = exit_dt - entry_dt
    holding_days= holding_td.days
    holding_hrs = round(holding_td.total_seconds() / 3600, 1)

    # Record this partial exit
    partial_exit = {
        "exit_price":    ep,
        "exit_datetime": exit_dt.isoformat(),
        "exit_date":     exit_dt.strftime("%Y-%m-%d"),
        "exit_time":     exit_dt.strftime("%H:%M"),
        "contracts":     closing,
        "pnl_per":       pnl_per,
        "pnl_total":     pnl_total,
        "pnl_pct":       pnl_pct,
        "holding_hours": holding_hrs,
        "holding_days":  holding_days,
    }

    # Add to trade's exit history
    if "exits" not in trade:
        trade["exits"] = []
    trade["exits"].append(partial_exit)
    trade["contracts_remaining"] = remaining

    # Fetch analytics for this exit segment
    opt_ticker = trade.get("opt_ticker")
    analytics  = {}
    if opt_ticker:
        print(f"[JOURNAL] Fetching holding period data for {opt_ticker}...")
        # For partial exits, fetch from last exit or entry
        last_exit_dt = entry_dt
        if len(trade["exits"]) > 1:
            try:
                last_exit_dt = datetime.fromisoformat(trade["exits"][-2]["exit_datetime"])
            except:
                pass
        bars = fetch_option_history(opt_ticker, last_exit_dt, exit_dt)
        if bars:
            analytics = calc_holding_analytics(bars, entry, ep)
            partial_exit.update(analytics)
            print(f"[JOURNAL] Analytics: peak={analytics.get('peak_pct',0):+.1f}% "
                  f"max_dd={analytics.get('max_drawdown',0):.1f}%")

    # Calculate cumulative P&L across all exits
    all_exits  = trade["exits"]
    cum_pnl    = sum(e.get("pnl_total",0) for e in all_exits)
    cum_conts  = sum(e.get("contracts",0) for e in all_exits)

    if remaining <= 0:
        # All contracts closed — move to closed list
        trade["status"]           = "CLOSED"
        trade["exit_price"]       = ep
        trade["exit_datetime"]    = exit_dt.isoformat()
        trade["exit_date"]        = exit_dt.strftime("%Y-%m-%d")
        trade["exit_time"]        = exit_dt.strftime("%H:%M")
        trade["pnl_total"]        = round(cum_pnl, 2)
        trade["pnl_pct"]          = pnl_pct  # Last exit pct
        trade["holding_days"]     = holding_days
        trade["holding_hours"]    = holding_hrs
        trade["is_partial"]       = len(all_exits) > 1
        # Use analytics from last exit for peak/drawdown
        if analytics:
            trade.update({k: v for k, v in analytics.items()
                          if k not in ("bars_count",)})

        journal["closed"].append(trade)
        journal["trades"] = [t for t in journal["trades"]
                             if t.get("id") != trade["id"]]
        print(f"[JOURNAL] Fully closed: {ticker} — total P&L ${cum_pnl:+.2f}")
    else:
        # Partial close — keep in open list with updated remaining
        print(f"[JOURNAL] Partial close: {ticker} {closing} contracts @ ${ep} "
              f"— {remaining} remaining")

    save_journal(journal)

    # Return the partial exit record for display
    partial_exit["trade"]            = trade
    partial_exit["remaining"]        = remaining
    partial_exit["total_contracts"]  = trade["contracts"]
    partial_exit["is_partial"]       = remaining > 0
    partial_exit["cum_pnl"]          = round(cum_pnl, 2)
    return partial_exit

# ── Edit Trade ────────────────────────────────────────────────────────

def edit_trade(ticker: str, field: str, value: str,
               trade_id: int = None) -> tuple:
    """
    Edit a field on an open trade.
    Returns (success, message, trade).

    Editable fields:
    - entry_date, entry_time    — fix entry datetime
    - exit_date, exit_time      — fix exit datetime (on closed trades)
    - entry_price               — fix entry price
    - contracts                 — fix contract count
    - expiry                    — fix expiry date
    - strike                    — fix strike price
    - note                      — add/edit note
    """
    journal = load_journal()

    # Find trade — open first, then closed
    target = None
    in_open = True

    if trade_id is not None:
        for t in journal["trades"]:
            if t.get("id") == trade_id:
                target  = t
                in_open = True
                break
        if not target:
            for t in journal["closed"]:
                if t.get("id") == trade_id:
                    target  = t
                    in_open = False
                    break
    else:
        # Most recent open trade for ticker
        matches = [t for t in journal["trades"]
                   if t.get("ticker","").upper() == ticker.upper()]
        if matches:
            target  = matches[-1]
            in_open = True
        else:
            # Check closed
            matches = [t for t in journal["closed"]
                       if t.get("ticker","").upper() == ticker.upper()]
            if matches:
                target  = matches[-1]
                in_open = False

    if not target:
        return False, "No trade found for " + ticker, None

    field = field.lower().strip()
    old_value = target.get(field, "?")

    try:
        if field == "entry_date":
            # Validate date format
            parse_exit_datetime(value, target.get("entry_time","09:30"))
            target["entry_date"] = value
            # Rebuild entry_datetime
            target["entry_datetime"] = parse_exit_datetime(
                value, target.get("entry_time","09:30")
            ).isoformat()
            target["entry_auto_filled"] = False

        elif field == "entry_time":
            parse_exit_datetime(target.get("entry_date","2026-01-01"), value)
            target["entry_time"] = value
            target["entry_datetime"] = parse_exit_datetime(
                target.get("entry_date","2026-01-01"), value
            ).isoformat()
            target["entry_auto_filled"] = False

        elif field == "exit_date" and not in_open:
            target["exit_date"] = value
            target["exit_datetime"] = parse_exit_datetime(
                value, target.get("exit_time","16:00")
            ).isoformat()

        elif field == "exit_time" and not in_open:
            target["exit_time"] = value
            target["exit_datetime"] = parse_exit_datetime(
                target.get("exit_date","2026-01-01"), value
            ).isoformat()

        elif field == "entry_price":
            target["entry_price"] = round(float(value), 2)
            # Recalculate total cost
            target["total_cost"] = round(float(value) * target.get("contracts",1) * 100, 2)
            # Recalculate P&L if closed
            if not in_open and target.get("exit_price"):
                ep  = float(target["exit_price"])
                en  = float(value)
                c   = target.get("contracts",1)
                target["pnl_per_contract"] = round((ep-en)*100, 2)
                target["pnl_total"]        = round((ep-en)*100*c, 2)
                target["pnl_pct"]          = round(((ep-en)/en)*100, 1)

        elif field == "contracts":
            target["contracts"]  = int(value)
            target["total_cost"] = round(float(target.get("entry_price",0)) * int(value) * 100, 2)
            if target.get("contracts_remaining") is None:
                target["contracts_remaining"] = int(value)

        elif field == "expiry":
            target["expiry"]     = value
            # Rebuild opt_ticker
            new_ticker = build_opt_ticker(
                target.get("ticker",""),
                target.get("strike",""),
                target.get("option_type","call"),
                value
            )
            if new_ticker:
                target["opt_ticker"] = new_ticker

        elif field == "strike":
            target["strike"] = value
            new_ticker = build_opt_ticker(
                target.get("ticker",""),
                value,
                target.get("option_type","call"),
                target.get("expiry","")
            )
            if new_ticker:
                target["opt_ticker"] = new_ticker

        elif field == "note":
            target["note"] = value

        elif field == "option_type":
            target["option_type"] = "call" if value.upper() in ("C","CALL") else "put"

        else:
            return False, "Unknown field: " + field + ". Valid: entry_date, entry_time, exit_date, exit_time, entry_price, contracts, expiry, strike, note, option_type", None

        save_journal(journal)
        return True, "Updated " + field + ": " + str(old_value) + " → " + str(value), target

    except Exception as e:
        return False, "Error editing " + field + ": " + str(e), None

# ── Display Functions ──────────────────────────────────────────────────

def get_journal_summary() -> str:
    journal  = load_journal()
    open_t   = journal.get("trades", [])
    closed_t = journal.get("closed", [])
    lines    = []

    if open_t:
        lines.append("OPEN TRADES (" + str(len(open_t)) + ")")
        lines.append("")
        for t in open_t:
            otype   = t["option_type"][0].upper()
            fc      = ""
            if t.get("fc_score"):
                fc = "[" + str(t["fc_score"]) + "/7 " + str(t["fc_verdict"]) + "]"
            held_h = ""
            try:
                entry_dt = datetime.fromisoformat(t["entry_datetime"])
                now_et   = datetime.now(ZoneInfo("America/New_York"))
                hrs      = round((now_et - entry_dt).total_seconds() / 3600, 1)
                held_h   = " | Held " + str(hrs) + "h"
            except:
                pass
            remaining = t.get("contracts_remaining", t["contracts"])
            exits_so_far = t.get("exits",[])
            cum_pnl_so_far = sum(e.get("pnl_total",0) for e in exits_so_far)
            # Calculate DTE remaining
            dte_str = ""
            try:
                exp = t.get("expiry","")
                parts = exp.replace("-","/").split("/")
                if len(parts) == 3:
                    m, d, y = parts
                    y = "20"+y if len(y)==2 else y
                    from datetime import datetime as _dt
                    exp_date = _dt(int(y),int(m),int(d))
                    dte = (exp_date - _dt.now()).days
                    dte_str = " [" + str(dte) + "d]"
            except:
                pass

            lines.append(
                t["ticker"] + " " + t["strike"] + otype + " " + t["expiry"] +
                dte_str + " x" + str(remaining) + "/" + str(t["contracts"]) +
                " @ $" + str(t["entry_price"])
            )
            lines.append(
                "  In: " + t["entry_date"] + " " + t["entry_time"] +
                held_h + " | Cost: $" + str(t["total_cost"]) +
                (" " + fc if fc else "")
            )
            if exits_so_far:
                lines.append(
                    "  Partial exits: " + str(len(exits_so_far)) +
                    " | Realized: $" + str(round(cum_pnl_so_far,2))
                )
    else:
        lines.append("No open trades")

    recent = closed_t[-5:] if closed_t else []
    if recent:
        lines.append("")
        lines.append("RECENT CLOSED (last 5 of " + str(len(closed_t)) + ")")
        lines.append("")
        for t in reversed(recent):
            otype  = t["option_type"][0].upper()
            pnl    = t.get("pnl_total", 0) or 0
            pct    = t.get("pnl_pct", 0) or 0
            result = "WIN" if pnl > 0 else "LOSS"
            peak   = t.get("peak_pct")
            dd     = t.get("max_drawdown")
            held   = ""
            if t.get("holding_days") is not None:
                d = t["holding_days"]
                h = t.get("holding_hours", 0)
                held = str(d) + "d " + str(round(h % 24, 1)) + "h" if d > 0 else str(h) + "h"

            lines.append(
                result + ": " + t["ticker"] + " " + t["strike"] + otype +
                " x" + str(t["contracts"])
            )
            lines.append(
                "  $" + str(t["entry_price"]) + " -> $" + str(t["exit_price"]) +
                " (" + str(round(pct,1)) + "%) = $" + str(round(pnl,2))
            )
            lines.append(
                "  " + t["entry_date"] + " " + t.get("entry_time","") +
                " -> " + t["exit_date"] + " " + t.get("exit_time","") +
                (" | Held: " + held if held else "")
            )
            if peak is not None:
                lot  = t.get("left_on_table")
                lot_str = " | Left on table: " + str(lot) + "%" if lot is not None else ""
                lines.append(
                    "  Peak: +" + str(peak) + "% at " + str(t.get("peak_time","?")) +
                    " | Max DD: -" + str(t.get("max_drawdown","?")) + "%" + lot_str
                )

    return "\n".join(lines) if lines else "No trades recorded yet."

def get_pnl_summary() -> str:
    journal  = load_journal()
    closed_t = journal.get("closed", [])
    open_t   = journal.get("trades", [])

    if not closed_t:
        open_cost = sum(t.get("total_cost",0) for t in open_t)
        return (
            "P&L Summary\n\n"
            "No closed trades yet.\n"
            "Open: " + str(len(open_t)) + " positions\n"
            "Deployed: $" + str(round(open_cost,2))
        )

    total_pnl    = sum(t.get("pnl_total",0) or 0 for t in closed_t)
    wins         = [t for t in closed_t if (t.get("pnl_total",0) or 0) > 0]
    losses       = [t for t in closed_t if (t.get("pnl_total",0) or 0) <= 0]
    win_rate     = round(len(wins)/len(closed_t)*100,1) if closed_t else 0
    avg_win_pct  = round(sum(t.get("pnl_pct",0) or 0 for t in wins)/len(wins),1) if wins else 0
    avg_loss_pct = round(sum(t.get("pnl_pct",0) or 0 for t in losses)/len(losses),1) if losses else 0
    total_cost   = sum(t.get("total_cost",0) for t in closed_t)
    roi          = round((total_pnl / total_cost) * 100, 1) if total_cost > 0 else 0

    # Holding period stats
    held_hrs  = [t.get("holding_hours",0) for t in closed_t if t.get("holding_hours")]
    avg_hold  = round(sum(held_hrs)/len(held_hrs),1) if held_hrs else None

    # Peak analytics
    peaks = [t.get("peak_pct",0) for t in closed_t if t.get("peak_pct") is not None]
    avg_peak = round(sum(peaks)/len(peaks),1) if peaks else None
    lots  = [t.get("left_on_table",0) for t in closed_t if t.get("left_on_table") is not None]
    avg_lot = round(sum(lots)/len(lots),1) if lots else None
    dds   = [t.get("max_drawdown",0) for t in closed_t if t.get("max_drawdown") is not None]
    avg_dd = round(sum(dds)/len(dds),1) if dds else None

    # By FlowCheck verdict
    trade_v = [t for t in closed_t if t.get("fc_verdict") == "TRADE"]
    watch_v = [t for t in closed_t if t.get("fc_verdict") == "WATCH"]
    t_wr    = round(sum(1 for t in trade_v if (t.get("pnl_total",0) or 0)>0)/len(trade_v)*100,1) if trade_v else None
    w_wr    = round(sum(1 for t in watch_v if (t.get("pnl_total",0) or 0)>0)/len(watch_v)*100,1) if watch_v else None

    # Best / worst
    by_pct   = sorted(closed_t, key=lambda x: x.get("pnl_pct",0) or 0, reverse=True)
    best     = by_pct[0] if by_pct else None
    worst    = by_pct[-1] if by_pct else None

    lines = [
        "TRADE JOURNAL P&L",
        "",
        "Closed: " + str(len(closed_t)) + " trades",
        "Win rate: " + str(win_rate) + "% (" + str(len(wins)) + "W / " + str(len(losses)) + "L)",
        "Total P&L: $" + str(round(total_pnl,2)),
        "ROI on invested: " + str(roi) + "%",
        "Avg win: +" + str(avg_win_pct) + "% | Avg loss: " + str(avg_loss_pct) + "%",
    ]

    if avg_hold:
        lines.append("Avg hold time: " + str(avg_hold) + "h")
    if avg_peak:
        lines.append("Avg peak gain: +" + str(avg_peak) + "%")
    if avg_dd:
        lines.append("Avg max drawdown: -" + str(avg_dd) + "%")
    if avg_lot:
        lines.append("Avg left on table: " + str(avg_lot) + "% (exited early)")

    if t_wr is not None:
        lines.append("TRADE verdict: " + str(t_wr) + "% win (" + str(len(trade_v)) + " trades)")
    if w_wr is not None:
        lines.append("WATCH verdict: " + str(w_wr) + "% win (" + str(len(watch_v)) + " trades)")

    if best:
        otype = best["option_type"][0].upper()
        lines.append("")
        lines.append(
            "Best: " + best["ticker"] + " " + best["strike"] + otype +
            " +" + str(round(best.get("pnl_pct",0),1)) + "%" +
            " ($" + str(round(best.get("pnl_total",0),2)) + ")"
        )
    if worst:
        otype = worst["option_type"][0].upper()
        lines.append(
            "Worst: " + worst["ticker"] + " " + worst["strike"] + otype +
            " " + str(round(worst.get("pnl_pct",0),1)) + "%" +
            " ($" + str(round(worst.get("pnl_total",0),2)) + ")"
        )

    if open_t:
        open_cost = sum(t.get("total_cost",0) for t in open_t)
        lines.append("")
        lines.append("Open: " + str(len(open_t)) + " trades | $" + str(round(open_cost,2)) + " deployed")

    return "\n".join(lines)
