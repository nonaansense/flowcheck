import json, os, re, time

def get_client():
    """Create Anthropic client lazily — reads env var at call time not import time."""
    from anthropic import Anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in environment variables")
    return Anthropic(api_key=api_key)

SYSTEM_PROMPT = """You are an elite options trader and coach evaluating flow alerts.

Score the trade on these criteria (1 point each):

TRADE CHECKLIST (7 points):
1. Clear catalyst (earnings, FDA, known event)?
2. Expiry timing: sweet spot 5-14 days AFTER earnings. ✅ sweet spot, ⚠️ tight/loose, ❌ before/same day
3. IV reasonable — not buying on earnings day?
4. Liquidity: OI >500 AND spread <10%?
5. OTM under 10%?
6. Risk/reward acceptable?
7. Not chasing — option not already moved 50%+ from flow entry?

MARKET ADJUSTMENTS:
8. VIX + SPY trend (provided)
9. Time of day: prime 10am-3:30pm ET = no penalty, noisy open/close = -0.5

Apply market adjustment to raw score for final score.

Respond ONLY with valid JSON:
{
  "raw_score": 6,
  "market_adjustment": -1,
  "final_score": 5,
  "verdict": "WATCH",
  "checklist": [
    {"label": "Catalyst", "pass": true},
    {"label": "Expiry timing", "pass": true, "note": "8d after earnings"},
    {"label": "IV ok", "pass": true},
    {"label": "Liquidity", "pass": true},
    {"label": "OTM <10%", "pass": true},
    {"label": "Risk/reward", "pass": true},
    {"label": "Not chasing", "pass": false, "note": "+175% from flow entry"}
  ],
  "options_pricing": "CHEAP",
  "options_pricing_note": "Implied 8.2% vs avg actual 11.4%",
  "earnings_quality": "HIGH",
  "earnings_quality_note": "Beats 7/8 quarters avg +12.3%",
  "chasing_risk": "HIGH",
  "chasing_note": "Option up 175% from flow fill",
  "market_verdict": "CAUTION",
  "market_reasoning": "VIX elevated at 24",
  "time_quality": "HIGH",
  "reasoning": "2-3 sentences on score and key factors",
  "one_liner": "Single punchy sentence under 15 words",
  "improvements": [
    "→ Better strike: $X (reason in under 10 words)",
    "→ Better expiry: MonDD (reason in under 10 words)",
    "→ Wait for: specific condition in under 10 words",
    "→ Watch: specific risk in under 10 words"
  ]
}

Verdict: TRADE (6-7), WATCH (4-5), SKIP (0-3) based on FINAL score.

CRITICAL FORMATTING RULES:
- one_liner: MAX 15 words, must be a complete sentence
- improvements: each item MAX 15 words, must be complete
- reasoning: MAX 2 sentences
- Never cut a sentence mid-word in any field

IMPORTANT: If data is missing or zero (bid=$0, OI<100), explain WHY specifically:
- Bid=$0 at market open (9:30-10:00 AM) = options spreads not yet posted by market makers, normal at open
- OI<100 = brand new position opened today, very thin liquidity
- No earnings date = earnings recently passed or far out
Never just say "wait for complete data" — explain the specific reason."""


