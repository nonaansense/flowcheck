# FlowCheck v18 — Options Flow Intelligence

Railway-hosted system that monitors Bullflow.io SSE stream, scores options flow, and delivers structured trade alerts to Telegram.

**Live URL:** https://web-production-19e44.up.railway.app  
**Stack:** FastAPI · Supabase · Railway · Claude Haiku · Finnhub · Tiingo · Tradier · Polygon · Bullflow.io

---

## Architecture

```
Bullflow SSE stream → background thread → Prefilter → Scorer → Telegram 🅱
@FL0WG0D tweets   → IFTTT → /webhook → Vision parser → Scorer → Telegram 🐦
Robinhood screenshots → Telegram bot → Trade journal

7:30 AM ET  (Mon-Fri) → Pre-market gap alerts (watchlist positions)
8:00 AM ET  (Mon-Fri) → Pre-market summary + carryover OI check
8:30 AM ET  (Mon-Fri) → Earnings calendar pre-load (14 days)
Market hours (5 min)  → Technical scanner — breakout detection
Market hours (15 min) → Exit signal monitor
4:00 PM ET  (Mon-Fri) → Tape watcher EOD summary
4:00 PM ET  (Mon-Fri) → Outcome tracking
4:15 PM ET  (Mon-Fri) → EOD OI verification via Tradier
4:30 PM ET  (Mon-Fri) → EOD price + peak updates via Tradier
4:45 PM ET  (Friday)  → Weekly P&L + signal hit rate report
12:01 AM ET (daily)   → Analyses cleanup + archive to Supabase
```

---

## Bullflow Alert Pipeline

Two named Bullflow filters drive all conviction logic:

| Filter name | Purpose |
|-------------|---------|
| `Big_Money_Order_Flow` | Institutional / large premium fills |
| `Retail_Order_Flow` | Retail follow-through fills ($25K–$500K) |

Every fill from either filter is routed through three independent detection layers before the main FlowCheck scorer sees it.

---

## Detection Layers

### 1. Tape Watcher

Fires only when **big money has a footprint**. Order of arrival does not matter — retail fills accumulate throughout the day and count when big money arrives later (and vice versa). All same-ticker+direction fills count regardless of strike or expiry.

**Rule A — Intraday conviction:**
- 1+ fill from `Big_Money_Order_Flow`
- 1+ fill from `Retail_Order_Flow` (same ticker + direction, same trading day)
- Strike/expiry mix-and-match OK
- Re-fires when a new big money fill arrives after initial alert

**Rule B — Multi-day BM accumulation:**
- 2+ fills from `Big_Money_Order_Flow` on the **exact same** strike + expiry
- Across different calendar days (7-day window, 5 trading days)
- Fires without retail — repeat accumulation at the same contract is standalone conviction

Pure retail flow (no big money) is always silently ignored.

### 2. Cross-Filter Conviction

Tracks rolling state per ticker + direction. Fires when thresholds are met.

**Normal conviction:**
- `CONVICTION_BIG_MONEY_MIN` (default 1) fills from `Big_Money_Order_Flow`
- `CONVICTION_RETAIL_MIN` (default 2) fills from `Retail_Order_Flow`
- Retail fills count regardless of strike/expiry (mix-and-match)
- Retail window: 6.5 hours (one full trading session)
- Big money window: 7 calendar days (5 trading days)

**BM auto-conviction (no retail needed):**
- 2+ fills from `Big_Money_Order_Flow` on same strike + expiry across different days
- Fires immediately with 🗓️ N-DAY ACCUMULATION badge
- Re-fires on each additional same-contract BM fill

### 3. Ticker Cluster

Fires when multiple distinct contracts on the same ticker accumulate within a rolling window — broad strike/expiry sweeping rather than a single focused bet.

---

## Double Confirmation Escalation

When **both** the tape watcher AND cross-filter conviction fire on the same ticker + direction within the same session, a `🔥🔥 DOUBLE CONFIRMATION` escalation alert fires. Two independent systems agreeing is the highest-conviction signal.

---

## Alert Cooldown

Same ticker + direction will not fire more than once per `ALERT_COOLDOWN_MINUTES` (default 10 min). Logged as `[COOLDOWN]` in Railway logs.

---

## Entry Reminder

`ENTRY_REMINDER_MINUTES` (default 10) after a tape or conviction alert fires, a follow-up message shows the current stock price and move since the alert.

---

## Straddle / Strangle Detection

When both calls and puts on the same ticker appear within `STRADDLE_WINDOW_HOURS` (default 2h) with balanced premium (within `STRADDLE_SKEW_MAX` = 40%), fires a ⚖️ STRADDLE or STRANGLE alert. Does not count as directional conviction.

