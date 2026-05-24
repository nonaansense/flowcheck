import os, re, requests
from anthropic import Anthropic

def get_client():
    return Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def extract_image_url(tweet_url: str) -> str | None:
    """Try fxtwitter to get Bullflow screenshot URL."""
    try:
        fx_url = tweet_url.replace("twitter.com","api.fxtwitter.com").replace("x.com","api.fxtwitter.com")
        print(f"[VISION] Trying fxtwitter: {fx_url}")
        r = requests.get(fx_url, timeout=15)
        if r.status_code == 200:
            data   = r.json()
            tweet  = data.get("tweet", {})
            media  = tweet.get("media", {})
            photos = media.get("photos", [])
            if photos:
                url = photos[0].get("url")
                if url:
                    print(f"[VISION] Found image via fxtwitter: {url}")
                    return url
    except Exception as e:
        print(f"[VISION] fxtwitter error: {e}")
    return None

def download_image(url: str) -> bytes | None:
    try:
        print("[VISION] Downloading image...")
        r = requests.get(url, timeout=20)
        if r.status_code == 200:
            return r.content
    except Exception as e:
        print(f"[VISION] Download error: {e}")
    return None

def parse_image(image_bytes: bytes) -> dict | None:
    """Use Claude vision to extract Bullflow data from screenshot."""
    import base64
    client = get_client()
    b64    = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = """Extract ALL fields from this Bullflow options flow screenshot.
Return ONLY a JSON object like this example:
{
  "ticker": "MSFT",
  "strike": "435",
  "option_type": "call",
  "expiry": "June 5, 2026",
  "expiry_raw": "06/05/26",
  "premium": 5400000,
  "otm": 1.4,
  "option_price": 10.10,
  "ask_size": 5390,
  "bid_size": 0,
  "mid_size": 0,
  "multi_pct": 0.0,
  "open_interest": 1300,
  "volume": 5500,
  "implied_volatility": null,
  "raw_text": "MSFT 435 Call Exp. 06/05/26 OTM: 1.4% Prem: $5.4M Ask:5.39K Bid:0"
}

Rules:
- ticker: stock symbol
- strike: strike price as string
- option_type: "call" or "put"
- expiry: human readable e.g. "June 5, 2026"
- expiry_raw: MM/DD/YY format e.g. "06/05/26"
- premium: total premium in RAW DOLLARS
  Examples: $462.8K = 462800, $1.6M = 1600000, $5.4M = 5400000
  NEVER return more than 8 digits for premium — $462.8K is 462800 NOT 462800000
- otm: OTM% as number e.g. 1.4
- option_price: Avg. Fill price shown
- ask_size: Ask Size contracts (convert K) — THIS IS THE KEY FILL SIGNAL
- bid_size: Bid Size contracts (convert K)
- mid_size: Mid Size contracts (convert K)
- multi_pct: Multi% number e.g. 0.0
- open_interest: OI (convert K)
- volume: Vol (convert K)
- implied_volatility: IV% or null

CRITICAL: ask_size vs bid_size determines if buyer was aggressive.
ask_size >> bid_size = aggressive buyer at ask = BULLISH signal.
Return ONLY the JSON, nothing else."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": prompt}
            ]}]
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
        import json
        data = json.loads(raw)
        print(f"[VISION] Extracted: {data.get('ticker')} {data.get('strike')} {data.get('option_type')} {data.get('expiry')}")
        return data
    except Exception as e:
        print(f"[VISION] Parse error: {e}")
        return None

def extract_trade_from_tweet(tweet_text: str, tweet_url: str) -> dict | None:
    """
    Main entry point. Tries text parsing first, then vision on image.
    Returns merged trade dict.
    """
    from parser import parse_tweet
    
    # Try text parse first for basic fields
    trade = parse_tweet(tweet_text)
    if trade and trade.get("ticker") and trade.get("strike") and trade.get("expiry_raw"):
        print(f"[VISION] Text parse succeeded: {trade.get('ticker')}")
        # Still try image to get fill data (ask_size, bid_size, OI, volume)
        # unless we already have fill data from text
        has_fill_data = trade.get("ask_size") or trade.get("bid_size")
        if has_fill_data:
            return trade
        print(f"[VISION] Fetching image for fill data (ask_size/bid_size)...")
    else:
        print("[VISION] Text parse insufficient — trying image extraction")

    if not tweet_url:
        print("[VISION] No tweet URL — cannot extract image")
        return trade  # Return partial if we have ticker at least

    print("[VISION] Extracting image from tweet URL...")
    image_url = extract_image_url(tweet_url)
    if not image_url:
        print("[VISION] Could not extract image URL")
        return trade

    image_bytes = download_image(image_url)
    if not image_bytes:
        print("[VISION] Could not download image")
        return trade

    vision_data = parse_image(image_bytes)
    if vision_data:
        # Merge vision data over text parse
        if trade:
            for k, v in vision_data.items():
                if v is not None:
                    trade[k] = v
        else:
            trade = vision_data
        return trade

    return trade
