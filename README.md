# FlowCheck v18 — Automated Options Flow Intelligence

Railway-hosted system that monitors @FL0WG0D tweets and Bullflow.io SSE stream, scores options flow with Claude Haiku, and delivers structured trade alerts to Telegram.

**Live URL:** https://web-production-19e44.up.railway.app  
**Stack:** FastAPI · Supabase · Railway · Claude Haiku · Finnhub · Tiingo · Massive/Polygon · Bullflow.io

---

## Architecture

```
@FL0WG0D tweets → IFTTT → /webhook → FlowCheck scorer → Telegram
Bullflow SSE stream → /bullflow-stream → FlowCheck scorer → Telegram
Robinhood screenshots → Telegram bot → Journal
```

---

## Alert Format

```
✅ NVDA 190C 06/20/26 [21d] 🟢$214 🐦        ← Source: 🐦 FlowGod | 🅱 Bullflow
6.5/7→ 6.5/7 TRADE                            ← Raw score → adjusted score
VIX 15.4 Calm · SPY Flat +0.1% today          ← Market regime
🚨 FULL_ASK — maximum aggression              ← Fill type
👀 NOTABLE flow $1.1M — whale activity        ← Premium tier
🚨 Vol/OI 8.2x — massive new position         ← Volume signal
✅ Short interest: 3.0% — low — clean setup   ← Massive short interest (bi-weekly)
💰 Flow filled @ $6.25 | Entry limit: $6.44   ← Bullflow fill price + suggested entry
💰 Size: 2 contracts @ $6.25 = $1,250 (1.2%) ← Position sizing
🎯 Target: +100% | Stop: -60% option loss      ← Exit targets by DTE
📊 Support: $188.50 → $185.20                 ← Key levels
```

---

## Scoring System (7-point)

Claude Haiku evaluates each flow on 7 criteria:

| Points | Verdict | Action |
|--------|---------|--------|
| 6-7 | ✅ TRADE | Alert sent to Telegram |
| 4-5 | 👀 WATCH | Stored silently (Bullflow) / Sent (FlowGod) |
| 0-3 | ❌ SKIP | Discarded |

**Score boosts:**
- PUT_SELL_BID ≥$500K + ≥5x Vol/OI → force 6.0 minimum
- Sector rotation (3rd flow in sector) → +1.0 | (5th+) → +1.5
- Cross-source confirmation (FlowGod + Bullflow same ticker) → 🔥 CONFIRMED

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

Web journal at `/journal-view` — tracks entries, exits, P&L, peak returns.

### Logging trades via Telegram bot

**Entry (screenshot):** Send Robinhood fill confirmation photo  
**Exit (screenshot):** Send BTC/STC confirmation photo  
**Manual entry:** `/entry TICKER STRIKE CALL/PUT EXPIRY CONTRACTS PRICE`  
**Manual exit:** `/exit TICKER PRICE`

### Robinhood screenshot detection

| Screen shows | Position effect | Detected as |
|-------------|----------------|-------------|
| Buy | Open | BTO → entry |
| Sell | Open + Est credit | STO → entry (put sell) |
| Sell | Close | STC → exit |
| Buy | Close + Total cost | BTC → exit |

**P&L calculation:**
- BTO → STC: `(exit - entry) × contracts × 100`
- STO → BTC: `(entry - exit) × contracts × 100` (profit when option decays)
- Realized P&L from Robinhood screenshot used when available

---

## Short Interest

Fetched from Massive (Polygon) `/stocks/v1/short-interest` — bi-weekly FINRA data.

| Short % | Display | Context |
|---------|---------|---------|
| ≥ 25% | 🔥 | Extreme — squeeze candidate |
| ≥ 15% | ⚠️ | Elevated — squeeze potential |
| ≥ 8% | 📊 | Moderate |
| < 8% | ✅ | Low — clean setup |

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

## Railway Variables

```
BULLFLOW_API_KEY        = bull_01c7e...
DUAL_FLOW_MODE          = true
FLOW_SOURCE             = flowgod
FILTER_MIN_PREMIUM      = 500000
FILTER_MIN_DTE          = 7
FILTER_MAX_DTE          = 90
FILTER_MAX_OTM          = 20.0
FILTER_EXCLUDE_ETF_HEDGES = true
BULLFLOW_MIN_SCORE      = 6.0
MASSIVE_API_KEY         = (Massive/Polygon key)
POLYGON_API_KEY         = (legacy Polygon key)
TELEGRAM_BOT_TOKEN      = ...
TELEGRAM_CHAT_ID        = ...
FINNHUB_API_KEY         = ...
TIINGO_API_KEY          = ...
SUPABASE_URL            = ...
SUPABASE_KEY            = ...
ANTHROPIC_API_KEY       = ...
ACCOUNT_SIZE            = 100000
```

---

## Utility Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Status check |
| `POST /test-alert` | Test flow alert (pass JSON body) |
| `GET /sync-bullflow-filters` | Recreate Bullflow custom alert |
| `GET /test-bullflow` | Verify Bullflow connection |
| `GET /journal-view` | Web journal UI |
| `GET /backfill-price-history` | Seed price history from last_price |
| `GET /analysis/{id}` | Full analysis page |

---

## Data Sources

| Source | Used for | Tier |
|--------|----------|------|
| Finnhub | Price, earnings, float | Free |
| Tiingo | SPY history | Free |
| Yahoo Finance | VIX | Free |
| Massive/Polygon | Greeks, short interest, ATR | Paid |
| Bullflow.io | Real-time options flow SSE | Paid |
| Anthropic Haiku | Flow scoring, vision parsing | Pay-per-use |

---

## Known Limitations

- Polygon/Massive candles: 401 on free tier (technical scanner disabled)
- Bullflow duplicate stream on Railway rolling deploy (resolves in ~60s)
- `$AI` ticker (C3.ai) — Finnhub can't resolve symbol
- Short interest: bi-weekly cadence (not real-time)
