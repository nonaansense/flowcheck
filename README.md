# FlowCheck v18 — Automated Options Flow Intelligence

Railway-hosted system that monitors @FL0WG0D tweets and Bullflow.io SSE stream, scores options flow with Claude Haiku, and delivers structured trade alerts to Telegram.

**Live URL:** https://web-production-19e44.up.railway.app  
**Stack:** FastAPI · Supabase · Railway · Claude Haiku · Finnhub · Tiingo · Tradier · Massive/Polygon · Bullflow.io

---

## Architecture

```
@FL0WG0D tweets → IFTTT → /webhook → Vision parser → Scorer → Telegram 🐦
Bullflow SSE stream → background thread → Prefilter → Scorer → Telegram 🅱
Robinhood screenshots → Telegram bot → Trade journal
EOD pricer (Tradier) → 4:30PM daily → position price/peak updates
Pre-market summary → 8:00AM ET weekdays → carryover OI check → Telegram
Earnings calendar → 8:30AM ET weekdays → pre-loaded cache
Weekly P&L report → Friday 4:45PM ET → Telegram
Analyses archive → 12:01AM daily → Supabase weekly key
```

---

## Alert Format

### TRADE (full detail)
```
━━━ SIGNAL ━━━
✅ NVDA 190C 06/20/26 [21d] 🟢$214 🅱
6.5/7→ 6.5/7 TRADE · VIX 15.4 Calm · SPY +0.1%

━━━ FLOW ━━━
💰 $1.1M — whale activity
🚨 FULL_ASK — maximum aggression
🚨 Vol/OI 8.2x — massive new position
  ⚡ Sweep · OTM 3.2% · 21d DTE
✅ Short interest: 1.2% | 0.8d to cover — low — clean setup

━━━ CONTEXT ━━━
🏢 NVIDIA Corporation
   Designs GPUs and system-on-chip units for gaming and AI
   Technology
📅 Earnings: Aug 27, 2026

━━━ THESIS ━━━
→ Pre-earnings accumulation with strong Vol/OI conviction
→ ATR suggests 14% move possible in 21 days
❌ Expiry 7d BEFORE earnings — misses catalyst
📈 $3.2M total flow over 2 days — accumulation
🔁 NVDA 190C seen 2x — repeat buyer

━━━ ENTRY ━━━
💰 Flow filled @ $6.25 | Limit: $6.44
💰 Size: 2 contracts @ $6.25 = $1,250 (1.2%)
🛑 Stop: $203.80 (Fixed 5% stop)
🎯 Target: +100% | -60% option loss
  Sell 50% at +51%, hold to +100%
📊 Support: $188.50 → $185.20
  Thesis broken below $188.50

━━━ RISK ━━━
⚠️ News in last 24h — flow may be news-driven
📰 <a href="...">NVIDIA AI chip demand surges</a>

🐦 https://x.com/i/status/...
🔗 https://web-production-19e44.up.railway.app/analysis/42
```

### WATCH/SKIP (compact)
```
👀 PLTR 130C 07/18/26 [47d] 🟢$156.54
5.5/7→ 5.5/7 WATCH · $500K · FULL ASK · 6.0x Vol/OI
→ Informed accumulation but pre-earnings expiry weakens thesis
💰 Flow @ $6.25 | Limit: $6.44
🛑 $148.71
🎯 +110% · 2 contracts @ $6.25
VIX 15.3 Calm · SPY +0.1%
❌ Expiry 16d BEFORE earnings
📋 Full analysis → /analysis/47
```

---

## Scoring System (7-point)

Claude Haiku evaluates each flow on 7 criteria:

| Points | Verdict | Telegram |
|--------|---------|---------| 
| 6-7 | ✅ TRADE | Full alert sent |
| 4-5 | 👀 WATCH | Compact alert + analysis link |
| 0-3 | ❌ SKIP | Nothing sent (Bullflow) / compact (FlowGod) |

**Score boosts:**
- PUT_SELL_BID ≥$500K + ≥5x Vol/OI → force 6.0 minimum
- Sector rotation (3rd flow in sector) → +1.0 | (5th+) → +1.5
- FlowGod confirms prior Bullflow alert → +0.5 (Bullflow was early)

**WATCH → TRADE upgrade:**  
When a prior WATCH alert gets confirmed to TRADE by a second source or higher score, a follow-up Telegram notification fires automatically (skipped for test trades).

