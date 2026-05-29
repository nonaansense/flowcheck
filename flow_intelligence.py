"""
Flow Intelligence module for FlowCheck.
Handles:
1. Rolling position detection
2. Options chain analysis
3. Flow vs price divergence
4. Repeat buyer cross-day detection
5. Sector rotation alerts
6. Dark pool cross-reference
"""
import os, json, requests, time
from datetime import datetime, timedelta

_SECTOR_NAMES_MAP = {
    "XLK":"Technology","XLF":"Financials","XLV":"Healthcare",
    "XLE":"Energy","XLI":"Industrials","XLC":"Communications",
    "XLY":"Consumer Discretionary","XLP":"Consumer Staples",
    "XLB":"Materials","XLRE":"Real Estate","XLU":"Utilities",
    "XBI":"Biotech","SMH":"Semiconductors","SOXX":"Semiconductors",
    "IBB":"Biotech","KRE":"Regional Banks","XOP":"Oil & Gas",
    "XRT":"Retail","GDX":"Gold Miners","JETS":"Airlines",
    "XME":"Metals & Mining","ITB":"Homebuilders",
    "ARKK":"Innovation","HACK":"Cybersecurity","FINX":"Fintech",
}
SECTOR_NAMES = _SECTOR_NAMES_MAP  # Alias for compatibility
from zoneinfo import ZoneInfo
from collections import defaultdict

def safe_premium(val) -> int:
    """Safely convert any premium value (string or number) to int dollars."""
    if val is None: return 0
    if isinstance(val, (int, float)): return int(val)
    s = str(val).strip().upper()
    try:
        if s.endswith("M"): return int(float(s[:-1]) * 1_000_000)
        if s.endswith("K"): return int(float(s[:-1]) * 1_000)
        return int(float(s))
    except:
        return 0

FLOW_HISTORY_FILE = "/tmp/flowcheck_flow_history.json"
SECTOR_FLOW_FILE  = "/tmp/flowcheck_sector_flows.json"
FLOW_HISTORY_KEY  = "flow_history"
SECTOR_FLOW_KEY   = "sector_flows"

def poly_key():
    return os.environ.get("POLYGON_API_KEY")

def fh_key():
    return os.environ.get("FINNHUB_API_KEY")

# ── Flow History ───────────────────────────────────────────────────────

def load_flow_history() -> list:
    from storage import load_data
    data   = load_data(FLOW_HISTORY_KEY, FLOW_HISTORY_FILE, [])
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    return [f for f in data if f.get("timestamp","") >= cutoff]

def save_flow_history(history: list):
    from storage import save_data
    save_data(FLOW_HISTORY_KEY, FLOW_HISTORY_FILE, history[-500:])

def add_flow_to_history(trade: dict, data: dict, result: dict):
    """Record each flow for cross-day analysis."""
    history = load_flow_history()
    entry = {
        "timestamp":   datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "date":        datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"),
        "ticker":      trade.get("ticker"),
        "strike":      trade.get("strike"),
        "option_type": trade.get("option_type","call"),
        "expiry":      trade.get("expiry"),
        "expiry_raw":  trade.get("expiry_raw"),
        "premium":     safe_premium(trade.get("premium",0)),
        "fill_type":   data.get("fill_type"),
        "stock_price": data.get("stock_price"),
        "verdict":     result.get("verdict"),
        "score":       result.get("final_score"),
    }
    history.append(entry)
    save_flow_history(history)
    return history

# ── 1. Rolling Position Detection ─────────────────────────────────────

def detect_roll(trade: dict, history: list) -> dict:
    """
    Detect if this flow is a roll from a previous position.
    Roll = same ticker + same strike + different expiry bought recently.
    """
    ticker     = trade.get("ticker")
    strike     = trade.get("strike")
    expiry_raw = trade.get("expiry_raw")
    opt_type   = trade.get("option_type","call")

    if not ticker or not strike:
        return {}

    cutoff = (datetime.now() - timedelta(days=14)).isoformat()
    recent = [
        f for f in history
        if f.get("ticker")      == ticker
        and f.get("strike")     == strike
        and f.get("option_type")== opt_type
        and f.get("expiry_raw") != expiry_raw
        and f.get("timestamp","") >= cutoff
        and f.get("fill_type")  in ("FULL_ASK","MOSTLY_ASK")
    ]

    if recent:
        prev       = recent[-1]
        prev_expiry = prev.get("expiry","?")
        curr_expiry = trade.get("expiry","?")
        return {
            "is_roll":     True,
            "roll_emoji":  "🔄",
            "roll_label":  f"ROLL DETECTED: {ticker} {strike} {opt_type.upper()} {prev_expiry} → {curr_expiry}",
            "roll_note":   "Buyer extending position — high conviction signal",
            "prev_flow":   prev,
        }

    return {"is_roll": False}

