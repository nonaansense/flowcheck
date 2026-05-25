import os
"""
Outcome tracking for FlowCheck.
Runs at 4:00 PM ET daily.
Tracks both stock price movement AND option P&L.
"""
import json, os, requests, time
from datetime import datetime
from zoneinfo import ZoneInfo
from sms import send_sms

OUTCOMES_FILE = "/tmp/flowcheck_outcomes.json"

def poly_key():
    return os.environ.get("POLYGON_API_KEY")

OUTCOMES_KEY = "outcomes"

def load_outcomes() -> dict:
    from storage import load_data
    return load_data(OUTCOMES_KEY, OUTCOMES_FILE, {"history": []})

def save_outcomes(data: dict):
    from storage import save_data
    save_data(OUTCOMES_KEY, OUTCOMES_FILE, data)

    try:
        with open(OUTCOMES_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[OUTCOMES] Save error: {e}")

def get_closing_stock_price(ticker: str) -> float | None:
    """Get closing stock price from Polygon."""
    key = poly_key()
    if not key:
        from fetcher import fetch_price
        return fetch_price(ticker)
    try:
        r = requests.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}",
            params={"apiKey": key},
            timeout=8
        )
        if r.status_code == 200:
            day = r.json().get("ticker", {}).get("day", {})
            c   = day.get("c")
            if c:
                return round(float(c), 2)
    except Exception as e:
        print(f"[OUTCOMES] Stock price error {ticker}: {e}")
    from fetcher import fetch_price
    return fetch_price(ticker)

def get_closing_option_price(ticker: str, strike: str,
                              opt_type: str, expiry_raw: str) -> float | None:
    """Get closing option price from Polygon."""
    key = poly_key()
    if not key or not expiry_raw or not strike:
        return None
    try:
        parts = expiry_raw.split("/")
        if len(parts) != 3:
            return None
        m, d, y = parts
        y = "20"+y if len(y)==2 else y
        exp_str    = f"{y}{m.zfill(2)}{d.zfill(2)}"
        cp         = "C" if "call" in opt_type.lower() else "P"
        strike_int = int(float(strike) * 1000)
        opt_ticker = f"O:{ticker.upper()}{exp_str}{cp}{strike_int:08d}"

        r = requests.get(
            f"https://api.polygon.io/v2/last/trade/{opt_ticker}",
            params={"apiKey": key},
            timeout=8
        )
        if r.status_code == 200:
            price = r.json().get("results", {}).get("p")
            if price:
                print(f"[OUTCOMES] {opt_ticker}: ${price}")
                return round(float(price), 2)
    except Exception as e:
        print(f"[OUTCOMES] Option price error: {e}")
    return None

