# FlowCheck v18 — Automated Options Flow Intelligence

Railway-hosted system that monitors @FL0WG0D tweets and Bullflow.io SSE stream, scores options flow with Claude Haiku, and delivers structured trade alerts to Telegram.

**Live URL:** https://web-production-19e44.up.railway.app  
**Stack:** FastAPI · Supabase · Railway · Claude Haiku · Finnhub · Tiingo · Massive/Polygon · Bullflow.io

---

## Architecture

```
@FL0WG0D tweets → IFTTT → /webhook → Vision parser → Scorer → Telegram 🐦
Bullflow SSE stream → background thread → Scorer → Telegram 🅱
Robinhood screenshots → Telegram bot → Trade journal
EOD pricer → 4:30PM daily → position price/peak updates
Pre-market summary → 8:00AM ET weekdays → Telegram
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
  ⚡ Sweep · OTM -3.2% · 21d DTE
✅ Short interest: 1.2% | 0.8d to cover — low — clean setup

━━━ CONTEXT ━━━
🏢 NVIDIA Corporation
   Designs GPUs and system-on-chip units for gaming and AI
   Technology · Earnings: Aug 27, 2026

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

🔗 https://web-production-19e44.up.railway.app/analysis/42
```

### WATCH/SKIP (compact)
```
👀 PLTR 130C 07/18/26 [47d] 🟢$156.54
5.5/7→ 5.5/7 WATCH · $500K · FULL ASK · 6.0x Vol/OI
→ Informed accumulation but pre-earnings expiry weakens thesis
💰 Flow @ $6.25 | Limit: $6.44
🛑 $148.71 · 🎯 +110% · 2 contracts @ $6.25
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
When a prior WATCH alert gets confirmed to TRADE by a second source or higher score, a follow-up Telegram notification fires automatically.

**Cross-source confirmation:**
- Bullflow first → FlowGod later = `🅱 Bullflow caught this early — FlowGod now confirms` + **+0.5 boost**
- FlowGod first → Bullflow later = `🐦 FlowGod already on this — Bullflow late` (no boost)

---

## Dual Flow Sources

### 🐦 FlowGod (IFTTT → webhook)
- @FL0WG0D tweets → IFTTT → `/webhook`
- 30-minute dedup window (prevents same ticker firing twice)
- Vision parser extracts ticker, strike, expiry, fill type
- All WATCH/TRADE sent to Telegram

### 🅱 Bullflow (SSE stream)
- Real-time SSE stream from bullflow.io
- Custom alert: `premiumMin: $500K + Stocks only + DTE 7-90 + OTM ≤20%`
- Ticker-level 2h dedup (prevents SNOW×15)
- **TRADE only** sent to Telegram (WATCH stored silently)
- Real Vol/OI used when available in payload

---

## Bullflow Filters

| Filter | Value | Purpose |
|--------|-------|---------|
| Premium | ≥ $500K | Removes retail noise |
| DTE | 7–90 days | No lotto tickets or multi-year LEAPs |
| OTM | ≤ 20% | No deep OTM lotto plays |
| Stocks only | true | Excludes SPX/SPXW/RUT/NDX index options |
| Ticker dedup | 2h window | One alert per ticker per 2 hours |

---

## Journal System

Web journal at `/journal-view` · P&L summary at `/journal-summary`

### Robinhood screenshot detection

| Screen shows | Position effect | Field | Detected as |
|-------------|----------------|-------|-------------|
| Buy | Open | Est debit | BTO → entry |
| Sell | Open | Est credit | STO → entry (put sell) |
| Sell | Close | — | STC → exit |
| Buy | Close | Total cost + Realized profit | BTC → exit |

Detection priority: Realized profit → Position effect: Close → Est credit + Sell

**P&L calculation:**
- BTO → STC: `(exit - entry) × contracts × 100`
- STO → BTC: `(entry - exit) × contracts × 100` (profit when premium decays)
- Realized P&L from Robinhood screenshot used when available

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

Scans all watchlist tickers every 5 minutes (market hours) for M5/M10/M15/M30/H1 breakout signals using Massive intraday candles. Uses `MASSIVE_API_KEY` + `MASSIVE_API_KEY_2` in round-robin rotation. Expired options auto-removed on reload.

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

## Scheduled Jobs

| Time | Job |
|------|-----|
| 8:00 AM ET (Mon-Fri) | Pre-market summary |
| 8:30 AM ET (Mon-Fri) | Earnings calendar pre-load (14 days) |
| 9:00 AM ET (daily) | Railway balance check |
| Every 5 min (market hours) | Technical scanner — breakout detection |
| Every 15 min (market hours) | Exit signal monitor |
| 4:00 PM ET (Mon-Fri) | Outcome tracking |
| 4:15 PM ET (Mon-Fri) | EOD OI verification |
| 4:30 PM ET (Mon-Fri) | EOD price + peak updates |
| 4:45 PM ET (Friday) | Weekly P&L report |
| 12:01 AM ET (daily) | Analyses cleanup + archive |

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/status` | Stream status, open positions, today's alerts, Railway balance |
| `/price TICKER` | Real-time stock price |
| `/evaluate` | AI review of all open positions — HOLD / TRIM / CLOSE (deduped) |
| `/evaluate @rh_ira` | Evaluate specific account only |
| `/positions` | All open positions with current P&L |
| `/watchlist` | Active watchlist with DTE |
| `/portfolio` | Portfolio summary by account |
| `/stats` | Win rate and P&L statistics |
| `/exit TICKER PRICE` | Manual exit log |
| `/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE` | Manual entry log |
| `/oi TICKER STRIKE C/P EXPIRY` | Open interest check |
| `/oi all` | OI check for all yesterday's alerts |
| `/find TICKER` | Find open position by ticker |
| `/history` | Recent alert history |
| `/sectors` | Sector rotation summary |

