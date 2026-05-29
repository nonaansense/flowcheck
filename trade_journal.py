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

JOURNAL_FILE  = "/tmp/flowcheck_journal.json"
JOURNAL_KEY   = "journal"
ACCOUNTS_FILE = "/tmp/flowcheck_accounts.json"
ACCOUNTS_KEY  = "accounts"

def poly_key():
    return os.environ.get("POLYGON_API_KEY")

def normalize_expiry(expiry: str) -> str:
    """Normalize expiry to MM/DD/YY format consistently."""
    if not expiry:
        return expiry
    expiry = expiry.strip()
    from datetime import datetime as _dt, date as _date
    
    # Try standard formats first
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y"):
        try:
            return _dt.strptime(expiry, fmt).strftime("%m/%d/%y")
        except:
            continue
    
    import re
    # Handle MM/YY (e.g. "06/26" = June 2026) — detect by year range
    if re.match(r"^\d{1,2}/\d{2}$", expiry):
        parts = expiry.split("/")
        month_v = int(parts[0])
        val2    = int(parts[1])
        if month_v <= 12 and val2 >= 25:
            # Looks like MM/YY — use 3rd Friday
            try:
                import calendar
                year_v    = 2000 + val2
                fridays   = [d for d in range(1,32)
                             if d <= calendar.monthrange(year_v, month_v)[1]
                             and _dt(year_v, month_v, d).weekday() == 4]
                third_fri = fridays[2] if len(fridays) >= 3 else fridays[-1]
                return _dt(year_v, month_v, third_fri).strftime("%m/%d/%y")
            except:
                pass

    # Handle MM/DD without year (e.g. "08/21")
    if re.match(r"^\d{1,2}/\d{1,2}$", expiry):
        try:
            month, day = expiry.split("/")
            today = _date.today()
            year  = today.year
            candidate = _date(year, int(month), int(day))
            if candidate < today:
                year += 1
            return _dt(year, int(month), int(day)).strftime("%m/%d/%y")
        except:
            pass
    
    # Handle M/D/YY or M/DD/YY
    try:
        parts = expiry.split("/")
        if len(parts) == 3 and len(parts[2]) == 2:
            return _dt.strptime(expiry, "%m/%d/%y").strftime("%m/%d/%y")
    except:
        pass
    
    return expiry  # Return as-is if can't parse

# -- Accounts (stored separately in Supabase) -------------------------

def load_accounts() -> dict:
    from storage import load_data
    return load_data(ACCOUNTS_KEY, ACCOUNTS_FILE,
                     {"default": {"name": "Main", "size": 10000}})

def save_accounts(accounts: dict):
    from storage import save_data
    save_data(ACCOUNTS_KEY, ACCOUNTS_FILE, accounts)

def get_accounts() -> dict:
    return load_accounts()

def add_account(account_id: str, name: str, size: float) -> dict:
    accounts = load_accounts()
    accounts[account_id] = {"name": name, "size": float(size)}
    save_accounts(accounts)
    print("[JOURNAL] Account saved: [" + account_id + "] " + name)
    return accounts[account_id]

def delete_account(account_id: str) -> tuple:
    """
    Delete an account. Returns (success, message).
    Cannot delete 'default' account.
    Cannot delete account with open trades.
    """
    if account_id == "default":
        return False, "Cannot delete the default account"

    accounts = load_accounts()
    if account_id not in accounts:
        return False, "Account not found: " + account_id + chr(10) + "Existing accounts: " + ", ".join(accounts.keys())

    # Check for open trades
    journal   = load_journal()
    open_t    = journal.get("trades", [])

    # 1. Duplicate guard — same ticker + same fill datetime already exists
    if entry_date and entry_time:
        for existing in open_t:
            if (existing.get("ticker","").upper() == ticker.upper() and
                existing.get("entry_date","")     == entry_date and
                existing.get("entry_time","").replace(" ","") == (entry_time or "").replace(" ","")):
                print("[JOURNAL] Duplicate detected: " + ticker + " " + entry_date + " " + str(entry_time))
                existing["_duplicate"] = True
                return existing  # Return existing trade, not a new one
    open_acct = [t for t in open_t if t.get("account_id","default") == account_id]
    if open_acct:
        return False, (
            "Cannot delete [@" + account_id + "] — " +
            str(len(open_acct)) + " open trade(s) still assigned to it." + chr(10) +
            "Close or reassign trades first with /edit TICKER @" + account_id + " account_id OTHER_ACCOUNT"
        )

    name = accounts[account_id].get("name", account_id)
    del accounts[account_id]
    save_accounts(accounts)
    return True, "Account deleted: [" + account_id + "] " + name

# -- Journal -----------------------------------------------------------

def load_journal() -> dict:
    from storage import load_data
    default = {"trades": [], "closed": [], "missed": []}
    data = load_data(JOURNAL_KEY, JOURNAL_FILE, default)
    data["accounts"] = load_accounts()
    return data

def save_journal(data: dict):
    from storage import save_data
    if "accounts" in data:
        save_accounts(data["accounts"])
    save_data(JOURNAL_KEY, JOURNAL_FILE, data)

def list_accounts_summary() -> str:
    """Format account list for Telegram."""
    accounts = get_accounts()
    journal  = load_journal()
    closed   = journal.get("closed", [])
    open_t   = journal.get("trades", [])

    lines = ["ACCOUNTS", ""]
    for aid, acc in accounts.items():
        name  = acc.get("name", aid)
        size  = acc.get("size", 0)
        # P&L for this account
        acc_closed = [t for t in closed if t.get("account_id") == aid]
        acc_open   = [t for t in open_t if t.get("account_id") == aid]
        total_pnl  = sum(t.get("pnl_total",0) or 0 for t in acc_closed)
        deployed   = sum(t.get("total_cost",0) for t in acc_open)
        wins       = sum(1 for t in acc_closed if (t.get("pnl_total",0) or 0) > 0)
        wr         = round(wins/len(acc_closed)*100,1) if acc_closed else None

        lines.append(
            "[" + aid + "] " + name + " — $" + str(f"{size:,.0f}")
        )
        if acc_closed:
            lines.append(
                "  " + str(len(acc_closed)) + " closed | " +
                ("+" if total_pnl >= 0 else "") + "$" + str(round(total_pnl,2)) +
                " P&L | " + str(wr) + "% win rate"
            )
        if acc_open:
            lines.append(
                "  " + str(len(acc_open)) + " open | $" +
                str(round(deployed,2)) + " deployed"
            )
        if not acc_closed and not acc_open:
            lines.append("  No trades yet")

    return chr(10).join(lines)

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

