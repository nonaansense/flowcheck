import os, re, requests
from anthropic import Anthropic

# Cache successful image URL extractions to avoid re-fetching same tweet
_image_cache = {}

def get_client():
    return Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

def extract_image_url(tweet_url: str, tweet_text: str = "") -> str | None:
    """Try multiple services to get tweet image URL."""

    # Check cache first
    import re as _re
    _tweet_id_c = None
    _m_c = _re.search(r"/status/([0-9]+)", tweet_url or "")
    if _m_c:
        _tweet_id_c = _m_c.group(1)
        if _tweet_id_c in _image_cache:
            print(f"[VISION] Cache hit for tweet {_tweet_id_c}")
            return _image_cache[_tweet_id_c]

    # Try 0: follow t.co link directly from tweet text — fastest path
    tco_matches = _re.findall(r"https://t[.]co/[A-Za-z0-9]+", tweet_text or "")
    for tco_url in tco_matches:
        try:
            r = requests.get(tco_url, timeout=10, allow_redirects=True)
            final_url = r.url
            # Check if it resolved to an image or pic.twitter.com
            if any(x in final_url for x in ["pbs.twimg.com", "pic.twitter.com", "pic.x.com"]):
                print(f"[VISION] Found image via t.co redirect: {final_url}")
                return final_url
            # Try fetching as image directly
            if r.headers.get("content-type","").startswith("image/"):
                print(f"[VISION] t.co resolved to image: {final_url}")
                return final_url
        except Exception as e:
            print(f"[VISION] t.co expand error: {e}")

    # Normalize URL
    tweet_url = tweet_url.strip()
    tweet_id  = None
    m = _re.search(r"/status/([0-9]+)", tweet_url)
    if m:
        tweet_id = m.group(1)

    # Try 1: fxtwitter API
    try:
        fx_url = tweet_url.replace("twitter.com","api.fxtwitter.com").replace("x.com","api.fxtwitter.com")
        print(f"[VISION] Trying fxtwitter: {fx_url}")
        r = requests.get(fx_url, timeout=15)
        print(f"[VISION] fxtwitter status: {r.status_code}")
        if r.status_code == 200:
            data   = r.json()
            tweet  = data.get("tweet", {})
            media  = tweet.get("media", {})
            photos = media.get("photos", [])
            if photos:
                url = photos[0].get("url")
                if url:
                    print(f"[VISION] Found image via fxtwitter: {url}")
                    if tweet_id: _image_cache[tweet_id] = url
                    return url
            print(f"[VISION] fxtwitter: no photos in response. Keys: {list(tweet.keys())}")
    except Exception as e:
        print(f"[VISION] fxtwitter error: {e}")

    # Try 2: vxtwitter API
    try:
        vx_url = tweet_url.replace("twitter.com","api.vxtwitter.com").replace("x.com","api.vxtwitter.com")
        print(f"[VISION] Trying vxtwitter: {vx_url}")
        r = requests.get(vx_url, timeout=15)
        print(f"[VISION] vxtwitter status: {r.status_code}")
        if r.status_code == 200:
            data   = r.json()
            medias = data.get("media_extended", []) or data.get("mediaURLs", [])
            if medias:
                url = medias[0].get("url") if isinstance(medias[0], dict) else medias[0]
                if url and ("pbs.twimg" in url or "twimg" in url):
                    print(f"[VISION] Found image via vxtwitter: {url}")
                    if tweet_id: _image_cache[tweet_id] = url
                    return url
    except Exception as e:
        print(f"[VISION] vxtwitter error: {e}")

    # Try 3: Twitter oEmbed API (no auth required)
    if tweet_id:
        try:
            r = requests.get(
                "https://publish.twitter.com/oembed",
                params={"url": tweet_url, "omit_script": "true"},
                timeout=10
            )
            if r.status_code == 200:
                html  = r.json().get("html","")
                imgs  = re.findall(r"https://pbs[.]twimg[.]com/media/[A-Za-z0-9_-]+", html)
                if imgs:
                    url = imgs[0].split("?")[0] + "?format=jpg&name=orig"
                    print(f"[VISION] Found image via oEmbed: {url}")
                    _image_cache[tweet_id] = url
                    return url
        except Exception as e:
            print(f"[VISION] oEmbed error: {e}")

    print(f"[VISION] Could not extract image from {tweet_url}")
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
- premium: total premium as INTEGER in raw dollars, NO letters
  CORRECT: 462800 (for $462.8K), 1600000 (for $1.6M), 5400000 (for $5.4M)
  WRONG: "462.8K", "1.6M", "462800000"
  RULE: multiply K by 1000, multiply M by 1000000, return plain integer only
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
        has_fill_data = trade.get("ask_size") or trade.get("bid_size")
        if has_fill_data:
            return trade
        print(f"[VISION] Fetching image for fill data...")
    elif trade and trade.get("ticker"):
        # Partial text parse — has ticker but no strike/expiry
        # Try image first, fall back to returning partial trade
        print(f"[VISION] Partial text parse — ticker={trade.get('ticker')} premium={trade.get('premium')} — trying image")
    else:
        print("[VISION] Text parse insufficient — trying image extraction")

    if not tweet_url:
        print("[VISION] No tweet URL — returning partial trade")
        if trade and trade.get("ticker"):
            print(f"[VISION] Returning partial: {trade.get('ticker')} ${trade.get('premium','?')}")
            return trade
        return None

    print("[VISION] Extracting image from tweet URL...")
    image_url = extract_image_url(tweet_url, tweet_text=tweet_text)
    if not image_url:
        print("[VISION] Could not extract image URL — returning partial")
        if trade and trade.get("ticker"):
            return trade
        return None

    image_bytes = download_image(image_url)
    if not image_bytes:
        print("[VISION] Could not download image — returning partial")
        if trade and trade.get("ticker"):
            return trade
        return None

    vision_data = parse_image(image_bytes)
    if vision_data:
        # Normalize premium before merging — vision may return string like '462.0K'
        raw_p = vision_data.get("premium")
        if raw_p is not None:
            if isinstance(raw_p, str):
                s = str(raw_p).strip().upper()
                try:
                    if s.endswith("M"):   vision_data["premium"] = int(float(s[:-1])*1_000_000)
                    elif s.endswith("K"): vision_data["premium"] = int(float(s[:-1])*1_000)
                    else:                 vision_data["premium"] = int(float(s))
                except:
                    vision_data.pop("premium", None)  # Remove bad value, keep text parse value
            elif float(raw_p) > 500_000_000:
                vision_data["premium"] = int(float(raw_p)) // 1000  # Fix over-multiplied value

        # Merge vision data over text parse
        if trade:
            for k, v in vision_data.items():
                if v is not None:
                    trade[k] = v
        else:
            trade = vision_data
        return trade

    return trade