---

## Railway Variables

```
# Flow sources
BULLFLOW_API_KEY          = bull_01c7e...
DUAL_FLOW_MODE            = true
FLOW_SOURCE               = flowgod

# Filters
FILTER_MIN_PREMIUM        = 500000
FILTER_MIN_DTE            = 7
FILTER_MAX_DTE            = 90
FILTER_MAX_OTM            = 20.0
FILTER_EXCLUDE_ETF_HEDGES = true
BULLFLOW_MIN_SCORE        = 6.0

# APIs
MASSIVE_API_KEY           = (primary Massive/Polygon key)
MASSIVE_API_KEY_2         = (secondary key — doubles rate limit)
FINNHUB_API_KEY           = ...
TIINGO_API_KEY            = ...
ANTHROPIC_API_KEY         = ...

# Telegram
TELEGRAM_BOT_TOKEN        = ...
TELEGRAM_CHAT_ID          = ...

# Storage
SUPABASE_URL              = ...
SUPABASE_KEY              = ...

# Account
ACCOUNT_SIZE              = 100000

# Railway balance monitoring (update after each top-up)
RAILWAY_BALANCE           = 4.43
RAILWAY_DAILY_COST        = 0.37
```

---

## Utility Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Status check |
| `POST /test-alert` | Test flow alert |
| `GET /sync-bullflow-filters` | Recreate Bullflow custom alert |
| `GET /test-bullflow` | Verify Bullflow connection |
| `GET /journal-view` | Web journal UI |
| `GET /journal-summary` | P&L summary stats (JSON) |
| `GET /backfill-price-history` | Seed price history |
| `GET /analysis/{id}` | Full analysis web page |
| `GET /history` | Recent alert history |

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
| Finnhub | Price, earnings, float, company profile | Free |
| Tiingo | SPY daily history | Free |
| Yahoo Finance | VIX | Free (no key) |
| Massive/Polygon (key 1) | Short interest, ATR, greeks, candles | Paid |
| Massive/Polygon (key 2) | Technical scanner candles (round-robin) | Paid |
| Bullflow.io | Real-time SSE options flow | Paid |
| Anthropic Haiku | Scoring, vision parsing, company descriptions | Pay-per-use |

---

## Position Evaluation (/evaluate)

On-demand AI review of all open positions using Claude Haiku. Each position is evaluated against current market conditions, DTE, P&L, and original thesis.

```
/evaluate          — all accounts, deduped by ticker+strike
/evaluate @rh_ira  — IRA account only
/evaluate @rh_brok — brokerage account only
```

**Output format:**
```
=== Position Review May 31 04:15PM ET ===
🟢 NVDA 190C [21d] (2 accts) — HOLD
   +45% | $214.50
   -> Thesis intact, strong momentum, healthy DTE remaining

🟡 PLTR 130C [47d] — TRIM
   +12% | $156.54
   -> Take partial profits, earnings mismatch unresolved

🔴 TSLA 500C [3d] — CLOSE
   -38% | $1.64
   -> 3 DTE, deep OTM, no recovery path before expiry
--------------------
Total open P&L: +$4,250
```

Costs ~$0.001 per position (Haiku). Max 10 positions per call.

---

## Known Limitations

- Duplicate Bullflow stream during Railway rolling deploy (~15s overlap, self-resolving)
- Short interest: bi-weekly FINRA cadence (not real-time)
- Railway balance check is manual — update `RAILWAY_BALANCE` after each top-up
- Upgrade to Railway Hobby plan for automatic API-based balance monitoring
- `$AI` ticker (C3.ai) — Finnhub resolution unreliable on free tier
