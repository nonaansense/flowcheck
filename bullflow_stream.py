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
from alert_toggles import is_enabled as _alert_on

ET = ZoneInfo("America/New_York")

_seen_symbols:    set  = set()   # module-level for dedup across calls
_seen_tickers:    set  = set()
_double_confirm:  dict = {}      # ticker_dir → {tape_ts, conviction_ts}
_ALERT_COOLDOWN:  dict = {}      # ticker_dir → last_alert_ts (feature 6)

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

    if alert_nm in ("Big_Money_Order_Flow", "ETFs-Order-Flow"):
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

    # ── Big_Money_Order_Flow ─────────────────────────────────────────────
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
        ("Big_Money_Order_Flow", hc_filters),
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

    alert_name = filters.pop("name", "Big_Money_Order_Flow")
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


def _handle_bullflow_alert(alert_data: dict, process_fn, send_sms_fn=None, alert_id: str = ""):
    """Process a single Bullflow alert event. Extracted to avoid Python 3.12 scoping issues."""
    import os, time
    alert_name = alert_data.get("alertName", "")
    alert_type = alert_data.get("alertType", "")
    symbol     = alert_data.get("symbol", "")
    premium    = float(alert_data.get("alertPremium", 0) or 0)
    alert_type = alert_data.get("alertType","")
    alert_name = alert_data.get("alertName","")
    symbol     = alert_data.get("symbol","")
    premium    = float(alert_data.get("alertPremium",0) or 0)
    
    # ── Tape Watcher routing ─────────────────────
    _tape_alert_names = [
        n.strip() for n in
        os.environ.get("TAPE_ALERT_NAMES","Retail_Order_Flow").split(",")
    ]
    if alert_name in _tape_alert_names:
        try:
            from tape_watcher import process_tape, build_tape_alert
            # Parse alert_data inline (no separate parse function)
            # Parse from OCC symbol O:TSLA260618C00437500 — ticker needed first for price fetch
            import re as _re_tp
            from datetime import datetime as _dt_tp
            _sym_raw  = alert_data.get("symbol","") or ""
            _sym_m = _re_tp.match(r"O:([A-Z]+)(\d{6})([CP])(\d+)", _sym_raw)
            if _sym_m:
                _tkr_tp   = _sym_m.group(1)
                _exp_dt   = _dt_tp.strptime("20" + _sym_m.group(2), "%Y%m%d")
                _exp_tp   = _exp_dt.strftime("%m/%d/%y")
                _dte_tp   = max(0, (_exp_dt.date() - _dt_tp.now().date()).days)
                _otype_tp = "call" if _sym_m.group(3) == "C" else "put"
                _strk_tp  = str(int(_sym_m.group(4)) / 1000)
            else:
                _tkr_tp   = _sym_raw
                _exp_tp   = alert_data.get("expiry","") or ""
                _dte_tp   = int(alert_data.get("dte") or 0)
                _otype_tp = alert_data.get("optionType","call") or "call"
                _strk_tp  = str(alert_data.get("strike","") or "")
            _fill_px  = float(alert_data.get("averageFillPrice",0) or 0)
            _prem_tp  = float(alert_data.get("alertPremium",0) or 0)
            _vol_tp   = int(alert_data.get("volume") or alert_data.get("vol") or 0)
            _oi_tp    = int(alert_data.get("openInterest") or alert_data.get("oi") or 1)
            _sweep_tp = "sweep" in (alert_type or "").lower()
            _otm_tp   = float(alert_data.get("percentOtm") or
                               alert_data.get("otmPercent") or 0)
            # Stock price — fetch live if Bullflow doesn't send it
            _stk_px   = float(alert_data.get("spotPrice") or
                              alert_data.get("stockPrice") or
                              alert_data.get("underlyingPrice") or 0)
            if not _stk_px and _tkr_tp:
                try:
                    from fetcher import fetch_price as _gcp, _price_cache as _pc
                    # Try cache first (populated by main scanner)
                    _cached = _pc.get(_tkr_tp)
                    if _cached:
                        _stk_px = float(_cached[0] or 0)
                    else:
                        _stk_px = float(_gcp(_tkr_tp) or 0)
                except Exception as _px_e:
                    print(f"[TAPE] Price fetch error for {_tkr_tp}: {_px_e}")
            # Earnings date
            _earn_str_tp = None
            try:
                from fetcher import fetch_earnings_date as _fed_tp
                _earn_res_tp = _fed_tp(_tkr_tp)
                if _earn_res_tp:
                    _earn_str_tp = _earn_res_tp[0]
                    _earn_timing = _earn_res_tp[3]
                    if _earn_timing: _earn_str_tp += f" {_earn_timing}"
            except: pass

            # IV rank
            _iv_pct_tp   = None
            _iv_rank_tp  = None
            _iv_note_tp  = None
            try:
                from fetcher import fetch_iv_from_tradier as _fiv_tp
                from signal_quality import check_iv_rank as _civr_tp
                _iv_pct_tp = _fiv_tp(_tkr_tp, _strk_tp, _otype_tp, _exp_tp)
                if _iv_pct_tp:
                    _ivr = _civr_tp(_tkr_tp, _iv_pct_tp)
                    _iv_rank_tp = _ivr.get("iv_rank")
                    _iv_note_tp = _ivr.get("note","")
            except: pass

            # ── Alert cooldown check ───────────────────────────────
            _cooldown_mins = int(os.environ.get("ALERT_COOLDOWN_MINUTES","10"))
            _cool_key      = f"{_tkr_tp}_{_otype_tp}"
            _cool_last     = _ALERT_COOLDOWN.get(_cool_key, 0)
            _cool_ok       = (time.time() - _cool_last) >= _cooldown_mins * 60

            # Skip 0DTE contracts — same-day expiry is a different game
            if _dte_tp == 0:
                print(f"[TAPE] Skipping 0DTE: {_tkr_tp} {_strk_tp} {_exp_tp}")
                # Fall through to normal FlowCheck scoring but skip tape/cluster/conviction
            else:
                pass  # DTE ok — continue processing below

            # Skip 0DTE for tape/cluster/conviction (still fall through to FlowCheck)
            _dte_ok = (_dte_tp >= 1)

            # Sell-the-news risk
            _stn_risk_tp = None
            try:
                from signal_quality import check_sell_the_news_risk as _stn_tp
                from economic_calendar import days_to_next_macro_event as _dtm_tp
                _days_earn2_tp = None
                _earn_past2_tp = True
                if _earn_res_tp and not _earn_res_tp[2]:
                    _earn_past2_tp = False
                    _days_earn2_tp = (_earn_res_tp[1].date() -
                                      datetime.now().date()).days
                _macro2_tp = _dtm_tp(max_days=14)
                _dmacro_tp, _mname_tp = _macro2_tp if _macro2_tp else (None, None)
                _stn_risk_tp = _stn_tp(_tkr_tp, _days_earn2_tp, _earn_past2_tp,
                                        _iv_rank_tp, _dmacro_tp, _mname_tp)
            except: pass

            # Recent IPO risk
            _ipo_risk_tp = None
            try:
                from signal_quality import check_recent_ipo_risk as _ipr_tp
                from fetcher import fetch_ipo_date as _fipo_tp
                _ipo_days_tp = None
                _ipo_res_tp = _fipo_tp(_tkr_tp)
                if _ipo_res_tp:
                    _ipo_days_tp = _ipo_res_tp[2]
                _ipo_risk_tp = _ipr_tp(_tkr_tp, [], _ipo_days_tp)
            except: pass

            # Float and short interest
            _float_tp = None
            _short_tp = None
            try:
                from fetcher import fetch_float_and_short as _ffs_tp
                _ffs     = _ffs_tp(_tkr_tp)
                _float_tp = _ffs.get("float_shares")
                _short_tp = _ffs.get("short_interest")
            except: pass

            # Recent news (last 48h) — Finnhub + Google News RSS merged
            _news_tp = []
            try:
                from news_check import fetch_combined_news as _frn_tp
                _news_tp = _frn_tp(_tkr_tp, hours=48, max_results=3)
            except: pass

            _parsed_tape = {
                "ticker":       _tkr_tp,
                "strike":       _strk_tp,
                "expiry":       _exp_tp,
                "option_type":  _otype_tp,
                "option_price": _fill_px,
                "premium":      _prem_tp,
                "fill_type":    "FULL_ASK",
                "is_sweep":     _sweep_tp,
                "stock_price":  _stk_px,
                "otm_pct":      _otm_tp,
                "dte":          _dte_tp,
                "vol_oi_ratio": round(_vol_tp / max(_oi_tp,1), 1),
                "earnings_str": _earn_str_tp,
                "iv_pct":       _iv_pct_tp,
                "iv_rank":      _iv_rank_tp,
                "iv_note":      _iv_note_tp,
                "news":         _news_tp,
                "float_shares": _float_tp,
                "short_interest": _short_tp,
                "stn_risk":     _stn_risk_tp.get("risk") if _stn_risk_tp else None,
                "stn_note":     _stn_risk_tp.get("note") if _stn_risk_tp else None,
                "ipo_note":     _ipo_risk_tp.get("note") if _ipo_risk_tp else None,
            }
            print(f"[TAPE] Processing: {_tkr_tp} {_strk_tp} {_exp_tp} @ ${_fill_px:.2f}")
            _tape_bot  = os.environ.get("TELEGRAM_BOT_TOKEN","")
            _tape_chat = (os.environ.get("TELEGRAM_TRADE_CHAT_ID","")
                          or os.environ.get("TELEGRAM_CHAT_ID",""))
            _all_chat_tape = os.environ.get("TELEGRAM_ALL_CHAT_ID","")

            # Skip tape/cluster/conviction for 0DTE contracts
            if not _dte_ok:
                return

            # Exact-match repeat buyer detection (same strike+expiry)
            _tape_result = process_tape(_parsed_tape, alert_name)
            if _tape_result:
                # Always track for double-confirmation regardless of alert toggle
                _tw_dir = "call" if "call" in str(_tape_result.get("option_type","call")).lower() else "put"
                _tw_dk  = f"{_tape_result['ticker']}_{_tw_dir}"
                _double_confirm.setdefault(_tw_dk, {})["tape_ts"] = time.time()
                # Always update cooldown so dedup works even when alerts are off
                _ALERT_COOLDOWN[_cool_key] = time.time()

                # Send Telegram — gated on cooldown and alert toggle
                if not _cool_ok:
                    print(f"[COOLDOWN] {_tkr_tp} {_otype_tp} — tape alert suppressed "
                          f"(last alert {int(time.time()-_cool_last)//60}min ago)")
                elif not _alert_on("tape"):
                    print(f"[TOGGLES] tape alerts disabled — Telegram suppressed, state updated")
                elif _tape_bot and _tape_chat:
                    from sms import send_telegram as _st_tape
                    _tape_msg = build_tape_alert(_tape_result, alert_name)
                    _st_tape(_tape_msg, _tape_bot, _tape_chat)
                    if _all_chat_tape:
                        _st_tape(_tape_msg, _tape_bot, _all_chat_tape)
                    print(f"[TAPE] ✅ Alert sent: "
                          f"{_tape_result['ticker']} rule={_tape_result.get('rule','?')}")
                    # Entry reminder
                    try:
                        from main import scheduler, send_entry_reminder
                        from datetime import datetime as _dtr, timedelta as _tdr
                        _rem_mins = int(os.environ.get("ENTRY_REMINDER_MINUTES","10"))
                        _rem_time = _dtr.now() + _tdr(minutes=_rem_mins)
                        _rem_px   = _tape_result.get("stock_px",0) or _tape_result.get("big_money",[{}])[0].get("stock_px",0) if _tape_result.get("big_money") else 0
                        scheduler.add_job(
                            lambda t=_tape_result["ticker"],d=_tw_dir,px=_rem_px,
                                   b=_tape_bot,c=_tape_chat: send_entry_reminder(
                                       t,d,f"Tape {_tape_result.get('rule','')}",px,b,c),
                            "date", run_date=_rem_time,
                            id=f"rem_{_tape_result['ticker']}_{int(_dtr.now().timestamp())}",
                            max_instances=1)
                    except Exception as _re: pass

            # Broad ticker-level cluster detection (different strikes/expiries,
            # same direction, accumulating within a rolling window)
            try:
                from ticker_cluster import process_cluster, build_cluster_alert
                _cluster_result = process_cluster(_parsed_tape)
                if _cluster_result:
                    if not _alert_on("cluster"):
                        print(f"[TOGGLES] cluster alerts disabled")
                    elif _tape_bot and _tape_chat:
                        from sms import send_telegram as _st_cl
                        _cluster_msg = build_cluster_alert(_cluster_result, alert_name)
                        _st_cl(_cluster_msg, _tape_bot, _tape_chat)
                        if _all_chat_tape:
                            _st_cl(_cluster_msg, _tape_bot, _all_chat_tape)
                        print(f"[CLUSTER] ✅ Alert sent: "
                              f"{_cluster_result['ticker']} "
                              f"{_cluster_result['distinct_count']} contracts")
            except Exception as _ce:
                print(f"[CLUSTER] Error: {_ce}")
        except Exception as _te:
            print(f"[TAPE] Error: {_te}")
        # Still falls through to normal FlowCheck processing

    # ── Cross-filter conviction detector ─────────────────────────────────
    # Runs on EVERY alert from either filter — big money or retail/tape.
    # Fires when BIG_MONEY_MIN + RETAIL_MIN thresholds both met, same direction.
    try:
        from cross_filter_conviction import process_conviction, build_conviction_alert
        _cfc_alert_names = [
            os.environ.get("CONVICTION_BIG_MONEY_FILTER", "Big_Money_Order_Flow"),
            os.environ.get("CONVICTION_RETAIL_FILTER", "Retail_Order_Flow"),
        ]
        if alert_name in _cfc_alert_names:
            # Re-use _parsed_tape if available (tape watcher block populated it)
            # otherwise build a minimal dict from raw alert_data
            _cfc_parsed = {}
            try:
                _cfc_parsed = _parsed_tape  # set by tape watcher block above
            except NameError:
                import re as _re_cfc
                from datetime import datetime as _dt_cfc
                _sym_cfc = alert_data.get("symbol","") or ""
                _m_cfc   = _re_cfc.match(r"O:([A-Z]+)(\d{6})([CP])(\d+)", _sym_cfc)
                if _m_cfc:
                    _exp_cfc = _dt_cfc.strptime("20"+_m_cfc.group(2),"%Y%m%d")
                    _cfc_parsed = {
                        "ticker":      _m_cfc.group(1),
                        "strike":      str(int(_m_cfc.group(4))/1000),
                        "expiry":      _exp_cfc.strftime("%m/%d/%y"),
                        "option_type": "call" if _m_cfc.group(3)=="C" else "put",
                        "option_price": float(alert_data.get("averageFillPrice",0) or 0),
                        "premium":     float(alert_data.get("alertPremium",0) or 0),
                        "stock_price": float(alert_data.get("spotPrice") or
                                             alert_data.get("stockPrice") or 0),
                        "otm_pct":     float(alert_data.get("percentOtm") or 0),
                        "dte":         max(0,(_exp_cfc.date()-_dt_cfc.now().date()).days),
                        "is_sweep":    "sweep" in (alert_type or "").lower(),
                    }

            if _cfc_parsed.get("ticker"):
                _cfc_result = process_conviction(_cfc_parsed, alert_name)
                if _cfc_result:
                    # Always track for double-confirmation regardless of toggle
                    _dc_key = f"{_cfc_result['ticker']}_{_cfc_result['direction']}"
                    _double_confirm.setdefault(_dc_key, {})["conviction_ts"] = __import__('time').time()

                    # Send Telegram — gated on toggle only
                    _cfc_bot  = os.environ.get("TELEGRAM_BOT_TOKEN","")
                    _cfc_chat = (os.environ.get("TELEGRAM_TRADE_CHAT_ID","") or
                                 os.environ.get("TELEGRAM_CHAT_ID",""))
                    _cfc_type = "bm_auto" if _cfc_result.get("bm_auto") else "conviction"
                    if not _alert_on(_cfc_type):
                        print(f"[TOGGLES] {_cfc_type} disabled — Telegram suppressed, state updated")
                    elif _cfc_bot and _cfc_chat:
                        from sms import send_telegram as _st_cfc
                        _cfc_msg = build_conviction_alert(_cfc_result)
                        _st_cfc(_cfc_msg, _cfc_bot, _cfc_chat)
                        _all_chat_cfc = os.environ.get("TELEGRAM_ALL_CHAT_ID","")
                        if _all_chat_cfc:
                            _st_cfc(_cfc_msg, _cfc_bot, _all_chat_cfc)
                        print(f"[CONVICTION] ✅ Alert sent: "
                              f"{_cfc_result['ticker']} {_cfc_result['sentiment']}")
    except Exception as _cfc_e:
        print(f"[CONVICTION] Error: {_cfc_e}")

    # ── Double confirmation escalation ─────────────────────────────────
    # Fires when BOTH tape watcher AND cross-filter conviction fire on
    # same ticker+direction within DOUBLE_CONFIRM_WINDOW_HOURS
    try:
        _dc_window = float(os.environ.get("DOUBLE_CONFIRM_WINDOW_HOURS","6.5")) * 3600
        _now_dc    = __import__('time').time()
        for _dc_k, _dc_v in list(_double_confirm.items()):
            _tape_t = _dc_v.get("tape_ts", 0)
            _conv_t = _dc_v.get("conviction_ts", 0)
            _last_esc = _dc_v.get("escalated_ts", 0)
            if (_tape_t and _conv_t
                    and abs(_tape_t - _conv_t) <= _dc_window
                    and _last_esc < min(_tape_t, _conv_t)):
                _double_confirm[_dc_k]["escalated_ts"] = _now_dc
                _dc_ticker = _dc_k.rsplit("_",1)[0]
                _dc_dir    = _dc_k.rsplit("_",1)[1]
                _dc_emoji  = "📈" if _dc_dir == "call" else "📉"
                _dc_lines = [
                    f"🔥🔥 DOUBLE CONFIRMATION: ${_dc_ticker}",
                    f"━━━ {_dc_emoji} TAPE + CONVICTION BOTH ACTIVE ━━━",
                    "",
                    "Both the tape watcher (big money footprint) AND",
                    "cross-filter conviction (BM + retail threshold) fired",
                    f"on ${_dc_ticker} {_dc_dir}s within the same session.",
                    "",
                    "💡 Highest-conviction setup — two independent systems agree",
                    f"📈 https://www.tradingview.com/chart/?symbol={_dc_ticker}",
                ]
                _dc_msg = "\n".join(_dc_lines)
                _dc_bot  = os.environ.get("TELEGRAM_BOT_TOKEN","")
                _dc_chat = (os.environ.get("TELEGRAM_TRADE_CHAT_ID","") or
                            os.environ.get("TELEGRAM_CHAT_ID",""))
                if not _alert_on("double"):
                    print(f"[TOGGLES] double confirmation disabled — skipping")
                elif _dc_bot and _dc_chat:
                    from sms import send_telegram as _sms_dc
                    _sms_dc(_dc_msg, _dc_bot, _dc_chat)
                    print(f"[ESCALATION] 🔥🔥 Double confirmation: {_dc_ticker} {_dc_dir}")
    except Exception as _dce:
        print(f"[ESCALATION] Error: {_dce}")

    # ── Straddle / Strangle detector ──────────────
    # Fires when both calls AND puts appear on same ticker within the window
    try:
        from spread_detector import process_straddle, build_straddle_alert
        _straddle_names = [
            os.environ.get("CONVICTION_BIG_MONEY_FILTER", "Big_Money_Order_Flow"),
            os.environ.get("CONVICTION_RETAIL_FILTER", "Retail_Order_Flow"),
        ]
        if alert_name in _straddle_names:
            _st_parsed = {}
            try:
                _st_parsed = _parsed_tape
            except NameError:
                _st_parsed = {
                    "ticker":      alert_data.get("symbol","")[:5] or "",
                    "option_type": "call",
                    "expiry":      "",
                    "premium":     float(alert_data.get("alertPremium",0) or 0),
                }
            _st_result = process_straddle(_st_parsed, alert_name)
            if _st_result:
                _st_bot  = os.environ.get("TELEGRAM_BOT_TOKEN","")
                _st_chat = (os.environ.get("TELEGRAM_TRADE_CHAT_ID","") or
                            os.environ.get("TELEGRAM_CHAT_ID",""))
                if not _alert_on("straddle"):
                    print(f"[TOGGLES] straddle alerts disabled — skipping")
                elif _st_bot and _st_chat:
                    from sms import send_telegram as _sms_st
                    _sms_st(build_straddle_alert(_st_result), _st_bot, _st_chat)
                    print(f"[STRADDLE] ✅ Alert sent: {_st_result['ticker']}")
    except Exception as _ste:
        print(f"[STRADDLE] Error: {_ste}")

    # ── Dark pool routing ────────────────────────
    # Handles alerts from a "Dark_Pool_Order_Flow" Bullflow filter if configured.
    # Dark pool prints on the underlying often precede options flow by hours.
    # Create this filter manually on Bullflow dashboard (dark pool block trades).
    _dark_pool_name = os.environ.get("DARK_POOL_FILTER_NAME","Dark_Pool_Order_Flow")
    if alert_name == _dark_pool_name:
        try:
            _dp_ticker  = alert_data.get("symbol","") or ""
            _dp_premium = float(alert_data.get("alertPremium",0) or 0)
            _dp_px      = float(alert_data.get("spotPrice") or alert_data.get("price",0) or 0)
            _dp_bot     = os.environ.get("TELEGRAM_BOT_TOKEN","")
            _dp_chat    = (os.environ.get("TELEGRAM_TRADE_CHAT_ID","") or
                           os.environ.get("TELEGRAM_CHAT_ID",""))
            if _dp_ticker and _dp_bot and _dp_chat and _dp_premium >= 500000:
                _dp_prem_s = (f"${_dp_premium/1_000_000:.1f}M"
                              if _dp_premium >= 1_000_000
                              else f"${_dp_premium/1_000:.0f}K")
                _dp_time   = __import__('datetime').datetime.now(
                    __import__('zoneinfo').ZoneInfo("America/New_York")
                ).strftime("%-I:%M %p")
                _dp_lines = [
                    f"🌑 DARK POOL PRINT: ${_dp_ticker}",
                    "━━━ Large block trade off-exchange ━━━",
                    "",
                    f"Size: {_dp_prem_s} | Stock: ${_dp_px:.2f} | {_dp_time}",
                    "",
                    "💡 Dark pool prints often precede options flow by hours"
                    " — watch for follow-on call/put activity",
                    f"📈 https://www.tradingview.com/chart/?symbol={_dp_ticker}",
                ]
                _dp_msg = "\n".join(_dp_lines)
                if not _alert_on("darkpool"):
                    print(f"[TOGGLES] darkpool alerts disabled — skipping")
                else:
                    from sms import send_telegram as _sms_dp
                    _sms_dp(_dp_msg, _dp_bot, _dp_chat)
                    print(f"[DARKPOOL] ✅ Alert sent: {_dp_ticker} {_dp_prem_s}")
        except Exception as _dpe:
            print(f"[DARKPOOL] Error: {_dpe}")

    # ── Sector clustering ─────────────────────────
    try:
        from sector_cluster import process_sector, build_sector_alert
        _all_filter_names = [
            os.environ.get("CONVICTION_BIG_MONEY_FILTER","Big_Money_Order_Flow"),
            os.environ.get("CONVICTION_RETAIL_FILTER","Retail_Order_Flow"),
        ]
        if alert_name in _all_filter_names:
            _sc_parsed = {}
            try: _sc_parsed = _parsed_tape
            except NameError:
                _sc_parsed = {"ticker":"","option_type":"call","premium":0.0}
            _sc_result = process_sector(_sc_parsed)
            if _sc_result:
                _sc_bot  = os.environ.get("TELEGRAM_BOT_TOKEN","")
                _sc_chat = (os.environ.get("TELEGRAM_TRADE_CHAT_ID","") or
                            os.environ.get("TELEGRAM_CHAT_ID",""))
                if not _alert_on("sector"):
                    print(f"[TOGGLES] sector alerts disabled — skipping")
                elif _sc_bot and _sc_chat:
                    from sms import send_telegram as _sms_sc
                    _sms_sc(build_sector_alert(_sc_result), _sc_bot, _sc_chat)
                    print(f"[SECTOR] ✅ Alert sent: {_sc_result['sector']}")
    except Exception as _sce:
        print(f"[SECTOR] Error: {_sce}")

    # ── Expiry clustering ─────────────────────────
    # Fires when 4+ tickers buy same expiry date = event-driven bet
    try:
        from expiry_cluster import process_expiry, build_expiry_alert
        _ec_parsed = {}
        try: _ec_parsed = _parsed_tape
        except NameError:
            _ec_parsed = {"ticker":"","option_type":"call","expiry":"","strike":"","premium":0.0}
        _ec_result = process_expiry(_ec_parsed, alert_name)
        if _ec_result:
            _ec_bot  = os.environ.get("TELEGRAM_BOT_TOKEN","")
            _ec_chat = (os.environ.get("TELEGRAM_TRADE_CHAT_ID","") or
                        os.environ.get("TELEGRAM_CHAT_ID",""))
            if not _alert_on("expiry"):
                print(f"[TOGGLES] expiry alerts disabled — skipping")
            elif _ec_bot and _ec_chat:
                from sms import send_telegram as _sms_ec
                _sms_ec(build_expiry_alert(_ec_result), _ec_bot, _ec_chat)
                print(f"[EXPIRY] ✅ Alert sent: {_ec_result['expiry']}")
    except Exception as _ece:
        print(f"[EXPIRY] Error: {_ece}")

    # ── Pair flow rapid accumulation detector ────
    try:
        from pair_flow_tracker import process_pair_flow, build_pair_alert
        # Always parse directly from alert_data — never depends on _parsed_tape
        # since Pair_of_3_in_5_mins is a separate filter from tape/conviction filters
        _pf_sym  = alert_data.get("symbol","")
        _pf_occ  = None
        try:
            import re as _pf_re
            _pf_m = _pf_re.search(r'O:([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d+)', _pf_sym)
            if _pf_m:
                _pf_stk  = int(_pf_m.group(6)) / 1000.0
                _pf_occ  = {
                    "ticker":      _pf_m.group(1),
                    "option_type": "call" if _pf_m.group(5) == "C" else "put",
                    "strike":      str(int(_pf_stk)) if _pf_stk == int(_pf_stk) else f"{_pf_stk:.1f}",
                    "expiry":      f"{_pf_m.group(3)}/{_pf_m.group(4)}/{_pf_m.group(2)}",
                    "dte":         0,
                }
        except Exception: pass

        _pf_parsed = {
            "ticker":       (_pf_occ["ticker"]       if _pf_occ else _pf_sym.split(":")[0][:10]),
            "option_type":  (_pf_occ["option_type"]  if _pf_occ else "call"),
            "strike":       (_pf_occ["strike"]        if _pf_occ else ""),
            "expiry":       (_pf_occ["expiry"]        if _pf_occ else ""),
            "dte":          (_pf_occ["dte"]           if _pf_occ else 0),
            "option_price": float(alert_data.get("averageFillPrice") or
                                  alert_data.get("tradePrice")       or
                                  alert_data.get("alertPrice")        or 0),
            "premium":      float(alert_data.get("alertPremium") or 0),
            "is_sweep":     str(alert_data.get("alertFillType","")).upper() in ("FULL_ASK","AA"),
            "stock_price":  float(alert_data.get("stockPrice") or 0),
        }
        _pf_result = process_pair_flow(_pf_parsed, alert_name)
        if _pf_result:
            # Always track state — only gate the Telegram send
            if not _alert_on("pair_flow"):
                print(f"[TOGGLES] pair_flow disabled — Telegram suppressed, state updated")
            else:
                _pf_bot  = os.environ.get("TELEGRAM_BOT_TOKEN","")
                _pf_chat = (os.environ.get("TELEGRAM_TRADE_CHAT_ID","") or
                            os.environ.get("TELEGRAM_CHAT_ID",""))
                if _pf_bot and _pf_chat:
                    from sms import send_telegram as _sms_pf
                    _sms_pf(build_pair_alert(_pf_result), _pf_bot, _pf_chat)
                    print(f"[PAIR] ✅ Alert sent: {_pf_result['ticker']} "
                          f"{_pf_result['direction']} x{_pf_result['count']}")
    except Exception as _pfe:
        print(f"[PAIR] Error: {_pfe}")

    # ── SPX block trade repeat detector ──────────
    try:
        from spx_block_tracker import process_spx_block, build_spx_alert
        _spx_parsed = {}
        try: _spx_parsed = _parsed_tape
        except NameError:
            _spx_parsed = {
                "ticker":      alert_data.get("symbol","")[:5] or "",
                "strike":      "",
                "expiry":      "",
                "option_type": "call",
                "option_price":0.0,
                "premium":     float(alert_data.get("alertPremium",0) or 0),
                "is_sweep":    False,
                "dte":         0,
            }
        _spx_result = process_spx_block(_spx_parsed, alert_name)
        if _spx_result and _alert_on("spx_block"):
            _spx_bot  = os.environ.get("TELEGRAM_BOT_TOKEN","")
            _spx_chat = (os.environ.get("TELEGRAM_SPX_CHAT_ID","") or
                         os.environ.get("TELEGRAM_TRADE_CHAT_ID","") or
                         os.environ.get("TELEGRAM_CHAT_ID",""))
            if _spx_bot and _spx_chat:
                from sms import send_telegram as _sms_spx
                _sms_spx(build_spx_alert(_spx_result), _spx_bot, _spx_chat)
                print(f"[SPX] ✅ Alert sent: {_spx_result['key']}")
        elif _spx_result:
            print(f"[TOGGLES] spx_block disabled — Telegram suppressed, state updated")
    except Exception as _spxe:
        print(f"[SPX] Error: {_spxe}")

    # ── Repeater channel routing ──────────────────
    # "Urgent Repeater" and "Repeat Buyer" with DTE ≤ 14
    _is_repeater = any(w in alert_name for w in
                       ("Urgent Repeater","Bullflow Repeater",
                        "Repeat Buyer","Repeater"))
    if _is_repeater:
        try:
            # Parse inline — parse_bullflow_alert doesn't exist as a standalone fn
            _sym_r2  = alert_data.get('symbol','') or ''
            import re as _re_rp
            _tk_m2   = _re_rp.match(r'O:([A-Z]+)[0-9]', _sym_r2)
            _parsed_rep = {
                'ticker':      _tk_m2.group(1) if _tk_m2 else _sym_r2,
                'strike':      str(alert_data.get('strike','') or ''),
                'expiry':      alert_data.get('expiry','') or alert_data.get('expirationDate',''),
                'option_type': alert_data.get('optionType','call') or 'call',
                'dte':         int(alert_data.get('dte') or 0),
                'fill_type':   'FULL_ASK',
                'is_sweep':    'sweep' in (alert_data.get('alertType','') or '').lower(),
                'stock_price': float(alert_data.get('spotPrice') or alert_data.get('stockPrice') or 0),
                'otm_pct':     float(alert_data.get('percentOtm') or alert_data.get('otmPercent') or 0),
            }
            _dte_rep    = _parsed_rep.get("dte", 99) or 99
            _rep_chat   = os.environ.get("TELEGRAM_REPEATER_CHAT_ID","")
            _rep_bot    = os.environ.get("TELEGRAM_BOT_TOKEN","")
            if _rep_chat and _rep_bot and 0 < _dte_rep <= 14:
                from sms import send_telegram as _st_rep
                _tkr_rep   = _parsed_rep.get("ticker","?")
                _strk_rep  = _parsed_rep.get("strike","?")
                _exp_rep   = _parsed_rep.get("expiry","?")
                _otype_rep = (_parsed_rep.get("option_type","call") or "call")[0].upper()
                _prem_rep  = float(alert_data.get("alertPremium",0) or 0)
                _fill_rep  = _parsed_rep.get("fill_type","")
                _px_rep    = float(alert_data.get("spotPrice") or
                                   alert_data.get("stockPrice") or 0)
                _otm_rep   = float(alert_data.get("percentOtm") or
                                   alert_data.get("otmPercent") or 0)
                _prem_str  = (f"${_prem_rep/1_000_000:.1f}M"
                              if _prem_rep >= 1_000_000
                              else f"${_prem_rep/1_000:.0f}K")
                _rep_msg = (
                    f"🔁 {alert_name.upper()}: ${_tkr_rep}\n"
                    f"{_strk_rep}{_otype_rep} {_exp_rep} | {_dte_rep}d DTE\n"
                    f"{_prem_str} {_fill_rep}"
                    f"{' ⚡ SWEEP' if _parsed_rep.get('is_sweep') else ''}\n"
                    f"Stock: ${_px_rep:.2f}"
                    f"{f' | OTM {_otm_rep:.1f}%' if _otm_rep else ''}\n"
                    f"⚠️ Short dated — consider spreads\n"
                    f"📈 https://www.tradingview.com/chart/?symbol={_tkr_rep}"
                )
                _st_rep(_rep_msg, _rep_bot, _rep_chat)
                print(f"[REPEATER] 🔁 {_tkr_rep} {alert_name} "
                      f"{_dte_rep}d → repeater channel")
            elif _dte_rep > 14:
                print(f"[REPEATER] Skipped {alert_name} "
                      f"— {_dte_rep}d DTE > 14d")
        except Exception as _re:
            print(f"[REPEATER] Error: {_re}")
        # Still falls through to normal processing below
    
    # ── SPX/ETF channel routing ───────────────────
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
        return
    
    
    
    premium    = alert_data.get("alertPremium",0)
    
    # Allow all hours — Bullflow filters at source
    # After-hours and pre-market flow is valid signal
    
    print(f"[BULLFLOW] Alert: {alert_id} {alert_type} {symbol} "
          f"${premium:,.0f} [{alert_name}]")
    
    trade = build_trade_from_alert(alert_data)
    if not trade:
        return
    
    # Deduplicate — same TICKER within 2 hours = skip
    # Prevents SNOW 180C, SNOW 185C, SNOW 190C all firing
    # Dedup on Bullflow alert ID first (catches double-sends)
    alert_id_key = alert_id or alert_data.get("alertId","") or alert_data.get("id","")
    if alert_id_key and alert_id_key in _seen_symbols:
        print(f"[BULLFLOW] Alert ID dedup skip: {alert_id_key[:8]}... (duplicate)")
        return
    if alert_id_key:
        _seen_symbols.add(alert_id_key)
    
    ticker_dedup_key = f"{trade['ticker']}_{int(float(alert_data.get('timestamp',0)) // 7200)}"
    symbol_dedup_key = f"{symbol}_{int(float(alert_data.get('timestamp',0)) // 60)}"
    if ticker_dedup_key in _seen_tickers:
        print(f"[BULLFLOW] Ticker dedup skip: {trade['ticker']} (already processed in last 2h)")
        return
    if symbol_dedup_key in _seen_symbols:
        print(f"[BULLFLOW] Symbol dedup skip: {symbol}")
        return
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
        return
    
    # Skip Grenade trades unless ALLOW_GRENADES=true
    allow_grenades = os.environ.get("ALLOW_GRENADES","").lower() == "true"
    if not allow_grenades and "grenade" in alert_name.lower() and trade.get("dte",99) <= 7:
        print(f"[BULLFLOW] Grenade skip (DTE≤7): {symbol}")
        return
    
    # Skip splits (multi-exchange non-sweep orders)
    if "split" in alert_name.lower() and "sweep" not in alert_name.lower():
        print(f"[BULLFLOW] Split order skip: {symbol}")
        return
    
    # Premium sanity check against Railway variable
    min_prem = float(os.environ.get("FILTER_MIN_PREMIUM","500000"))
    if premium < min_prem:
        print(f"[BULLFLOW] Premium ${premium:,.0f} < ${min_prem:,.0f} skip")
        return

    # Minimum 1 day to expiration — 0DTE is a different trading strategy
    _main_dte = int(trade.get("dte", 1) if trade else 1)
    _min_dte  = int(os.environ.get("FILTER_MIN_DTE", "1"))
    if _main_dte < _min_dte:
        print(f"[BULLFLOW] DTE {_main_dte} < {_min_dte} skip (0DTE filtered)")
        return
    
    # ── Prefilter: ITM, sector, DTE, OTM ──────────
    try:
        from prefilter import prefilter as _pf
        _pf_result = _pf(trade)
        if not _pf_result.get("pass"):
            print(f"[BULLFLOW] {trade['ticker']} filtered: {_pf_result.get('reason','')}")
            return
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
                            alert_data2 = msg.get("data", {})
                            _alert_id2   = msg.get("id","")
                            try:
                                _handle_bullflow_alert(alert_data2, process_fn, send_sms_fn, _alert_id2)
                            except Exception as _hbe:
                                print(f"[BULLFLOW] Alert handler error: {_hbe}")
                                import traceback
                                traceback.print_exc()
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