---

## Sector Clustering

When `SECTOR_CLUSTER_MIN` (default 4) distinct tickers from the same sector get flow in the same direction within `SECTOR_WINDOW_HOURS` (default 8h), fires a 🌐 SECTOR CLUSTER alert. Sector fetched from Finnhub company profile.

---

## Dark Pool

Infrastructure ready for a `Dark_Pool_Order_Flow` Bullflow filter. When configured, prints ≥ $500K fire a 🌑 DARK POOL PRINT alert. Set `DARK_POOL_FILTER_NAME` env var to match your Bullflow filter name.

---

## Alert Enrichment (tape + conviction alerts)

Every tape watcher and cross-filter conviction alert includes:

| Field | Source | Example |
|-------|--------|---------|
| **GEX context** | Bullflow SPY GEX, 15-min cache | `📐 🟢 SPY positive GEX → $595 wall = gravity target` |
| **Flow count** (flow count / history) | Supabase flow_history, 30d | `📅 3rd big money alert in 30d ($2.8M total) — recurring name` |
| **Stop / target** | ATR-based (1.5× ATR, 1.5% fallback) | `🛑 Stop: $209.20 | 🎯 Target: +100%` |
| **IV Rank flag** | Finnhub IV rank | Low (<30th) = informed buying • High (>70th) = possible hedge |
| **Intraday velocity** | Fill timestamps | `⚡ 3 fills in 12min — urgent accumulation` |

---

## Expiry Clustering

When `EXPIRY_CLUSTER_MIN` (default 4) distinct tickers buy the **same expiry date** in the same direction within a session, fires a `🗓️ EXPIRY CLUSTER` alert. Signals event-driven positioning (FOMC, earnings cluster, macro event) rather than individual conviction. Only counts `Big_Money_Order_Flow` fills. Calls and puts tracked separately.

```
EXPIRY_CLUSTER_MIN      = 4     # tickers to trigger
EXPIRY_CLUSTER_WINDOW_H = 6.5   # rolling window (hours)
```

---

## P&L Attribution by signal combination (attribution)

The Friday weekly report shows win rates by signal combination — `conviction+tape: 75% (6/8)` vs `conviction: 44% (4/9)` — derived from the `signal_sources` field logged per trade outcome. Top 6 combinations ranked by frequency. Builds automatically from live data over time.

---

## Alert Format

### Tape Watcher — Rule A (intraday)
```
🎬 TAPE CONVICTION (retail tracked → BM confirmed) — Big_Money_Order_Flow
━━━ 📈 INTRADAY: $NVDA 220C ━━━

💰 BIG MONEY (1 fill | $1.1M):
  [BIG $] 220C 07/17/26 | $4.80 | $1.1M ⚡ | 2:43 PM

📊 RETAIL CONFIRM (2 fills | $132K):
  [RETAIL] 225C 08/21/26 | $3.20 | $85K | 11:12 AM
  [RETAIL] 215C 07/17/26 | $2.90 | $47K | 1:58 PM

💵 Total: $1.2M | Skew: 89% BM [████████░░]
Stock: $212.45 | 30d DTE
📊 Float: 24.4B shares | Short int: 0.9%
📅 Earnings: Aug 20, 2026 AMC
💡 Big money + retail same day = intraday conviction
```

### Tape Watcher — Rule B (multi-day)
```
🗓️  TAPE ACCUMULATION — Big_Money_Order_Flow
━━━ 📈 2-DAY BIG MONEY: $NVDA 220C 07/17/26 ━━━

💰 BIG MONEY (2 fills | $2.0M | 2 sessions):
  [BIG $] 220C 07/17/26 | $4.80 | $1.1M ⚡ | 2:43 PM (Jun 18)
  [BIG $] 220C 07/17/26 | $4.65 | $900K ⚡ | 10:15 AM (Jun 19)

📅 Sessions: Jun 18, Jun 19
💡 Same contract bought multiple sessions = institutional accumulation
```

### Cross-Filter Conviction
```
🔥 CROSS-FILTER CONVICTION
━━━ 📈 BULLISH CONVICTION: $NVDA ━━━

💰 BIG MONEY (1 fill | $1.1M):
  [BIG $] 220C 07/17/26 | $4.80 | $1.1M ⚡ | 2:43 PM

📊 RETAIL FOLLOW (2 fills | $132K):
  [RETAIL] 225C 08/21/26 | $3.20 | $85K | 11:12 AM
  [RETAIL] 215C 07/17/26 | $2.90 | $47K | 1:58 PM

💵 Total deployed: $1.2M
⚖️  Flow skew: 89% big money [████████░░]
📊 Float: 24.4B shares | Short int: 0.9%
💡 Big money entered → retail confirmed (calls across any strike/expiry) = conviction
```

