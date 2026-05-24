"""
Exit signal monitor for FlowCheck.
Runs every 15 minutes during market hours.
Monitors open positions for:
1. Target hit (2:1 R/R from entry)
2. Stop loss breach
3. DTE warning (3 days left)
4. IV spike (consider selling)
5. Large adverse move (cut losses)
"""
import os, json, requests, time
from datetime import datetime
from zoneinfo import ZoneInfo
from sms import send_sms

POSITIONS_FILE = "/tmp/flowcheck_positions.json"

def poly_key():
    return os.environ.get("POLYGON_API_KEY")

# ── Position Management ────────────────────────────────────────────────

def load_positions() -> list:
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except:
        return []

def save_positions(positions: list):
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f)
    except Exception as e:
        print(f"[EXIT] Save error: {e}")

def add_position(trade: dict, data: dict, result: dict):
    """Add a new position to monitor when TRADE verdict fires."""
    positions = load_positions()

    stock_price  = data.get("stock_price")
    option_price = trade.get("option_price") or data.get("flow_fill_price")

    if not stock_price:
        print(f"[EXIT] No stock price for {trade.get('ticker')} — skipping position tracking")
        return

    # Use smart stop based on technical levels
    try:
        from risk_manager import calc_smart_stop
        smart = calc_smart_stop(trade.get("ticker",""), float(stock_price),
                                 trade.get("option_type","call"))
        stop_price  = smart.get("stop_price", round(float(stock_price)*0.95, 2))
        stop_reason = smart.get("stop_reason","Fixed 5% stop")
    except Exception as e:
        print(f"[EXIT] Smart stop error: {e}")
        stop_pct   = 0.08 if float(stock_price) < 20 else 0.05
        stop_price = round(float(stock_price) * (1-stop_pct), 2)
        stop_reason = f"Fixed {int(stop_pct*100)}% stop"
    # Target: 10% above entry
    target_price = round(float(stock_price) * 1.10, 2)

    position = {
        "id":            len(positions),
        "ticker":        trade.get("ticker"),
        "strike":        trade.get("strike"),
        "option_type":   trade.get("option_type","call"),
        "expiry":        trade.get("expiry","?"),
        "expiry_raw":    trade.get("expiry_raw",""),
        "dte_at_entry":  data.get("days_to_expiry"),
        "entry_stock":   float(stock_price),
        "entry_option":  float(option_price) if option_price else None,
        "stop_price":    stop_price,
        "target_price":  target_price,
        "score":         result.get("final_score"),
        "verdict":       result.get("verdict"),
        "added":         datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "date":          datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"),
        "status":        "OPEN",
        "exit_reason":   None,
        "exit_price":    None,
        "exit_option_price": None,
        "pnl_pct":       None,
        "option_pnl_pct":None,
        "alerted_stop":  False,
        "alerted_target":False,
        "alerted_dte":   False,
    }
    positions.append(position)
    save_positions(positions)
    print(f"[EXIT] Tracking {trade.get('ticker')} — entry=${stock_price}, "
          f"stop=${stop_price}, target=${target_price}")

def get_option_price(ticker: str, strike: str, opt_type: str, expiry_raw: str) -> float | None:
    """Fetch current option price from Polygon."""
    key = poly_key()
    if not key or not expiry_raw:
        return None
    try:
        # Build Polygon option ticker format: O:FLNC260618C00023000
        parts = expiry_raw.split("/")
        if len(parts) == 3:
            m, d, y = parts
            y = "20"+y if len(y)==2 else y
            exp_str = f"{y}{m.zfill(2)}{d.zfill(2)}"
            cp      = "C" if "call" in opt_type.lower() else "P"
            # Strike in format 00023000 = $23.00 * 1000
            try:
                strike_int = int(float(strike) * 1000)
                opt_ticker = f"O:{ticker.upper()}{exp_str}{cp}{strike_int:08d}"
            except:
                return None

            r = requests.get(
                f"https://api.polygon.io/v2/last/trade/{opt_ticker}",
                params={"apiKey": key},
                timeout=8
            )
            if r.status_code == 200:
                price = r.json().get("results", {}).get("p")
                if price:
                    print(f"[EXIT] {opt_ticker}: ${price}")
                    return round(float(price), 2)
    except Exception as e:
        print(f"[EXIT] Option price error: {e}")
    return None

# ── Exit Signal Checker ────────────────────────────────────────────────

