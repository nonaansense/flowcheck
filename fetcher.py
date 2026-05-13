import yfinance as yf
from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

# Sector ETF map
SECTOR_ETF_MAP = {
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK",
    "CSCO": "XLK", "ORCL": "XLK", "CRM": "XLK", "QCOM": "XLK",
    "ANET": "XLK", "CRWV": "XLK", "MU": "XLK", "SNOW": "XLK",
    "JPM": "XLF", "BAC": "XLF", "GS": "XLF", "MS": "XLF",
    "XOM": "XLE", "CVX": "XLE", "BE": "XLE",
    "ALB": "XLB", "FCX": "XLB", "NEM": "XLB",
    "JNJ": "XLV", "PFE": "XLV", "INO": "XLV",
    "AMZN": "XLY", "TSLA": "XLY", "META": "XLC",
    "ASTS": "XLK", "RKLB": "XLI", "SPCE": "XLI",
    "NOK": "XLC", "BAND": "XLC", "GLD": "XLB",
}


# ─────────────────────────────────────────
# TIME OF DAY CHECK
# ─────────────────────────────────────────
def check_time_of_day():
    """Flag noisy open/close windows (first/last 30 min of market)."""
    now_et = datetime.now(ZoneInfo("America/New_York"))
    hour, minute = now_et.hour, now_et.minute
    total_min = hour * 60 + minute

    market_open  = 9 * 60 + 30   # 9:30 AM
    noisy_open   = 10 * 60        # 10:00 AM
    noisy_close  = 15 * 60 + 30  # 3:30 PM
    market_close = 16 * 60        # 4:00 PM

    if total_min < market_open or total_min > market_close:
        return {"window": "AFTER_HOURS", "emoji": "🌙",
                "label": "After hours", "quality": "LOW",
                "note": "After-hours flow — lower reliability."}
    elif total_min < noisy_open:
        return {"window": "NOISY_OPEN", "emoji": "⚠️",
                "label": "Opening 30 min", "quality": "LOW",
                "note": "First 30 min is noisy — market makers adjusting, not informed flow."}
    elif total_min > noisy_close:
        return {"window": "NOISY_CLOSE", "emoji": "⚠️",
                "label": "Closing 30 min", "quality": "LOW",
                "note": "Last 30 min is noisy — position squaring, not directional bets."}
    else:
        return {"window": "PRIME", "emoji": "✅",
                "label": "Prime hours", "quality": "HIGH",
                "note": "10:00 AM–3:30 PM prime window — highest quality flow."}


# ─────────────────────────────────────────
# MARKET CONDITIONS
# ─────────────────────────────────────────
def fetch_market_conditions():
    conditions = {
        "vix": None, "vix_label": None, "vix_emoji": None,
        "spy_5d_pct": None, "spy_trend": None, "spy_emoji": None,
        "market_bias": None, "market_score_adjustment": 0,
        "market_summary": None,
    }
    try:
        vix_hist = yf.Ticker("^VIX").history(period="1d", interval="1m")
        if not vix_hist.empty:
            v = round(vix_hist["Close"].iloc[-1], 1)
            conditions["vix"] = v
            if v < 18:
                conditions["vix_label"] = "Calm"
                conditions["vix_emoji"] = "✅"
            elif v < 25:
                conditions["vix_label"] = "Elevated"
                conditions["vix_emoji"] = "⚠️"
                conditions["market_score_adjustment"] -= 0.5
            elif v < 35:
                conditions["vix_label"] = "High"
                conditions["vix_emoji"] = "🔴"
                conditions["market_score_adjustment"] -= 1
            else:
                conditions["vix_label"] = "Extreme"
                conditions["vix_emoji"] = "🚨"
                conditions["market_score_adjustment"] -= 2
    except Exception as e:
        print(f"[MARKET] VIX error: {e}")

    try:
        spy_hist = yf.Ticker("SPY").history(period="7d", interval="1d")
        if len(spy_hist) >= 5:
            pct = round(((spy_hist["Close"].iloc[-1] - spy_hist["Close"].iloc[-5])
                         / spy_hist["Close"].iloc[-5]) * 100, 1)
            conditions["spy_5d_pct"] = pct
            if pct > 2:
                conditions["spy_trend"] = f"Uptrend +{pct}%"
                conditions["spy_emoji"] = "✅"
            elif pct > -2:
                conditions["spy_trend"] = f"Flat {pct:+.1f}%"
                conditions["spy_emoji"] = "⚠️"
            else:
                conditions["spy_trend"] = f"Downtrend {pct:+.1f}%"
                conditions["spy_emoji"] = "🔴"
                conditions["market_score_adjustment"] -= 1
    except Exception as e:
        print(f"[MARKET] SPY error: {e}")

    adj = conditions["market_score_adjustment"]
    if adj >= 0:
        conditions["market_bias"] = "FAVORABLE"
        conditions["market_summary"] = "Market conditions favor buying premium."
    elif adj >= -1:
        conditions["market_bias"] = "CAUTION"
        conditions["market_summary"] = "Elevated volatility — be selective, size smaller."
    elif adj >= -2:
        conditions["market_bias"] = "UNFAVORABLE"
        conditions["market_summary"] = "High VIX or downtrend — avoid buying premium."
    else:
        conditions["market_bias"] = "AVOID"
        conditions["market_summary"] = "Extreme conditions — do not buy premium today."

    return conditions


