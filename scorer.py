import json, os, re, time
from anthropic import Anthropic

def get_client():
    return Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You are an elite options trader evaluating institutional flow alerts.

FLOW TYPES — understand before scoring:
- Type A: Known catalyst (earnings/FDA upcoming) — score on timing + IV
- Type B: Insider/informed (large premium, no public catalyst, unusual activity)
- Type C: Momentum/breakout (near-ATM, no catalyst, aggressive fill)
- Type D: Post-earnings (reported 0-45 days ago) — confirmed catalyst + deflated IV

CHECKLIST (1 point each, 7 total):

CRITICAL PRINCIPLE: @FL0WG0D only posts UNUSUAL flow. Every alert has already passed
a human filter. Your job is to assess RISK, not validate whether it's worth watching.
FULL_ASK fill on large premium = someone with conviction. Respect that signal.

1. Flow signal strength?
   - PASS (1.0): FULL_ASK fill AND premium >$200K — conviction buyer, this IS the signal
   - PASS (1.0): known upcoming catalyst (earnings/FDA)
   - PASS (1.0): post-earnings 0-45 days — confirmed catalyst + deflated IV
   - PASS (0.5): MOSTLY_ASK fill with moderate premium
   - FAIL (0): bid-side fill, high multi% (spread), premium <$50K
   - NOTE: NEVER fail criterion 1 because of earnings timing alone
     BLDP 10K contracts 100% at ask = PASS regardless of when earnings are

2. Expiry appropriate for thesis type?
   - INFORMED/INSIDER flow (no public catalyst, large premium):
     → ANY expiry 7-180 days = PASS — they know something, timing is their choice
     → <7 days with no catalyst = FAIL
   - EARNINGS play (expiry relative to earnings):
     → 5-14 days after earnings = sweet spot ✅
     → 1-4 days after = very tight ⚠️ (0.5)
     → Before earnings = ❌ FAIL
   - MOMENTUM/BREAKOUT (OTM <3%):
     → 7-30 days = PASS
   - POST-EARNINGS continuation:
     → 14+ days = PASS
   - KEY: If fill is FULL_ASK and premium is large, be LENIENT on timing
     The buyer chose that expiry for a reason

3. IV reasonable?
   - FAIL only if buying right on earnings day (IV crush imminent)
   - For informed/insider flow — IV is less relevant, they're buying for the move

4. Liquidity: OI >500 AND spread <10%?
   - EXCEPTION: small cap stocks (price <$10) naturally have lower OI
     For sub-$10 stocks, OI >200 is acceptable
   - EXCEPTION: if premium is very large (>$500K), buyer already validated liquidity
   - Unknown OI with large premium = benefit of doubt (0.5)

5. OTM reasonable for thesis?
   - Standard: OTM <15% = PASS
   - Deep OTM (15-30%) with FULL_ASK + large premium = possible insider = PASS (0.5)
   - Deep OTM (15-30%) with small premium = lottery ticket = FAIL
   - Very deep OTM (>30%) = FAIL regardless

6. Risk/reward acceptable?
   - BREAKOUT BET (OTM <2%, no catalyst, <21 DTE): -0.5
   - Noisy open (9:30-10:00 AM) + breakout bet: additional -0.5
   - Otherwise: evaluate premium vs potential gain

7. Not chasing — option not already up 50%+ from flow entry?

VERDICT RULES:
- TRADE (6-7): High conviction, enter with proper position size
- WATCH (4-5): Interesting but wait for technical confirmation
- SKIP (0-3): Clear reasons to avoid
- NEVER give SKIP to a FULL_ASK + large premium flow unless there is a
  specific structural reason (bid=0, spread >50%, DTE <3, OTM >30%)

