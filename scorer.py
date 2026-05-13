import anthropic
import os
import json
import re

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are an elite options trader and coach evaluating flow alerts.

Score the trade on these 9 criteria (1 point each where marked, noted otherwise):

TRADE CHECKLIST (7 points):
1. Clear catalyst (earnings, FDA, known event)?
2. Expiry timing: sweet spot is 5-14 days AFTER earnings/catalyst. ✅ sweet spot, ⚠️ tight/loose, ❌ before or same day
3. IV reasonable — not buying on earnings day (max IV)?
4. Liquidity: OI >500 AND spread <10%?
5. OTM under 10%?
6. Risk/reward acceptable?
7. Not chasing — option price not already moved 50%+ from flow entry?

MARKET CONDITIONS (adjustments, not binary):
8. Market environment: VIX + SPY trend (provided in data)
9. Time of day: prime hours (10am-3:30pm ET) = no penalty, noisy open/close = -0.5

Apply market adjustment to raw score to get final score.

ALSO evaluate:
- Implied move vs historical move (cheap/fair/expensive options)
- Earnings surprise history (consistent beater = higher conviction)
- Chasing risk (how much option moved since flow entry)
- Premium size significance (unusual vs normal flow)

Respond ONLY with valid JSON:
{
  "raw_score": 6,
  "market_adjustment": -1,
  "final_score": 5,
  "verdict": "WATCH",
  "checklist": [
    {"label": "Catalyst", "pass": true},
    {"label": "Expiry timing", "pass": true, "note": "8d after earnings — sweet spot"},
    {"label": "IV ok", "pass": true},
    {"label": "Liquidity", "pass": true},
    {"label": "OTM <10%", "pass": true},
    {"label": "Risk/reward", "pass": true},
    {"label": "Not chasing", "pass": false, "note": "+175% from flow entry"}
  ],
  "options_pricing": "CHEAP",
  "options_pricing_note": "Implied 8.2% vs avg actual 11.4% — options underpriced",
  "earnings_quality": "HIGH",
  "earnings_quality_note": "Beats 7/8 quarters, avg +12.3% surprise — reliable beater",
  "chasing_risk": "HIGH",
  "chasing_note": "Option up 175% from flow fill — very late entry",
  "market_verdict": "CAUTION",
  "market_reasoning": "VIX elevated at 24 reduces premium-buying edge",
  "time_quality": "HIGH",
  "reasoning": "2-3 sentences on the trade score and key factors",
  "one_liner": "Single punchy sentence under 15 words",
  "improvements": [
    "→ Better strike: use $X instead because...",
    "→ Better expiry: use MonDD — X days after earnings, sweet spot",
    "→ Wait for: stock to pull back to $X before entering",
    "→ Watch out for: specific risk"
  ]
}

Verdict: TRADE (6-7), WATCH (4-5), SKIP (0-3) based on FINAL score."""


def score_trade(trade, data):
    hist_moves = data.get("historical_moves", [])
    hist_str   = ", ".join([f"{m:+.1f}%" for m in hist_moves]) if hist_moves else "N/A"
    surprises  = data.get("earnings_surprises", [])
    surp_str   = ", ".join([f"{s:+.1f}%" for s in surprises]) if surprises else "N/A"
    market     = data.get("market", {})
    sector     = data.get("sector", {})
    tod        = data.get("time_of_day", {})

    flow_fill   = data.get("flow_fill_price")
    current_ask = data.get("current_ask")
    chase_move  = data.get("price_move_since_flow")

    prompt = f"""Score this options flow alert:

TRADE:
- Ticker: {trade.get('ticker')}
- Strike: {trade.get('strike')} {trade.get('option_type', 'call').upper()}
- Expiry: {trade.get('expiry')} ({data.get('days_to_expiry', '?')} days away)
- Original flow fill: ${flow_fill or 'N/A'}
- Current ask: ${current_ask or 'N/A'}
- Move since flow: {f'+{chase_move}%' if chase_move and chase_move > 0 else f'{chase_move}%' if chase_move is not None else 'N/A'}
- Chasing risk: {data.get('chasing_flag', 'N/A')} {data.get('chasing_emoji', '')}

LIVE TRADE DATA:
- Stock price: ${data.get('stock_price', 'N/A')}
- Bid/Ask: ${data.get('bid', 'N/A')} / ${data.get('ask', 'N/A')}
- Spread: {data.get('spread_pct', 'N/A')}%
- Open Interest: {data.get('open_interest', 'N/A')}
- Current OTM: {data.get('otm_pct', 'N/A')}%
- Implied Volatility: {data.get('implied_volatility', 'N/A')}%

EARNINGS TIMING:
- Earnings date: {data.get('earnings_date', 'Unknown')}
- Expiry timing: {data.get('expiry_timing_label', 'N/A')} {data.get('expiry_timing_emoji', '')}

IMPLIED VS HISTORICAL MOVE:
- ATM straddle implied move: {data.get('implied_move_pct', 'N/A')}%
- Avg actual earnings move (last 8): {data.get('avg_earnings_move', 'N/A')}%
- Historical moves: {hist_str}
- Assessment: {data.get('implied_vs_historical', 'N/A')} {data.get('implied_vs_historical_emoji', '')}

EARNINGS QUALITY:
- EPS surprise history (last 8 qtrs): {surp_str}
- Avg surprise: {data.get('avg_earnings_surprise', 'N/A')}%
- Beat rate: {data.get('beats_pct', 'N/A')}%

MARKET CONDITIONS (real-time):
- VIX: {market.get('vix', 'N/A')} — {market.get('vix_label', 'N/A')} {market.get('vix_emoji', '')}
- SPY 5-day: {market.get('spy_trend', 'N/A')} {market.get('spy_emoji', '')}
- Sector ({sector.get('etf', 'N/A')}): {sector.get('sector_trend', 'N/A')} {sector.get('sector_emoji', '')}
- Market bias: {market.get('market_bias', 'N/A')}

TIME OF DAY:
- Window: {tod.get('label', 'N/A')} {tod.get('emoji', '')}
- Quality: {tod.get('quality', 'N/A')}
- Note: {tod.get('note', '')}

ORIGINAL TWEET:
{trade.get('raw_text', '')}

Score this trade factoring in all data above."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
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
        "raw_score": 0, "market_adjustment": 0, "final_score": 0,
        "verdict": "SKIP", "checklist": [],
        "options_pricing": "UNKNOWN", "options_pricing_note": "",
        "earnings_quality": "UNKNOWN", "earnings_quality_note": "",
        "chasing_risk": "UNKNOWN", "chasing_note": "",
        "market_verdict": "UNKNOWN", "market_reasoning": "",
        "time_quality": "UNKNOWN",
        "reasoning": "Could not analyze this trade.",
        "one_liner": "Analysis failed — check logs.",
        "improvements": []
    }