def fetch_current_option_price_simple(opt_ticker: str) -> float | None:
    """Fetch current mid price for an option contract."""
    key = poly_key()
    if not key or not opt_ticker:
        return None
    try:
        r = requests.get(
            f"https://api.polygon.io/v3/snapshot/options/{opt_ticker.split(':')[1].split('2')[0]}/" + opt_ticker,
            params={"apiKey": key},
            timeout=6
        )
        if r.status_code == 200:
            data  = r.json().get("results", {})
            quote = data.get("last_quote", {})
            bid   = float(quote.get("bid", 0) or 0)
            ask   = float(quote.get("ask", 0) or 0)
            if bid and ask:
                return round((bid + ask) / 2, 2)
            day = data.get("day", {})
            close = day.get("close") or day.get("c")
            if close:
                return round(float(close), 2)
    except Exception as e:
        print(f"[JOURNAL] Current price error: {e}")
    return None

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
              entry_date: str = None, entry_time: str = None,
              account_id: str = "default",
              spread_type: str = None,
              short_strike: str = None,
              long_strike: str = None,
              spread_width: float = None,
              credit: float = None,
              fees: float = None,
              reg_fees: float = None) -> dict:
    """
    Log a trade entry. Supports single legs and credit/debit spreads.

    For spreads pass spread_type:
      spread_type:  "credit_call" | "credit_put" | "debit_call" | "debit_put"
                    "iron_condor" | "iron_butterfly"
      short_strike: strike sold (credit spreads)
      long_strike:  strike bought (protection leg)
      spread_width: difference between strikes e.g. 5.0
      credit:       net premium received (credit spreads) or paid (debit spreads)
      price:        use 0 for spreads — credit field is used instead
    """
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

    # Find matching FlowCheck alert — check memory first, then Supabase
    fc_score = fc_verdict = None
    try:
        today = now_et.strftime("%Y-%m-%d")

        # Try in-memory analyses first
        analyses_list = []
        try:
            from main import analyses as _mem_analyses
            analyses_list = _mem_analyses
        except:
            pass

        # Fall back to Supabase if memory is empty
        if not analyses_list:
            try:
                from storage import db_get
                import json as _json
                raw = db_get("analyses_today")
                if raw:
                    data = _json.loads(raw)
                    if data.get("date") == today:
                        analyses_list = data.get("analyses", [])
            except:
                pass

        matches = [
            a for a in analyses_list
            if a.get("trade",{}).get("ticker","").upper() == ticker.upper()
            and a.get("date","") == today
        ]
        if matches:
            latest     = matches[-1]
            fc_score   = latest.get("result",{}).get("final_score")
            fc_verdict = latest.get("result",{}).get("verdict")
            print(f"[JOURNAL] FC score attached: {ticker} {fc_score}/7 {fc_verdict}")
    except Exception as e:
        print(f"[JOURNAL] FC score lookup error: {e}")

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
        "total_cost":     round(float(credit or price) * int(contracts) * 100, 2),
        "fc_score":       fc_score,
        "fc_verdict":     fc_verdict,
        "entry_auto_filled": auto_filled,
        "account_id":     account_id,
        "order_type":     None,  # BTO/STO/STC/BTC — set by caller
        "fees":           float(fees) if fees else 0.0,
        "reg_fees":       float(reg_fees) if reg_fees else 0.0,
        # Spread fields
        "is_spread":      spread_type is not None,
        "spread_type":    spread_type,
        "short_strike":   short_strike,
        "long_strike":    long_strike,
        "spread_width":   float(spread_width) if spread_width else None,
        "credit":         float(credit) if credit else None,
        "max_profit": round(
            (abs(float(credit or 0)) * int(contracts) * 100)
            if spread_type and "credit" in (spread_type or "")
            else ((abs(float(spread_width or 0)) - abs(float(credit or 0))) * int(contracts) * 100),
            2) if credit else None,
        "max_loss": round(
            ((abs(float(spread_width or 0)) - abs(float(credit or 0))) * int(contracts) * 100)
            if spread_type and "credit" in (spread_type or "")
            else (abs(float(credit or 0)) * int(contracts) * 100),
            2) if credit else None,
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

def add_tags(ticker: str, tags: list) -> tuple:
    """Add tags to most recent open or closed trade for ticker."""
    journal = load_journal()
    target  = None

    for t in journal["trades"]:
        if t.get("ticker","").upper() == ticker.upper():
            target = t
            break
    if not target:
        for t in journal["closed"]:
            if t.get("ticker","").upper() == ticker.upper():
                target = t

    if not target:
        return False, "No trade found for " + ticker

    if "tags" not in target:
        target["tags"] = []

    added = []
    for tag in tags:
        tag = tag.lstrip("#").lower().strip()
        if tag and tag not in target["tags"]:
            target["tags"].append(tag)
            added.append(tag)

    save_journal(journal)
    all_tags = ["#" + t for t in target["tags"]]
    return True, "Tags: " + " ".join(all_tags)

