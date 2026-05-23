import json, os, re, time
from anthropic import Anthropic

def get_client():
    return Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

SYSTEM_PROMPT = """You score options flow alerts on a 7-point checklist.

KEY PRINCIPLE: @FL0WG0D only posts unusual flow. Every alert already passed a human filter.
Your job is to assess RISK, not gatekeep. FULL_ASK fill = someone paid premium aggressively.
That IS the signal. Earnings timing is context, not a veto.

FLOW TYPES:
- INFORMED: Large premium + FULL_ASK + no public catalyst = possible insider/informed money
- EARNINGS: Known catalyst upcoming, buying before the event
- POST-EARNINGS: Reported 0-45 days ago, IV deflated, continuation play
- BREAKOUT: Near-ATM (<2% OTM), no catalyst, betting on immediate price break

CHECKLIST — score each 0 or 1 (partial 0.5 allowed):

1. FLOW SIGNAL STRENGTH
   1.0 = FULL_ASK fill AND premium >$200K (the flow IS the signal)
   1.0 = Known catalyst (earnings/FDA) upcoming
   1.0 = Post-earnings 0-45 days (confirmed catalyst + cheap IV)
   0.5 = MOSTLY_ASK fill OR unknown fill with large premium
   0.0 = Bid-side fill, high multi%, premium <$50K

2. EXPIRY TIMING
   1.0 = Informed flow (no catalyst): ANY 7-180 day expiry — they chose it deliberately
   1.0 = Earnings play: expiry 5-14 days AFTER earnings
   1.0 = Post-earnings: 14+ days
   1.0 = Breakout: 7-30 days
   0.5 = Earnings play: 1-4 days after earnings (tight but viable)
   0.0 = Expiry BEFORE earnings on an earnings play
   NOTE: For informed/insider flow, NEVER fail criterion 2 on timing alone

3. IV REASONABLE
   1.0 = Not buying on earnings day itself (IV crush risk)
   0.5 = Day before earnings (elevated but manageable)
   0.0 = Buying call on earnings morning (IV will crush)

4. LIQUIDITY
   1.0 = OI >500 OR premium very large (>$1M) OR unknown but large premium
   0.5 = OI 200-500 (acceptable for small cap stocks under $10)
   0.5 = OI unknown but Vol/OI ratio signals real activity
   0.0 = OI <200 on large cap, bid=$0 with tiny premium, spread >50%
   NOTE: Sub-$10 stocks naturally have lower OI — don't penalize

5. OTM APPROPRIATE
   1.0 = OTM <10%
   0.5 = OTM 10-20% with large premium + FULL_ASK (possible insider)
   0.5 = OTM 20-35% with EXTREME size signal (Vol/OI >10x, premium >$500K)
   0.0 = OTM >35% with small premium and no unusual signal

6. RISK/REWARD
   1.0 = Good setup, reasonable risk
   0.5 = Breakout bet (OTM <2%, no catalyst) — high failure rate
   0.5 = Noisy open (9:30-10:00 AM) + breakout bet
   0.0 = Multiple structural problems

7. NOT CHASING
   1.0 = Option not already up 50%+ from flow entry
   0.0 = Clearly chasing a big move

MARKET ADJUSTMENT: Add market_score_adjustment from VIX/SPY data.

BONUSES (add to raw score before market adjustment):
- Vol/OI >5x: +0.5 criterion 1
- Vol/OI >10x: +1.0 criterion 1 (extraordinary signal)
- 2nd+ flow same ticker today: +0.5 criterion 1
- Flow 10:00-11:30 AM: +0.5 criterion 6
- Flow after 2:00 PM: +0.5 criterion 6

VERDICT:
- TRADE: final score 6-7
- WATCH: final score 4-5
- SKIP: final score 0-3

HARD RULES:
- FULL_ASK + premium >$500K = minimum WATCH regardless of other criteria
- Vol/OI >10x + FULL_ASK = minimum WATCH regardless of other criteria
- Breakout bet (OTM <2%, no catalyst, <21 DTE, no earnings) = maximum WATCH

URGENT DIRECTIONAL BET RULE (override breakout cap):
- FULL_ASK + premium >$500K + OTM <3% + DTE <5 = TRADE signal
- This pattern means: buyer paid urgently for near-ATM options expiring very soon
- They expect an immediate large move — this is high conviction, not a breakout gamble
- Example: $596K FULL_ASK on DELL 230C with 2 DTE = someone expects DELL to move NOW
- Do NOT penalize short DTE when combined with FULL_ASK + large premium + near-ATM

Return ONLY valid JSON, no text after the closing brace:
{
  "raw_score": <number 0-7>,
  "market_adjustment": <number>,
  "final_score": <raw_score + market_adjustment>,
  "verdict": "<TRADE|WATCH|SKIP>",
  "checklist": {
    "criterion_1": {"score": <0|0.5|1>, "note": "<10 words>"},
    "criterion_2": {"score": <0|0.5|1>, "note": "<10 words>"},
    "criterion_3": {"score": <0|0.5|1>, "note": "<10 words>"},
    "criterion_4": {"score": <0|0.5|1>, "note": "<10 words>"},
    "criterion_5": {"score": <0|0.5|1>, "note": "<10 words>"},
    "criterion_6": {"score": <0|0.5|1>, "note": "<10 words>"},
    "criterion_7": {"score": <0|0.5|1>, "note": "<10 words>"}
  },
  "reasoning": "<2 sentences: flow type + key factors>",
  "one_liner": "<under 15 words, complete sentence>",
  "improvements": [
    "<under 15 words>",
    "<under 15 words>"
  ]
}"""

