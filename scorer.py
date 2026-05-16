import anthropic
import os
import json
import re

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are an elite options trader and coach evaluating flow alerts.

Score the trade 1-7 (1 point each):
1. Clear catalyst (earnings, FDA, known event)?
2. Expiry RIGHT AFTER catalyst (not same day, not before)?
3. IV reasonable — not buying on earnings day (max IV)?
4. Liquidity: OI >500 AND spread <10%?
5. OTM under 10%?
6. Risk/reward acceptable?
7. Not chasing — price hasn't already moved 50%+ from original flow entry?

Respond ONLY with valid JSON in this exact format:
{
  "score": 6,
  "verdict": "TRADE",
  "checklist": [
    {"label": "Catalyst", "pass": true},
    {"label": "Expiry timing", "pass": true},
    {"label": "IV ok", "pass": true},
    {"label": "Liquidity", "pass": true},
    {"label": "OTM <10%", "pass": true},
    {"label": "Risk/reward", "pass": true},
    {"label": "Not chasing", "pass": false}
  ],
  "reasoning": "2-3 sentences explaining the score and key factors",
  "one_liner": "Single punchy sentence verdict under 15 words",
  "improvements": [
    "→ Better strike: use $X instead because...",
    "→ Better expiry: use MonDD instead because...",
    "→ Wait for: stock to break $X before entering",
    "→ Watch out for: specific risk"
  ]
}

Verdict must be exactly: TRADE (6-7), WATCH (4-5), or SKIP (0-3)"""


def score_trade(trade, data):
    """Send trade + live data to Claude for scoring."""

    # Build context string
    hist_moves = data.get("historical_moves", [])
    hist_str = ", ".join([f"{m:+.1f}%" for m in hist_moves]) if hist_moves else "N/A"
    avg_move = data.get("avg_earnings_move")

    prompt = f"""Score this options flow alert:

TRADE:
- Ticker: {trade.get('ticker')}
- Strike: {trade.get('strike')} {trade.get('option_type', 'call').upper()}
- Expiry: {trade.get('expiry')} ({data.get('days_to_expiry', '?')} days)
- Original premium: ${format_premium(trade.get('premium'))}
- OTM from tweet: {trade.get('otm', 'N/A')}%

LIVE DATA (fetched now):
- Stock price: ${data.get('stock_price', 'N/A')}
- Bid/Ask: ${data.get('bid', 'N/A')} / ${data.get('ask', 'N/A')}
- Spread: {data.get('spread_pct', 'N/A')}%
- Open Interest: {data.get('open_interest', 'N/A')}
- Current OTM: {data.get('otm_pct', 'N/A')}%
- Implied Volatility: {data.get('implied_volatility', 'N/A')}%
- Earnings date: {data.get('earnings_date', 'Unknown')}
- Historical earnings moves (last 8): {hist_str}
- Avg earnings move: {avg_move}%

ORIGINAL TWEET:
{trade.get('raw_text', '')}

Score this trade and provide improvement suggestions."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()

        # Strip markdown if present
        raw = re.sub(r"```json\s*|\s*```", "", raw).strip()

        result = json.loads(raw)
        return result

    except json.JSONDecodeError as e:
        print(f"[SCORER] JSON parse error: {e}\nRaw: {raw}")
        return default_result()
    except Exception as e:
        print(f"[SCORER] Error: {e}")
        return default_result()


def format_premium(premium):
    if not premium:
        return "N/A"
    if premium >= 1_000_000:
        return f"{premium/1_000_000:.1f}M"
    if premium >= 1_000:
        return f"{premium/1_000:.0f}K"
    return str(premium)


def default_result():
    return {
        "score": 0,
        "verdict": "SKIP",
        "checklist": [],
        "reasoning": "Could not analyze this trade.",
        "one_liner": "Analysis failed — check logs.",
        "improvements": []
    }