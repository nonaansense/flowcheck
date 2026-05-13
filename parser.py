import re

def parse_tweet(text):
    """
    Parse a flow alert tweet into structured trade data.
    Handles formats like:
    - "$ORCL — $384K Call buyer ORCL 207.5 Call Exp. 05/22/26"
    - "$QCOM $2.3 million into these calls QCOM 230 Call Exp. 06/05/26"
    - "TSLA 395 Call Exp. 05/29/26 +$2.30 (+42.59%)"
    """

    if not text:
        return None

    text_upper = text.upper()

    # Extract ticker — look for $TICKER or standalone known pattern
    ticker = None
    ticker_match = re.search(r'\$([A-Z]{1,5})\b', text)
    if ticker_match:
        ticker = ticker_match.group(1)
    else:
        # Try to find ticker before a number (e.g. "ORCL 207.5")
        ticker_match = re.search(r'\b([A-Z]{2,5})\s+\d+', text_upper)
        if ticker_match:
            ticker = ticker_match.group(1)

    if not ticker:
        return None

    # Skip non-trade tickers
    skip_words = {"THE", "FOR", "AND", "YOU", "THIS", "INTO", "CALL", "PUT", "EXP", "OTM"}
    if ticker in skip_words:
        return None

    # Extract option type
    option_type = "call"
    if re.search(r'\bPUT\b', text_upper):
        option_type = "put"

    # Extract strike price
    strike = None
    # Match patterns like "207.5 Call", "230 Call", "395 Call"
    strike_match = re.search(r'\b(\d{1,4}(?:\.\d{1,2})?)\s+(?:CALL|PUT)\b', text_upper)
    if strike_match:
        strike = strike_match.group(1)
    else:
        # Try ticker followed by number
        strike_match = re.search(rf'{ticker}\s+(\d+(?:\.\d+)?)', text_upper)
        if strike_match:
            strike = strike_match.group(1)

    # Extract expiry
    expiry = None
    expiry_raw = None

    # Format: Exp. 05/22/26 or 05/22/26
    exp_match = re.search(r'(?:EXP\.?\s*)?(\d{2}/\d{2}/\d{2,4})', text)
    if exp_match:
        expiry_raw = exp_match.group(1)
        # Convert MM/DD/YY to readable
        parts = expiry_raw.split("/")
        if len(parts) == 3:
            month_names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            try:
                m = int(parts[0])
                d = int(parts[1])
                y = parts[2] if len(parts[2]) == 4 else "20" + parts[2]
                expiry = f"{month_names[m]} {d}, {y}"
                expiry_short = f"{month_names[m]}{d}"
            except:
                expiry = expiry_raw
                expiry_short = expiry_raw

    # Extract premium
    premium = None
    # Match $384K, $2.3M, $2.3 million
    prem_match = re.search(r'\$(\d+(?:\.\d+)?)\s*([KMB](?:illion|illion)?)', text, re.IGNORECASE)
    if prem_match:
        val = float(prem_match.group(1))
        unit = prem_match.group(2)[0].upper()
        multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(unit, 1)
        premium = val * multiplier

    # Extract OTM if present
    otm = None
    otm_match = re.search(r'OTM[:\s]+(\d+(?:\.\d+)?)%', text, re.IGNORECASE)
    if otm_match:
        otm = float(otm_match.group(1))

    # Require at minimum: ticker + strike OR ticker + expiry
    if not ticker:
        return None
    if not strike and not expiry:
        return None

    return {
        "ticker": ticker,
        "strike": strike,
        "option_type": option_type,
        "expiry": expiry,
        "expiry_short": expiry_short if expiry else None,
        "expiry_raw": expiry_raw,
        "premium": premium,
        "otm": otm,
        "raw_text": text,
    }
