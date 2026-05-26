"""
Tastytrade API integration for option price fetching.
Uses Tastytrade's REST API — free for account holders.

Required Railway environment variables:
  TASTY_USERNAME = your Tastytrade login email
  TASTY_PASSWORD = your Tastytrade password

API docs: https://developer.tastytrade.com
"""
import os, requests, json
from datetime import datetime, date
from zoneinfo import ZoneInfo

TASTY_BASE  = "https://api.tastytrade.com"
_session_token = None
_token_expiry  = None

def get_credentials():
    return (
        os.environ.get("TASTY_USERNAME",""),
        os.environ.get("TASTY_PASSWORD","")
    )

def has_credentials() -> bool:
    u, p = get_credentials()
    return bool(u and p)

def get_session_token() -> str | None:
    """
    Authenticate with Tastytrade and get session token.
    Tokens last 24 hours — cached in memory.
    """
    global _session_token, _token_expiry

    # Return cached token if still valid
    if _session_token and _token_expiry:
        if datetime.now(ZoneInfo("America/New_York")) < _token_expiry:
            return _session_token

    username, password = get_credentials()
    if not username or not password:
        print("[TASTY] No credentials configured")
        return None

    try:
        # Try multiple auth formats — Tastytrade API varies by version
        payloads = [
            {"login": username, "password": password, "remember-me": True},
            {"login": username, "password": password},
            {"username": username, "password": password},
        ]
        for payload in payloads:
            r = requests.post(
                TASTY_BASE + "/sessions",
                json=payload,
                headers={
                    "Content-Type":  "application/json",
                    "Accept":        "application/json",
                },
                timeout=10
            )
            print(f"[TASTY] Auth attempt status: {r.status_code} body: {r.text[:200]}")
            if r.status_code in (200, 201):
                data = r.json()
                token = (data.get("data",{}).get("session-token") or
                         data.get("session-token") or
                         data.get("data",{}).get("token"))
                if token:
                    _session_token = token
                    from datetime import timedelta
                    _token_expiry  = datetime.now(ZoneInfo("America/New_York")) + timedelta(hours=23)
                    print("[TASTY] ✅ Authenticated successfully")
                    return _session_token
        print("[TASTY] All auth attempts failed")
        return None
    except Exception as e:
        print(f"[TASTY] Auth error: {e}")
        return None

def format_expiry_tasty(expiry: str) -> str | None:
    """Convert expiry to YYYY-MM-DD for Tastytrade API."""
    expiry = expiry.strip()
    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(expiry, fmt).strftime("%Y-%m-%d")
            break
        except:
            continue
    return None

def get_option_price_tasty(ticker: str, strike: str, opt_type: str, expiry: str) -> float | None:
    """
    Fetch option price from Tastytrade API.
    Returns mark price (midpoint of bid/ask).
    """
    token = get_session_token()
    if not token:
        return None

    exp_date = format_expiry_tasty(expiry)
    if not exp_date:
        print(f"[TASTY] Could not parse expiry: {expiry}")
        return None

    # Build option symbol
    call_put    = "C" if opt_type.lower() in ("c","call") else "P"
    exp_compact = datetime.strptime(exp_date, "%Y-%m-%d").strftime("%y%m%d")
    strike_f    = float(strike)
    strike_int  = int(strike_f * 1000)
    strike_str  = str(strike_int).zfill(8)
    occ_symbol  = f"{ticker.upper():<6}{exp_compact}{call_put}{strike_str}"

    headers = {
        "Authorization": token,
        "Content-Type":  "application/json",
    }

    try:
        # Get option chain for this expiry
        r = requests.get(
            TASTY_BASE + f"/option-chains/{ticker.upper()}/nested",
            headers=headers,
            timeout=10
        )
        if r.status_code != 200:
            print(f"[TASTY] Chain error: {r.status_code}")
            return None

        data = r.json().get("data",{}).get("items",[])
        for expiration in data:
            if expiration.get("expiration-date","") != exp_date:
                continue
            for strike_data in expiration.get("strikes",[]):
                s = float(strike_data.get("strike-price",0))
                if abs(s - strike_f) < 0.01:
                    # Found matching strike
                    leg_key = "call" if call_put == "C" else "put"
                    leg     = strike_data.get(leg_key, {})
                    mark    = leg.get("mark") or leg.get("mid") or leg.get("last")
                    if mark and float(mark) > 0:
                        print(f"[TASTY] ✅ {ticker} {strike}{call_put} {exp_date}: ${mark}")
                        return float(mark)

        print(f"[TASTY] Option not found: {ticker} {strike}{call_put} {exp_date}")
        return None

    except Exception as e:
        print(f"[TASTY] Price fetch error: {e}")
        return None

def get_option_prices_batch(positions: list) -> dict:
    """
    Fetch prices for multiple options efficiently.
    positions: list of dicts with ticker, strike, opt_type, expiry
    Returns dict keyed by (ticker, strike, opt_type, expiry) -> price
    """
    token = get_session_token()
    if not token:
        return {}

    # Group by ticker to minimize API calls
    by_ticker = {}
    for p in positions:
        t = p["ticker"].upper()
        by_ticker.setdefault(t, []).append(p)

    results = {}
    headers = {"Authorization": token, "Content-Type": "application/json"}

    for ticker, opts in by_ticker.items():
        try:
            r = requests.get(
                TASTY_BASE + f"/option-chains/{ticker}/nested",
                headers=headers,
                timeout=10
            )
            if r.status_code != 200:
                print(f"[TASTY] Chain error for {ticker}: {r.status_code}")
                continue

            data = r.json().get("data",{}).get("items",[])

            for opt in opts:
                exp_date  = format_expiry_tasty(opt["expiry"])
                strike_f  = float(opt["strike"])
                call_put  = "C" if opt["opt_type"].lower() in ("c","call") else "P"
                leg_key   = "call" if call_put == "C" else "put"

                for expiration in data:
                    if expiration.get("expiration-date","") != exp_date:
                        continue
                    for strike_data in expiration.get("strikes",[]):
                        s = float(strike_data.get("strike-price",0))
                        if abs(s - strike_f) < 0.01:
                            leg  = strike_data.get(leg_key, {})
                            mark = leg.get("mark") or leg.get("mid") or leg.get("last")
                            if mark and float(mark) > 0:
                                key = (ticker, opt["strike"], opt["opt_type"], opt["expiry"])
                                results[key] = float(mark)
                                print(f"[TASTY] ✅ {ticker} {opt['strike']}{call_put}: ${mark}")
                            break

        except Exception as e:
            print(f"[TASTY] Batch error for {ticker}: {e}")

    return results

def test_connection() -> str:
    """Test Tastytrade connection. Returns status string."""
    if not has_credentials():
        return "No credentials — add TASTY_USERNAME and TASTY_PASSWORD to Railway"
    token = get_session_token()
    if token:
        return "✅ Connected to Tastytrade API"
    return "❌ Authentication failed — check credentials"
