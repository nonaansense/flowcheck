"""
Vision Parser — Extracts trade data from Bullflow screenshots.
Handles both direct image URLs and tweet page URLs.
"""
import os, re, json, base64, requests


def get_client():
    from anthropic import Anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set")
    return Anthropic(api_key=api_key)


def extract_image_url_from_tweet(tweet_url: str) -> str | None:
    """
    Fetch tweet page and extract image URL from meta tags.
    Works on both twitter.com and x.com URLs.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        # Try fxtwitter which returns clean JSON with image data
        # Convert tweet URL to fxtwitter API format
        tweet_url = tweet_url.replace("twitter.com", "x.com")
        fx_url = tweet_url.replace("x.com", "api.fxtwitter.com")

        print(f"[VISION] Trying fxtwitter: {fx_url[:80]}")
        r = requests.get(fx_url, headers=headers, timeout=15)

        if r.status_code == 200:
            data   = r.json()
            tweet  = data.get("tweet", {})
            media  = tweet.get("media", {})
            photos = media.get("photos", [])
            if photos:
                img_url = photos[0].get("url")
                print(f"[VISION] Found image via fxtwitter: {img_url[:80]}")
                return img_url

        # Fallback: scrape og:image from tweet page via nitter
        nitter_url = tweet_url.replace("x.com", "nitter.net").replace("twitter.com", "nitter.net")
        print(f"[VISION] Trying nitter: {nitter_url[:80]}")
        r2 = requests.get(nitter_url, headers=headers, timeout=15)
        if r2.status_code == 200:
            # Look for image URLs in the HTML
            matches = re.findall(r'https://[^"\']+(?:jpg|jpeg|png|webp)[^"\']*', r2.text)
            # Filter for tweet media
            media_urls = [m for m in matches if "media" in m or "twimg" in m or "pbs.twimg" in m]
            if media_urls:
                print(f"[VISION] Found image via nitter: {media_urls[0][:80]}")
                return media_urls[0]

    except Exception as e:
        print(f"[VISION] Tweet image extraction error: {e}")

    return None


def download_image_as_base64(url: str) -> tuple:
    """Download image from URL and return (base64_data, media_type)."""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            content_type = r.headers.get("content-type", "image/jpeg")
            if "png" in content_type:
                media_type = "image/png"
            elif "webp" in content_type:
                media_type = "image/webp"
            elif "gif" in content_type:
                media_type = "image/gif"
            else:
                media_type = "image/jpeg"
            return base64.standard_b64encode(r.content).decode("utf-8"), media_type
    except Exception as e:
        print(f"[VISION] Image download error: {e}")
    return None, None


def extract_trade_from_image_url(image_url: str) -> dict | None:
    """Send image to Claude vision and extract trade data."""
    print(f"[VISION] Downloading image...")
    b64_data, media_type = download_image_as_base64(image_url)

    if not b64_data:
        print("[VISION] Could not download image")
        return None

    try:
        client   = get_client()
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": media_type,
                            "data":       b64_data,
                        }
                    },
                    {
                        "type": "text",
                        "text": """This is a Bullflow.io options flow alert screenshot.
Extract ALL visible trade data and return ONLY a JSON object:

{
  "ticker": "ORCL",
  "strike": "207.5",
  "option_type": "call",
  "expiry": "May 22, 2026",
  "expiry_raw": "05/22/26",
  "premium": 384400,
  "otm": 6.3,
  "option_price": 4.70,
  "open_interest": 116,
  "volume": 830,
  "raw_text": "ORCL 207.5 Call Exp. 05/22/26 OTM: 6.3% Prem: $384.4K"
}

Rules:
- ticker: stock symbol (e.g. ORCL, TSLA, META)
- strike: strike price as string (e.g. "207.5")
- option_type: "call" or "put"
- expiry: human readable (e.g. "May 22, 2026")
- expiry_raw: MM/DD/YY format (e.g. "05/22/26")
- premium: total premium in dollars (K=thousands, M=millions)
- otm: OTM% as number
- option_price: current option price shown
- open_interest: OI number or null
- raw_text: one-line summary of the alert

Return ONLY the JSON, nothing else."""
                    }
                ]
            }]
        )

        raw   = response.content[0].text.strip()
        raw   = re.sub(r"```json\s*|\s*```", "", raw).strip()
        trade = json.loads(raw)
        print(f"[VISION] Extracted: {trade.get('ticker')} {trade.get('strike')} {trade.get('option_type')} {trade.get('expiry')}")
        return trade

    except json.JSONDecodeError as e:
        print(f"[VISION] JSON parse error: {e}")
        return None
    except Exception as e:
        print(f"[VISION] Claude vision error: {e}")
        return None


def extract_trade_from_tweet(tweet_text: str, image_url: str = None, tweet_url: str = None) -> dict | None:
    """
    Main entry point. Priority order:
    1. Text parse (fastest, free)
    2. Direct image URL → Claude vision
    3. Tweet URL → extract image → Claude vision
    """
    from parser import parse_tweet

    # 1. Try text parsing first
    trade = parse_tweet(tweet_text)
    if trade and trade.get("ticker") and trade.get("strike"):
        print(f"[VISION] Text parse succeeded: {trade.get('ticker')}")
        return trade

    print(f"[VISION] Text parse insufficient — trying image extraction")

    # 2. Try direct image URL
    if image_url and image_url.strip():
        trade = extract_trade_from_image_url(image_url)
        if trade and trade.get("ticker"):
            return trade

    # 3. Try extracting image from tweet URL
    if tweet_url and tweet_url.strip():
        print(f"[VISION] Extracting image from tweet URL...")
        extracted_image_url = extract_image_url_from_tweet(tweet_url)
        if extracted_image_url:
            trade = extract_trade_from_image_url(extracted_image_url)
            if trade and trade.get("ticker"):
                return trade

    print("[VISION] All extraction methods failed")
    return None