SCORING BONUSES (apply to final score):
- Vol/OI >5x: +0.5 to criterion 1
- Vol/OI >3x: +0.25 to criterion 1
- 2nd+ flow same ticker today: +0.5 to criterion 1
- Flow between 10:00-11:30 AM ET: +0.5 to criterion 6
- Flow after 2:00 PM ET: +0.5 to criterion 6
- Sector ETF bullish + FULL_ASK fill: +0.5 to criterion 6
- Tomorrow has EXTREME macro: cap verdict at WATCH regardless of score

SECTOR CONTEXT:
- Sector {data.get('sector',{{}}).get('etf','?')}: {data.get('sector',{{}}).get('sector_trend','N/A')}
- Bullish sector + strong fill = tailwind → more lenient on scoring
- Bearish sector + no catalyst = headwind → more conservative on criterion 6

MARKET ADJUSTMENT: Apply market_score_adjustment from market data (VIX/SPY trend).

VERDICT: TRADE (6-7), WATCH (4-5), SKIP (0-3) based on FINAL score.

BREAKOUT BET RULE:
If OTM <2% AND DTE <21 AND no earnings catalyst:
- This is a breakout bet — flag it
- Suggest straddle (buy both call AND put) OR wait for confirmed breakout
- Cap verdict at WATCH regardless of score (cannot be TRADE)

POST-EARNINGS SCORING:
- 0-7 days: IV deflated, BEST entry window — score generously
- 8-21 days: still good momentum window
- 22-45 days: continuation play, evaluate on premium + fill

FORMATTING RULES:
- one_liner: MAX 15 words, complete sentence
- improvements: each MAX 15 words, complete sentence  
- reasoning: MAX 2 sentences, identify flow type
- Never cut sentences mid-word"""

def default_result():
    return {
        "raw_score": 0, "final_score": 0, "verdict": "SKIP",
        "market_adjustment": 0,
        "checklist": {},
        "reasoning": "Analysis unavailable — check API credentials.",
        "one_liner": "Could not analyze — check Railway logs.",
        "improvements": ["→ Check ANTHROPIC_API_KEY in Railway variables"]
    }

def score_trade(trade: dict, data: dict) -> dict:
    surp_str = "N/A"

    def cap_at_word(text: str, limit: int = 120) -> str:
        if not text or len(text) <= limit: return text or ""
        t = text[:limit]
        sp = t.rfind(" ")
        return (t[:sp] + "…") if sp > 0 else (t + "…")

    premium_str = "N/A"
    if trade.get("premium"):
        p = trade["premium"]
        premium_str = f"${p/1000000:.1f}M" if p >= 1000000 else f"${p/1000:.0f}K"

    mkt = data.get("market", {})
    adj_str = f"({mkt.get('market_score_adjustment',0):+.1f}mkt)" if mkt.get('market_score_adjustment') else ""

    prompt = f"""Evaluate this options flow alert:

TWEET: {trade.get('raw_text','')}
TICKER: {trade.get('ticker')} | STRIKE: {trade.get('strike')} | TYPE: {trade.get('option_type','call').upper()}
EXPIRY: {trade.get('expiry','?')} | DTE: {data.get('days_to_expiry','?')} days
PREMIUM: {premium_str}

FILL AGGRESSION — KEY SIGNAL:
- Fill type: {data.get('fill_type','UNKNOWN')} {data.get('fill_emoji','')}
- Detail: {data.get('fill_label','Unknown')}
- Ask%: {data.get('ask_pct','N/A')}% | Multi%: {data.get('multi_pct','N/A')}%

PREMIUM SIZE SIGNAL:
- Raw premium: {premium_str}
- Size assessment: {data.get('premium_label','Not assessed')} {data.get('premium_emoji','')}
- NOTE: Large premium relative to stock price = stronger informed money signal
  e.g. $582K on a $4 stock is FAR more significant than $582K on a $400 stock
  Always mention relative premium context in your reasoning

LIVE MARKET DATA:
- Stock price: ${data.get('stock_price','N/A')} | OTM: {data.get('otm_pct','N/A')}%
- OI: {data.get('open_interest','N/A')} | Spread: {data.get('spread_pct','N/A')}%
- Bid: ${data.get('bid','N/A')} | Ask: ${data.get('ask','N/A')}

