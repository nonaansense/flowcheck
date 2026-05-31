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
EOD pricer → daily price/peak updates → position tracking
Pre-market summary → 8AM ET weekdays → Telegram
Weekly P&L report → Friday 4:45PM ET → Telegram
```

---

## Alert Format

```
✅ NVDA 190C 06/20/26 [21d] 🟢$214 🅱         ← verdict | option | DTE | stock price | source
6.5/7→ 6.5/7 TRADE                             ← raw score → adjusted score | verdict
VIX 15.4 Calm · SPY Flat +0.1% today           ← market regime
🚨 FULL_ASK — maximum aggression               ← fill type
👀 NOTABLE flow $1.1M — whale activity         ← premium tier
🚨 Vol/OI 8.2x — massive new position          ← volume signal
✅ Short interest: 3.0% | 1.4d to cover        ← Massive bi-weekly short interest
🅱 Bullflow caught this early — FlowGod confirms ← cross-source confirmation
💰 Flow filled @ $6.25 | Entry limit: $6.44    ← fill price + suggested entry
💰 Size: 2 contracts @ $6.25 = $1,250 (1.2%)  ← position sizing
🎯 Target: +100% | Stop: -60% option loss       ← exit targets by DTE
📊 Support: $188.50 → $185.20                  ← key levels
```

---

## Scoring System (7-point)

Claude Haiku evaluates each flow on 7 criteria:

| Points | Verdict | Action |
|--------|---------|--------|
| 6-7 | ✅ TRADE | Always sent to Telegram |
| 4-5 | 👀 WATCH | Sent (FlowGod) / Stored silently (Bullflow) |
| 0-3 | ❌ SKIP | Discarded |

**Score boosts:**
- PUT_SELL_BID ≥$500K + ≥5x Vol/OI → force 6.0 minimum
- Sector rotation (3rd flow in sector) → +1.0 | (5th+) → +1.5
- FlowGod confirms a prior Bullflow alert → +0.5 (Bullflow was early = strong signal)

**Cross-source confirmation logic:**
- Bullflow alerts first → FlowGod tweets later = `🅱 Bullflow caught this early — FlowGod now confirms` + **+0.5 boost**
- FlowGod tweets first → Bullflow alerts later = `🐦 FlowGod already on this — Bullflow late confirmation` (no boost — Bullflow was slow)

---

## Dual Flow Sources

### 🐦 FlowGod (IFTTT → webhook)
- @FL0WG0D tweets → IFTTT → `/webhook`
- Vision parser extracts ticker, strike, expiry, fill type from screenshot
- All WATCH/TRADE verdicts sent to Telegram

### 🅱 Bullflow (SSE stream)
- Real-time options flow stream from bullflow.io
- Custom alert: `premiumMin: $500K + Stocks only + DTE 7-90 + OTM ≤20%`
- Ticker-level 2h dedup (prevents SNOW×15 repeats)
- **TRADE only** sent to Telegram (WATCH stored silently)

---

## Filters (Bullflow)

| Filter | Value | Purpose |
|--------|-------|---------|
| Premium | ≥ $500K | Removes retail noise |
| DTE | 7–90 days | No lotto tickets or multi-year LEAPs |
| OTM | ≤ 20% | No deep OTM lotto plays |
| Stocks only | true | Excludes SPX/SPXW/RUT index hedges |
| Ticker dedup | 2h window | One alert per ticker per 2 hours |

---

## Journal System

Web journal at `/journal-view` — tracks entries, exits, P&L, peak returns, left on table.

### Logging trades via Telegram bot

**Entry (screenshot):** Send Robinhood fill confirmation photo  
**Exit (screenshot):** Send BTC/STC confirmation photo  
**Manual entry:** `/entry TICKER STRIKE CALL/PUT EXPIRY CONTRACTS PRICE`  
**Manual exit:** `/exit TICKER PRICE`

### Robinhood screenshot detection

| Screen shows | Position effect | Est credit/cost | Detected as |
|-------------|----------------|-----------------|-------------|
| Buy | Open | Est debit | BTO → entry |
| Sell | Open | Est credit | STO → entry (put sell) |
| Sell | Close | — | STC → exit |
| Buy | Close | Total cost + Realized profit | BTC → exit |

Detection uses priority chain:
1. `Realized profit` present → always exit
2. `Position effect: Close` → exit
3. `Est credit + Sell` → STO entry

**P&L calculation:**
- BTO → STC: `(exit - entry) × contracts × 100`
- STO → BTC: `(entry - exit) × contracts × 100` (profit when premium decays)
- Realized P&L from Robinhood screenshot used when available (most accurate)

**Editable journal fields:** entry_price, exit_price, strike, contracts, expiry, ticker, option_type, order_type, fill_type, note, score, verdict

---

## Short Interest

Fetched from Massive (Polygon) `/stocks/v1/short-interest` — bi-weekly FINRA data.  
Requires `MASSIVE_API_KEY`.

| Short % | Display | Bullish flow context |
|---------|---------|---------------------|
| ≥ 25% | 🔥 | Extreme — squeeze candidate |
| ≥ 15% | ⚠️ | Elevated — squeeze potential |
| ≥ 8% | 📊 | Moderate |
| < 8% | ✅ | Low — clean setup |

---

## Technical Scanner

Scans all watchlist tickers every 5 minutes for M5/M10/M15/M30/H1 breakout signals using Massive intraday candles.

- Uses `MASSIVE_API_KEY` + `MASSIVE_API_KEY_2` in round-robin rotation (doubles call limit)
- Expired options auto-removed from watchlist on reload
- Fires Telegram alert when breakout detected on open position

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
| 8:00 AM ET (Mon-Fri) | Pre-market summary — open positions + watchlist |
| 9:00 AM ET (daily) | Railway balance check |
| 9:30 AM ET (Mon-Fri) | Market open — position monitor starts |
| Every 5 min (market hours) | Technical scanner — breakout detection |
| 4:30 PM ET (Mon-Fri) | EOD pricer — update all position prices/peaks |
| 4:45 PM ET (Friday) | Weekly P&L report |
| 12:01 AM ET (daily) | Analyses cleanup — reset daily memory |

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
MASSIVE_API_KEY_2         = (secondary Massive key — doubles rate limit)
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
RAILWAY_BALANCE           = 4.46
RAILWAY_DAILY_COST        = 0.37
```