def parse_exit_datetime(date_str: str, time_str: str) -> datetime:
    """
    Parse date and time into ET datetime.
    Supports multiple formats:

    Date formats:
      YYYY-MM-DD  e.g. 2026-05-27
      MM/DD/YY    e.g. 05/27/26
      MM/DD/YYYY  e.g. 05/27/2026
      MM-DD-YY    e.g. 05-27-26

    Time formats (all ET):
      10:34       24-hour
      10:34am     12-hour with am/pm
      10:34AM
      2:30pm
      2:30PM
      14:30       24-hour afternoon
    """
    # Normalize date
    date_str = date_str.strip().replace("/", "-")
    parts    = date_str.split("-")
    if len(parts[0]) == 4:
        dt_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    elif len(parts[2]) == 4:
        dt_date = datetime.strptime(date_str, "%m-%d-%Y").date()
    else:
        dt_date = datetime.strptime(date_str, "%m-%d-%y").date()

    # Normalize time — strip spaces, uppercase AM/PM
    time_str = time_str.strip().upper().replace(" ", "")

    # Try all supported formats
    time_formats = [
        "%I:%M%p",   # 10:34AM or 2:30PM
        "%I%p",      # 10AM or 2PM (no minutes)
        "%H:%M",     # 14:30 or 10:34
        "%H",        # 14 (hour only, rare)
    ]
    dt_time = None
    for fmt in time_formats:
        try:
            dt_time = datetime.strptime(time_str, fmt).time()
            break
        except:
            continue

    if dt_time is None:
        raise ValueError(
            "Could not parse time: " + time_str +
            " — use 10:34AM, 2:30PM, 10:34, or 14:30"
        )

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
    # For put/call sells (STO): entry = credit received, exit = debit paid to close
    # Profit = entry - exit (premium collected minus premium paid back)
    order_type = trade.get("order_type","BTO").upper()
    is_sell_open = order_type in ("STO",) or trade.get("fill_type","") == "PUT_SELL_BID"
    if is_sell_open:
        pnl_per   = round((entry - ep) * 100, 2)   # Profit when option decays
        pnl_pct   = round(((entry - ep) / entry) * 100, 1)
    else:
        pnl_per   = round((ep - entry) * 100, 2)   # Profit when option rises
        pnl_pct   = round(((ep - entry) / entry) * 100, 1)
    pnl_total = round(pnl_per * closing, 2)

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
               trade_id: int = None,
               account_id: str = None) -> tuple:
    """
    Edit a field on a trade.
    Returns (success, message, trade).
    Pass account_id to disambiguate when same ticker exists in multiple accounts.

    Editable fields:
    - entry_date, entry_time    — fix entry datetime
    - exit_date, exit_time      — fix exit datetime (on closed trades)
    - entry_price               — fix entry price
    - contracts                 — fix contract count
    - expiry                    — fix expiry date
    - strike                    — fix strike price
    - option_type               — fix call/put
    - note                      — add/edit note
    - account_id                — reassign to different account
    """
    journal  = load_journal()
    accounts = journal.get("accounts", {})

    # Find trade — open first, then closed
    target  = None
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
        # Filter open trades by ticker (and account if specified)
        matches = [
            t for t in journal["trades"]
            if t.get("ticker","").upper() == ticker.upper()
            and (account_id is None or t.get("account_id","default") == account_id)
        ]

        # If multiple matches and no account specified — return ambiguity error
        if len(matches) > 1 and account_id is None:
            accts = []
            for t in matches:
                aid   = t.get("account_id","default")
                aname = accounts.get(aid,{}).get("name", aid)
                accts.append("[" + aid + "] " + aname + " — " +
                             str(t.get("strike","")) +
                             t.get("option_type","call")[0].upper() + " " +
                             str(t.get("expiry","")) +
                             " @ $" + str(t.get("entry_price","")))
            return False, (
                "Multiple open " + ticker + " trades found. Specify account:" + chr(10) +
                chr(10).join(accts) + chr(10) + chr(10) +
                "Usage: /edit " + ticker + " @ACCOUNT " + field + " " + value + chr(10) +
                "Example: /edit BE @RH_Trad entry_time 10:34AM"
            ), None

        if matches:
            target  = matches[-1]
            in_open = True
        else:
            # Check closed trades
            matches = [
                t for t in journal["closed"]
                if t.get("ticker","").upper() == ticker.upper()
                and (account_id is None or t.get("account_id","default") == account_id)
            ]
            if len(matches) > 1 and account_id is None:
                accts = []
                for t in matches[-3:]:
                    aid   = t.get("account_id","default")
                    aname = accounts.get(aid,{}).get("name", aid)
                    accts.append("[" + aid + "] " + aname + " " +
                                 str(t.get("exit_date","")) +
                                 " @ $" + str(t.get("entry_price","")))
                return False, (
                    "Multiple closed " + ticker + " trades. Specify account:" + chr(10) +
                    chr(10).join(accts) + chr(10) + chr(10) +
                    "Usage: /edit " + ticker + " @ACCOUNT " + field + " " + value
                ), None
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

        elif field == "account_id":
            # Validate account exists
            accounts = journal.get("accounts", {})
            if value not in accounts and value != "default":
                return False, "Unknown account: " + value + ". Valid: " + ", ".join(accounts.keys()), None
            target["account_id"] = value

        elif field == "fc_score":
            try:
                target["fc_score"] = float(value)
            except:
                return False, "fc_score must be a number e.g. 6", None
        elif field == "fc_verdict":
            if value.upper() not in ("TRADE","WATCH","SKIP"):
                return False, "fc_verdict must be TRADE, WATCH, or SKIP", None
            target["fc_verdict"] = value.upper()
        elif field == "long_strike":
            target["long_strike"] = str(value)
        elif field == "short_strike":
            target["short_strike"] = str(value)
        elif field == "credit":
            try:
                target["credit"] = float(value)
                # Recalculate max profit/loss
                width  = float(target.get("spread_width",0) or 0)
                cred   = abs(float(value))
                contr  = int(target.get("contracts",1))
                is_deb = "debit" in (target.get("spread_type","") or "")
                if width > 0:
                    if is_deb:
                        target["max_profit"] = round((width - cred) * contr * 100, 2)
                        target["max_loss"]   = round(cred * contr * 100, 2)
                    else:
                        target["max_profit"] = round(cred * contr * 100, 2)
                        target["max_loss"]   = round((width - cred) * contr * 100, 2)
            except:
                return False, "credit must be a number e.g. 3.50", None
        elif field == "spread_width":
            try:
                target["spread_width"] = float(value)
            except:
                return False, "spread_width must be a number e.g. 10", None
        else:
            valid = "entry_date, entry_time, exit_date, exit_time, entry_price, contracts, expiry, strike, note, option_type, account_id, fc_score, fc_verdict, long_strike, short_strike, credit, spread_width"
            return False, "Unknown field: " + field + ". Valid: " + valid, None

        save_journal(journal)

        # Auto-recalc analytics if time/price fields changed
        recalc_fields = {"entry_date","entry_time","exit_date","exit_time","entry_price","expiry","strike"}
        recalc_msg = ""
        if field in recalc_fields and target.get("opt_ticker"):
            try:
                success, rmsg = recalc_analytics(target["ticker"])
                recalc_msg = " | " + ("Analytics recalculated" if success else "Recalc skipped: " + rmsg[:40])
            except Exception as re:
                recalc_msg = " | Recalc skipped: " + str(re)[:40]

        return True, "Updated " + field + ": " + str(old_value) + " -> " + str(value) + recalc_msg, target

    except Exception as e:
        return False, "Error editing " + field + ": " + str(e), None

# ── Recalculate Analytics ─────────────────────────────────────────────

