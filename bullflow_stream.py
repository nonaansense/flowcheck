"""
Bullflow SSE streaming client.
Connects to https://api.bullflow.io/v1/streaming/alerts and processes
real-time flow alerts, feeding them into FlowCheck's process_alert pipeline.
"""
import os
import json
import time
import threading
import requests
from datetime import datetime, date
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

def parse_occ_symbol(sym: str) -> dict | None:
    """
    Parse OCC option symbol: O:TICKER[YYMMDD][C/P][STRIKE*1000 padded 8 digits]
    Example: O:AMD251205P00205000 → ticker=AMD exp=12/05/25 put strike=205.0
    """
    try:
        if sym.startswith("O:"):
            sym = sym[2:]
        # Find where the date starts (6 digits)
        import re
        m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', sym)
        if not m:
            return None
        ticker   = m.group(1)
        date_str = m.group(2)   # YYMMDD
        cp       = m.group(3)   # C or P
        strike_s = m.group(4)   # 8 digits, strike * 1000

        # Parse date
        yy, mm, dd_s = int(date_str[:2]), int(date_str[2:4]), int(date_str[4:])
        year   = 2000 + yy
        expiry = f"{mm:02d}/{dd_s:02d}/{str(year)[2:]}"  # MM/DD/YY

        # Parse strike
        strike = float(strike_s) / 1000.0

        # DTE
        try:
            exp_dt = date(year, mm, dd_s)
            dte    = (exp_dt - date.today()).days
        except:
            dte = 30

        return {
            "ticker":      ticker,
            "strike":      str(int(strike) if strike == int(strike) else strike),
            "option_type": "call" if cp == "C" else "put",
            "expiry":      expiry,
            "dte":         dte,
            "occ_symbol":  sym,
        }
    except Exception as e:
        print(f"[BULLFLOW] OCC parse error for {sym}: {e}")
        return None

def build_trade_from_alert(alert: dict) -> dict | None:
    """Convert Bullflow alert payload to FlowCheck trade dict."""
    sym     = alert.get("symbol","")
    parsed  = parse_occ_symbol(sym)
    if not parsed:
        print(f"[BULLFLOW] Could not parse symbol: {sym}")
        return None

    # alertPremium can be float or formatted string like "1091.31K"
    raw_prem = alert.get("alertPremium", 0) or 0
    try:
        premium = float(raw_prem)
    except (ValueError, TypeError):
        raw_str = str(raw_prem).strip().upper().replace(",","")
        if raw_str.endswith("K"):
            premium = float(raw_str[:-1]) * 1_000
        elif raw_str.endswith("M"):
            premium = float(raw_str[:-1]) * 1_000_000
        elif raw_str.endswith("B"):
            premium = float(raw_str[:-1]) * 1_000_000_000
        else:
            try: premium = float(raw_str)
            except: premium = 0
        print(f"[BULLFLOW] Parsed premium string '{raw_prem}' → ${premium:,.0f}")
    fill_px  = float(alert.get("averageFillPrice", 0) or 0)
    ts       = alert.get("timestamp", time.time())
    alert_nm = alert.get("alertName","")
    alert_tp = alert.get("alertType","algo")

    # Map Bullflow alert names to fill types
    fill_type = "UNKNOWN"
    nm_lower  = alert_nm.lower()
    if "ask" in nm_lower or "urgent" in nm_lower or "sweep" in nm_lower:
        fill_type = "FULL_ASK"
    elif "bullflow" in nm_lower or "sizable" in nm_lower or "repeater" in nm_lower:
        fill_type = "MOSTLY_ASK"
    elif "bid" in nm_lower:
        # Bid side put = put SELLING = bullish conviction
        # Bid side call = call selling = bearish, less interesting
        opt = "put" if parsed and parsed.get("option_type") == "put" else "call"
        fill_type = "PUT_SELL_BID" if opt == "put" else "MOSTLY_BID"

    # Is sweep?
    is_sweep = any(w in nm_lower for w in ["sweep","urgent","sizable"])

    # Build trade dict compatible with FlowCheck pipeline
    # Map Bullflow alert names to Vol/OI signals for scorer
    vol_oi_signal = 0.0
    nm_up = alert_nm.upper()
    if "UNUSUAL" in nm_up:     vol_oi_signal = 15.0  # Single trade > OI = massive
    elif "RISING VOL" in nm_up: vol_oi_signal = 8.0   # First vol>OI crossing
    elif "VOL>OI" in nm_up:    vol_oi_signal = 5.0   # Cumulative vol > OI
    elif "URGENT" in nm_up:    vol_oi_signal = 6.0   # Rapid repeats
    elif "BULLFLOW" in nm_up:  vol_oi_signal = 4.0   # Aggressive repeats
    elif "SIZABLE" in nm_up:   vol_oi_signal = 3.0   # Large size

    trade = {
        **parsed,
        "premium":        premium,
        "fill_type":      fill_type,
        "is_sweep":       is_sweep,
        "alert_name":     alert_nm,
        "alert_type":     alert_tp,
        "source":         "bullflow",
        "timestamp":      ts,
        "flow_timestamp": ts,
        "vol_oi_ratio":   vol_oi_signal,
        "open_interest":  int(premium / fill_px / 100) if fill_px > 0 else 0,
        "option_price":   fill_px,        # averageFillPrice = entry price for the flow
        "avg_fill_price": fill_px,
    }

    # Estimate contracts from premium and fill price
    if fill_px > 0 and premium > 0:
        est_contracts = int(premium / (fill_px * 100))
        trade["estimated_contracts"] = est_contracts

    print(f"[BULLFLOW] Parsed: {parsed['ticker']} {parsed['strike']}"
          f"{parsed['option_type'][0].upper()} {parsed['expiry']} "
          f"${premium:,.0f} {fill_type} [{alert_nm}]")
    return trade