**Cross-source confirmation:**
- Bullflow first → FlowGod later = `🅱 Bullflow caught this early — FlowGod now confirms` + **+0.5 boost**
- FlowGod first → Bullflow later = `🐦 FlowGod already on this — Bullflow late` (no boost)

---

## Dual Flow Sources

### 🐦 FlowGod (IFTTT → webhook)
- @FL0WG0D tweets → IFTTT → `POST /webhook`
- 30-minute dedup window (prevents same ticker firing twice)
- Vision parser: t.co expansion → fxtwitter → vxtwitter fallback
- Extracts ticker, strike, expiry, fill type from screenshot
- Incomplete trades (missing strike/expiry) are dropped before scoring

### 🅱 Bullflow (SSE stream)
- Real-time SSE stream from bullflow.io
- Custom alert: `premiumMin: $500K + Stocks only + DTE 2–120`
- ITM filter: max 10% ITM applied in `prefilter.py` (Bullflow API doesn't support it)
- Ticker-level 2h dedup (prevents same ticker spamming)
- **TRADE only** sent to Telegram (WATCH stored silently)
- OTM% filter: max 20% OTM

---

## Bullflow Filters

| Filter | Value | Where applied |
|--------|-------|---------------|
| Premium | ≥ $500K | Bullflow API |
| DTE min | 2 days | Bullflow API |
| DTE max | 120 days | Bullflow API |
| OTM | ≤ 20% | Bullflow API + prefilter.py |
| ITM | ≤ 10% (`FILTER_MAX_ITM`) | prefilter.py only |
| Stocks only | true | Bullflow API |
| Ticker dedup | 2h window | bullflow_stream.py |
| Sector/Industry | Biotech, Pharma, REIT, Cannabis blocked | prefilter.py |

Recreate Bullflow custom alert: `GET /sync-bullflow-filters`

---

## Journal System

Web journal at `/journal-view` · P&L summary at `/journal-summary`

### Entry formats

**Single leg:**
```
/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE [@account] [DATE] [TIME]
/entry NVDA 190 C 06/20/26 2 6.25 @rh_trad
```

**Spread:**
```
/entry TICKER LONG/SHORT C/P EXPIRY CONTRACTS NET_PRICE spread:TYPE [@account]
/entry MU 1100/1200 C 01/16/27 5 12.50 spread:debit_call @rh_trad
/entry AAPL 185/180 P 06/20/26 3 2.50 spread:credit_put @rh_ira
```
Spread types: `debit_call` `debit_put` `credit_call` `credit_put`

**Exit:**
```
/exit TICKER PRICE [CONTRACTS] [DATE] [TIME]
/exit NVDA 12.50           — close all
/exit NVDA 12.50 1         — close 1 contract
```

### Robinhood screenshot detection

| Screen shows | Position effect | Field | Detected as |
|-------------|----------------|-------|-------------|
| Buy | Open | Est debit | BTO → entry |
| Sell | Open | Est credit | STO → entry (put sell) |
| Sell | Close | — | STC → exit |
| Buy | Close | Total cost + Realized profit | BTC → exit |

**P&L calculation:**
- BTO → STC: `(exit - entry) × contracts × 100`
- STO → BTC: `(entry - exit) × contracts × 100`
- Spread: `(current_net - entry_net) × contracts × 100` (both legs via Tradier)

### Spread P&L in /eval
Both legs fetched in real-time via Tradier options chain. Set `legs=spread` in journal-view to tag existing positions as spreads.

---

## Short Interest

Fetched from Massive `/stocks/v1/short-interest` — bi-weekly FINRA data.

| Short % | Display | Context |
|---------|---------|---------| 
| ≥ 25% | 🔥 | Extreme — squeeze candidate |
| ≥ 15% | ⚠️ | Elevated — squeeze potential |
| ≥ 8% | 📊 | Moderate |
| < 8% | ✅ | Low — clean setup |

---

## Technical Scanner

Scans all watchlist tickers every 5 minutes (market hours) for M5/M10/M15/M30/H1 breakout signals. Uses **Tradier `timesales` endpoint** for intraday candles (120 req/min limit, 0.6s delay between tickers). Weekends and holidays auto-skipped.

---

## Exit Targets by DTE

| DTE | Stop | Target |
|-----|------|--------|
| ≤ 3d | Exit flat by 2PM | — |
| ≤ 7d | Break entry-day low | — |
| 8–21d | -50% option | +100% |
| 22–45d | -60% option | +100% |
| > 45d | -70% option | +110% |

**PUT_SELL targets:** Capture 50-80% premium decay. Exit at 20-50% decay or near strike.

---

## Earnings Highlighting

When flow comes in for a stock with earnings within 7 days, a banner appears at the **top of the SIGNAL section**:

| Days to earnings | Display |
|-----------------|---------|
| 0 (today) | `🚨🚨 EARNINGS TODAY AMC 🚨🚨` |
| 1 (tomorrow) | `⚠️ EARNINGS TOMORROW BMO` |
| 2-7 days | `📅 EARNINGS IN 3d AMC` |
| 8+ days | `📅 Earnings: Aug 25, 2026` (in THESIS only) |

AMC = After Market Close · BMO = Before Market Open (from Finnhub earnings calendar)

---

## Scheduled Jobs

| Time | Job |
|------|-----|
| 8:00 AM ET (Mon-Fri) | Pre-market summary + yesterday carryover OI check |
| 8:30 AM ET (Mon-Fri) | Earnings calendar pre-load (14 days) |
| 9:00 AM ET (daily) | Railway balance check |
| Every 5 min (market hours) | Technical scanner — breakout detection |
| Every 15 min (market hours) | Exit signal monitor |
| 4:00 PM ET (Mon-Fri) | Outcome tracking |
| 4:15 PM ET (Mon-Fri) | EOD OI verification via Tradier |
| 4:30 PM ET (Mon-Fri) | EOD price + peak updates via Tradier |
| 4:45 PM ET (Friday) | Weekly P&L report |
| 12:01 AM ET (daily) | Analyses cleanup + archive to Supabase |

---

## Telegram Commands

### Quick keyboard
Send `/kb` or `/start` to show the persistent button keyboard. Send `/stop` to hide it.

| Button | Command | Notes |
|--------|---------|-------|
| 📊 /eval | `/eval` | Fires immediately |
| 📓 /journal | `/journal` | Fires immediately |
| 🔢 /count | `/count` | Fires immediately |
| 📈 /flow ... | `/flow TICKER` | Prompts for ticker |
| 💹 /price ... | `/price TICKER` | Prompts for ticker |
| 😐 /sent ... | `/sent TICKER` | Prompts for ticker |
| 🔬 /test | `/test` | Fires immediately |
| ⚙️ /status | `/status` | Fires immediately |
| ❓ /help | `/help` | Fires immediately |

### All commands

**System**
| Command | Description |
|---------|-------------|
| `/test` | Full connectivity check — all APIs and services |
| `/status` | Stream status, open positions count, today's alerts |
| `/kb` | Show command keyboard |
| `/stop` | Hide keyboard |
| `/help` | Command list |
| `/journal_help` | Full journal command reference |

**Flow & Research**
| Command | Description |
|---------|-------------|
| `/flow TICKER` | Today's captured flows for a ticker |
| `/flow TICKER MM-DD` | Flows for ticker on a past date |
| `/flow MM-DD` | All flows on a past date |
| `/sent TICKER` | Sentiment — price, SMAs, RSI, news, flow, insiders |
| `/price TICKER` | Real-time stock price |
| `/find TICKER` | Find open position by ticker |
| `/sectors` | Sector rotation summary |
| `/history` | Today's alert history link |

**Positions**
| Command | Description |
|---------|-------------|
| `/positions` | All open positions with P&L |
| `/eval` | AI review of all positions (HOLD/TRIM/CLOSE) |
| `/eval 21-40` | Positions 21-40 (paginate) |
| `/eval @rh_ira` | Evaluate specific account |
| `/eval NVDA` | Evaluate specific ticker |
| `/count` | Open position count by account |
| `/portfolio` | Portfolio summary by account |
| `/stats` | Win rate and P&L statistics |

**Journal**
| Command | Description |
|---------|-------------|
| `/journal` or `/jv` | Open web journal |
| `/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE` | Log single-leg entry |
| `/entry TICKER LONG/SHORT C/P EXPIRY CONTRACTS PRICE spread:TYPE` | Log spread entry |
| `/exit TICKER PRICE [CONTRACTS]` | Log exit |
| `/close TICKER` | Mark position as closed |
| `/tag TICKER #tag` | Add tag to position |
| `/debrief TICKER` | AI post-trade debrief |

**Analysis**
| Command | Description |
|---------|-------------|
| `/oi TICKER STRIKE C/P EXPIRY` | Open interest check |
| `/oi all` | OI check for all yesterday's alerts |
| `/watchlist` | Active technical watchlist |
| `/backtest URL TIME` | Replay FlowGod tweet through pipeline |

---

## Railway Variables

```
# Flow sources
BULLFLOW_API_KEY          = bull_01c7e...
DUAL_FLOW_MODE            = true
FLOW_SOURCE               = flowgod

# Filters
FILTER_MIN_PREMIUM        = 500000
FILTER_MIN_DTE            = 2
FILTER_MAX_DTE            = 120
FILTER_MAX_OTM            = 20.0
FILTER_MAX_ITM            = 10.0       # Applied in prefilter.py (Bullflow API doesn't support)
FILTER_EXCLUDE_ETF_HEDGES = true
FILTER_EXCLUDE_SECTORS    = Biotechnology,Pharmaceutical,Drug Manufacturers,REIT,Real Estate,Cannabis
FILTER_MAX_ITM_CALL       = 5    # Calls: prefer ATM/OTM only
FILTER_MAX_ITM_PUT        = 30   # Puts: deep ITM allowed (real conviction)
BULLFLOW_MIN_SCORE        = 6.0

# APIs
MASSIVE_API_KEY           = (primary Massive/Polygon key)
MASSIVE_API_KEY_2         = (secondary key — doubles rate limit)
TRADIER_TOKEN             = (options chain, candles, EOD pricing)
ANTHROPIC_API_KEY         = ...
TELEGRAM_BOT_TOKEN        = ...
TELEGRAM_CHAT_ID          = ...

# Storage
SUPABASE_URL              = ...
SUPABASE_KEY              = ...

# Account
ACCOUNT_SIZE              = 100000
BASE_URL                  = https://web-production-19e44.up.railway.app

# Channels
TELEGRAM_ALL_CHAT_ID      = (all-alerts channel — commentary/FYI tweets from FlowGod)

# Railway balance (update after each top-up)
RAILWAY_BALANCE           = 4.37
RAILWAY_DAILY_COST        = 0.37
```

---

## Utility Endpoints

| Endpoint | Purpose |
|----------|---------| 
| `GET /health` | Status check |
| `POST /test-alert` | Test flow alert (JSON body) |
| `GET /sync-bullflow-filters` | Recreate Bullflow custom alert with current filters |
| `GET /test-bullflow` | Verify Bullflow connection |
| `GET /journal-view` | Web journal UI |
| `GET /journal-summary` | P&L summary stats (JSON) |
| `GET /analysis/{id}` | Full analysis web page |
| `GET /history` | Recent alert history |
| `GET /backtest-bullflow?date=YYYY-MM-DD&speed=60` | Replay Bullflow historical flow |

**Test alert body:**
```json
{
  "ticker": "NVDA",
  "opt_type": "call",
  "strike": "190",
  "expiry": "06/20/26",
  "premium": 1100000,
  "fill_type": "FULL_ASK",
  "vol_oi": 8.2,
  "oi": 500,
  "avg_fill_price": 6.25,
  "source": "bullflow"
}
```

---

## Data Sources

| Source | Used for | Tier |
|--------|----------|------|
| Finnhub | Price, earnings, company profile | Free |
| Tiingo | SPY/XLK daily history | Free |
| Yahoo Finance | VIX | Free (no key) |
| Tradier | Options chain, intraday candles, EOD pricing | Free developer |
| Massive/Polygon (key 1) | Short interest, ATR, greeks | Paid |
| Massive/Polygon (key 2) | Round-robin rate doubling | Paid |
| Bullflow.io | Real-time SSE options flow | Paid |
| Anthropic Haiku | Scoring, vision parsing, company descriptions, /eval | Pay-per-use |

---

## Persistence

All data persists across Railway redeploys via Supabase:

| Key | Content | TTL |
|-----|---------|-----|
| `analyses_today` | Today's scored alerts | Daily |
| `analyses_yesterday` | Yesterday's alerts (for carryover OI check) | 1 day |
| `analyses_week_YYYY-MM` | Monthly archive | Permanent |
| `flow_history` | 30-day flow capture log | 30 days |
| `journal` | All trade entries | Permanent |
| `accounts` | Account config | Permanent |
| `outcomes` | Trade outcome tracking | Permanent |
| `watchlist` | Technical scanner tickers | Permanent |

---

## Known Limitations

- Duplicate Bullflow stream during Railway rolling deploy (~15s overlap, self-resolving via PID lock)
- Short interest: bi-weekly FINRA cadence (not real-time)
- Railway balance check is manual — update `RAILWAY_BALANCE` after each top-up
- Tradier candles: 120 req/min limit (0.6s delay between tickers handles this)
- ITM filter (`FILTER_MAX_ITM`) only applied in FlowCheck — Bullflow API does not support it natively