def track_outcomes(analyses: list):
    """Called at 4:00 PM ET. Tracks stock + option P&L for all WATCH/TRADE alerts."""
    now_et    = datetime.now(ZoneInfo("America/New_York"))
    today_str = now_et.strftime("%Y-%m-%d")

    tracked = [
        a for a in analyses
        if a.get("date") == today_str
        and a.get("result", {}).get("verdict") in ("WATCH", "TRADE")
        and a.get("data", {}).get("stock_price")
    ]

    if not tracked:
        print("[OUTCOMES] No WATCH/TRADE alerts to track today")
        return

    print(f"[OUTCOMES] Tracking {len(tracked)} alerts...")
    outcomes      = load_outcomes()
    today_results = []

    for a in tracked:
        ticker      = a["trade"].get("ticker")
        entry_stock = a["data"].get("stock_price")
        entry_opt   = a["trade"].get("option_price") or a["data"].get("flow_fill_price")
        verdict     = a["result"].get("verdict")
        score       = a["result"].get("final_score", 0)
        alert_time  = a.get("time", "?")
        strike      = a["trade"].get("strike")
        opt_type    = a["trade"].get("option_type","call")
        expiry_raw  = a["trade"].get("expiry_raw","")

        if not ticker or not entry_stock:
            continue

        # Get closing stock price
        close_stock = get_closing_stock_price(ticker)
        time.sleep(3)

        if not close_stock:
            print(f"[OUTCOMES] Could not get closing price for {ticker}")
            continue

        stock_pct = round(((close_stock - entry_stock) / entry_stock) * 100, 2)

        # Win threshold — configurable via OPTION_WIN_PCT env var (default 50%)
        win_threshold = float(os.environ.get("OPTION_WIN_PCT", "50"))

        if close_opt and entry_opt and float(entry_opt) > 0:
            opt_gain = ((close_opt - float(entry_opt)) / float(entry_opt)) * 100
            is_win   = opt_gain >= win_threshold
        else:
            # Fallback: estimate stock move needed for option to hit threshold
            # Rough approximation: 0.4 delta option → stock needs to move threshold/5 * 2%
            stock_threshold = round(win_threshold / 25, 1)  # e.g. 50% → 2% stock move
            is_win = stock_pct >= stock_threshold

        # Get closing option price
        close_opt = None
        opt_pct   = None
        if entry_opt and strike and expiry_raw:
            close_opt = get_closing_option_price(ticker, strike, opt_type, expiry_raw)
            time.sleep(3)
            if close_opt and float(entry_opt) > 0:
                opt_pct = round(((close_opt - float(entry_opt)) / float(entry_opt)) * 100, 1)

        result = {
            "date":         today_str,
            "ticker":       ticker,
            "verdict":      verdict,
            "score":        score,
            "alert_time":   alert_time,
            "entry_stock":  entry_stock,
            "close_stock":  close_stock,
            "stock_pct":    stock_pct,
            "entry_option": entry_opt,
            "close_option": close_opt,
            "option_pct":   opt_pct,
            "is_win":       is_win,
        }

        outcomes["history"].append(result)
        today_results.append(result)

        opt_str = f" | Option: {opt_pct:+.1f}%" if opt_pct is not None else ""
        emoji   = "✅" if stock_pct > 2 else "📈" if is_win else "❌" if stock_pct < -2 else "📉"
        print(f"[OUTCOMES] {ticker} {verdict}: stock {stock_pct:+.1f}%{opt_str} {emoji}")

    save_outcomes(outcomes)

    # Build stats
    history = outcomes["history"]
    if len(history) < 1:
        return

    wins     = sum(1 for r in history if r["is_win"])
    win_rate = round(wins / len(history) * 100, 1)

    trade_hist = [r for r in history if r["verdict"] == "TRADE"]
    watch_hist = [r for r in history if r["verdict"] == "WATCH"]
    opt_hist   = [r for r in history if r.get("option_pct") is not None]

    trade_wr  = round(sum(1 for r in trade_hist if r["is_win"]) / len(trade_hist) * 100, 1) if trade_hist else 0
    watch_wr  = round(sum(1 for r in watch_hist if r["is_win"]) / len(watch_hist) * 100, 1) if watch_hist else 0
    avg_stock = round(sum(r["stock_pct"] for r in history) / len(history), 2)
    avg_opt   = round(sum(r["option_pct"] for r in opt_hist) / len(opt_hist), 1) if opt_hist else None

    if not today_results:
        return

    win_threshold = float(os.environ.get("OPTION_WIN_PCT", "50"))
    lines = [f"📊 FlowCheck Outcomes — {now_et.strftime('%a %b %d')} (win = option +{win_threshold:.0f}%+)", ""]
    lines.append("Today's results:")

    for r in today_results:
        stock_emoji = "✅" if r["stock_pct"] > 0 else "❌"
        opt_str     = f" | Option: {r['option_pct']:+.1f}%" if r.get("option_pct") is not None else ""
        lines.append(f"  {stock_emoji} {r['ticker']} {r['verdict']}: "
                     f"Stock {r['stock_pct']:+.1f}%{opt_str}")

    lines.append("")
    lines.append(f"📈 Running stats ({len(history)} alerts):")
    lines.append(f"  Overall win rate: {win_rate}%")
    lines.append(f"  TRADE win rate: {trade_wr}% ({len(trade_hist)} alerts)")
    lines.append(f"  WATCH win rate: {watch_wr}% ({len(watch_hist)} alerts)")
    lines.append(f"  Avg stock move: {avg_stock:+.2f}%")
    if avg_opt is not None:
        lines.append(f"  Avg option move: {avg_opt:+.1f}% ({len(opt_hist)} tracked)")

    send_sms("\n".join(lines))
    print(f"[OUTCOMES] Summary sent — {win_rate}% win rate")

def get_stats() -> dict:
    history = load_outcomes().get("history", [])
    if not history:
        return {"total": 0, "win_rate": 0, "avg_stock_move": 0, "avg_option_move": None}

    wins      = sum(1 for r in history if r["is_win"])
    opt_hist  = [r for r in history if r.get("option_pct") is not None]
    trade_h   = [r for r in history if r["verdict"] == "TRADE"]
    watch_h   = [r for r in history if r["verdict"] == "WATCH"]

    return {
        "total":           len(history),
        "win_rate":        round(wins/len(history)*100, 1),
        "avg_stock_move":  round(sum(r["stock_pct"] for r in history)/len(history), 2),
        "avg_option_move": round(sum(r["option_pct"] for r in opt_hist)/len(opt_hist), 1) if opt_hist else None,
        "options_tracked": len(opt_hist),
        "trade_count":     len(trade_h),
        "trade_wr":        round(sum(1 for r in trade_h if r["is_win"])/len(trade_h)*100,1) if trade_h else 0,
        "watch_count":     len(watch_h),
        "watch_wr":        round(sum(1 for r in watch_h if r["is_win"])/len(watch_h)*100,1) if watch_h else 0,
        "last_7":          history[-7:]
    }