def recalc_analytics(ticker: str) -> tuple:
    """
    Re-fetch Polygon bars and recalculate peak/drawdown analytics
    after an edit to entry_time or entry_price.
    Returns (success, message).
    """
    journal = load_journal()

    # Check open trades first, then closed
    target  = None
    in_open = True
    for t in journal["trades"]:
        if t.get("ticker","").upper() == ticker.upper():
            target = t
            break
    if not target:
        for t in journal["closed"]:
            if t.get("ticker","").upper() == ticker.upper():
                target  = t
                in_open = False
                break

    if not target:
        return False, "No trade found for " + ticker

    opt_ticker = target.get("opt_ticker")
    if not opt_ticker:
        return False, "No option ticker — check expiry and strike are correct"

    try:
        entry_dt = datetime.fromisoformat(target["entry_datetime"])
        if in_open:
            # For open trades use current time as end
            exit_dt = datetime.now(ZoneInfo("America/New_York"))
        else:
            if not target.get("exit_datetime"):
                return False, "No exit datetime recorded"
            exit_dt = datetime.fromisoformat(target["exit_datetime"])

        print(f"[JOURNAL] Recalculating analytics for {opt_ticker}...")
        bars = fetch_option_history(opt_ticker, entry_dt, exit_dt)
        if not bars:
            return False, "No Polygon data available for this period"

        entry_price = float(target.get("entry_price", 0))
        exit_price  = float(target.get("exit_price", 0)) if target.get("exit_price") else entry_price
        analytics   = calc_holding_analytics(bars, entry_price, exit_price)

        if analytics:
            target.update(analytics)
            save_journal(journal)
            return True, (
                "Analytics updated: "
                "Peak +" + str(analytics.get("peak_pct","?")) + "% at " +
                str(analytics.get("peak_time","?")) +
                " | Max DD -" + str(analytics.get("max_drawdown","?")) + "%"
            )
        return False, "Could not calculate analytics from available data"

    except Exception as e:
        return False, "Recalc error: " + str(e)

# ── Add to Existing Position ───────────────────────────────────────────

def add_to_position(ticker: str, contracts: int, price: float,
                     entry_date: str = None, entry_time: str = None) -> tuple:
    """
    Add contracts to an existing open position.
    Calculates blended average entry price.
    Returns (success, message, trade).
    """
    journal = load_journal()
    now_et  = datetime.now(ZoneInfo("America/New_York"))

    # Find existing open trade
    matches = [t for t in journal["trades"]
               if t.get("ticker","").upper() == ticker.upper()
               and t.get("status") == "OPEN"]
    if not matches:
        return False, "No open trade for " + ticker + " — use /entry to create one", None

    trade = matches[-1]

    # Parse add datetime
    auto_filled = False
    if entry_date and entry_time:
        try:
            add_dt = parse_exit_datetime(entry_date, entry_time)
        except:
            add_dt      = now_et
            auto_filled = True
    elif entry_date:
        try:
            add_dt = parse_exit_datetime(entry_date, "09:30")
        except:
            add_dt      = now_et
            auto_filled = True
    else:
        add_dt      = now_et
        auto_filled = True

    # Calculate blended average
    existing_contracts = int(trade.get("contracts_remaining", trade["contracts"]))
    existing_price     = float(trade["entry_price"])
    new_contracts      = int(contracts)
    new_price          = float(price)

    total_contracts = existing_contracts + new_contracts
    blended_price   = round(
        (existing_price * existing_contracts + new_price * new_contracts) / total_contracts, 3
    )
    total_cost_added = round(new_price * new_contracts * 100, 2)

    # Record the add
    if "adds" not in trade:
        trade["adds"] = []
    trade["adds"].append({
        "datetime":  add_dt.isoformat(),
        "date":      add_dt.strftime("%Y-%m-%d"),
        "time":      add_dt.strftime("%H:%M"),
        "contracts": new_contracts,
        "price":     new_price,
        "auto_filled": auto_filled,
    })

    # Update trade
    trade["contracts"]           = total_contracts
    trade["contracts_remaining"] = total_contracts
    trade["entry_price"]         = blended_price  # Blended average
    trade["total_cost"]          = round(
        float(trade.get("total_cost",0)) + total_cost_added, 2
    )

    save_journal(journal)
    return True, (
        "Added " + str(new_contracts) + "x @ $" + str(new_price) +
        " to " + ticker + chr(10) +
        "New avg: $" + str(blended_price) +
        " | Total: " + str(total_contracts) + " contracts" +
        (" (auto-filled time)" if auto_filled else "")
    ), trade

# ── Export to CSV ──────────────────────────────────────────────────────

def export_journal_csv() -> str:
    """Export all closed trades as CSV string."""
    journal  = load_journal()
    closed_t = journal.get("closed", [])

    if not closed_t:
        return "No closed trades to export."

    headers = [
        "id","ticker","strike","option_type","expiry","contracts",
        "entry_date","entry_time","entry_price","total_cost","entry_auto_filled",
        "exit_date","exit_time","exit_price","pnl_per_contract","pnl_total","pnl_pct",
        "holding_days","holding_hours",
        "peak_price","peak_pct","peak_time","trough_price","trough_pct",
        "max_drawdown","left_on_table",
        "fc_score","fc_verdict","note",
        "is_partial","adds_count"
    ]

    rows = [",".join(headers)]
    for t in closed_t:
        def clean(v):
            if v is None: return ""
            return str(v).replace(",",";").replace(chr(10)," ").replace(chr(13)," ")
        row = [
            clean(t.get("id","")),
            clean(t.get("ticker","")),
            clean(t.get("strike","")),
            clean(t.get("option_type","")),
            clean(t.get("expiry","")),
            clean(t.get("contracts","")),
            clean(t.get("entry_date","")),
            clean(t.get("entry_time","")),
            clean(t.get("entry_price","")),
            clean(t.get("total_cost","")),
            clean(t.get("entry_auto_filled","")),
            clean(t.get("exit_date","")),
            clean(t.get("exit_time","")),
            clean(t.get("exit_price","")),
            clean(t.get("pnl_per_contract","")),
            clean(t.get("pnl_total","")),
            clean(t.get("pnl_pct","")),
            clean(t.get("holding_days","")),
            clean(t.get("holding_hours","")),
            clean(t.get("peak_price","")),
            clean(t.get("peak_pct","")),
            clean(t.get("peak_time","")),
            clean(t.get("trough_price","")),
            clean(t.get("trough_pct","")),
            clean(t.get("max_drawdown","")),
            clean(t.get("left_on_table","")),
            clean(t.get("fc_score","")),
            clean(t.get("fc_verdict","")),
            clean(t.get("note","")),
            clean(t.get("is_partial","")),
            clean(len(t.get("adds",[]))),
        ]
        rows.append(",".join(row))

    return chr(10).join(rows)

# ── Missed Trade ───────────────────────────────────────────────────────