def get_peak_return(occ_symbol: str, entry_price: float, trade_timestamp: float) -> dict | None:
    """
    Fetch peak return for a closed/open trade from Bullflow peakReturn endpoint.
    Returns {peakPriceSinceTimestamp, peakPercentReturnSinceTimestamp} or None.
    """
    key = os.environ.get("BULLFLOW_API_KEY","")
    if not key:
        return None
    try:
        r = requests.get(
            "https://api.bullflow.io/v1/data/peakReturn",
            params={
                "key":             key,
                "sym":             f"O:{occ_symbol}" if not occ_symbol.startswith("O:") else occ_symbol,
                "old_price":       entry_price,
                "trade_timestamp": int(trade_timestamp),
            },
            timeout=15
        )
        if r.status_code == 200:
            return r.json()
        print(f"[BULLFLOW] peakReturn {r.status_code} for {occ_symbol}")
    except Exception as e:
        print(f"[BULLFLOW] peakReturn error: {e}")
    return None

def create_custom_alert(name: str, filters: dict) -> dict | None:
    """Create a custom alert on Bullflow to pre-filter flows."""
    key = os.environ.get("BULLFLOW_API_KEY","")
    if not key:
        return None
    try:
        payload = {"name": name, **filters}
        r = requests.post(
            f"https://api.bullflow.io/v1/alerts/create-alert?key={key}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        if r.status_code in (200, 201):  # 201 = Created (success)
            return r.json()
        print(f"[BULLFLOW] create_alert error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[BULLFLOW] create_alert error: {e}")
    return None

def setup_flowcheck_filters():
    """
    Create FlowCheck's custom alert filters on Bullflow.
    Called once on startup. Filters match FlowGod-style high conviction flows.
    """
    key = os.environ.get("BULLFLOW_API_KEY","")
    if not key:
        return

    # Check existing — only create if none exist
    try:
        r = requests.get(
            f"https://api.bullflow.io/v1/alerts/custom-alerts?key={key}",
            timeout=10
        )
        if r.status_code == 200:
            existing = r.json().get("alerts",[])
            fc_alerts = [a for a in existing if "FlowCheck" in a.get("alertName","")]
            force_recreate = os.environ.get("BULLFLOW_FORCE_RECREATE","").lower() == "true"
            if fc_alerts and not force_recreate:
                print(f"[BULLFLOW] Custom alert already exists ({len(fc_alerts)}) — skipping creation")
                return
            if len(fc_alerts) > 1:
                print(f"[BULLFLOW] Warning: {len(fc_alerts)} duplicate alerts found — delete extras in Bullflow dashboard")
                return
    except Exception as e:
        print(f"[BULLFLOW] Could not check existing alerts: {e}")

    print("[BULLFLOW] Creating custom alert filter...")

    # Get filter thresholds from env
    min_premium = int(os.environ.get("FILTER_MIN_PREMIUM", 150000))
    min_dte     = int(os.environ.get("FILTER_MIN_DTE", 7))
    max_dte     = int(os.environ.get("FILTER_MAX_DTE", 90))
    max_otm     = float(os.environ.get("FILTER_MAX_OTM", 20.0))

    # Blocklist — only exclude instruments that are almost always hedges/protection
    # NOT excluding SPY/QQQ calls — those can be genuine directional
    # NOT excluding sector ETFs — unusual flow there is worth seeing
    etf_blocklist = [
        # Pure hedge/protection instruments
        "VIX","VIXY","UVXY","SVXY",          # Volatility
        "SQQQ","SPXS","SDOW","SPXU",         # Leveraged inverse
        "TLT","IEF","SHY","TBT","TMF","TMV", # Bonds
        "GLD","SLV","GDX","GDXJ",            # Gold/silver
        "USO","UCO","SCO",                   # Oil ETFs
    ]
    exclude_etf = os.environ.get("FILTER_EXCLUDE_ETF_HEDGES","true").lower() != "false"

    # Stocks only + DTE range + OTM filter
    # Don't raise premium — low-price stocks have lower absolute premiums
    # Instead filter by: stocks only, reasonable DTE, not deep ITM
    filters = {
        "premiumMin":    min_premium,
        "dteMin":        min_dte,        # No same-week lotto tickets
        "dteMax":        max_dte,        # No multi-year LEAPs
        "otmPercentMax": max_otm,        # No deep OTM lotto tickets
        "quickFilters":  ["Stocks"],     # Stocks only — excludes SPX/SPXW/RUT/NDX
    }
    if exclude_etf:
        filters["tickerBlocklist"] = etf_blocklist
    print(f"[BULLFLOW] Filter: ${min_premium:,} + Stocks + DTE {min_dte}-{max_dte} + OTM≤{max_otm}%")
    if exclude_etf:
        filters["tickerBlocklist"] = etf_blocklist

    # Remove name from filters dict if present to avoid duplicate
    alert_name = filters.pop("name", "FlowCheck High Conviction")
    result = create_custom_alert(alert_name, filters)
    if result:
        print(f"[BULLFLOW] Created custom alert: {result}")
    else:
        print("[BULLFLOW] Could not create custom alert — using all algo alerts")

def stream_alerts(process_fn, send_sms_fn=None):
    """
    Connect to Bullflow SSE stream and process alerts.
    process_fn(tweet_text, tweet_url, pre_parsed_trade) — same signature as process_alert.
    Runs forever with auto-reconnect.
    """
    key = os.environ.get("BULLFLOW_API_KEY","")
    if not key:
        print("[BULLFLOW] No BULLFLOW_API_KEY — stream not started")
        return

    # Setup custom filters once
    try:
        setup_flowcheck_filters()
    except Exception as e:
        print(f"[BULLFLOW] Filter setup error: {e}")

    import atexit, os as _os
    def _cleanup_lock():
        try: _os.remove(_LOCK_FILE)
        except: pass
    atexit.register(_cleanup_lock)

    retry_delay   = 5
    _seen_symbols = set()
    _seen_tickers = set()
    while True:
        try:
            print(f"[BULLFLOW] Connecting to SSE stream...")
            time.sleep(1)  # Small delay to prevent duplicate connection race
            with requests.get(
                "https://api.bullflow.io/v1/streaming/alerts",
                params={"key": key},
                stream=True,
                timeout=(10, None),  # 10s connect, no read timeout
            ) as resp:
                resp.raise_for_status()
                print("[BULLFLOW] Connected to live alert stream")
                retry_delay = 5  # Reset on successful connect

                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if not line.startswith("data: "):
                        continue
                    try:
                        msg   = json.loads(line[6:])
                        event = msg.get("event","")

                        if event == "init":
                            print(f"[BULLFLOW] Stream initialized at {msg.get('startedAt','?')}")

                        elif event == "heartbeat":
                            pass  # Expected every 10s

                        elif event == "alert":
                            alert_id   = msg.get("id","")
                            alert_data = msg.get("data",{})
                            alert_type = alert_data.get("alertType","")
                            alert_name = alert_data.get("alertName","")
                            symbol     = alert_data.get("symbol","")
                            premium    = alert_data.get("alertPremium",0)

                            # Market hours check
                            now_et = datetime.now(ET)
                            if now_et.hour < 9 or (now_et.hour == 9 and now_et.minute < 30):
                                print(f"[BULLFLOW] Pre-market alert skipped: {symbol}")
                                continue
                            if now_et.hour >= 16:
                                print(f"[BULLFLOW] After-hours alert skipped: {symbol}")
                                continue

                            print(f"[BULLFLOW] Alert: {alert_id} {alert_type} {symbol} "
                                  f"${premium:,.0f} [{alert_name}]")

                            trade = build_trade_from_alert(alert_data)
                            if not trade:
                                continue

                            # Deduplicate — same TICKER within 2 hours = skip
                            # Prevents SNOW 180C, SNOW 185C, SNOW 190C all firing
                            ticker_dedup_key = f"{trade['ticker']}_{int(float(alert_data.get('timestamp',0)) // 7200)}"
                            symbol_dedup_key = f"{symbol}_{int(float(alert_data.get('timestamp',0)) // 60)}"
                            if ticker_dedup_key in _seen_tickers:
                                print(f"[BULLFLOW] Ticker dedup skip: {trade['ticker']} (already processed in last 2h)")
                                continue
                            if symbol_dedup_key in _seen_symbols:
                                print(f"[BULLFLOW] Symbol dedup skip: {symbol}")
                                continue
                            _seen_symbols.add(symbol_dedup_key)
                            _seen_tickers.add(ticker_dedup_key)
                            if len(_seen_symbols) > 500:
                                _seen_symbols.clear()
                            if len(_seen_tickers) > 200:
                                _seen_tickers.clear()

                            # Filter out ETF hedges if configured
                            exclude_etf = os.environ.get("FILTER_EXCLUDE_ETF_HEDGES","").lower() == "true"
                            hedge_etfs  = {"VIX","VIXY","UVXY","SVXY",
                                          "SQQQ","SPXS","SDOW","SPXU",
                                          "TLT","IEF","SHY","TBT","TMF","TMV",
                                          "GLD","SLV","GDX","GDXJ",
                                          "USO","UCO","SCO",
                                          # Index options — always hedges/institutional
                                          "SPX","SPXW","NDX","RUT","VIX"}
                            if exclude_etf and trade["ticker"] in hedge_etfs:
                                print(f"[BULLFLOW] Hedge instrument skip: {symbol}")
                                continue

                            # Skip Grenade trades unless ALLOW_GRENADES=true
                            allow_grenades = os.environ.get("ALLOW_GRENADES","").lower() == "true"
                            if not allow_grenades and "grenade" in alert_name.lower() and trade.get("dte",99) <= 7:
                                print(f"[BULLFLOW] Grenade skip (DTE≤7): {symbol}")
                                continue

                            # Skip splits (multi-exchange non-sweep orders)
                            if "split" in alert_name.lower() and "sweep" not in alert_name.lower():
                                print(f"[BULLFLOW] Split order skip: {symbol}")
                                continue

                            # Premium sanity check against Railway variable
                            min_prem = float(os.environ.get("FILTER_MIN_PREMIUM","500000"))
                            if premium < min_prem:
                                print(f"[BULLFLOW] Premium ${premium:,.0f} < ${min_prem:,.0f} skip")
                                continue

                            # Build a synthetic tweet text for the pipeline
                            tweet = (f"${trade['ticker']} - ${premium:,.0f} "
                                    f"{trade['option_type'].title()} "
                                    f"{trade['strike']} [{alert_name}] via Bullflow")

                            # Feed into FlowCheck pipeline from background thread
                            import asyncio
                            try:
                                # Create new event loop for this thread call
                                loop = asyncio.new_event_loop()
                                loop.run_until_complete(process_fn(tweet, None, trade))
                                loop.close()
                            except Exception as pe:
                                print(f"[BULLFLOW] process_alert error: {pe}")

                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        print(f"[BULLFLOW] Line processing error: {e}")

        except requests.RequestException as e:
            print(f"[BULLFLOW] Stream error: {e} — reconnecting in {retry_delay}s")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 120)  # Max 2 min between retries
        except Exception as e:
            print(f"[BULLFLOW] Unexpected error: {e} — reconnecting in {retry_delay}s")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 120)

