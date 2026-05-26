"""
Robinhood API sync for FlowCheck.
Uses robin_stocks to poll for new option fills and auto-log to journal.

Setup:
1. Add to Railway variables:
   ROBINHOOD_USERNAME = your@email.com
   ROBINHOOD_PASSWORD = yourpassword
   ROBINHOOD_DEVICE_TOKEN = (generated on first auth — see /setup-robinhood)

2. First auth: hit /setup-robinhood endpoint from browser
   This triggers MFA — enter code via /robinhood-mfa?code=XXXXXX
   Device token saved to Supabase for future auths

3. After setup, sync runs every 5 minutes automatically
"""
import os, json, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

RH_USER_KEY   = "rh_username"
RH_PASS_KEY   = "rh_password"
RH_DEVICE_KEY = "rh_device_token"
RH_SEEN_KEY   = "rh_seen_orders"

_logged_in    = False
_pending_mfa  = None  # callback waiting for MFA code

def get_credentials():
    return (
        os.environ.get("ROBINHOOD_USERNAME",""),
        os.environ.get("ROBINHOOD_PASSWORD",""),
    )

def has_credentials() -> bool:
    u, p = get_credentials()
    return bool(u and p)

def get_device_token() -> str | None:
    """Get saved device token from Supabase."""
    from storage import db_get
    return db_get(RH_DEVICE_KEY)

def save_device_token(token: str):
    """Save device token to Supabase."""
    from storage import db_set
    db_set(RH_DEVICE_KEY, token)
    print("[RH] Device token saved to Supabase")

def get_seen_orders() -> set:
    """Get set of already-logged order IDs."""
    from storage import db_get
    raw = db_get(RH_SEEN_KEY)
    if raw:
        try:
            return set(json.loads(raw))
        except:
            pass
    return set()

def save_seen_orders(seen: set):
    """Save seen order IDs to Supabase."""
    from storage import db_set
    db_set(RH_SEEN_KEY, json.dumps(list(seen)[-500:]))  # Keep last 500

def login(mfa_code: str = None) -> tuple:
    """
    Login to Robinhood. Returns (success, message).
    On first login from new IP, MFA is required.
    """
    global _logged_in, _pending_mfa

    username, password = get_credentials()
    if not username or not password:
        return False, "No credentials — add ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD to Railway"

    try:
        import robin_stocks.robinhood as rh

        # Use saved device token if available
        device_token = get_device_token()

        if device_token:
            # Try login with saved device token
            try:
                rh.login(
                    username, password,
                    device_token=device_token,
                    store_session=False,
                    mfa_code=mfa_code,
                )
                _logged_in = True
                print("[RH] ✅ Logged in with saved device token")
                return True, "Logged in successfully"
            except Exception as e:
                err = str(e).lower()
                if "mfa" in err or "challenge" in err:
                    _logged_in = False
                    return False, "MFA_REQUIRED"
                print(f"[RH] Device token login failed: {e} — trying fresh login")

        # Fresh login
        try:
            result = rh.login(
                username, password,
                store_session=False,
                mfa_code=mfa_code,
            )
            # Save device token for future logins
            new_token = result.get("device_token") if isinstance(result, dict) else None
            if new_token:
                save_device_token(new_token)
            _logged_in = True
            print("[RH] ✅ Logged in successfully")
            return True, "Logged in successfully"
        except Exception as e2:
            err2 = str(e2).lower()
            print(f"[RH] Fresh login error: {e2}")
            if "mfa" in err2 or "challenge" in err2 or "verification" in err2:
                return False, "MFA_REQUIRED"
            # Check if already logged in despite exception
            if _logged_in:
                return True, "Logged in successfully"
            return False, f"Login failed: {str(e2)[:100]}"

    except Exception as e:
        err = str(e)
        print(f"[RH] Login error: {err}")
        if "mfa" in err.lower() or "challenge" in err.lower() or "verification" in err.lower():
            return False, "MFA_REQUIRED"
        if _logged_in:
            return True, "Logged in successfully"
        return False, f"Login failed: {err[:100]}"

def parse_option_order(order: dict) -> dict | None:
    """Parse a Robinhood option order into FlowCheck journal format."""
    try:
        state = order.get("state","")
        if state != "filled":
            return None

        order_type = order.get("opening_strategy") or order.get("closing_strategy","")
        # BTO = long_call/long_put, STC = short_call/short_put (closing)
        is_entry = "long" in (order_type or "").lower() or "open" in (order_type or "").lower()
        is_exit  = "short" in (order_type or "").lower() or "close" in (order_type or "").lower()

        # Get legs
        legs = order.get("legs",[])
        if not legs:
            return None

        leg = legs[0]
        option_url = leg.get("option","")

        # Parse fill price
        executions = leg.get("executions",[])
        if not executions:
            return None
        fill_price = float(executions[0].get("price",0))
        contracts  = int(float(executions[0].get("quantity",1)))
        fill_time  = executions[0].get("timestamp","")

        # Parse option instrument data from URL
        # URL format: https://api.robinhood.com/options/instruments/{id}/
        option_data = {}
        if option_url:
            try:
                import requests
                r = requests.get(option_url, timeout=8)
                if r.status_code == 200:
                    option_data = r.json()
            except:
                pass

        strike    = str(option_data.get("strike_price",""))
        opt_type  = option_data.get("type","call")
        expiry    = option_data.get("expiration_date","")
        ticker    = option_data.get("chain_symbol","")

        # Parse fill datetime
        fill_dt   = None
        fill_date = ""
        fill_time_str = ""
        if fill_time:
            try:
                fill_dt = datetime.fromisoformat(fill_time.replace("Z","+00:00"))
                fill_dt = fill_dt.astimezone(ZoneInfo("America/New_York"))
                fill_date     = fill_dt.strftime("%Y-%m-%d")
                fill_time_str = fill_dt.strftime("%I:%M%p")
            except:
                pass

        # Normalize expiry to MM/DD/YY
        if expiry:
            try:
                exp_dt = datetime.strptime(expiry, "%Y-%m-%d")
                expiry = exp_dt.strftime("%m/%d/%y")
            except:
                pass

        if not ticker or not strike or not expiry:
            return None

        action = "entry" if is_entry else "exit"
        ot     = "BTO" if is_entry else "STC"

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
            "order_type":  ot,
            "order_id":    order.get("id",""),
            "source":      "robinhood_api",
            "confidence":  "high",
        }
    except Exception as e:
        print(f"[RH] Parse error: {e}")
        return None