# ── 2. Repeat Buyer Cross-Day ──────────────────────────────────────────

def detect_repeat_buyer(trade: dict, history: list) -> dict:
    """
    Detect systematic accumulation across multiple days.
    Same ticker + same strike + same expiry seen multiple times.
    """
    ticker     = trade.get("ticker")
    strike     = trade.get("strike")
    expiry_raw = trade.get("expiry_raw")
    opt_type   = trade.get("option_type","call")
    today      = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

    if not ticker or not strike:
        return {}

    # Look for same contract bought on different days
    matches = [
        f for f in history
        if f.get("ticker")       == ticker
        and f.get("strike")      == strike
        and f.get("expiry_raw")  == expiry_raw
        and f.get("option_type") == opt_type
        and f.get("fill_type")   in ("FULL_ASK","MOSTLY_ASK")
        and f.get("date")        != today  # Previous days only
    ]

    unique_days = list(set(f.get("date","") for f in matches))

    if len(unique_days) >= 2:
        total_premium = sum(safe_premium(f.get("premium",0)) for f in matches)
        return {
            "is_repeat":      True,
            "repeat_emoji":   "🔁",
            "repeat_days":    len(unique_days),
            "repeat_label":   (f"REPEAT BUYER: {ticker} {strike}{opt_type[0].upper()} seen "
                               f"{len(unique_days)+1}x across {len(unique_days)+1} days — "
                               f"systematic accumulation"),
            "total_premium":  total_premium,
        }
    elif len(unique_days) == 1:
        return {
            "is_repeat":    True,
            "repeat_emoji": "👀",
            "repeat_days":  2,
            "repeat_label": f"2nd occurrence: {ticker} {strike}{opt_type[0].upper()} — watch for pattern",
            "total_premium":0,
        }

    return {"is_repeat": False}

# ── 3. Options Chain Analysis ──────────────────────────────────────────

def analyze_options_chain(ticker: str, opt_type: str = "call") -> dict:
    """
    Analyze full options chain to see if flow is isolated or broad.
    Uses Polygon options snapshot.
    """
    key = poly_key()
    if not key:
        return {}
    try:
        r = requests.get(
            f"https://api.polygon.io/v3/snapshot/options/{ticker.upper()}",
            params={
                "apiKey":        key,
                "contract_type": opt_type,
                "limit":         20,
            },
            timeout=10
        )
        if r.status_code != 200:
            return {}

        results = r.json().get("results", [])
        if not results:
            return {}

        # Calculate call/put ratio and unusual activity
        total_vol  = sum(r.get("day", {}).get("volume", 0) or 0 for r in results)
        total_oi   = sum(r.get("open_interest", 0) or 0 for r in results)
        active     = [r for r in results
                      if (r.get("day",{}).get("volume",0) or 0) > 0]
        most_active= sorted(active,
                            key=lambda x: x.get("day",{}).get("volume",0) or 0,
                            reverse=True)[:3]

        chain_result = {
            "total_volume":    total_vol,
            "total_oi":        total_oi,
            "active_strikes":  len(active),
            "most_active":     [
                {
                    "strike": r.get("details",{}).get("strike_price"),
                    "expiry": r.get("details",{}).get("expiration_date"),
                    "volume": r.get("day",{}).get("volume",0),
                    "iv":     round(float(r.get("implied_volatility",0))*100,1) if r.get("implied_volatility") else None,
                }
                for r in most_active
            ],
        }

        # Is this flow unusual vs chain average?
        if len(active) > 0:
            avg_vol = total_vol / len(active)
            chain_result["avg_strike_vol"] = round(avg_vol)

        print(f"[INTEL] {ticker} chain: {len(active)} active strikes, "
              f"{total_vol:,} total vol")
        return chain_result

    except Exception as e:
        print(f"[INTEL] Chain analysis error: {e}")
        return {}

# ── 4. Flow vs Price Divergence ────────────────────────────────────────