def check_exit_signals():
    """
    Called every 15 minutes during market hours.
    Checks all open positions for exit conditions.
    """
    now_et = datetime.now(ZoneInfo("America/New_York"))
    total  = now_et.hour * 60 + now_et.minute

    if total < 9*60+30 or total > 16*60:
        return

    positions = load_positions()
    open_pos  = [p for p in positions if p.get("status") == "OPEN"]

    if not open_pos:
        return

    print(f"[EXIT] Checking {len(open_pos)} open positions...")
    changed = False

    for pos in open_pos:
        ticker     = pos.get("ticker")
        stock_entry= pos.get("entry_stock", 0)
        stop       = pos.get("stop_price", 0)
        target     = pos.get("target_price", 0)
        opt_entry  = pos.get("entry_option")
        expiry_raw = pos.get("expiry_raw","")
        strike     = pos.get("strike","")
        opt_type   = pos.get("option_type","call")

        try:
            # Get current stock price
            r = requests.get(
                f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}",
                params={"apiKey": poly_key()},
                timeout=8
            )
            if r.status_code != 200:
                continue

            snap        = r.json().get("ticker", {})
            curr_price  = snap.get("day", {}).get("c") or snap.get("lastTrade", {}).get("p")
            if not curr_price:
                continue
            curr_price = round(float(curr_price), 2)
            time.sleep(13)

            # Get current option price
            curr_opt = get_option_price(ticker, strike, opt_type, expiry_raw)
            time.sleep(13)

            # Calculate moves
            stock_pct  = round(((curr_price - stock_entry) / stock_entry) * 100, 2)
            opt_pct    = None
            if curr_opt and opt_entry:
                opt_pct = round(((curr_opt - opt_entry) / opt_entry) * 100, 1)

            # Check DTE
            dte = None
            if expiry_raw:
                try:
                    m, d, y = expiry_raw.split("/")
                    y = "20"+y if len(y)==2 else y
                    from datetime import datetime as _dt
                    dte = (_dt(int(y),int(m),int(d)) - _dt.now()).days
                except:
                    pass

            opt_str   = f" | Option: {opt_pct:+.1f}%" if opt_pct is not None else ""
            otype     = opt_type[0].upper()
            expiry    = pos.get("expiry","?")

            # ── Exit conditions ────────────────────────────────────────

            # 1. Stop loss hit
            if curr_price <= stop and not pos.get("alerted_stop"):
                msg = (
                    f"🛑 STOP LOSS: <b>{ticker}</b>\n"
                    f"{ticker} {strike}{otype} {expiry}\n"
                    f"\n"
                    f"Stock: ${curr_price} hit stop ${stop}\n"
                    f"Move: {stock_pct:+.1f}%{opt_str}\n"
                    f"<b>→ Consider closing position to limit losses</b>"
                )
                send_sms(msg)
                pos["alerted_stop"] = True
                changed = True
                print(f"[EXIT] 🛑 Stop loss alert: {ticker} ${curr_price}")

            # 2. Target hit
            elif curr_price >= target and not pos.get("alerted_target"):
                msg = (
                    f"🎯 TARGET HIT: <b>{ticker}</b>\n"
                    f"{ticker} {strike}{otype} {expiry}\n"
                    f"\n"
                    f"Stock: ${curr_price} hit target ${target}\n"
                    f"Move: {stock_pct:+.1f}%{opt_str}\n"
                    f"<b>→ Consider taking profits or trailing stop</b>\n"
                    f"<b>→ Sell half, let rest run with stop at entry</b>"
                )
                send_sms(msg)
                pos["alerted_target"] = True
                changed = True
                print(f"[EXIT] 🎯 Target hit: {ticker} ${curr_price}")

            # 3. DTE warning
            elif dte is not None and dte <= 3 and not pos.get("alerted_dte"):
                msg = (
                    f"⏰ DTE WARNING: <b>{ticker}</b>\n"
                    f"{ticker} {strike}{otype} {expiry}\n"
                    f"\n"
                    f"Only {dte} day(s) left on option\n"
                    f"Stock: ${curr_price} ({stock_pct:+.1f}% from entry){opt_str}\n"
                    f"<b>→ Close or roll — theta decay accelerating</b>"
                )
                send_sms(msg)
                pos["alerted_dte"] = True
                changed = True
                print(f"[EXIT] ⏰ DTE warning: {ticker} {dte}d left")

            # 4. Large adverse move (>8% against)
            elif stock_pct <= -8 and not pos.get("alerted_stop"):
                msg = (
                    f"⚠️ LARGE ADVERSE MOVE: <b>{ticker}</b>\n"
                    f"{ticker} {strike}{otype} {expiry}\n"
                    f"\n"
                    f"Stock: ${curr_price} ({stock_pct:+.1f}% from entry){opt_str}\n"
                    f"<b>→ Reassess — consider cutting losses</b>"
                )
                send_sms(msg)
                pos["alerted_stop"] = True
                changed = True
                print(f"[EXIT] ⚠️ Large adverse move: {ticker} {stock_pct:+.1f}%")

            # Update current prices
            pos["current_stock"]  = curr_price
            pos["current_option"] = curr_opt
            pos["stock_pnl_pct"]  = stock_pct
            pos["option_pnl_pct"] = opt_pct

        except Exception as e:
            print(f"[EXIT] Error checking {ticker}: {e}")

    if changed:
        save_positions(positions)

def get_open_positions() -> list:
    return [p for p in load_positions() if p.get("status") == "OPEN"]

def close_position(ticker: str, exit_reason: str = "MANUAL"):
    """Mark a position as closed."""
    positions = load_positions()
    for p in positions:
        if p.get("ticker") == ticker and p.get("status") == "OPEN":
            p["status"]      = "CLOSED"
            p["exit_reason"] = exit_reason
            p["closed_at"]   = datetime.now(ZoneInfo("America/New_York")).isoformat()
    save_positions(positions)
    print(f"[EXIT] Closed position: {ticker}")