### Main FlowCheck alert (TRADE)
```
━━━ SIGNAL ━━━
✅ NVDA 190C 06/20/26 [21d] 🟢$214 🅱
6.5/7→ 6.5/7 TRADE · VIX 15.4 Calm · SPY +0.1%

━━━ FLOW ━━━
💰 $1.1M — whale activity
🚨 FULL_ASK — maximum aggression
🚨 Vol/OI 8.2x — massive new position
  ⚡ Sweep · OTM 3.2% · 21d DTE · Float: 24.4B | Short: 0.9%
  ⚠️ Stock up 2.1% vs ADM 1.4% (1.5x) — approaching extended territory

━━━ CONTEXT ━━━
🏢 NVIDIA Corporation — Technology
📅 Earnings: Aug 27, 2026 AMC
📰 NVIDIA AI chip demand surges (Reuters, 3h ago)

━━━ THESIS ━━━
→ Pre-earnings accumulation with strong Vol/OI conviction
→ ATR suggests 14% move possible in 21 days
❌ Expiry 7d BEFORE earnings — misses catalyst

━━━ ENTRY ━━━
💰 Flow filled @ $6.25 | Limit: $6.44
💰 Size: 2 contracts @ $6.25 = $1,250 (1.2%)
🛑 Stop: $203.80 | 🎯 Target: +100%
📊 Support: $188.50 → $185.20

⏰ Opening noise window (9:30–10:00 ET) — flow reliability lower
```

---

## Scoring (6-point conviction)

Claude Haiku evaluates each flow signal on 6 criteria:

| Score | Verdict | Action |
|-------|---------|--------|
| 5–6 | ELITE / HIGH | Full TRADE alert |
| 4 | MODERATE | Full TRADE alert |
| 2–3 | LOW | WATCH — stored, not sent |
| 0–1 | SKIP | Dropped |

Time-of-day weighting note appended when flow arrives during opening noise window (9:30–10:00 ET), pre-market, or late-day (3:30 PM+).

---

## Outcome Tracking & Weekly Report

Every trade result logs `signal_sources` (which of tape/conviction/cluster/sector fired). The Friday weekly report includes signal hit rates broken down by source — empirical data on which combinations perform best.

---

## ADM Context

Average Daily Move calculated over the last 20 trading days. If today's stock move exceeds 1.5x ADM, an extended-move warning appears in the alert. Entry risk is higher when the easy money has already been made.

---

## Retail Flow Toggle

```
RETAIL_FLOW_ENABLED = true   (default)
```

Set to `false` to stop processing `Retail_Order_Flow` fills entirely. Big money (Rule B accumulation) still fires. Toggle mid-session with `/retail on` / `/retail off` in Telegram without redeploying.

---

## IPO Risk

Recent IPOs (within `RECENT_IPO_THRESHOLD_DAYS`) are flagged in alerts with thin-float warning plus lockup expiry countdown (standard 180-day lockup from IPO date).

---

## Railway Variables