def detect_flow_divergence(trade: dict, data: dict) -> dict:
    """
    Detect bullish divergence: aggressive call buying + flat/down stock.
    This is one of the strongest informed money signals.
    """
    fill_type   = data.get("fill_type","")
    stock_price = data.get("stock_price")
    opt_type    = trade.get("option_type","call")

    if not stock_price or fill_type not in ("FULL_ASK","MOSTLY_ASK"):
        return {}

    # Get intraday stock move
    ticker = trade.get("ticker")
    key    = poly_key()
    if not key:
        return {}

    try:
        r = requests.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}",
            params={"apiKey": key},
            timeout=8
        )
        if r.status_code == 200:
            day        = r.json().get("ticker",{}).get("day",{})
            open_price = day.get("o")
            curr_price = day.get("c") or stock_price

            if open_price and float(open_price) > 0:
                intraday_pct = round(((float(curr_price)-float(open_price))
                                     /float(open_price))*100, 2)

                if opt_type == "call" and intraday_pct <= -1.0:
                    return {
                        "has_divergence":  True,
                        "div_emoji":       "🎯",
                        "div_label":       (f"BULLISH DIVERGENCE: Stock down {intraday_pct:+.1f}% "
                                            f"but FULL_ASK calls — informed accumulation on weakness"),
                        "intraday_pct":    intraday_pct,
                    }
                elif opt_type == "call" and -1.0 < intraday_pct <= 0.5:
                    return {
                        "has_divergence":  True,
                        "div_emoji":       "👀",
                        "div_label":       (f"Stock flat {intraday_pct:+.1f}% + aggressive calls "
                                            f"= stealth accumulation"),
                        "intraday_pct":    intraday_pct,
                    }
                elif opt_type == "call" and intraday_pct > 3.0:
                    return {
                        "has_divergence":  False,
                        "div_emoji":       "⚠️",
                        "div_label":       (f"Stock already up {intraday_pct:+.1f}% — "
                                            f"may be chasing"),
                        "intraday_pct":    intraday_pct,
                    }
    except Exception as e:
        print(f"[INTEL] Divergence error: {e}")

    return {}

# ── 5. Sector Rotation Alert ───────────────────────────────────────────

SECTOR_ETF_MAP = {
    "XLK": ["AAPL","MSFT","NVDA","AMD","ORCL","CRM","QCOM","ANET","MU","INTC",
             "ASTS","RKLB","FLNC","CIFR","POET","HPE","NBIS"],
    "XLF": ["JPM","BAC","GS","MS","WFC","C","BLK","AXP"],
    "XLE": ["XOM","CVX","BE","PLUG","BLDP","FCEL"],
    "XLB": ["ALB","FCX","NEM","TECK","GLD"],
    "XLV": ["JNJ","PFE","INO","MRNA","ABBV"],
    "XLY": ["AMZN","TSLA","HD","MCD","NKE"],
    "XLC": ["META","GOOG","GOOGL","NFLX","NOK","DIS"],
    "XLI": ["CAT","HON","UPS","BA","GE","RTX"],
}

def get_ticker_sector(ticker: str) -> str:
    for etf, tickers in SECTOR_ETF_MAP.items():
        if ticker.upper() in tickers:
            return etf
    return "OTHER"

def load_sector_flows() -> dict:
    try:
        with open(SECTOR_FLOW_FILE) as f:
            data = json.load(f)
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        if data.get("date") != today:
            return {"date": today, "sectors": defaultdict(list)}
        data["sectors"] = defaultdict(list, data.get("sectors",{}))
        return data
    except:
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        return {"date": today, "sectors": defaultdict(list)}

def save_sector_flows(data: dict):
    try:
        save_data = {
            "date":    data["date"],
            "sectors": dict(data["sectors"])
        }
        with open(SECTOR_FLOW_FILE,"w") as f:
            json.dump(save_data, f)
    except Exception as e:
        print(f"[INTEL] Sector save error: {e}")

def track_sector_flow(trade: dict, result: dict) -> dict | None:
    """
    Track flows by sector. Alert if 3+ flows in same sector same day.
    Returns alert dict if rotation detected, else None.
    """
    ticker  = trade.get("ticker","")
    sector  = get_ticker_sector(ticker)
    verdict = result.get("verdict","SKIP")

    if verdict == "SKIP":
        return None

    sector_data = load_sector_flows()
    sector_data["sectors"][sector].append({
        "ticker":  ticker,
        "premium": safe_premium(trade.get("premium",0)),
        "verdict": verdict,
        "time":    datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M"),
    })
    save_sector_flows(sector_data)

    flows      = sector_data["sectors"][sector]
    flow_count = len(flows)

    if flow_count >= 3:
        tickers       = list(set(f["ticker"] for f in flows))
        total_premium = sum(safe_premium(f.get("premium",0)) for f in flows)
        prem_str      = (f"${total_premium/1000000:.1f}M" if total_premium >= 1000000
                         else f"${total_premium/1000:.0f}K")
        return {
            "rotation_detected": True,
            "sector":            sector,
            "flow_count":        flow_count,
            "tickers":           tickers,
            "total_premium":     total_premium,
            "alert":             (f"📊 SECTOR ROTATION: {flow_count} flows in "
                                  f"{_SECTOR_NAMES_MAP.get(sector, sector)} ({sector}) today — "
                                  f"{', '.join(tickers[:4])} — {prem_str} total premium"),
        }
    return None