---

## Utility Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Status check |
| `POST /test-alert` | Test flow alert (JSON body) |
| `GET /sync-bullflow-filters` | Recreate Bullflow custom alert |
| `GET /test-bullflow` | Verify Bullflow connection + list alerts |
| `GET /journal-view` | Web journal UI |
| `GET /backfill-price-history` | Seed price history from last_price |
| `GET /analysis/{id}` | Full analysis page |

**Test alert body:**
```json
{
  "ticker": "PLTR",
  "opt_type": "call",
  "strike": "130",
  "expiry": "07/18/26",
  "premium": 500000,
  "fill_type": "FULL_ASK",
  "vol_oi": 6.0,
  "oi": 1000,
  "avg_fill_price": 6.25
}
```

---

## Data Sources

| Source | Used for | Tier |
|--------|----------|------|
| Finnhub | Price, earnings, float | Free |
| Tiingo | SPY daily history | Free |
| Yahoo Finance | VIX | Free (no key) |
| Massive/Polygon (key 1) | Short interest, ATR, greeks | Paid |
| Massive/Polygon (key 2) | Technical scanner candles (round-robin) | Paid |
| Bullflow.io | Real-time options flow SSE | Paid |
| Anthropic Haiku | Flow scoring, vision parsing | Pay-per-use |

---

## Known Limitations

- Duplicate Bullflow stream during Railway rolling deploy (~60s overlap, self-resolving)
- `$AI` ticker (C3.ai) — maps correctly but Finnhub resolution unreliable
- Short interest: bi-weekly cadence (not real-time)
- Railway balance check is manual — upgrade to Hobby plan for automatic API-based balance monitoring
- Pre-market summary and weekly report require test/validation on first run
