"""
Email parsing for Robinhood fill confirmations.
Receives forwarded Robinhood emails via Mailgun webhook.
Parses fill details and auto-logs to journal.

Setup:
1. Sign up at mailgun.com (free — 1000 emails/month)
2. Add a domain or use sandbox
3. Set up a route: match "from:robinhood.com" → forward to webhook
4. Webhook URL: https://your-app.railway.app/webhook-email
5. Add MAILGUN_API_KEY to Railway variables
6. Forward your Robinhood emails to your Mailgun address
"""
import os, re
from datetime import datetime
from zoneinfo import ZoneInfo

def parse_robinhood_email(subject: str, body: str, html: str = "") -> dict | None:
    """
    Parse a Robinhood order fill confirmation email.

    Robinhood email subjects look like:
      "You bought 3 FLNC $23 calls expiring 06/18/2026"
      "You sold 1 BE $460 call expiring 07/17/2026"
      "Your order to buy AAPL was filled"
      "Option exercise: FLNC $23 call"

    Returns parsed trade dict or None if not a fill.
    """
    subject = (subject or "").strip()
    body    = (body or "").strip()
    text    = subject + " " + body

    # Skip non-fill emails
    non_fill_patterns = [
        "dividend", "account statement", "tax", "welcome",
        "password", "verification", "security", "statement",
        "pending", "cancelled", "canceled", "rejected",
    ]
    text_lower = text.lower()
    if any(p in text_lower for p in non_fill_patterns):
        return None

    # Must mention buying/selling options
    if not any(w in text_lower for w in ["bought", "sold", "filled", "exercised", "call", "put"]):
        return None

    result = {}

    # ── Action ────────────────────────────────────────────────────────
    if any(w in text_lower for w in ["you bought", "buy", "bto", "buy to open"]):
        result["action"]     = "entry"
        result["order_type"] = "BTO"
    elif any(w in text_lower for w in ["you sold", "sell", "stc", "sell to close"]):
        result["action"]     = "exit"
        result["order_type"] = "STC"
    else:
        result["action"] = "entry"

    # ── Ticker ────────────────────────────────────────────────────────
    # "You bought 3 FLNC $23 calls" or "FLNC $23 call"
    ticker_match = re.search(
        r'\b([A-Z]{1,5})\s+\$[\d.]+\s+(?:call|put)',
        text, re.IGNORECASE
    )
    if not ticker_match:
        # Try "bought X shares of TICKER"
        ticker_match = re.search(
            r'(?:bought|sold)\s+\d+\s+([A-Z]{1,5})\s+\$',
            text, re.IGNORECASE
        )
    if ticker_match:
        result["ticker"] = ticker_match.group(1).upper()

    # ── Contracts ─────────────────────────────────────────────────────
    contracts_match = re.search(
        r'(?:bought|sold)\s+(\d+)\s+[A-Z]',
        text, re.IGNORECASE
    )
    if contracts_match:
        result["contracts"] = int(contracts_match.group(1))

    # ── Strike ────────────────────────────────────────────────────────
    strike_match = re.search(r'\$(\d+(?:\.\d+)?)\s+(?:call|put)', text, re.IGNORECASE)
    if strike_match:
        result["strike"] = strike_match.group(1)

    # ── Option type ───────────────────────────────────────────────────
    if re.search(r'\bcall\b', text, re.IGNORECASE):
        result["option_type"] = "call"
    elif re.search(r'\bput\b', text, re.IGNORECASE):
        result["option_type"] = "put"

    # ── Expiry ────────────────────────────────────────────────────────
    # "expiring 06/18/2026" or "expiring June 18, 2026"
    exp_match = re.search(
        r'expir\w+\s+(\d{1,2}/\d{1,2}/\d{2,4})',
        text, re.IGNORECASE
    )
    if not exp_match:
        exp_match = re.search(
            r'expir\w+\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})',
            text, re.IGNORECASE
        )
    if exp_match:
        raw_exp = exp_match.group(1)
        # Normalize to MM/DD/YY
        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%B %d, %Y", "%B %d %Y"):
            try:
                dt = datetime.strptime(raw_exp.strip(), fmt)
                result["expiry"] = dt.strftime("%m/%d/%y")
                break
            except:
                continue

    # ── Fill price ────────────────────────────────────────────────────
    # "at $2.85 per contract" or "filled at $2.85"
    price_match = re.search(
        r'(?:at|filled at|price)\s+\$(\d+(?:\.\d+)?)\s+(?:per|each)',
        text, re.IGNORECASE
    )
    if not price_match:
        # Try "average price $2.85"
        price_match = re.search(
            r'(?:average|avg)?\s*price\s+\$(\d+(?:\.\d+)?)',
            text, re.IGNORECASE
        )
    if price_match:
        result["price"] = float(price_match.group(1))

    # ── Fill time ─────────────────────────────────────────────────────
    # Try to extract from email body — Robinhood includes fill time
    time_match = re.search(
        r'(\d{1,2}:\d{2}\s*(?:AM|PM))\s*ET',
        text, re.IGNORECASE
    )
    if time_match:
        result["time"] = time_match.group(1).replace(" ","")

    # Date from email timestamp (passed in separately)
    # result["date"] set by caller from email headers

    # ── Fees ──────────────────────────────────────────────────────────
    fees_match = re.search(
        r'(?:commission|fee|regulatory)\s+\$(\d+(?:\.\d+)?)',
        text, re.IGNORECASE
    )
    if fees_match:
        result["fees"] = float(fees_match.group(1))

    # ── Account type ─────────────────────────────────────────────────
    if re.search(r'individual|brokerage|margin', text, re.IGNORECASE):
        result["account_type"] = "Individual"
    elif re.search(r'traditional\s+ira|trad\s+ira', text, re.IGNORECASE):
        result["account_type"] = "Traditional IRA"
    elif re.search(r'roth\s+ira', text, re.IGNORECASE):
        result["account_type"] = "Roth IRA"

    # ── Validate ─────────────────────────────────────────────────────
    required = ["ticker", "option_type"]
    if not all(k in result for k in required):
        print(f"[EMAIL] Missing required fields: {[k for k in required if k not in result]}")
        return None

    result["source"]     = "email"
    result["confidence"] = "high" if len(result) >= 6 else "medium"
    print(f"[EMAIL] Parsed: {result}")
    return result

def verify_mailgun_signature(token: str, timestamp: str, signature: str) -> bool:
    """Verify Mailgun webhook signature for security."""
    import hmac, hashlib
    api_key = os.environ.get("MAILGUN_API_KEY","")
    if not api_key:
        return True  # Skip verification if no key set
    computed = hmac.new(
        api_key.encode("utf-8"),
        (timestamp + token).encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)