def add_missed_trade(ticker: str, reason: str = "") -> dict:
    """Log a FlowCheck alert that was not taken."""
    journal = load_journal()
    now_et  = datetime.now(ZoneInfo("America/New_York"))

    # Find today's FlowCheck alert for this ticker
    fc_score = fc_verdict = fc_one_liner = None
    try:
        from main import analyses
        today   = now_et.strftime("%Y-%m-%d")
        matches = [a for a in analyses
                   if a.get("trade",{}).get("ticker","").upper() == ticker.upper()
                   and a.get("date") == today]
        if matches:
            latest      = matches[-1]
            fc_score    = latest.get("result",{}).get("final_score")
            fc_verdict  = latest.get("result",{}).get("verdict")
            fc_one_liner= latest.get("result",{}).get("one_liner","")
    except:
        pass

    missed = {
        "id":         "M" + str(len(journal.get("missed",[])) + 1),
        "type":       "MISSED",
        "ticker":     ticker.upper(),
        "date":       now_et.strftime("%Y-%m-%d"),
        "time":       now_et.strftime("%H:%M"),
        "reason":     reason,
        "fc_score":   fc_score,
        "fc_verdict": fc_verdict,
        "fc_summary": fc_one_liner,
    }

    if "missed" not in journal:
        journal["missed"] = []
    journal["missed"].append(missed)
    save_journal(journal)
    print(f"[JOURNAL] Missed trade logged: {ticker}")
    return missed

def get_missed_summary() -> str:
    """Summarize missed trades vs taken trades."""
    journal = load_journal()
    missed  = journal.get("missed", [])
    closed  = journal.get("closed", [])

    if not missed:
        return "No missed trades logged yet." + chr(10) + "Use /missed TICKER REASON to log skipped alerts."

    # Compare outcomes
    missed_verdicts = {}
    for m in missed:
        v = m.get("fc_verdict","?")
        missed_verdicts[v] = missed_verdicts.get(v, 0) + 1

    lines = [
        "MISSED TRADES (" + str(len(missed)) + ")",
        "",
    ]
    for m in missed[-10:]:
        fc   = str(m.get("fc_score","?")) + "/7 " + str(m.get("fc_verdict","?"))
        rsn  = m.get("reason","") or "No reason given"
        lines.append(m["ticker"] + " | " + m["date"] + " | " + fc)
        lines.append("  Reason: " + rsn)
        if m.get("fc_summary"):
            lines.append("  Alert: " + str(m["fc_summary"])[:60])

    lines.append("")
    lines.append("By verdict: " + str(missed_verdicts))

    if closed:
        taken_wins = sum(1 for t in closed if (t.get("pnl_total",0) or 0) > 0)
        taken_wr   = round(taken_wins/len(closed)*100,1) if closed else 0
        lines.append("Trades taken win rate: " + str(taken_wr) + "% (" + str(len(closed)) + " trades)")

    return chr(10).join(lines)

# ── Recalculate Analytics ─────────────────────────────────────────────

def recalc_analytics(ticker: str) -> tuple:
    """Re-fetch Polygon bars and recalculate peak/drawdown after an edit."""
    journal = load_journal()
    target  = None
    in_open = True

    for t in journal["trades"]:
        if t.get("ticker","").upper() == ticker.upper():
            target = t
            break
    if not target:
        for t in journal["closed"]:
            if t.get("ticker","").upper() == ticker.upper():
                target  = t
                in_open = False
                break
    if not target:
        return False, "No trade found for " + ticker

    opt_ticker = target.get("opt_ticker")
    if not opt_ticker:
        return False, "No option ticker — check expiry and strike"

    try:
        entry_dt = datetime.fromisoformat(target["entry_datetime"])
        if in_open:
            exit_dt = datetime.now(ZoneInfo("America/New_York"))
        else:
            if not target.get("exit_datetime"):
                return False, "No exit datetime recorded"
            exit_dt = datetime.fromisoformat(target["exit_datetime"])

        print("[JOURNAL] Recalculating analytics for " + opt_ticker)
        bars = fetch_option_history(opt_ticker, entry_dt, exit_dt)
        if not bars:
            return False, "No Polygon data available for this period"

        entry_price = float(target.get("entry_price", 0))
        exit_price  = float(target.get("exit_price", 0)) if target.get("exit_price") else entry_price
        analytics   = calc_holding_analytics(bars, entry_price, exit_price)

        if analytics:
            target.update(analytics)
            save_journal(journal)
            msg = ("Analytics updated: Peak +" + str(analytics.get("peak_pct","?")) +
                   "% at " + str(analytics.get("peak_time","?")) +
                   " | Max DD -" + str(analytics.get("max_drawdown","?")) + "%")
            return True, msg
        return False, "Could not calculate analytics from available data"

    except Exception as e:
        return False, "Recalc error: " + str(e)


# ── Add to Existing Position ───────────────────────────────────────────

def add_to_position(ticker: str, contracts: int, price: float,
                    entry_date: str = None, entry_time_str: str = None) -> tuple:
    """
    Add contracts to an existing open position.
    Calculates blended average entry price.
    """
    journal = load_journal()
    now_et  = datetime.now(ZoneInfo("America/New_York"))

    matches = [t for t in journal["trades"]
               if t.get("ticker","").upper() == ticker.upper()
               and t.get("status") == "OPEN"]
    if not matches:
        return False, "No open trade for " + ticker + " — use /entry first", None

    trade = matches[-1]

    auto_filled = False
    if entry_date and entry_time_str:
        try:
            add_dt = parse_exit_datetime(entry_date, entry_time_str)
        except Exception:
            add_dt      = now_et
            auto_filled = True
    elif entry_date:
        try:
            add_dt = parse_exit_datetime(entry_date, "09:30")
        except Exception:
            add_dt      = now_et
            auto_filled = True
    else:
        add_dt      = now_et
        auto_filled = True

    existing_contracts = int(trade.get("contracts_remaining", trade["contracts"]))
    existing_price     = float(trade["entry_price"])
    new_contracts      = int(contracts)
    new_price          = float(price)
    total_contracts    = existing_contracts + new_contracts
    blended_price      = round(
        (existing_price * existing_contracts + new_price * new_contracts) / total_contracts, 3
    )
    cost_added = round(new_price * new_contracts * 100, 2)

    if "adds" not in trade:
        trade["adds"] = []
    trade["adds"].append({
        "datetime":   add_dt.isoformat(),
        "date":       add_dt.strftime("%Y-%m-%d"),
        "time":       add_dt.strftime("%H:%M"),
        "contracts":  new_contracts,
        "price":      new_price,
        "auto_filled": auto_filled,
    })

    trade["contracts"]           = total_contracts
    trade["contracts_remaining"] = total_contracts
    trade["entry_price"]         = blended_price
    trade["total_cost"]          = round(float(trade.get("total_cost", 0)) + cost_added, 2)

    save_journal(journal)
    auto_note = " (time auto-filled)" if auto_filled else ""
    msg = ("Added " + str(new_contracts) + "x @ $" + str(new_price) + " to " + ticker +
           chr(10) + "Avg: $" + str(blended_price) +
           " | Total: " + str(total_contracts) + " contracts" + auto_note)
    return True, msg, trade


