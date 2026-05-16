import yfinance as yf
from datetime import datetime, timedelta
import re

def fetch_trade_data(trade):
    """
    Given parsed trade info, fetch all live market data needed for scoring.
    Returns dict with stock price, options chain data, earnings date, etc.
    """
    ticker = trade.get("ticker")
    strike = trade.get("strike")
    option_type = trade.get("option_type", "call")
    expiry_raw = trade.get("expiry_raw")  # MM/DD/YY format

    data = {
        "ticker": ticker,
        "stock_price": None,
        "bid": None,
        "ask": None,
        "open_interest": None,
        "spread_pct": None,
        "otm_pct": None,
        "earnings_date": None,
        "days_to_expiry": None,
        "historical_moves": [],
        "avg_earnings_move": None,
        "implied_move": None,
    }

    try:
        stock = yf.Ticker(ticker)

        # --- Stock price ---
        hist = stock.history(period="1d", interval="1m")
        if not hist.empty:
            data["stock_price"] = round(hist["Close"].iloc[-1], 2)
        else:
            info = stock.info
            data["stock_price"] = round(info.get("regularMarketPrice", 0), 2)

        stock_price = data["stock_price"]

        # --- Earnings date ---
        try:
            calendar = stock.calendar
            if calendar is not None and not calendar.empty:
                earnings = calendar.get("Earnings Date")
                if earnings is not None and len(earnings) > 0:
                    ed = earnings[0]
                    if hasattr(ed, 'strftime'):
                        data["earnings_date"] = ed.strftime("%b %d, %Y")
                    else:
                        data["earnings_date"] = str(ed)
        except Exception as e:
            print(f"[FETCHER] Earnings date error: {e}")

        # --- Options chain ---
        if expiry_raw and strike and stock_price:
            try:
                # Convert MM/DD/YY to YYYY-MM-DD for yfinance
                parts = expiry_raw.split("/")
                if len(parts) == 3:
                    m, d, y = parts
                    y = "20" + y if len(y) == 2 else y
                    expiry_yf = f"{y}-{m.zfill(2)}-{d.zfill(2)}"

                    # Get available expiry dates and find closest
                    available = stock.options
                    closest_expiry = find_closest_expiry(available, expiry_yf)

                    if closest_expiry:
                        chain = stock.option_chain(closest_expiry)
                        options = chain.calls if option_type == "call" else chain.puts

                        # Find closest strike
                        strike_float = float(strike)
                        options["strike_diff"] = abs(options["strike"] - strike_float)
                        closest = options.nsmallest(1, "strike_diff").iloc[0]

                        data["bid"] = round(float(closest.get("bid", 0)), 2)
                        data["ask"] = round(float(closest.get("ask", 0)), 2)
                        data["open_interest"] = int(closest.get("openInterest", 0))
                        data["implied_volatility"] = round(float(closest.get("impliedVolatility", 0)) * 100, 1)

                        # Spread %
                        if data["ask"] and data["ask"] > 0:
                            spread = data["ask"] - data["bid"]
                            data["spread_pct"] = round((spread / data["ask"]) * 100, 1)

                        # OTM %
                        if stock_price:
                            if option_type == "call":
                                otm = ((strike_float - stock_price) / stock_price) * 100
                            else:
                                otm = ((stock_price - strike_float) / stock_price) * 100
                            data["otm_pct"] = round(otm, 1)

            except Exception as e:
                print(f"[FETCHER] Options chain error: {e}")

        # --- Days to expiry ---
        if expiry_raw:
            try:
                parts = expiry_raw.split("/")
                m, d, y = parts
                y = "20" + y if len(y) == 2 else y
                exp_date = datetime(int(y), int(m), int(d))
                data["days_to_expiry"] = (exp_date - datetime.now()).days
            except Exception as e:
                print(f"[FETCHER] Days to expiry error: {e}")

        # --- Historical earnings moves ---
        try:
            hist_quarterly = stock.history(period="2y", interval="1d")
            earnings_moves = estimate_earnings_moves(stock, hist_quarterly)
            if earnings_moves:
                data["historical_moves"] = earnings_moves
                data["avg_earnings_move"] = round(sum(abs(m) for m in earnings_moves) / len(earnings_moves), 1)
        except Exception as e:
            print(f"[FETCHER] Historical moves error: {e}")

    except Exception as e:
        print(f"[FETCHER] General error for {ticker}: {e}")

    return data


def find_closest_expiry(available_dates, target_date):
    """Find the closest available expiry to our target."""
    if not available_dates:
        return None
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d")
        closest = min(available_dates,
                     key=lambda d: abs((datetime.strptime(d, "%Y-%m-%d") - target).days))
        return closest
    except:
        return available_dates[0] if available_dates else None


def estimate_earnings_moves(stock, hist):
    """
    Estimate historical earnings day moves from price history.
    Uses earnings dates from yfinance to find day-after moves.
    """
    moves = []
    try:
        # Get earnings history
        earnings_hist = stock.earnings_dates
        if earnings_hist is None or earnings_hist.empty:
            return moves

        for date, row in earnings_hist.iterrows():
            try:
                date_str = date.strftime("%Y-%m-%d")
                # Find price day before and day after
                before = hist[hist.index.strftime("%Y-%m-%d") < date_str]
                after = hist[hist.index.strftime("%Y-%m-%d") >= date_str]

                if before.empty or after.empty:
                    continue

                price_before = before.iloc[-1]["Close"]
                price_after = after.iloc[0]["Close"]

                move_pct = ((price_after - price_before) / price_before) * 100
                moves.append(round(move_pct, 1))

                if len(moves) >= 8:  # Last 8 earnings
                    break
            except:
                continue
    except Exception as e:
        print(f"[FETCHER] Earnings moves error: {e}")

    return moves