```
# Bullflow
BULLFLOW_API_KEY             = bull_01c7e...
DUAL_FLOW_MODE               = true

# Bullflow named filters
CONVICTION_BIG_MONEY_FILTER  = Big_Money_Order_Flow
CONVICTION_RETAIL_FILTER     = Retail_Order_Flow
RETAIL_FLOW_ENABLED          = true

# Conviction thresholds
CONVICTION_BIG_MONEY_MIN     = 1        # BM fills required for normal conviction
CONVICTION_RETAIL_MIN        = 2        # retail fills required for normal conviction
CONVICTION_WINDOW_HOURS      = 6.5      # retail fill window (one trading session)
CONVICTION_BM_MULTIDAY_DAYS  = 7        # BM fill window (7 calendar = 5 trading days)
CONVICTION_RETAIL_MIN_PREMIUM = 25000   # retail lower bound
CONVICTION_RETAIL_MAX_PREMIUM = 500000  # retail upper bound

# Tape watcher
TAPE_BM_MULTIDAY_DAYS        = 7        # same as CONVICTION_BM_MULTIDAY_DAYS

# Alert behaviour
ALERT_COOLDOWN_MINUTES       = 10       # suppress repeat same-ticker alerts
ENTRY_REMINDER_MINUTES       = 10       # follow-up check after alert fires
DOUBLE_CONFIRM_WINDOW_HOURS  = 6.5      # tape + conviction escalation window

# Straddle/strangle
STRADDLE_WINDOW_HOURS        = 2
STRADDLE_SKEW_MAX            = 0.4

# Sector clustering
SECTOR_CLUSTER_MIN           = 4
SECTOR_WINDOW_HOURS          = 8

# Dark pool (optional — create filter on Bullflow dashboard)
DARK_POOL_FILTER_NAME        = Dark_Pool_Order_Flow

# Main filters
FILTER_MIN_PREMIUM           = 500000
FILTER_MIN_DTE               = 1        # 0DTE filtered everywhere
FILTER_MAX_DTE               = 120
FILTER_MAX_OTM               = 20.0
FILTER_MAX_ITM               = 10.0
FILTER_EXCLUDE_SECTORS       = Biotechnology,Pharmaceutical,REIT,Cannabis

# APIs
ANTHROPIC_API_KEY            = ...
TELEGRAM_BOT_TOKEN           = ...
TELEGRAM_CHAT_ID             = ...
TELEGRAM_TRADE_CHAT_ID       = ...      # conviction/tape alerts
TELEGRAM_ALL_CHAT_ID         = ...      # all-alerts channel
SUPABASE_URL                 = ...
SUPABASE_KEY                 = ...
FINNHUB_API_KEY              = ...
TRADIER_TOKEN                = ...
POLYGON_API_KEY              = ...
TIINGO_API_KEY               = ...

# Account
ACCOUNT_SIZE                 = 100000
BASE_URL                     = https://web-production-19e44.up.railway.app
RAILWAY_BALANCE              = 4.37     # update after each top-up
RAILWAY_DAILY_COST           = 0.37
```

---

## Persistence (Supabase keys)

| Key | Content | Window |
|-----|---------|--------|
| `tape_history_v2` | Tape watcher state (ticker+direction buckets) | 7d BM / daily retail |
| `conviction_state` | Cross-filter conviction state | 7d BM / 6.5h retail |
| `analyses_today` | Today's scored alerts | Daily |
| `analyses_week_YYYY-MM` | Monthly archive | Permanent |
| `flow_history` | 30-day flow capture log | 30 days |
| `journal` | All trade entries | Permanent |
| `outcomes` | Trade outcome tracking with signal_sources | Permanent |
| `watchlist` | Technical scanner tickers | Permanent |
| `straddle_history` | Straddle detector state | 2h window |
| `sector_cluster_history` | Sector cluster state | 8h window |

---

## Telegram Commands

See `/help` in the bot or the Scheduled Jobs and Commands sections below.

### Scheduled Jobs

| Time (ET) | Job |
|-----------|-----|
| 7:30 AM Mon–Fri | Pre-market gap alerts (watchlist positions) |
| 8:00 AM Mon–Fri | Pre-market summary + carryover OI check |
| 8:30 AM Mon–Fri | Earnings calendar pre-load (14 days) |
| Every 5 min (market hours) | Technical scanner — breakout detection |
| Every 15 min (market hours) | Exit signal monitor |
| 4:00 PM Mon–Fri | Tape watcher EOD summary |
| 4:00 PM Mon–Fri | Outcome tracking |
| 4:15 PM Mon–Fri | EOD OI verification |
| 4:30 PM Mon–Fri | EOD price + peak updates |
| 4:45 PM Friday | Weekly P&L + signal hit rate report |
| 12:01 AM daily | Analyses cleanup + archive |

---

## Utility Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Status check |
| `POST /test-alert` | Test flow alert (JSON body) |
| `GET /sync-bullflow-filters` | Recreate Bullflow custom alert |
| `GET /test-bullflow` | Verify Bullflow connection |
| `GET /journal-view` | Web journal UI |
| `GET /analysis/{id}` | Full analysis web page |
| `GET /history` | Recent alert history |

---

## Data Sources

| Source | Used for |
|--------|----------|
| Bullflow.io | Real-time SSE options flow |
| Finnhub | Price, earnings, company profile, sector, IPO dates |
| Tiingo | SPY/XLK daily history |
| Yahoo Finance | VIX |
| Tradier | Options chain, intraday candles, EOD pricing |
| Polygon | Short interest, ATR, greeks |
| Anthropic Haiku | Scoring, vision parsing, /eval, descriptions |

---

## Known Limitations

- 0DTE contracts filtered at every layer — tape, conviction, and main scorer
- Short interest: bi-weekly FINRA cadence (not real-time)
- Sector data from Finnhub profile (cached per session, not persisted)
- Railway rolling deploy causes ~15s SSE overlap (self-resolving via PID lock)
- `FILTER_MAX_ITM` applied in FlowCheck only — Bullflow API does not support it