def sync_orders(send_sms_fn=None) -> list:
    """
    Fetch recent option orders from Robinhood and log any new fills.
    Returns list of newly logged trades.
    """
    global _logged_in

    if not has_credentials():
        return []

    # Always try to login — session may have expired
    if not _logged_in:
        success, msg = login()
        if not success:
            print(f"[RH SYNC] Login failed: {msg}")
            return []
    
    # Verify still logged in with a quick test
    try:
        import robin_stocks.robinhood as rh
        profile = rh.load_account_profile(info="account_number")
        if not profile:
            _logged_in = False
            success, msg = login()
            if not success:
                print(f"[RH SYNC] Re-login failed: {msg}")
                return []
    except:
        _logged_in = False
        success, msg = login()
        if not success:
            return []

    try:
        import robin_stocks.robinhood as rh

        # Get option orders from last 7 days
        orders   = rh.get_all_option_orders() or []
        seen     = get_seen_orders()
        new_logs = []

        for order in orders:
            order_id = order.get("id","")
            if not order_id or order_id in seen:
                continue
            if order.get("state","") != "filled":
                continue

            parsed = parse_option_order(order)
            if not parsed:
                seen.add(order_id)
                continue

            ticker = parsed["ticker"]
            print(f"[RH SYNC] New fill: {ticker} {parsed['strike']}{parsed['option_type'][0].upper()} {parsed['expiry']} x{parsed['contracts']} @ ${parsed['price']}")

            # Log to journal
            try:
                from trade_journal import add_entry, add_exit

                if parsed["action"] == "entry":
                    trade = add_entry(
                        parsed["ticker"],
                        parsed["strike"],
                        parsed["option_type"],
                        parsed["expiry"],
                        parsed["contracts"],
                        parsed["price"],
                        parsed["date"],
                        parsed["time"],
                        "default",  # account — can't determine from API easily
                    )
                    if trade.get("_duplicate"):
                        print(f"[RH SYNC] Duplicate skipped: {ticker}")
                        seen.add(order_id)
                        continue

                    # Set order_type
                    from trade_journal import load_journal, save_journal
                    j = load_journal()
                    for t in j.get("trades",[]):
                        if t.get("id") == trade.get("id"):
                            t["order_type"] = parsed["order_type"]
                            break
                    save_journal(j)

                    msg = (
                        "📥 Auto-logged from Robinhood:" + chr(10) +
                        parsed["ticker"] + " " + parsed["strike"] +
                        parsed["option_type"][0].upper() + " " + parsed["expiry"] +
                        " x" + str(parsed["contracts"]) + " @ $" + str(parsed["price"]) +
                        chr(10) + parsed["date"] + " " + parsed["time"] +
                        chr(10) + "Edit if needed: /edit " + ticker + " FIELD VALUE"
                    )

                elif parsed["action"] == "exit":
                    result = add_exit(
                        parsed["ticker"],
                        parsed["price"],
                        parsed["date"],
                        parsed["time"],
                        parsed["contracts"],
                    )
                    if result:
                        pnl   = result.get("pnl_total",0) or 0
                        sign  = "+" if pnl >= 0 else ""
                        label = "WIN ✅" if pnl > 0 else "LOSS ❌"
                        msg   = (
                            "📤 Auto-logged exit from Robinhood:" + chr(10) +
                            parsed["ticker"] + " " + parsed["strike"] +
                            parsed["option_type"][0].upper() + chr(10) +
                            label + " " + sign + "$" + str(round(pnl,2))
                        )
                    else:
                        msg = (
                            "📤 Exit from Robinhood (no open trade found):" + chr(10) +
                            parsed["ticker"] + " — log entry first if needed"
                        )

                if send_sms_fn and msg:
                    send_sms_fn(msg)

                new_logs.append(parsed)

            except Exception as e:
                print(f"[RH SYNC] Journal error: {e}")

            seen.add(order_id)

        save_seen_orders(seen)
        if new_logs:
            print(f"[RH SYNC] Logged {len(new_logs)} new fills")
        return new_logs

    except Exception as e:
        print(f"[RH SYNC] Sync error: {e}")
        _logged_in = False
        return []

def get_status() -> dict:
    """Return Robinhood sync status."""
    has_creds  = has_credentials()
    has_device = bool(get_device_token())
    return {
        "credentials":   "✅" if has_creds else "❌ Add ROBINHOOD_USERNAME + ROBINHOOD_PASSWORD",
        "device_token":  "✅ Saved" if has_device else "❌ Run /setup-robinhood first",
        "logged_in":     "✅" if _logged_in else "❌",
        "auto_sync":     "Every 5 min during market hours",
    }