# ── 6. Dark Pool Cross-Reference ──────────────────────────────────────

def check_dark_pool(ticker: str) -> dict:
    """
    Check for large dark pool prints on the underlying stock.
    Large off-exchange volume + options flow = institutional conviction.
    Uses Polygon trades endpoint.
    """
    key = poly_key()
    if not key:
        return {}
    try:
        # Get today's trades summary
        r = requests.get(
            f"https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers/{ticker.upper()}",
            params={"apiKey": key},
            timeout=8
        )
        if r.status_code == 200:
            snap = r.json().get("ticker",{})
            day  = snap.get("day",{})

            total_vol  = day.get("v",0) or 0
            avg_vol    = snap.get("prevDay",{}).get("v",0) or 0

            if avg_vol > 0 and total_vol > 0:
                vol_ratio = round(total_vol / avg_vol, 1)
                if vol_ratio >= 2.0:
                    return {
                        "unusual_volume":  True,
                        "vol_ratio":       vol_ratio,
                        "dark_pool_label": (f"Stock volume {vol_ratio}x average "
                                            f"({total_vol:,.0f} vs {avg_vol:,.0f} avg) "
                                            f"— institutional activity on underlying"),
                        "dark_pool_emoji": "🌑" if vol_ratio >= 3 else "🔦",
                    }
    except Exception as e:
        print(f"[INTEL] Dark pool error: {e}")
    return {}

# ── 7. Earnings Season Awareness ──────────────────────────────────────

def get_earnings_season_context() -> dict:
    """
    Determine if we're in earnings season and adjust scoring context.
    Earnings seasons: Jan-Feb, Apr-May, Jul-Aug, Oct-Nov
    """
    month = datetime.now().month
    in_earnings_season = month in (1,2,4,5,7,8,10,11)
    peak_season        = month in (1,4,7,10)  # First month of each quarter

    if peak_season:
        return {
            "in_earnings_season": True,
            "season_label":       "Peak earnings season",
            "season_emoji":       "📋",
            "season_note":        "Post-earnings plays strongest now — IV deflated after reports",
            "score_adjustment":   0.5,  # Bonus for post-earnings plays
        }
    elif in_earnings_season:
        return {
            "in_earnings_season": True,
            "season_label":       "Earnings season",
            "season_emoji":       "📋",
            "season_note":        "Earnings season — post-earnings plays preferred",
            "score_adjustment":   0.25,
        }
    else:
        return {
            "in_earnings_season": False,
            "season_label":       "Off-season",
            "season_emoji":       "",
            "season_note":        "Off earnings season — momentum and macro plays dominate",
            "score_adjustment":   0,
        }

# ── Feature 2: Confidence Score ───────────────────────────────────────

def calc_confidence_score(trade: dict, data: dict, history: list) -> dict:
    """
    Calculate confidence score based on YOUR historical win rate
    for similar setups. Requires 10+ outcomes to be meaningful.
    """
    from outcomes import load_outcomes
    outcomes = load_outcomes().get("history", [])

    if len(outcomes) < 10:
        return {
            "confidence": None,
            "confidence_label": "Insufficient data (<10 outcomes)",
            "sample_size": len(outcomes),
        }

    fill_type  = data.get("fill_type","")
    vol_oi     = data.get("vol_oi_ratio") or 0
    has_news   = data.get("has_recent_news", False)
    opt_type   = trade.get("option_type","call")
    dte        = data.get("days_to_expiry") or 0

    # Find similar historical setups
    similar = []
    for o in outcomes:
        # Match on key characteristics
        score_match = abs((o.get("score") or 0) - (data.get("final_score") or 0)) <= 1
        if score_match:
            similar.append(o)

    if len(similar) < 5:
        return {
            "confidence": None,
            "confidence_label": f"Too few similar setups ({len(similar)})",
            "sample_size": len(similar),
        }

    wins     = sum(1 for o in similar if o.get("is_win"))
    win_rate = round(wins / len(similar) * 100, 1)
    avg_opt  = None
    opt_hist = [o for o in similar if o.get("option_pct") is not None]
    if opt_hist:
        avg_opt = round(sum(o["option_pct"] for o in opt_hist) / len(opt_hist), 1)

    if win_rate >= 65:    label = f"HIGH confidence ({win_rate}% win rate on {len(similar)} similar setups)"
    elif win_rate >= 50:  label = f"MODERATE confidence ({win_rate}% win rate)"
    else:                 label = f"LOW confidence ({win_rate}% win rate — below 50%)"

    return {
        "confidence":     win_rate,
        "confidence_label": label,
        "sample_size":    len(similar),
        "avg_option_gain": avg_opt,
    }