# ── Export to CSV ──────────────────────────────────────────────────────

def export_journal_csv(account_id: str = None) -> str:
    """Export closed trades as CSV. Pass account_id to filter by account."""
    journal  = load_journal()
    closed_t = journal.get("closed", [])
    accounts = journal.get("accounts", {})

    # Filter by account if specified
    if account_id and account_id != "all":
        closed_t = [t for t in closed_t
                    if t.get("account_id","default") == account_id]
        acc_name = accounts.get(account_id,{}).get("name", account_id)
    else:
        acc_name = None

    if not closed_t:
        suffix = f" for {acc_name}" if acc_name else ""
        return "No closed trades to export" + suffix + "."

    multi_account = len(accounts) > 1

    headers = [
        "id", "account", "account_name",
        "ticker", "strike", "option_type", "expiry", "contracts",
        "entry_date", "entry_time", "entry_price", "total_cost", "entry_auto_filled",
        "exit_date", "exit_time", "exit_price",
        "pnl_per_contract", "pnl_total", "pnl_pct",
        "holding_days", "holding_hours",
        "peak_price", "peak_pct", "peak_time",
        "trough_price", "trough_pct", "max_drawdown", "left_on_table",
        "fc_score", "fc_verdict", "note", "tags", "is_partial", "adds_count",
    ]

    def clean(v):
        if v is None:
            return ""
        return str(v).replace(",", ";").replace("\n", " ").replace("\r", " ")

    rows = [",".join(headers)]
    for t in closed_t:
        aid  = t.get("account_id","default")
        aname = accounts.get(aid,{}).get("name", aid)
        row = [
            clean(t.get("id")),
            clean(aid),
            clean(aname),
            clean(t.get("ticker")),
            clean(t.get("strike")),
            clean(t.get("option_type")),
            clean(t.get("expiry")),
            clean(t.get("contracts")),
            clean(t.get("entry_date")),
            clean(t.get("entry_time")),
            clean(t.get("entry_price")),
            clean(t.get("total_cost")),
            clean(t.get("entry_auto_filled")),
            clean(t.get("exit_date")),
            clean(t.get("exit_time")),
            clean(t.get("exit_price")),
            clean(t.get("pnl_per_contract")),
            clean(t.get("pnl_total")),
            clean(t.get("pnl_pct")),
            clean(t.get("holding_days")),
            clean(t.get("holding_hours")),
            clean(t.get("peak_price")),
            clean(t.get("peak_pct")),
            clean(t.get("peak_time")),
            clean(t.get("trough_price")),
            clean(t.get("trough_pct")),
            clean(t.get("max_drawdown")),
            clean(t.get("left_on_table")),
            clean(t.get("fc_score")),
            clean(t.get("fc_verdict")),
            clean(t.get("note")),
            clean(" ".join(["#"+tg for tg in t.get("tags",[])])),
            clean(t.get("is_partial")),
            clean(len(t.get("adds", []))),
        ]
        rows.append(",".join(row))

    return "\n".join(rows)


# ── Missed Trade ───────────────────────────────────────────────────────

def add_missed_trade(ticker: str, reason: str = "") -> dict:
    """Log a FlowCheck alert that was not taken."""
    journal = load_journal()
    now_et  = datetime.now(ZoneInfo("America/New_York"))

    fc_score = fc_verdict = fc_one_liner = None
    try:
        from main import analyses
        today   = now_et.strftime("%Y-%m-%d")
        matches = [
            a for a in analyses
            if a.get("trade", {}).get("ticker", "").upper() == ticker.upper()
            and a.get("date") == today
        ]
        if matches:
            latest       = matches[-1]
            fc_score     = latest.get("result", {}).get("final_score")
            fc_verdict   = latest.get("result", {}).get("verdict")
            fc_one_liner = latest.get("result", {}).get("one_liner", "")
    except Exception:
        pass

    missed = {
        "id":         "M" + str(len(journal.get("missed", [])) + 1),
        "type":       "MISSED",
        "ticker":     ticker.upper(),
        "date":       now_et.strftime("%Y-%m-%d"),
        "time":       now_et.strftime("%H:%M"),
        "reason":     reason,
        "fc_score":   fc_score,
        "fc_verdict": fc_verdict,
        "fc_summary": fc_one_liner,
    }

    if "missed" not in journal:
        journal["missed"] = []
    journal["missed"].append(missed)
    save_journal(journal)
    print("[JOURNAL] Missed trade logged: " + ticker)
    return missed


def get_missed_summary() -> str:
    """Summarize missed trades."""
    journal = load_journal()
    missed  = journal.get("missed", [])
    closed  = journal.get("closed", [])

    if not missed:
        return "No missed trades logged.\nUse /missed TICKER REASON to log skipped alerts."

    verdict_counts = {}
    for m in missed:
        v = m.get("fc_verdict", "?")
        verdict_counts[v] = verdict_counts.get(v, 0) + 1

    lines = ["MISSED TRADES (" + str(len(missed)) + ")", ""]
    for m in missed[-10:]:
        fc  = str(m.get("fc_score", "?")) + "/7 " + str(m.get("fc_verdict", "?"))
        rsn = m.get("reason", "") or "No reason given"
        lines.append(m["ticker"] + " | " + m["date"] + " | " + fc)
        lines.append("  Reason: " + rsn)
        if m.get("fc_summary"):
            lines.append("  Alert: " + str(m["fc_summary"])[:60])

    lines.append("")
    lines.append("By verdict: " + str(verdict_counts))

    if closed:
        wins = sum(1 for t in closed if (t.get("pnl_total", 0) or 0) > 0)
        wr   = round(wins / len(closed) * 100, 1)
        lines.append("Taken trades win rate: " + str(wr) + "% (" + str(len(closed)) + ")")

    return "\n".join(lines)


# ── Display Functions ──────────────────────────────────────────────────

