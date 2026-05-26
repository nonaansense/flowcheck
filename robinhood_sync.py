"""
Robinhood API sync using direct HTTP calls (no robin_stocks dependency).
Stores auth token in Supabase so it persists across Railway restarts.

Required Railway variables:
  ROBINHOOD_USERNAME = your@email.com
  ROBINHOOD_PASSWORD = yourpassword
"""
import os, json, requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

RH_BASE        = "https://api.robinhood.com"
RH_TOKEN_KEY   = "rh_auth_token"
RH_DEVICE_KEY  = "rh_device_token"
RH_SEEN_KEY    = "rh_seen_orders"
RH_CLIENT_ID   = "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS"  # Robinhood public client ID

_auth_token    = None
_mfa_callback  = None

def get_credentials():
    return (
        os.environ.get("ROBINHOOD_USERNAME",""),
        os.environ.get("ROBINHOOD_PASSWORD",""),
    )

def has_credentials() -> bool:
    u, p = get_credentials()
    return bool(u and p)

def load_token() -> str | None:
    """Load saved auth token from Supabase."""
    from storage import db_get
    return db_get(RH_TOKEN_KEY)

def save_token(token: str):
    """Save auth token to Supabase."""
    from storage import db_set
    db_set(RH_TOKEN_KEY, token)
    print("[RH] Auth token saved to Supabase")

def get_device_token() -> str:
    """Get or generate a stable device token."""
    from storage import db_get, db_set
    token = db_get(RH_DEVICE_KEY)
    if token:
        return token
    # Generate a new UUID-style device token
    import uuid
    token = str(uuid.uuid4())
    db_set(RH_DEVICE_KEY, token)
    print("[RH] New device token generated: " + token[:8] + "...")
    return token

def get_seen_orders() -> set:
    from storage import db_get
    raw = db_get(RH_SEEN_KEY)
    if raw:
        try:
            return set(json.loads(raw))
        except:
            pass
    return set()

def save_seen_orders(seen: set):
    from storage import db_set
    db_set(RH_SEEN_KEY, json.dumps(list(seen)[-500:]))

def get_headers(token: str = None) -> dict:
    t = token or _auth_token or load_token()
    headers = {
        "Accept":        "application/json",
        "Content-Type":  "application/json",
        "X-Robinhood-API-Version": "1.431.4",
        "User-Agent":    "Robinhood/823 (iPhone; iOS 16.1.1; Scale/3.00)",
    }
    if t:
        headers["Authorization"] = "Bearer " + t
    return headers

def login(mfa_code: str = None) -> tuple:
    """
    Login to Robinhood API directly.
    Returns (success, message) or (False, "MFA_REQUIRED") if MFA needed.
    """
    global _auth_token

    username, password = get_credentials()
    if not username or not password:
        return False, "No credentials configured"

    device_token = get_device_token()

    payload = {
        "client_id":       RH_CLIENT_ID,
        "expires_in":      86400,
        "grant_type":      "password",
        "password":        password,
        "scope":           "internal",
        "username":        username,
        "challenge_type":  "sms",
        "device_token":    device_token,
    }
    if mfa_code:
        payload["mfa_code"] = mfa_code

    try:
        r = requests.post(
            RH_BASE + "/oauth2/token/",
            json=payload,
            headers={
                "Accept":       "application/json",
                "Content-Type": "application/json",
            },
            timeout=15
        )
        print(f"[RH] Login status: {r.status_code}")
        data = r.json()

        if r.status_code == 200:
            token = data.get("access_token")
            if token:
                _auth_token = token
                save_token(token)
                print("[RH] ✅ Logged in successfully")
                return True, "Logged in successfully"
            return False, "No access token in response"

        elif r.status_code == 400:
            # MFA required
            if data.get("mfa_required") or "mfa" in str(data).lower():
                print("[RH] MFA required")
                return False, "MFA_REQUIRED"
            detail = data.get("detail","") or str(data)
            return False, f"Login failed: {detail[:100]}"

        elif r.status_code == 401:
            if data.get("mfa_required"):
                return False, "MFA_REQUIRED"
            return False, "Invalid credentials"

        else:
            return False, f"HTTP {r.status_code}: {str(data)[:100]}"

    except Exception as e:
        print(f"[RH] Login error: {e}")
        return False, str(e)[:100]

def ensure_logged_in() -> bool:
    """Ensure we have a valid auth token. Returns True if ready."""
    global _auth_token

    # Try saved token first
    if not _auth_token:
        _auth_token = load_token()

    if _auth_token:
        # Quick verify
        r = requests.get(
            RH_BASE + "/user/",
            headers=get_headers(),
            timeout=8
        )
        if r.status_code == 200:
            print("[RH] ✅ Token valid")
            return True
        print(f"[RH] Saved token expired ({r.status_code}) — re-logging in")
        _auth_token = None

    # Fresh login
    success, msg = login()
    return success

def get_option_orders(count: int = 50) -> list:
    """Fetch option orders from Robinhood."""
    if not ensure_logged_in():
        return []
    try:
        r = requests.get(
            RH_BASE + "/options/orders/?page_size=" + str(count),
            headers=get_headers(),
            timeout=15
        )
        if r.status_code == 200:
            data    = r.json()
            results = data.get("results", [])
            print(f"[RH] Fetched {len(results)} option orders")
            return results
        print(f"[RH] Orders fetch failed: {r.status_code} {r.text[:100]}")
        return []
    except Exception as e:
        print(f"[RH] Orders error: {e}")
        return []