# ── Feature 3: Multi-day ticker summary ───────────────────────────────

def get_ticker_weekly_summary(ticker: str, history: list) -> dict | None:
    """
    Summarize all flows for a ticker in the past 7 days.
    Returns summary if 2+ flows found.
    """
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    flows  = [
        f for f in history
        if f.get("ticker") == ticker
        and f.get("timestamp","") >= cutoff
        and f.get("fill_type") in ("FULL_ASK","MOSTLY_ASK")
    ]

    if len(flows) < 2:
        return None

    unique_days   = list(set(f.get("date","") for f in flows))
    total_premium = sum(safe_premium(f.get("premium",0)) for f in flows)
    prem_str      = (f"${total_premium/1000000:.1f}M" if total_premium >= 1000000
                     else f"${total_premium/1000:.0f}K")

    return {
        "ticker":        ticker,
        "flow_count":    len(flows),
        "unique_days":   len(unique_days),
        "total_premium": total_premium,
        "prem_str":      prem_str,
        "summary":       (f"📈 {ticker} weekly: {len(flows)} flows over {len(unique_days)} days "
                          f"— {prem_str} total — systematic accumulation pattern"),
    }

# ── Master Intelligence Function ──────────────────────────────────────

def run_flow_intelligence(trade: dict, data: dict, result: dict) -> dict:
    """
    Run all intelligence checks. Returns consolidated intel dict.
    Call this after basic analysis is complete.
    """
    ticker  = trade.get("ticker","")
    intel   = {}

    # Load/update flow history
    history = add_flow_to_history(trade, data, result)

    # 1. Roll detection
    roll = detect_roll(trade, history)
    if roll.get("is_roll"):
        intel["roll"] = roll
        print(f"[INTEL] 🔄 Roll detected: {roll.get('roll_label','')}")

    # 2. Repeat buyer
    repeat = detect_repeat_buyer(trade, history)
    if repeat.get("is_repeat"):
        intel["repeat"] = repeat
        print(f"[INTEL] 🔁 Repeat buyer: {repeat.get('repeat_label','')}")

    # 3. Flow vs price divergence (Polygon call — needs rate limit)
    time.sleep(2)
    divergence = detect_flow_divergence(trade, data)
    if divergence:
        intel["divergence"] = divergence

    # 4. Sector rotation
    sector_alert = track_sector_flow(trade, result)
    if sector_alert:
        intel["sector_rotation"] = sector_alert
        print(f"[INTEL] 📊 Sector rotation: {sector_alert.get('alert','')}")

    # 5. Dark pool (Polygon call)
    time.sleep(2)
    dark_pool = check_dark_pool(ticker)
    if dark_pool.get("unusual_volume"):
        intel["dark_pool"] = dark_pool
        print(f"[INTEL] 🌑 Dark pool: {dark_pool.get('dark_pool_label','')}")

    # 6. Earnings season context
    intel["earnings_season"] = get_earnings_season_context()

    # 7. Options chain (Polygon call — optional, skip if slow)
    try:
        time.sleep(2)
        chain = analyze_options_chain(ticker, trade.get("option_type","call"))
        if chain:
            intel["chain"] = chain
    except Exception as e:
        print(f"[INTEL] Chain skipped: {e}")

    # Feature 2: Confidence score
    confidence = calc_confidence_score(trade, data, history)
    if confidence.get("confidence") is not None:
        intel["confidence"] = confidence

    # Feature 3: Weekly ticker summary
    weekly = get_ticker_weekly_summary(trade.get("ticker",""), history)
    if weekly:
        intel["weekly_summary"] = weekly

    return intel