def sync_positions(positions: list) -> dict:
    """
    Reconcile a list of positions against open journal trades.
    positions: list of dicts with ticker, strike, opt_type, expiry, contracts, price, account_id

    Returns:
      added:   trades that were in positions but not in journal — auto-logged
      missing: trades that are in journal but not in positions list — flagged
      matched: trades that match
    """
    journal  = load_journal()
    open_t   = journal.get("trades", [])
    added    = []
    matched  = []
    missing  = []

    # Check each position against journal
    for pos in positions:
        ticker     = pos.get("ticker","").upper()
        strike     = str(pos.get("strike",""))
        opt_type   = pos.get("opt_type","call").lower()
        expiry     = pos.get("expiry","")
        contracts  = int(pos.get("contracts",1))
        price      = float(pos.get("price",0))
        account_id = pos.get("account_id","default")

        # Find matching open trade
        found = False
        for t in open_t:
            if (t.get("ticker","").upper() == ticker and
                str(t.get("strike","")) == strike and
                t.get("option_type","call").lower() == opt_type and
                t.get("account_id","default") == account_id):
                found = True
                matched.append(ticker + " " + strike + opt_type[0].upper())
                break

        if not found:
            # Not in journal — add it
            trade = add_entry(
                ticker, strike, opt_type, expiry,
                contracts, price,
                None, None, account_id
            )
            added.append({
                "ticker":    ticker,
                "strike":    strike,
                "opt_type":  opt_type,
                "expiry":    expiry,
                "contracts": contracts,
                "price":     price,
                "account_id": account_id,
            })
            print(f"[SYNC] Added missing position: {ticker} {strike}{opt_type[0].upper()}")

    # Check journal for positions not in sync list
    sync_tickers = set(
        (p.get("ticker","").upper(), str(p.get("strike","")), p.get("account_id","default"))
        for p in positions
    )
    for t in open_t:
        key = (t.get("ticker","").upper(), str(t.get("strike","")), t.get("account_id","default"))
        if key not in sync_tickers:
            missing.append(
                t.get("ticker","") + " " + str(t.get("strike","")) +
                t.get("option_type","call")[0].upper() + " " + str(t.get("expiry",""))
            )

    return {"added": added, "matched": matched, "missing": missing}

def delete_trade(ticker: str, account_id: str = None, trade_id: str = None) -> tuple:
    """
    Delete a trade from open or closed journal.
    Returns (success, message).
    If multiple trades match ticker, requires account_id to disambiguate.
    """
    journal  = load_journal()
    accounts = journal.get("accounts", {})
    ticker   = ticker.upper()

    def match(t):
        if t.get("ticker","").upper() != ticker:
            return False
        if account_id and t.get("account_id","default") != account_id.lower():
            return False
        if trade_id and str(t.get("id","")) != str(trade_id):
            return False
        return True

    # Search open trades first, then closed
    for bucket in ("trades", "closed"):
        matches = [t for t in journal[bucket] if match(t)]
        if len(matches) > 1 and not account_id and not trade_id:
            accts = []
            for t in matches:
                aid   = t.get("account_id","default")
                aname = accounts.get(aid,{}).get("name", aid)
                otype = t.get("option_type","call")[0].upper()
                accts.append(
                    "[" + aid + "] " + aname + " — " +
                    t.get("strike","") + otype + " " +
                    t.get("expiry","") + " @ $" + str(t.get("entry_price",""))
                )
            return False, (
                "Multiple " + ticker + " trades found. Specify account:" + chr(10) +
                chr(10).join(accts) + chr(10) + chr(10) +
                "Usage: /delete " + ticker + " @ACCOUNT"
            )
        if matches:
            t = matches[-1]  # most recent
            journal[bucket].remove(t)
            save_journal(journal)
            otype = t.get("option_type","call")[0].upper()
            desc  = (
                t.get("ticker","") + " " + t.get("strike","") + otype +
                " " + t.get("expiry","") +
                " x" + str(t.get("contracts","?")) +
                " @ $" + str(t.get("entry_price",""))
            )
            return True, "Deleted: " + desc

    return False, "No trade found for " + ticker + ((" [@" + account_id + "]") if account_id else "")