def default_result():
    return {
        "raw_score": 0, "final_score": 0, "verdict": "SKIP",
        "market_adjustment": 0, "checklist": {},
        "reasoning": "Analysis unavailable.",
        "one_liner": "Could not analyze — check Railway logs.",
        "improvements": ["Check ANTHROPIC_API_KEY in Railway variables"]
    }

def cap(text: str, limit: int = 120) -> str:
    if not text or len(text) <= limit: return text or ""
    t  = text[:limit]
    sp = t.rfind(" ")
    return (t[:sp] + "…") if sp > 0 else (t + "…")

def score_trade(trade: dict, data: dict, pattern: dict = None) -> dict:
    if pattern is None:
        pattern = {}

    mkt     = data.get("market", {})
    premium = trade.get("premium", 0) or 0
    prem_str = f"${premium/1000000:.1f}M" if premium >= 1000000 else f"${premium/1000:.0f}K" if premium >= 1000 else f"${premium}"

    # Extract nested dicts before f-string to avoid {{}} hashable errors
    tod    = data.get('time_of_day') or {}
    sector = data.get('sector') or {}

    prompt = f"""Score this options flow alert:

TWEET: {trade.get('raw_text','')}
TICKER: {trade.get('ticker')} | STRIKE: {trade.get('strike')} | TYPE: {trade.get('option_type','call').upper()}
EXPIRY: {trade.get('expiry','?')} | DTE: {data.get('days_to_expiry','?')} days
PREMIUM: {prem_str}

FILL: {data.get('fill_type','UNKNOWN')} {data.get('fill_emoji','')} — {data.get('fill_label','unknown')}

PREMIUM SIZE: {data.get('premium_label','') or 'Standard size'} {data.get('premium_emoji','')}

VOL/OI: {data.get('vol_oi_ratio','N/A')}x {data.get('vol_oi_emoji','')} — {data.get('vol_oi_label','') or 'N/A'}
Note: Vol/OI >10x on FULL_ASK = extraordinary informed money signal

STOCK: ${data.get('stock_price','N/A')} | OTM: {data.get('otm_pct','N/A')}%
OI: {data.get('open_interest','N/A')} | Spread: {data.get('spread_pct','N/A')}%

EARNINGS: {data.get('earnings_date','Unknown')} | Context: {data.get('earnings_context','Unknown')}
Timing: {data.get('expiry_timing_label','N/A')} {data.get('expiry_timing_emoji','')}

BREAKOUT: {data.get('is_breakout_bet',False)} — {data.get('breakout_label','')}

TIME: {tod.get('label','?')} {tod.get('emoji','')}

MARKET: VIX {mkt.get('vix','?')} {mkt.get('vix_label','')} | SPY {mkt.get('spy_trend','?')}
Market adjustment: {mkt.get('market_score_adjustment',0)}
Sector {sector.get('etf','?')}: {sector.get('sector_trend','N/A')}

PATTERN: Alert #{pattern.get('count',1)} for this ticker today

Apply hard rules:
- FULL_ASK + premium >$500K = minimum WATCH
- Vol/OI >10x + FULL_ASK = minimum WATCH  
- Breakout bet = maximum WATCH"""

    for attempt in range(2):
        try:
            response = get_client().messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}]
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r"```json|```", "", raw).strip()
            # Strip anything after final }
            last = raw.rfind("}")
            if last != -1:
                raw = raw[:last+1]
            result = json.loads(raw)

            # Enforce hard rules
            fill_type = data.get("fill_type","")
            vol_oi    = data.get("vol_oi_ratio") or 0
            premium_v = trade.get("premium") or 0

            is_full_ask   = fill_type in ("FULL_ASK","MOSTLY_ASK")
            is_large      = premium_v >= 500000
            is_mega_vol   = float(vol_oi) >= 10 if vol_oi else False
            is_breakout   = data.get("is_breakout_bet", False)

            verdict = result.get("verdict","SKIP")

            # Hard rules — enforce minimum verdicts regardless of scorer
            is_near_atm    = abs(float(data.get("otm_pct") or 99)) < 3.0
            is_short_dte   = (data.get("days_to_expiry") or 99) <= 5
            is_pre_earnings = bool(data.get("earnings_date") and not data.get("earnings_is_past"))

            # Rule 1: FULL_ASK + large premium = minimum WATCH
            # Rule 2: Vol/OI >10x alone = minimum WATCH
            # Rule 3: Large premium + Vol/OI >5x = minimum WATCH
            # Rule 4: FULL_ASK + near-ATM + short DTE = TRADE signal
            #   (urgent buyer with 2-5 DTE is making a very specific directional bet)
            should_be_trade = (
                is_full_ask and is_large and is_near_atm and is_short_dte
            )
            should_upgrade = (
                (is_full_ask and is_large) or
                is_mega_vol or
                (is_large and float(vol_oi) >= 5 if vol_oi else False)
            )

            if should_be_trade and verdict in ("SKIP", "WATCH"):
                result["verdict"]     = "TRADE"
                result["final_score"] = max(result.get("final_score",0), 6.0)
                result["reasoning"]   = (result.get("reasoning","") +
                    " Upgraded to TRADE: FULL_ASK + large premium + near-ATM + urgent DTE = high conviction directional bet.")
            elif should_upgrade and verdict == "SKIP":
                result["verdict"]     = "WATCH"
                result["final_score"] = max(result.get("final_score",0), 4.0)
                reason = "Vol/OI >10x" if is_mega_vol else "FULL_ASK + large premium"
                result["reasoning"]   = (result.get("reasoning","") +
                    f" Upgraded to WATCH: {reason}.")

            if is_breakout and result.get("verdict") == "TRADE":
                result["verdict"] = "WATCH"
                result["final_score"] = min(result.get("final_score",5), 5.0)

            result["one_liner"]    = cap(result.get("one_liner",""))
            result["improvements"] = [cap(i) for i in (result.get("improvements") or [])]
            return result

        except Exception as e:
            err = str(e)
            print(f"[SCORER] Attempt {attempt+1} error: {err[:80]}")
            if attempt == 0:
                wait = 45 if "rate_limit" in err.lower() else 10
                print(f"[SCORER] Waiting {wait}s then retrying")
                time.sleep(wait)

    return default_result()