def fetch_sector_conditions(ticker):
    etf_symbol = SECTOR_ETF_MAP.get(ticker.upper(), "SPY")
    sector = {"etf": etf_symbol, "etf_5d_pct": None,
              "sector_trend": None, "sector_emoji": None}
    try:
        hist = yf.Ticker(etf_symbol).history(period="7d", interval="1d")
        if len(hist) >= 5:
            pct = round(((hist["Close"].iloc[-1] - hist["Close"].iloc[-5])
                         / hist["Close"].iloc[-5]) * 100, 1)
            sector["etf_5d_pct"] = pct
            if pct > 1:
                sector["sector_trend"] = f"Bullish +{pct}%"
                sector["sector_emoji"] = "✅"
            elif pct > -1:
                sector["sector_trend"] = f"Neutral {pct:+.1f}%"
                sector["sector_emoji"] = "⚠️"
            else:
                sector["sector_trend"] = f"Bearish {pct:+.1f}%"
                sector["sector_emoji"] = "🔴"
    except Exception as e:
        print(f"[MARKET] Sector error: {e}")
    return sector


# ─────────────────────────────────────────
# EARNINGS SURPRISE HISTORY
# ─────────────────────────────────────────
def fetch_earnings_surprise_history(stock):
    """Pull last 8 quarters of EPS surprise %."""
    surprises = []
    try:
        ed = stock.earnings_dates
        if ed is None or ed.empty:
            return surprises
        for _, row in ed.iterrows():
            surprise = row.get("Surprise(%)")
            if surprise is not None and not (isinstance(surprise, float) and surprise != surprise):
                surprises.append(round(float(surprise), 1))
            if len(surprises) >= 8:
                break
    except Exception as e:
        print(f"[FETCHER] Earnings surprise error: {e}")
    return surprises