def parse_option_order(order: dict) -> dict | None:
    """Parse a Robinhood option order into FlowCheck journal format."""
    try:
        state = order.get("state","")
        if state != "filled":
            return None

        legs = order.get("legs",[])
        if not legs:
            return None

        leg          = legs[0]
        side         = leg.get("side","buy")       # buy or sell
        position_eff = leg.get("position_effect","open")  # open or close

        # Determine action
        if side == "buy" and position_eff == "open":
            action, order_type = "entry", "BTO"
        elif side == "sell" and position_eff == "close":
            action, order_type = "exit", "STC"
        elif side == "sell" and position_eff == "open":
            action, order_type = "entry", "STO"
        elif side == "buy" and position_eff == "close":
            action, order_type = "exit", "BTC"
        else:
            action, order_type = "entry", "BTO"

        # Executions
        executions = leg.get("executions",[])
        if not executions:
            return None

        fill_price = float(executions[0].get("price",0))
        contracts  = int(float(executions[0].get("quantity",1)))
        fill_time  = executions[0].get("timestamp","")

        # Option instrument data from URL
        option_url  = leg.get("option","")
        ticker      = ""
        strike      = ""
        opt_type    = "call"
        expiry      = ""

        if option_url:
            r = requests.get(option_url, headers=get_headers(), timeout=8)
            if r.status_code == 200:
                inst        = r.json()
                strike      = str(inst.get("strike_price",""))
                opt_type    = inst.get("type","call")
                expiry_raw  = inst.get("expiration_date","")
                ticker      = inst.get("chain_symbol","")
                if expiry_raw:
                    try:
                        expiry = datetime.strptime(expiry_raw, "%Y-%m-%d").strftime("%m/%d/%y")
                    except:
                        expiry = expiry_raw

        # Parse fill datetime
        fill_date = fill_time_str = ""
        if fill_time:
            try:
                fill_dt       = datetime.fromisoformat(fill_time.replace("Z","+00:00"))
                fill_dt       = fill_dt.astimezone(ZoneInfo("America/New_York"))
                fill_date     = fill_dt.strftime("%Y-%m-%d")
                fill_time_str = fill_dt.strftime("%I:%M%p")
            except:
                pass

        if not ticker or not expiry:
            return None

        return {
            "ticker":      ticker.upper(),
            "strike":      strike,
            "option_type": opt_type,
            "expiry":      expiry,
            "contracts":   contracts,
            "price":       fill_price,
            "date":        fill_date,
            "time":        fill_time_str,
            "action":      action,
            "order_type":  order_type,
            "order_id":    order.get("id",""),
            "source":      "robinhood_api",
            "confidence":  "high",
        }
    except Exception as e:
        print(f"[RH] Parse error: {e}")
        return None

def sync_orders(send_sms_fn=None) -> list:
    """Fetch and log new filled option orders."""
    if not has_credentials():
        return []

    orders   = get_option_orders()
    if not orders:
        return []

    seen     = get_seen_orders()
    new_logs = []

    for order in orders:
        order_id = order.get("id","")
        if not order_id or order_id in seen:
            continue

        seen.add(order_id)

        if order.get("state","") != "filled":
            continue

        parsed = parse_option_order(order)
        if not parsed:
            continue

        ticker = parsed["ticker"]
        print(f"[RH SYNC] New fill: {ticker} {parsed['strike']}{parsed['option_type'][0].upper()} @ ${parsed['price']}")

        try:
            from trade_journal import add_entry, add_exit, load_journal, save_journal

            if parsed["action"] == "entry":
                trade = add_entry(
                    parsed["ticker"], parsed["strike"], parsed["option_type"],
                    parsed["expiry"], parsed["contracts"], parsed["price"],
                    parsed["date"], parsed["time"], "default",
                )
                if trade.get("_duplicate"):
                    print(f"[RH SYNC] Duplicate: {ticker}")
                    continue

                # Set order_type
                j = load_journal()
                for t in j.get("trades",[]):
                    if t.get("id") == trade.get("id"):
                        t["order_type"] = parsed["order_type"]
                        break
                save_journal(j)

                msg = (
                    "📥 Auto-logged from Robinhood:" + chr(10) +
                    ticker + " " + parsed["strike"] +
                    parsed["option_type"][0].upper() + " " + parsed["expiry"] +
                    " x" + str(parsed["contracts"]) + " @ $" + str(parsed["price"]) +
                    chr(10) + parsed["date"] + " " + parsed["time"] + " (BTO)" +
                    chr(10) + "Edit if needed: /edit " + ticker + " FIELD VALUE"
                )

            else:
                result = add_exit(
                    parsed["ticker"], parsed["price"],
                    parsed["date"], parsed["time"], parsed["contracts"],
                )
                pnl   = result.get("pnl_total",0) if result else 0
                label = "WIN ✅" if pnl and pnl > 0 else "LOSS ❌"
                msg   = (
                    "📤 Exit auto-logged from Robinhood:" + chr(10) +
                    ticker + " " + parsed.get("strike","") +
                    parsed["option_type"][0].upper() + chr(10) +
                    (label + " $" + str(round(pnl,2)) if result else "No open trade found")
                )

            if send_sms_fn:
                send_sms_fn(msg)
            new_logs.append(parsed)

        except Exception as e:
            print(f"[RH SYNC] Journal error for {ticker}: {e}")

    save_seen_orders(seen)
    if new_logs:
        print(f"[RH SYNC] Logged {len(new_logs)} new fills")
    return new_logs

def get_status() -> dict:
    has_creds   = has_credentials()
    has_token   = bool(load_token())
    return {
        "credentials":  "✅" if has_creds else "❌ Add ROBINHOOD_USERNAME + ROBINHOOD_PASSWORD",
        "auth_token":   "✅ Saved in Supabase" if has_token else "❌ Run /setup-robinhood",
        "auto_sync":    "Every 5 min during market hours",
    }
