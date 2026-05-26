import re
from datetime import datetime

MONTH_MAP = {
    "jan":"01","feb":"02","mar":"03","apr":"04","may":"05","jun":"06",
    "jul":"07","aug":"08","sep":"09","oct":"10","nov":"11","dec":"12",
    "january":"01","february":"02","march":"03","april":"04","june":"06",
    "july":"07","august":"08","september":"09","october":"10",
    "november":"11","december":"12"
}

def _parse_size(s: str):
    if not s: return None
    s = str(s).replace(",","").strip()
    try:
        if s.upper().endswith("K"): return int(float(s[:-1]) * 1000)
        if s.upper().endswith("M"): return int(float(s[:-1]) * 1000000)
        return int(float(s))
    except:
        return None

def parse_tweet(text: str):
    if not text:
        return None

    # Ticker
    ticker = None
    for pat in [r'\$([A-Z]{1,5})\b', r'\b([A-Z]{2,5})\s+\d+\s*(?:Call|Put)']:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            ticker = m.group(1).upper()
            break

    # Option type
    option_type = "put" if re.search(r'\bput\b', text, re.IGNORECASE) else "call"

    # Strike
    strike = None
    for pat in [r'(\d+(?:\.\d+)?)\s*(?:Call|Put)\b', r'(?:Call|Put)\s+(\d+(?:\.\d+)?)']:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            strike = m.group(1)
            break

    # Expiry — try several formats
    expiry = expiry_raw = expiry_short = None

    # MM/DD/YY or Exp. MM/DD/YY
    m = re.search(r'(?:[Ee]xp\.?\s+)?(\d{1,2})/(\d{1,2})/(\d{2,4})', text)
    if m:
        mo, dy, yr = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
        yr = "20"+yr if len(yr)==2 else yr
        expiry_raw   = f"{mo}/{dy}/{yr[2:]}"
        expiry_short = f"{mo}/{dy}"
        try: expiry = datetime(int(yr),int(mo),int(dy)).strftime("%B %d, %Y")
        except: expiry = f"{mo}/{dy}/{yr}"

    # Month DD, YYYY
    if not expiry:
        m = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})', text, re.IGNORECASE)
        if m:
            mo = MONTH_MAP.get(m.group(1).lower()[:3], "01")
            dy, yr = m.group(2).zfill(2), m.group(3)
            expiry_raw   = f"{mo}/{dy}/{yr[2:]}"
            expiry_short = f"{mo}/{dy}"
            expiry       = f"{m.group(1).capitalize()} {dy}, {yr}"

    # Premium
    premium = None
    m = re.search(r'\$([0-9]+(?:\.[0-9]+)?)\s*([KkMm])', text)
    if m:
        val  = float(m.group(1))
        unit = m.group(2).upper()
        premium = int(val * (1000 if unit=="K" else 1000000))

    # OTM
    otm = None
    m = re.search(r'OTM[:\s]+([0-9]+(?:\.[0-9]+)?)%?', text, re.IGNORECASE)
    if m: otm = float(m.group(1))

    # OI
    oi = None
    m = re.search(r'\bOI[:\s]+([0-9.,]+[KkMm]?)', text, re.IGNORECASE)
    if m: oi = _parse_size(m.group(1))

    # Bid/Ask price
    bid = ask = None
    m = re.search(r'\bBid[:\s]+\$?([0-9.]+)', text, re.IGNORECASE)
    if m: bid = float(m.group(1))
    m = re.search(r'\bAsk[:\s]+\$?([0-9.]+)', text, re.IGNORECASE)
    if m: ask = float(m.group(1))

    # Ask/Bid/Mid size
    ask_size = bid_size = mid_size = None
    m = re.search(r'Ask\s*Size[:\s]+([0-9.,]+[KkMm]?)', text, re.IGNORECASE)
    if m: ask_size = _parse_size(m.group(1))
    m = re.search(r'Bid\s*Size[:\s]+([0-9.,]+[KkMm]?)', text, re.IGNORECASE)
    if m: bid_size = _parse_size(m.group(1))
    m = re.search(r'Mid\s*Size[:\s]+([0-9.,]+[KkMm]?)', text, re.IGNORECASE)
    if m: mid_size = _parse_size(m.group(1))

    # Multi%
    multi_pct = None
    m = re.search(r'Multi[:\s]+([0-9.]+)%?', text, re.IGNORECASE)
    if m: multi_pct = float(m.group(1))

    # Avg Fill
    option_price = None
    m = re.search(r'(?:Avg\.?\s*Fill|Avg\s*Price)[:\s]+\$?([0-9.]+)', text, re.IGNORECASE)
    if m: option_price = float(m.group(1))

    # Volume
    volume = None
    m = re.search(r'\bVol[:\s]+([0-9.,]+[KkMm]?)', text, re.IGNORECASE)
    if m: volume = _parse_size(m.group(1))

    if not ticker:
        return None

    return {
        "ticker":        ticker,
        "strike":        strike,
        "option_type":   option_type,
        "expiry":        expiry,
        "expiry_short":  expiry_short,
        "expiry_raw":    expiry_raw,
        "premium":       premium,
        "otm":           otm,
        "open_interest": oi,
        "bid":           bid,
        "ask":           ask,
        "ask_size":      ask_size,
        "bid_size":      bid_size,
        "mid_size":      mid_size,
        "multi_pct":     multi_pct,
        "option_price":  option_price,
        "volume":        volume,
        "raw_text":      text,
    }