# ─────────────────────────────────────────
# MAIN TRADE DATA FETCHER
# ─────────────────────────────────────────
def fetch_trade_data(trade, flow_premium=None):
    """Fetch all live data for a trade including market conditions."""
    ticker    = trade.get("ticker")
    strike    = trade.get("strike")
    option_type = trade.get("option_type", "call")
    expiry_raw  = trade.get("expiry_raw")

    data = {
        "ticker": ticker,
        "stock_price": None, "bid": None, "ask": None,
        "open_interest": None, "spread_pct": None, "otm_pct": None,
        "earnings_date": None, "earnings_date_raw": None,
        "days_to_expiry": None, "days_earnings_to_expiry": None,
        "expiry_timing_label": None, "expiry_timing_emoji": None,
        "historical_moves": [], "avg_earnings_move": None,
        "implied_volatility": None,
        "implied_move_pct": None,
        "implied_vs_historical": None,
        "implied_vs_historical_emoji": None,
        "earnings_surprises": [], "avg_earnings_surprise": None,
        "beats_pct": None,
        "flow_fill_price": flow_premium,
        "current_ask": None,
        "price_move_since_flow": None,
        "chasing_flag": None,
        "chasing_emoji": None,
        "adv_options_volume": None,
        "premium_vs_adv": None,
        "time_of_day": check_time_of_day(),
        "market": {}, "sector": {},
    }

    # Always fetch market conditions
    data["market"] = fetch_market_conditions()
    data["sector"] = fetch_sector_conditions(ticker or "SPY")

    try:
        stock = yf.Ticker(ticker)

        # ── Stock price ──
        hist = stock.history(period="1d", interval="1m")
        if not hist.empty:
            data["stock_price"] = round(hist["Close"].iloc[-1], 2)
        else:
            data["stock_price"] = round(stock.info.get("regularMarketPrice", 0), 2)
        stock_price = data["stock_price"]

        # ── Earnings date ──
        try:
            calendar = stock.calendar
            if calendar is not None and not calendar.empty:
                earnings = calendar.get("Earnings Date")
                if earnings is not None and len(earnings) > 0:
                    ed = earnings[0]
                    if hasattr(ed, 'strftime'):
                        data["earnings_date"] = ed.strftime("%b %d, %Y")
                        data["earnings_date_raw"] = ed
                    else:
                        data["earnings_date"] = str(ed)
        except Exception as e:
            print(f"[FETCHER] Earnings date error: {e}")

        # ── Options chain ──
        current_ask = None
        if expiry_raw and strike and stock_price:
            try:
                parts = expiry_raw.split("/")
                if len(parts) == 3:
                    m, d, y = parts
                    y = "20" + y if len(y) == 2 else y
                    expiry_yf = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
                    available  = stock.options
                    closest_expiry = find_closest_expiry(available, expiry_yf)
                    if closest_expiry:
                        chain   = stock.option_chain(closest_expiry)
                        options = chain.calls if option_type == "call" else chain.puts
                        options = options.copy()
                        strike_float = float(strike)
                        options["strike_diff"] = abs(options["strike"] - strike_float)
                        closest = options.nsmallest(1, "strike_diff").iloc[0]

                        data["bid"]              = round(float(closest.get("bid", 0)), 2)
                        data["ask"]              = round(float(closest.get("ask", 0)), 2)
                        data["open_interest"]    = int(closest.get("openInterest", 0))
                        data["implied_volatility"] = round(float(closest.get("impliedVolatility", 0)) * 100, 1)
                        current_ask = data["ask"]
                        data["current_ask"] = current_ask

                        if data["ask"] and data["ask"] > 0:
                            data["spread_pct"] = round(((data["ask"] - data["bid"]) / data["ask"]) * 100, 1)

                        if option_type == "call":
                            data["otm_pct"] = round(((strike_float - stock_price) / stock_price) * 100, 1)
                        else:
                            data["otm_pct"] = round(((stock_price - strike_float) / stock_price) * 100, 1)

                        # ── Implied move from ATM straddle ──
                        try:
                            atm_calls = chain.calls.copy()
                            atm_puts  = chain.puts.copy()
                            atm_calls["diff"] = abs(atm_calls["strike"] - stock_price)
                            atm_puts["diff"]  = abs(atm_puts["strike"]  - stock_price)
                            atm_call = atm_calls.nsmallest(1, "diff").iloc[0]
                            atm_put  = atm_puts.nsmallest(1, "diff").iloc[0]
                            straddle = float(atm_call.get("lastPrice", 0)) + float(atm_put.get("lastPrice", 0))
                            if stock_price and straddle > 0:
                                data["implied_move_pct"] = round((straddle / stock_price) * 100, 1)
                        except Exception as e:
                            print(f"[FETCHER] Implied move error: {e}")

            except Exception as e:
                print(f"[FETCHER] Options chain error: {e}")

        # ── Chasing detection ──
        if flow_premium and current_ask and flow_premium > 0:
            move_pct = round(((current_ask - flow_premium) / flow_premium) * 100, 1)
            data["price_move_since_flow"] = move_pct
            if move_pct > 75:
                data["chasing_flag"] = "HIGH"
                data["chasing_emoji"] = "🚨"
            elif move_pct > 40:
                data["chasing_flag"] = "MODERATE"
                data["chasing_emoji"] = "⚠️"
            elif move_pct > 0:
                data["chasing_flag"] = "LOW"
                data["chasing_emoji"] = "✅"
            else:
                data["chasing_flag"] = "NONE"
                data["chasing_emoji"] = "✅"

        # ── Days to expiry ──
        if expiry_raw:
            try:
                parts = expiry_raw.split("/")
                m, d, y = parts
                y = "20" + y if len(y) == 2 else y
                exp_date = datetime(int(y), int(m), int(d))
                data["days_to_expiry"] = (exp_date - datetime.now()).days
            except Exception as e:
                print(f"[FETCHER] Days to expiry error: {e}")

        # ── Days earnings to expiry + timing label ──
        if data.get("earnings_date_raw") and data.get("days_to_expiry") is not None:
            try:
                ed = data["earnings_date_raw"]
                if hasattr(ed, 'date'):
                    ed = ed.date()
                exp_parts = expiry_raw.split("/")
                m, d, y = exp_parts
                y = "20" + y if len(y) == 2 else y
                exp_date = datetime(int(y), int(m), int(d)).date()
                today = datetime.now().date()

                days_to_earnings = (ed - today).days
                days_earn_to_exp = (exp_date - ed).days
                data["days_earnings_to_expiry"] = days_earn_to_exp

                if days_earn_to_exp < 0:
                    data["expiry_timing_label"] = "Expiry BEFORE earnings"
                    data["expiry_timing_emoji"] = "❌"
                elif days_earn_to_exp == 0:
                    data["expiry_timing_label"] = "Expiry SAME DAY as earnings"
                    data["expiry_timing_emoji"] = "❌"
                elif days_earn_to_exp <= 4:
                    data["expiry_timing_label"] = f"Expiry {days_earn_to_exp}d after earnings — very tight"
                    data["expiry_timing_emoji"] = "⚠️"
                elif days_earn_to_exp <= 14:
                    data["expiry_timing_label"] = f"Expiry {days_earn_to_exp}d after earnings — sweet spot"
                    data["expiry_timing_emoji"] = "✅"
                else:
                    data["expiry_timing_label"] = f"Expiry {days_earn_to_exp}d after earnings — too much time"
                    data["expiry_timing_emoji"] = "⚠️"
            except Exception as e:
                print(f"[FETCHER] Earnings timing error: {e}")

        # ── Historical earnings moves ──
        try:
            hist_quarterly = stock.history(period="2y", interval="1d")
            moves = estimate_earnings_moves(stock, hist_quarterly)
            if moves:
                data["historical_moves"]   = moves
                data["avg_earnings_move"]  = round(sum(abs(m) for m in moves) / len(moves), 1)
        except Exception as e:
            print(f"[FETCHER] Historical moves error: {e}")

        # ── Implied vs historical move comparison ──
        if data.get("implied_move_pct") and data.get("avg_earnings_move"):
            implied = data["implied_move_pct"]
            actual  = data["avg_earnings_move"]
            ratio   = implied / actual if actual > 0 else 1
            if ratio < 0.85:
                data["implied_vs_historical"] = f"Options CHEAP — implied {implied}% vs avg actual {actual}%"
                data["implied_vs_historical_emoji"] = "✅"
            elif ratio < 1.15:
                data["implied_vs_historical"] = f"Options FAIR — implied {implied}% vs avg actual {actual}%"
                data["implied_vs_historical_emoji"] = "⚠️"
            else:
                data["implied_vs_historical"] = f"Options EXPENSIVE — implied {implied}% vs avg actual {actual}%"
                data["implied_vs_historical_emoji"] = "❌"

        # ── Earnings surprise history ──
        surprises = fetch_earnings_surprise_history(stock)
        if surprises:
            data["earnings_surprises"] = surprises
            data["avg_earnings_surprise"] = round(sum(surprises) / len(surprises), 1)
            beats = sum(1 for s in surprises if s > 0)
            data["beats_pct"] = round((beats / len(surprises)) * 100)

        # ── Premium vs ADV (options volume) ──
        try:
            info = stock.info
            adv = info.get("averageDailyVolume10Day") or info.get("averageVolume")
            if adv and trade.get("premium"):
                # Rough proxy: premium as % of typical daily options dollar volume
                est_options_adv = adv * stock_price * 0.01  # ~1% of stock ADV
                ratio = trade["premium"] / est_options_adv if est_options_adv > 0 else 0
                data["premium_vs_adv"] = round(ratio * 100, 1)
        except Exception as e:
            print(f"[FETCHER] ADV error: {e}")

    except Exception as e:
        print(f"[FETCHER] General error for {ticker}: {e}")

    return data


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def find_closest_expiry(available_dates, target_date):
    if not available_dates:
        return None
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d")
        return min(available_dates,
                   key=lambda d: abs((datetime.strptime(d, "%Y-%m-%d") - target).days))
    except:
        return available_dates[0] if available_dates else None


def estimate_earnings_moves(stock, hist):
    moves = []
    try:
        earnings_hist = stock.earnings_dates
        if earnings_hist is None or earnings_hist.empty:
            return moves
        for date, _ in earnings_hist.iterrows():
            try:
                date_str = date.strftime("%Y-%m-%d")
                before = hist[hist.index.strftime("%Y-%m-%d") < date_str]
                after  = hist[hist.index.strftime("%Y-%m-%d") >= date_str]
                if before.empty or after.empty:
                    continue
                move_pct = ((after.iloc[0]["Close"] - before.iloc[-1]["Close"])
                            / before.iloc[-1]["Close"]) * 100
                moves.append(round(move_pct, 1))
                if len(moves) >= 8:
                    break
            except:
                continue
    except Exception as e:
        print(f"[FETCHER] Earnings moves error: {e}")
    return moves