def score_trade(trade, data):
    """Score trade using Claude API."""
    try:
        client = get_client()
    except ValueError as e:
        print(f"[SCORER] {e}")
        return default_result()

    hist_moves = data.get("historical_moves", [])
    hist_str   = ", ".join([f"{m:+.1f}%" for m in hist_moves]) if hist_moves else "N/A"
    surprises  = data.get("earnings_surprises", [])
    surp_str   = ", ".join([f"{s:+.1f}%" for s in surprises]) if surprises else "N/A"
    market     = data.get("market", {})
    sector     = data.get("sector", {})
    tod        = data.get("time_of_day", {})
    chase_move = data.get("price_move_since_flow")
    flow_fill  = data.get("flow_fill_price")
    curr_ask   = data.get("current_ask")

    prompt = f"""Score this options flow alert:

TRADE:
- Ticker: {trade.get('ticker')}
- Strike: {trade.get('strike')} {trade.get('option_type', 'call').upper()}
- Expiry: {trade.get('expiry')} ({data.get('days_to_expiry', '?')} days)
- Flow fill: ${flow_fill or 'N/A'} | Current ask: ${curr_ask or 'N/A'}
- Move since flow: {f'+{chase_move}%' if chase_move and chase_move > 0 else f'{chase_move}%' if chase_move is not None else 'N/A'}
- Chasing: {data.get('chasing_flag','N/A')} {data.get('chasing_emoji','')}

LIVE DATA:
- Stock: ${data.get('stock_price','N/A')} | Bid/Ask: ${data.get('bid','N/A')}/${data.get('ask','N/A')}
- Spread: {data.get('spread_pct','N/A')}% | OI: {data.get('open_interest','N/A')}
- OTM: {data.get('otm_pct','N/A')}% | IV: {data.get('implied_volatility','N/A')}%

EXPIRY TIMING — READ CAREFULLY:
- Days to expiry (calculated): {data.get('days_to_expiry','N/A')} days from TODAY
- Expiry date: {trade.get('expiry','N/A')}
- TODAY'S DATE: {__import__('datetime').datetime.now().strftime('%B %d, %Y')}
- If days_to_expiry is 0 or 1, this is a 0-DTE or 1-DTE — score harshly on criterion 2
- If days_to_expiry > 365, this is a LEAP — note as institutional long-term position

EARNINGS:
- Date: {data.get('earnings_date','Unknown')}
- Timing: {data.get('expiry_timing_label','N/A')} {data.get('expiry_timing_emoji','')}
- Implied move: {data.get('implied_move_pct','N/A')}% vs avg actual: {data.get('avg_earnings_move','N/A')}%
- {data.get('implied_vs_historical','N/A')} {data.get('implied_vs_historical_emoji','')}
- EPS surprises (last 8): {surp_str} | Beat rate: {data.get('beats_pct','N/A')}%

MARKET (real-time):
- VIX: {market.get('vix','N/A')} {market.get('vix_label','N/A')} {market.get('vix_emoji','')}
- SPY 5d: {market.get('spy_trend','N/A')} {market.get('spy_emoji','')}
- Sector {sector.get('etf','N/A')}: {sector.get('sector_trend','N/A')} {sector.get('sector_emoji','')}
- Bias: {market.get('market_bias','N/A')}

TIME: {tod.get('label','N/A')} {tod.get('emoji','')} — {tod.get('quality','N/A')}

TWEET: {trade.get('raw_text','')}

IMPORTANT: If live data fields show N/A or None, score based on what IS visible
in the tweet text and image data. Do not refuse to score — use available data.
For missing fields, note the gap but still provide best assessment.
Never say "wait for complete data" as a suggestion — give actionable trade advice."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"[SCORER] JSON parse error: {e}")
        return default_result()
    except Exception as e:
        err_str = str(e)
        if "rate_limit" in err_str or "rate limit" in err_str.lower():
            print(f"[SCORER] Rate limited — waiting 30s then retrying once")
            time.sleep(30)
            try:
                response = client.messages.create(
                    model="claude-sonnet-4-5",
                    max_tokens=1200,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}]
                )
                raw = response.content[0].text.strip()
                raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
                return json.loads(raw)
            except Exception as e2:
                print(f"[SCORER] Retry also failed: {e2}")
        else:
            print(f"[SCORER] API error: {e}")
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
        "raw_score": 0, "market_adjustment": 0, "final_score": 0,
        "verdict": "SKIP", "checklist": [],
        "options_pricing": "UNKNOWN", "options_pricing_note": "",
        "earnings_quality": "UNKNOWN", "earnings_quality_note": "",
        "chasing_risk": "UNKNOWN", "chasing_note": "",
        "market_verdict": "UNKNOWN", "market_reasoning": "",
        "time_quality": "UNKNOWN",
        "reasoning": "Claude API scoring failed — check Railway logs for details.",
        "one_liner": "Scoring error — see Railway logs.",
        "improvements": [
            "→ Check Railway logs for the specific error",
            "→ Verify ANTHROPIC_API_KEY is set correctly in Railway variables",
        ]
    }