def get_journal_summary(account_id: str = None) -> str:
    journal  = load_journal()
    open_t   = journal.get("trades", [])
    closed_t = journal.get("closed", [])
    lines    = []

    if open_t:
        accounts     = load_journal().get("accounts",{})
        show_account = len(accounts) > 1

        # Total capital at risk per account
        total_cost_all = round(sum(float(t.get("total_cost",0) or 0) for t in open_t), 2)
        if show_account and not account_id:
            acct_totals = {}
            for t in open_t:
                aid  = t.get("account_id","default")
                cost = float(t.get("total_cost",0) or 0)
                acct_totals[aid] = acct_totals.get(aid, 0) + cost
            totals_str = " | ".join(
                accounts.get(aid,{}).get("name", aid) + ": $" + str(round(v,2))
                for aid, v in acct_totals.items()
            )
            lines.append("OPEN TRADES (" + str(len(open_t)) + ") — $" + str(total_cost_all) + " at risk")
            lines.append("  " + totals_str)
        else:
            lines.append("OPEN TRADES (" + str(len(open_t)) + ") — $" + str(total_cost_all) + " at risk")
        lines.append("")
        for t in open_t:
            otype   = t["option_type"][0].upper()
            fc      = ""
            if t.get("fc_score"):
                fc = "[" + str(t["fc_score"]) + "/7 " + str(t["fc_verdict"]) + "]"
            held_h = ""
            now_et = datetime.now(ZoneInfo("America/New_York"))
            try:
                entry_dt = datetime.fromisoformat(t["entry_datetime"])
                hrs      = round((now_et - entry_dt).total_seconds() / 3600, 1)
                held_h   = " | Held " + str(hrs) + "h"
            except:
                pass

            # Fetch current option price for unrealized P&L
            curr_price = None
            unrealized = ""
            try:
                opt_ticker = t.get("opt_ticker")
                if opt_ticker:
                    curr = fetch_current_option_price_simple(opt_ticker)
                    if curr:
                        curr_price  = curr
                        entry_price = float(t.get("entry_price", 0))
                        contracts   = int(t.get("contracts_remaining", t.get("contracts", 1)))
                        unreal_pct  = round(((curr - entry_price) / entry_price) * 100, 1)
                        unreal_pnl  = round((curr - entry_price) * 100 * contracts, 2)
                        sign        = "+" if unreal_pnl >= 0 else ""
                        unrealized  = ("  Current: $" + str(curr) +
                                       " | Unrealized: " + sign + "$" + str(unreal_pnl) +
                                       " (" + sign + str(unreal_pct) + "%)")
            except:
                pass

            # Stop and target
            stop_target = ""
            try:
                from risk_manager import calc_smart_stop
                stock_price = t.get("entry_stock_price")
                if stock_price:
                    smart = calc_smart_stop(t.get("ticker",""), float(stock_price),
                                           t.get("option_type","call"))
                    stop  = smart.get("stop_price")
                    entry_p = float(t.get("entry_price",0))
                    target  = round(entry_p * 2.0, 2)  # 2:1 R/R default
                    if stop:
                        stop_target = ("  Stop: $" + str(stop) +
                                       " | Target: $" + str(target) +
                                       " | R/R: 2.0:1")
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

            acc_label = ""
            if show_account:
                aid  = t.get("account_id","default")
                aname = accounts.get(aid,{}).get("name", aid)
                acc_label = " [" + aname + "]"
            # Build contract description
            if t.get("is_spread") and t.get("spread_type"):
                stype  = t["spread_type"].replace("_","_").replace("debit","DEBIT").replace("credit","CREDIT").replace("_call"," CALL").replace("_put"," PUT").replace("iron_condor","IRON CONDOR")
                ss     = t.get("short_strike","?")
                ls     = t.get("long_strike","?")
                credit = t.get("credit","?")
                width  = t.get("spread_width","?")
                mp     = t.get("max_profit","?")
                ml     = t.get("max_loss","?")
                otype  = t.get("option_type","call")[0].upper()
                ot     = t.get("order_type","")
                ot_str = (" " + ot) if ot else ""
                # Show both legs clearly
                is_debit = "debit" in t.get("spread_type","")
                if is_debit:
                    leg1 = "BTO $" + str(ls) + otype  # bought leg
                    leg2 = "STO $" + str(ss) + otype  # sold leg
                else:
                    leg1 = "STO $" + str(ss) + otype  # sold leg
                    leg2 = "BTO $" + str(ls) + otype  # bought protection
                lines.append(
                    t["ticker"] + " " + stype + " " + t["expiry"] + dte_str +
                    " x" + str(remaining) + ot_str + acc_label
                )
                lines.append(
                    "  " + leg1 + " | " + leg2 + " | Width: $" + str(width)
                )
                lines.append(
                    "  Premium: $" + str(credit) +
                    " | Max profit: $" + str(mp) + " | Max loss: $" + str(ml)
                )
            else:
                ot     = t.get("order_type","BTO")
                ot_str = (" " + ot) if ot else " BTO"
                lines.append(
                    t["ticker"] + " " + t["strike"] + otype + " " + t["expiry"] +
                    dte_str + " x" + str(remaining) + "/" + str(t["contracts"]) +
                    " @ $" + str(t["entry_price"]) + ot_str + acc_label
                )
            lines.append(
                "  In: " + t["entry_date"] + " " + t["entry_time"] +
                held_h + " | Cost: $" + str(t["total_cost"]) +
                (" " + fc if fc else "")
            )
            if unrealized:
                lines.append(unrealized)
            if stop_target:
                lines.append(stop_target)
            if exits_so_far:
                lines.append(
                    "  Partial exits: " + str(len(exits_so_far)) +
                    " | Realized: $" + str(round(cum_pnl_so_far,2))
                )
            if t.get("note"):
                lines.append("  Note: " + str(t["note"]))
            if t.get("adds"):
                lines.append("  Adds: " + str(len(t["adds"])) + " — avg entry $" + str(t.get("entry_price","")))
    else:
        lines.append("No open trades")

    # Daily P&L by account
    from datetime import date as _date
    today_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    closed_today = [t for t in closed_t if t.get("exit_date","") == today_str]

    if closed_today:
        accounts = accounts if "accounts" in dir() else load_journal().get("accounts",{})
        day_total = sum(float(t.get("pnl_total",0) or 0) for t in closed_today)
        sign      = "+" if day_total >= 0 else ""
        label     = "✅" if day_total >= 0 else "❌"
        lines.append("")
        lines.append("TODAY'S P&L " + label + " " + sign + "$" + str(round(day_total,2)))

        if show_account and len(set(t.get("account_id","default") for t in closed_today)) > 1:
            acct_day = {}
            for t in closed_today:
                aid = t.get("account_id","default")
                acct_day[aid] = acct_day.get(aid,0) + float(t.get("pnl_total",0) or 0)
            for aid, pnl in acct_day.items():
                aname = accounts.get(aid,{}).get("name", aid)
                sign2 = "+" if pnl >= 0 else ""
                emoji = "✅" if pnl >= 0 else "❌"
                lines.append("  " + emoji + " " + aname + ": " + sign2 + "$" + str(round(pnl,2)))

        for t in closed_today:
            pnl   = float(t.get("pnl_total",0) or 0)
            pct   = float(t.get("pnl_pct",0) or 0)
            sign2 = "+" if pnl >= 0 else ""
            emoji = "✅" if pnl >= 0 else "❌"
            otype = t.get("option_type","call")[0].upper()
            acc   = ""
            if show_account:
                aid   = t.get("account_id","default")
                aname = accounts.get(aid,{}).get("name", aid)
                acc   = " [@" + aname + "]"
            lines.append(
                "  " + emoji + " " + t.get("ticker","") + " " +
                str(t.get("strike","")) + otype + ": " +
                sign2 + "$" + str(round(pnl,2)) +
                " (" + sign2 + str(round(pct,1)) + "%)" + acc
            )

    recent = closed_t[-5:] if closed_t else []
    if recent:
        lines.append("")
        lines.append("RECENT CLOSED (last 5 of " + str(len(closed_t)) + ")")
        lines.append("")
        for t in recent:
            # Show account label if multiple accounts
            acc_suffix = ""
            if show_account:
                aid   = t.get("account_id","default")
                aname = accounts.get(aid,{}).get("name", aid)
                acc_suffix = " [" + aname + "]"
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
                " x" + str(t["contracts"]) + acc_suffix
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

def get_pnl_summary(account_id: str = None) -> str:
    """P&L summary. Pass account_id to filter by account."""
    journal  = load_journal()
    closed_t = journal.get("closed", [])
    open_t   = journal.get("trades", [])
    accounts = journal.get("accounts", {})

    # Filter by account if specified
    if account_id and account_id != "all":
        closed_t = [t for t in closed_t if t.get("account_id","default") == account_id]
        open_t   = [t for t in open_t   if t.get("account_id","default") == account_id]
        acc_name = accounts.get(account_id,{}).get("name", account_id)
        acc_label = " [" + acc_name + "]"
    else:
        acc_label = " [All Accounts]" if len(accounts) > 1 else ""

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
        "TRADE JOURNAL P&L" + acc_label,
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