BREAKOUT DETECTION:
- Breakout bet: {data.get('is_breakout_bet',False)} {data.get('breakout_emoji','')}
- Note: {data.get('breakout_label','')}

TIME OF DAY: {data.get('time_of_day',{}).get('label','?')} {data.get('time_of_day',{}).get('emoji','')}
Note: {data.get('time_of_day',{}).get('note','')}

EARNINGS CONTEXT:
- Date: {data.get('earnings_date','Unknown')}
- Context: {data.get('earnings_context','Unknown')}
- Is past earnings: {data.get('earnings_is_past',False)}
- Days since earnings: {data.get('days_since_earnings','N/A')}
- Timing: {data.get('expiry_timing_label','N/A')} {data.get('expiry_timing_emoji','')}

MARKET CONDITIONS:
- VIX: {mkt.get('vix','N/A')} {mkt.get('vix_label','')} {mkt.get('vix_emoji','')}
- SPY: {mkt.get('spy_trend','N/A')} {mkt.get('spy_emoji','')}
- Sector {data.get('sector',{}).get('etf','?')}: {data.get('sector',{}).get('sector_trend','N/A')}
- Market score adjustment: {mkt.get('market_score_adjustment',0)}

Score this trade and return ONLY a JSON object:
{{
  "raw_score": <0-7 number>,
  "market_adjustment": <number like -1 or 0>,
  "final_score": <raw_score + market_adjustment>,
  "verdict": "<TRADE|WATCH|SKIP>",
  "checklist": {{
    "criterion_1": {{"pass": true/false, "note": "brief note"}},
    "criterion_2": {{"pass": true/false, "note": "brief note"}},
    "criterion_3": {{"pass": true/false, "note": "brief note"}},
    "criterion_4": {{"pass": true/false, "note": "brief note"}},
    "criterion_5": {{"pass": true/false, "note": "brief note"}},
    "criterion_6": {{"pass": true/false, "note": "brief note"}},
    "criterion_7": {{"pass": true/false, "note": "brief note"}}
  }},
  "reasoning": "2 sentences max identifying flow type and key factors",
  "one_liner": "Under 15 words complete sentence",
  "improvements": [
    "→ First improvement under 15 words",
    "→ Second improvement under 15 words"
  ]
}}

Remember: if is_breakout_bet is true, cap verdict at WATCH maximum."""

    try:
        client   = get_client()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            system=SYSTEM_PROMPT,
            messages=[{"role":"user","content":prompt}]
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"```json\s*|\s*```","",raw).strip()
        result = json.loads(raw)

        # Enforce breakout cap
        if data.get("is_breakout_bet") and result.get("verdict") == "TRADE":
            result["verdict"]    = "WATCH"
            result["final_score"] = min(result.get("final_score",5), 5)

        result["one_liner"]    = cap_at_word(result.get("one_liner",""), 120)
        result["improvements"] = [cap_at_word(i,120) for i in (result.get("improvements") or [])]
        return result

    except Exception as e:
        err = str(e)
        if any(x in err.lower() for x in ["rate_limit","rate limit","500","internal","overloaded"]):
            wait = 45 if "rate_limit" in err else 15
            print(f"[SCORER] API error — waiting {wait}s then retrying")
            time.sleep(wait)
            try:
                response = get_client().messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=1200,
                    system=SYSTEM_PROMPT, messages=[{"role":"user","content":prompt}]
                )
                raw    = response.content[0].text.strip()
                raw    = re.sub(r"```json\s*|\s*```","",raw).strip()
                result = json.loads(raw)
                if data.get("is_breakout_bet") and result.get("verdict") == "TRADE":
                    result["verdict"] = "WATCH"
                return result
            except Exception as e2:
                print(f"[SCORER] Retry failed: {e2}")
        else:
            print(f"[SCORER] API error: {e}")
        return default_result()