_LOCK_FILE = "/tmp/bullflow_stream.lock"

def start_stream_thread(process_fn, send_sms_fn=None):
    """Start Bullflow SSE stream in a background thread. Uses lock file to prevent duplicates across workers."""
    import os
    # Check lock file — if exists and process is still running, skip
    if os.path.exists(_LOCK_FILE):
        try:
            with open(_LOCK_FILE) as f:
                pid = int(f.read().strip())
            # Check if that PID is still running
            os.kill(pid, 0)  # Raises if process doesn't exist
            print(f"[BULLFLOW] Stream already running (PID {pid}) — skipping duplicate")
            return None
        except (ProcessLookupError, ValueError, OSError):
            print("[BULLFLOW] Stale lock file — starting fresh")
            os.remove(_LOCK_FILE)
    # Write our PID to lock file
    with open(_LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    key = os.environ.get("BULLFLOW_API_KEY","")
    if not key:
        print("[BULLFLOW] No API key — stream disabled")
        return None
    # Allow dual mode — stream runs if FLOW_SOURCE=bullflow OR DUAL_FLOW_MODE=true
    flow_source = os.environ.get("FLOW_SOURCE","flowgod").lower()
    dual_mode   = os.environ.get("DUAL_FLOW_MODE","").lower() == "true"
    if flow_source != "bullflow" and not dual_mode:
        print(f"[BULLFLOW] FLOW_SOURCE={flow_source} and DUAL_FLOW_MODE not set — stream disabled")
        return None
    print(f"[BULLFLOW] Starting stream (flow_source={flow_source} dual_mode={dual_mode})")
    # Double-check thread not already running
    existing = [t for t in threading.enumerate() if t.name == "bullflow-stream" and t.is_alive()]
    if existing:
        print("[BULLFLOW] Stream thread already running — not starting duplicate")
        _stream_started = True
        return existing[0]
    t = threading.Thread(
        target=stream_alerts,
        args=(process_fn, send_sms_fn),
        daemon=True,
        name="bullflow-stream"
    )
    t.start()
    print("[BULLFLOW] Stream thread started")
    return t
