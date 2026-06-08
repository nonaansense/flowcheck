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

    # Debug: log all available keys on first alert to understand Bullflow payload
    _known_keys = {"averageFillPrice","timestamp","alertName","alertType","symbol",
                   "alertPremium","spotPrice","stockPrice","underlyingPrice","volume",
                   "openInterest","vol","oi","_id","alertId","id"}
    _new_keys = set(alert.keys()) - _known_keys
    if _new_keys:
        print(f"[BULLFLOW] New payload keys found: {_new_keys}")
        for _k in _new_keys:
            print(f"[BULLFLOW]   {_k}: {alert.get(_k)}")

    # Determine fill type from Bullflow data
    # Priority: (1) custom alert = always ask side by our filter
    #           (2) side field from Bullflow
    #           (3) price comparison (fill vs ask/bid)
    #           (4) alert name inference
    nm_lower  = alert_nm.lower()
    alert_tp  = alert.get("alertType","algo")

    # Read side and price fields Bullflow provides
    _side_raw = str(alert.get("side","") or alert.get("tradeSide","") or "").upper()
    _ask_px   = float(alert.get("askPrice",0) or alert.get("ask",0) or 0)
    _bid_px   = float(alert.get("bidPrice",0) or alert.get("bid",0) or 0)
    _mid_px   = (_ask_px + _bid_px) / 2 if _ask_px and _bid_px else 0

    if alert_name in ("FlowCheck High Conviction", "ETFs-Order-Flow"):
        # Our custom alerts use Ask-side only quickFilters — always FULL_ASK
        fill_type = "FULL_ASK"
    elif _side_raw in ("ASK", "ABOVE_ASK", "A"):
        fill_type = "FULL_ASK"
    elif _side_raw in ("BID", "B"):
        opt = "put" if parsed and parsed.get("option_type") == "put" else "call"
        fill_type = "PUT_SELL_BID" if opt == "put" else "MOSTLY_BID"
    elif _side_raw in ("MID", "M"):
        fill_type = "MID"
    elif fill_px > 0 and _ask_px > 0:
        # Compare fill price to ask/bid
        if fill_px >= _ask_px * 0.99:
            fill_type = "FULL_ASK"
        elif _mid_px > 0 and fill_px >= _mid_px * 0.98:
            fill_type = "MID"
        elif _bid_px > 0 and fill_px <= _bid_px * 1.01:
            opt = "put" if parsed and parsed.get("option_type") == "put" else "call"
            fill_type = "PUT_SELL_BID" if opt == "put" else "MOSTLY_BID"
        else:
            fill_type = "MOSTLY_ASK"
    else:
        # Fallback: infer from alert name
        if "ask" in nm_lower or "urgent" in nm_lower or "sweep" in nm_lower:
            fill_type = "FULL_ASK"
        elif "sizable" in nm_lower or "repeater" in nm_lower:
            fill_type = "MOSTLY_ASK"
        elif "bid" in nm_lower:
            opt = "put" if parsed and parsed.get("option_type") == "put" else "call"
            fill_type = "PUT_SELL_BID" if opt == "put" else "MOSTLY_BID"
        else:
            fill_type = "UNKNOWN"

    # Pct at ask (if price data available)
    _pct_ask = None
    if fill_px > 0 and _ask_px > 0 and _bid_px > 0:
        _spread = _ask_px - _bid_px
        if _spread > 0:
            _pct_ask = round(min((fill_px - _bid_px) / _spread * 100, 100), 1)

    # Is sweep?
    is_sweep = any(w in nm_lower for w in ["sweep","urgent","sizable"]) or _side_raw in ("ASK","ABOVE_ASK")

    # Build trade dict compatible with FlowCheck pipeline
    # Map Bullflow alert names to Vol/OI signals for scorer
    # Use real vol/OI from Bullflow payload if available, else estimate from alert name
    raw_vol = float(alert.get("volume", 0) or alert.get("vol", 0) or 0)
    raw_oi  = float(alert.get("openInterest", 0) or alert.get("oi", 0) or 0)
    if raw_vol > 0 and raw_oi > 0:
        vol_oi_signal = round(raw_vol / raw_oi, 1)
        print(f"[BULLFLOW] Real Vol/OI: {raw_vol:.0f}/{raw_oi:.0f} = {vol_oi_signal}x")
    else:
        nm_up = alert_nm.upper()
        if "UNUSUAL" in nm_up:      vol_oi_signal = 15.0
        elif "RISING VOL" in nm_up: vol_oi_signal = 8.0
        elif "VOL>OI" in nm_up:     vol_oi_signal = 5.0
        elif "URGENT" in nm_up:     vol_oi_signal = 6.0
        elif "BULLFLOW" in nm_up:   vol_oi_signal = 4.0
        elif "SIZABLE" in nm_up:    vol_oi_signal = 3.0
        else:                        vol_oi_signal = 3.0


    # Calculate DTE from parsed expiry
    dte_val = None
    expiry_raw = parsed.get("expiry_raw","") or parsed.get("expiry","")
    if expiry_raw:
        try:
            from datetime import datetime as _dt2
            parts = expiry_raw.split("/")
            m2, d2, y2 = parts
            y2 = "20" + y2 if len(y2) == 2 else y2
            exp_dt  = _dt2(int(y2), int(m2), int(d2))
            dte_val = (exp_dt - _dt2.now()).days
        except:
            pass

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
        "option_price":   fill_px,
        "avg_fill_price": fill_px,
        "dte":            dte_val,
        "pct_at_ask":     _pct_ask,
        "ask_price":      _ask_px if _ask_px else None,
        "bid_price":      _bid_px if _bid_px else None,
    }

    # Estimate contracts from premium and fill price
    if fill_px > 0 and premium > 0:
        est_contracts = int(premium / (fill_px * 100))
        trade["estimated_contracts"] = est_contracts

    # Calculate OTM% from stock price if available in alert
    spot = float(alert.get("spotPrice",0) or alert.get("stockPrice",0) or
                 alert.get("underlyingPrice",0) or 0)
    if spot > 0 and parsed.get("strike"):
        try:
            strike_f = float(parsed["strike"])
            if "call" in parsed.get("option_type",""):
                otm_pct = round((spot - strike_f) / strike_f * 100, 1)
            else:
                otm_pct = round((strike_f - spot) / strike_f * 100, 1)
            # Bullflow convention: positive = OTM, negative = ITM
            trade["otm_pct"] = -otm_pct  # negate: ITM = negative in our system
            print(f"[BULLFLOW] OTM%: {otm_pct:+.1f}% (spot={spot} strike={strike_f})")
        except Exception as _oe:
            pass

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
        if r.status_code in (200, 201):
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
        if r.status_code in (200, 201):
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
            existing       = r.json().get("alerts",[])
            fc_alerts      = [a for a in existing if "FlowCheck" in a.get("alertName","")]
            force_recreate = os.environ.get("BULLFLOW_FORCE_RECREATE","").lower() == "true"

            if fc_alerts and not force_recreate:
                print(f"[BULLFLOW] Custom alert already exists ({len(fc_alerts)}) — skipping creation")
                return

            # Delete ALL existing FlowCheck alerts before recreating
            if fc_alerts and force_recreate:
                for old_alert in fc_alerts:
                    aid = old_alert.get("id") or old_alert.get("_id") or old_alert.get("alertId")
                    if aid:
                        try:
                            dr = requests.delete(
                                f"https://api.bullflow.io/v1/alerts/custom-alerts/{aid}?key={key}",
                                timeout=10
                            )
                            print(f"[BULLFLOW] Deleted old alert {aid}: {dr.status_code}")
                        except Exception as _de:
                            print(f"[BULLFLOW] Could not delete {aid}: {_de}")
    except Exception as e:
        print(f"[BULLFLOW] Could not check existing alerts: {e}")

    # ── FlowCheck High Conviction ─────────────────────────────────────────────
    print("[BULLFLOW] Creating custom alert filters...")

    # High Conviction — read from Railway env vars (BF_HC_*)
    hc_filters = {
        "premiumMin":    int(os.environ.get("BF_HC_MIN_PREMIUM",    "500000")),
        "dteMin":        int(os.environ.get("BF_HC_MIN_DTE",        "2")),
        "dteMax":        int(os.environ.get("BF_HC_MAX_DTE",        "30")),
        "minOTMPercent": float(os.environ.get("BF_HC_MIN_OTM",     "1")),
        "maxOTMPercent": float(os.environ.get("BF_HC_MAX_OTM",     "30")),
        "minSigScore":   float(os.environ.get("BF_HC_MIN_SIGSCORE", "0.51")),
        "maxIV":         float(os.environ.get("BF_HC_MAX_IV",       "100")),
        "quickFilters":  ["Stocks", "Sweeps", "AA", "Vol>OI"],
    }
    print(f"[BULLFLOW] High Conviction: ${hc_filters['premiumMin']:,}+ | "
          f"DTE {hc_filters['dteMin']}-{hc_filters['dteMax']} | "
          f"OTM {hc_filters['minOTMPercent']}-{hc_filters['maxOTMPercent']}% | "
          f"SigScore≥{hc_filters['minSigScore']} | IV≤{hc_filters['maxIV']}%")

    # ── ETFs-Order-Flow (SPY + QQQ) ───────────────────────────────────────────
    # ETF Order Flow — read from Railway env vars (BF_ETF_*)
    _etf_tickers = os.environ.get("BF_ETF_TICKERS", "SPY,QQQ").split(",")
    etf_filters = {
        "tickerAllowlist": [t.strip() for t in _etf_tickers],
        "premiumMin":    int(os.environ.get("BF_ETF_MIN_PREMIUM",    "300000")),
        "dteMin":        int(os.environ.get("BF_ETF_MIN_DTE",        "2")),
        "dteMax":        int(os.environ.get("BF_ETF_MAX_DTE",        "30")),
        "minOTMPercent": float(os.environ.get("BF_ETF_MIN_OTM",     "2")),
        "maxOTMPercent": float(os.environ.get("BF_ETF_MAX_OTM",     "45")),
        "minSigScore":   float(os.environ.get("BF_ETF_MIN_SIGSCORE", "0.51")),
        "maxIV":         float(os.environ.get("BF_ETF_MAX_IV",       "30")),
        "quickFilters":  ["Sweeps", "AA", "Unusual", "Vol>OI"],
    }
    print(f"[BULLFLOW] ETFs-Order-Flow: ${etf_filters['premiumMin']:,}+ | "
          f"Tickers: {','.join(etf_filters['tickerAllowlist'])} | "
          f"DTE {etf_filters['dteMin']}-{etf_filters['dteMax']} | "
          f"OTM {etf_filters['minOTMPercent']}-{etf_filters['maxOTMPercent']}% | "
          f"SigScore≥{etf_filters['minSigScore']} | IV≤{etf_filters['maxIV']}%")

    # ── Create/verify both alerts ─────────────────────────────────────────────
    _bk2 = os.environ.get("BULLFLOW_API_KEY","")
    _all_alerts = []
    try:
        import requests as _rqc2
        _er2 = _rqc2.get("https://api.bullflow.io/v1/alerts/custom-alerts",
                          params={"key": _bk2}, timeout=8)
        if _er2.status_code == 200:
            _all_alerts = _er2.json().get("alerts", [])
    except Exception as _ae:
        print(f"[BULLFLOW] Could not fetch existing alerts: {_ae}")

    _existing_names2 = [a.get("alertName","") for a in _all_alerts]

    for _aname, _afilters in [
        ("FlowCheck High Conviction", hc_filters),
        ("ETFs-Order-Flow",            etf_filters),
    ]:
        if _aname in _existing_names2:
            print(f"[BULLFLOW] '{_aname}' already exists — skipping")
        else:
            time.sleep(2)  # avoid 429 rate limit between creation calls
            _res = create_custom_alert(_aname, _afilters)
            if _res:
                print(f"[BULLFLOW] Created '{_aname}': {_res.get('id','?')}")
            else:
                print(f"[BULLFLOW] Failed to create '{_aname}'")

    # Pass High Conviction filters forward for legacy alert_name check
    filters = hc_filters.copy()

    # NOTE: SPX 0DTE alert removed — managed manually in Bullflow dashboard

    alert_name = filters.pop("name", "FlowCheck High Conviction")
    try:
        import requests as _rqc
        _ck = os.environ.get("BULLFLOW_API_KEY","")
        _er = _rqc.get("https://api.bullflow.io/v1/alerts/custom-alerts",
                        params={"key": _ck}, timeout=8)
        _alerts_raw = _er.json().get("alerts",[]) if _er.status_code == 200 else []
        _existing_names = [a.get("alertName","") for a in _alerts_raw]
        if alert_name in _existing_names:
            print(f"[BULLFLOW] Custom alert '{alert_name}' already exists — skipping")
        else:
            result = create_custom_alert(alert_name, filters)
            if result:
                print(f"[BULLFLOW] Created custom alert: {result}")
            else:
                print("[BULLFLOW] Could not create custom alert — using all algo alerts")
    except Exception as _ce:
        # Fallback: try to create anyway
        result = create_custom_alert(alert_name, filters)
        print(f"[BULLFLOW] Alert check failed, attempted create: {result}")


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

    _conn_state = {"last_connected": 0.0}  # Avoids global scoping issues
    retry_delay   = 30  # Start high — avoid rapid reconnects on deploy
    _seen_symbols = set()
    _seen_tickers = set()
    while True:
        try:
            _now_conn = time.time()
            if _conn_state["last_connected"] > 0 and (_now_conn - _conn_state["last_connected"]) < 30:
                _wait_gap = int(30 - (_now_conn - _conn_state["last_connected"]))
                print(f"[BULLFLOW] Waiting {_wait_gap}s — min reconnect gap")
                time.sleep(_wait_gap)
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
                _conn_state["last_connected"] = time.time()
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
                            retry_delay = 30  # Reset backoff after sustained connection
                            pass  # Expected every 10s

                        elif event == "alert":
                            alert_id   = msg.get("id","")
                            alert_data = msg.get("data",{})
                            alert_type = alert_data.get("alertType","")
                            alert_name = alert_data.get("alertName","")
                            symbol     = alert_data.get("symbol","")
                            premium    = float(alert_data.get("alertPremium",0) or 0)

                            # Route SPX/SPY to dedicated channel (algo + custom alerts)
                            _is_etf_alert  = alert_name in ("ETFs-Unusual-Flow","ETFs-Order-Flow")
                            _is_spx_alert  = (alert_name == "FlowCheck SPX 0DTE")
                            _is_spy_ticker = any(t in (symbol or "").upper() for t in ["SPY","SPXW","SPXL","SPXS"])
                            _is_qqq_ticker = any(t in (symbol or "").upper() for t in ["QQQ","SQQQ","TQQQ"])
                            _is_spx_ticker = _is_spy_ticker
                            # $1M min for algo alerts on SPX, $5M min for custom alert
                            _spx_min_prem  = 500_000
                            if (_is_etf_alert and premium >= 300_000) or (_is_spx_alert and premium >= 500_000) or ((_is_spy_ticker or _is_qqq_ticker) and premium >= 500_000):
                                try:
                                    from spx_flow import send_spx_alert as _ssa
                                    from fetcher import fetch_gex as _fgs
                                    import time as _ts2; _ts2.sleep(5)
                                    _ssa(alert_data, _fgs("SPY"))
                                    print(f"[SPX] Routed {symbol} ${premium:,.0f}")
                                except Exception as _e_spx:
                                    print(f"[SPX] {_e_spx}")
                                continue



                            premium    = alert_data.get("alertPremium",0)

                            # Allow all hours — Bullflow filters at source
                            # After-hours and pre-market flow is valid signal

                            print(f"[BULLFLOW] Alert: {alert_id} {alert_type} {symbol} "
                                  f"${premium:,.0f} [{alert_name}]")

                            trade = build_trade_from_alert(alert_data)
                            if not trade:
                                continue

                            # Deduplicate — same TICKER within 2 hours = skip
                            # Prevents SNOW 180C, SNOW 185C, SNOW 190C all firing
                            # Dedup on Bullflow alert ID first (catches double-sends)
                            alert_id_key = msg.get("id","")
                            if alert_id_key and alert_id_key in _seen_symbols:
                                print(f"[BULLFLOW] Alert ID dedup skip: {alert_id_key[:8]}... (duplicate)")
                                continue
                            if alert_id_key:
                                _seen_symbols.add(alert_id_key)

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

                            # ── Prefilter: ITM, sector, DTE, OTM ──────────
                            try:
                                from prefilter import prefilter as _pf
                                _pf_result = _pf(trade)
                                if not _pf_result.get("pass"):
                                    print(f"[BULLFLOW] {trade['ticker']} filtered: {_pf_result.get('reason','')}")
                                    continue
                            except Exception as _pfe:
                                print(f"[BULLFLOW] Prefilter error: {_pfe}")

                            # Build a synthetic tweet text for the pipeline
                            tweet = (f"${trade['ticker']} - ${premium:,.0f} "
                                    f"{trade['option_type'].title()} "
                                    f"{trade['strike']} [{alert_name}] via Bullflow")

                            # Process in a separate thread so SSE loop is never blocked
                            # Blocking here causes Bullflow to close the connection mid-stream
                            def _run_process():
                                import asyncio as _aio
                                try:
                                    _loop = _aio.new_event_loop()
                                    _loop.run_until_complete(process_fn(tweet, None, trade))
                                    _loop.close()
                                except RuntimeError as _pe:
                                    _msg = str(_pe)
                                    if "interpreter shutdown" in _msg or "cannot schedule" in _msg:
                                        return
                                    print(f"[BULLFLOW] process_alert RuntimeError: {_pe}")
                                except Exception as _pe:
                                    print(f"[BULLFLOW] process_alert error: {_pe}")
                            import threading as _thr_bf
                            _thr_bf.Thread(target=_run_process, daemon=True).start()

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
    # Delay startup to allow old container to shut down during rolling deploy
    import time as _st
    _st.sleep(20)  # 15s — enough for Railway to kill old container
    # Re-check lock after delay
    try:
        with open(_LOCK_FILE) as f:
            pid = int(f.read().strip())
        if pid != os.getpid():
            print(f"[BULLFLOW] Another process ({pid}) took over — aborting")
            return None
    except: pass
    print(f"[BULLFLOW] Lock confirmed for PID {os.getpid()} — starting stream")
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
        print(f"[BULLFLOW] Stream thread already running ({len(existing)}) — skipping duplicate")
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
