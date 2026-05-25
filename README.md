# FlowCheck v11.0

Automated options flow analysis system. Monitors @FL0WG0D on X via IFTTT,
analyzes Bullflow screenshots with Claude vision, scores against a multi-factor
checklist, and sends Telegram alerts with full market intelligence.

---

## Architecture

```
@FL0WG0D tweets
    ↓ IFTTT Pro (~30 sec)
POST /webhook
    ↓ Vision parser (Claude Haiku reads Bullflow screenshot)
Trade extraction (ticker, strike, expiry, fill, OI, vol, premium)
    ↓ Fetcher (Finnhub + Tiingo + Polygon + Yahoo)
Live data (price, earnings, VIX, SPY, Greeks, ATR, news, regime)
    ↓ Scorer (Claude Haiku 7-point checklist + hard rules)
TRADE / WATCH / SKIP verdict
    ↓ Intelligence (roll, repeat, divergence, sector rotation, dark pool)
    ↓ Risk manager (correlation, max positions, smart stop, position sizing)
Telegram alert → Main channel + TRADE channel (loud alarm)
```

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI server, webhook handler, scheduler, SMS builder |
| `fetcher.py` | Finnhub/Tiingo/Polygon/Yahoo, fill aggression, Greeks, ATR, regime |
| `scorer.py` | Claude Haiku 7-point scoring with hard rules and regime adjustments |
| `parser.py` | Tweet text parsing for Bullflow fields |
| `vision_parser.py` | Claude Haiku vision reads Bullflow screenshots |
| `economic_calendar.py` | FOMC/CPI/NFP calendar |
| `premarket_summary.py` | 8AM pre-market + 4:30PM EOD summaries |
| `premarket_gap.py` | 9AM pre-market gap check for watchlist tickers |
| `technical.py` | Polygon 1-min candles → M5/M10/M15/M30/H1 entry signals |
| `backtest.py` | Historical Polygon data backtest engine |
| `outcomes.py` | Win rate tracking (win = option up OPTION_WIN_PCT%+) |
| `exit_signals.py` | Stop/target/DTE/theta exit alerts every 15 min |
| `flow_intelligence.py` | Roll detection, repeat buyer, divergence, sector rotation |
| `risk_manager.py` | Correlation risk, max positions, smart stop, theta calendar |
| `iv_analysis.py` | IV rank, IV percentile, earnings IV crush risk |
| `news_check.py` | Finnhub ticker-specific news + insider filing cross-reference |
| `position_sizing.py` | Kelly criterion position sizing |
| `paper_trading.py` | Hypothetical TRADE tracking for comparison |
| `trade_journal.py` | Comprehensive trade journal — multi-account, screenshot logging, analytics |
| `telegram_commands.py` | Bot command handler — all /commands |
| `sentiment.py` | On-demand ticker sentiment — price, news, flow, insider, analyst |
| `market_calendar.py` | NYSE/NASDAQ holiday calendar 2025-2027, early close days |
| `storage.py` | Supabase PostgreSQL — persists journal, accounts, outcomes across redeploys |
| `debrief.py` | AI trade journal analysis via Claude Haiku |
| `price_alerts.py` | Real-time stop/target price alerts via Finnhub |
| `weekly_report.py` | Friday EOD weekly performance summary |
| `sms.py` | Telegram sender (main + TRADE channels) |
| `Procfile` | `web: uvicorn main:app --host 0.0.0.0 --port $PORT` |
| `railway.toml` | Railway build/deploy config |
| `requirements.txt` | Python dependencies |

---

## Environment Variables

### Required
| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key (scorer + vision parser) |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Main channel chat ID |
| `FINNHUB_API_KEY` | Stock prices, earnings, news, insiders |
| `TIINGO_API_KEY` | SPY/ETF history, sector data |
| `POLYGON_API_KEY` | Technical scanner, options chain, Greeks, ATR |
| `BASE_URL` | `https://web-production-19e44.up.railway.app` |

### Optional
| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_TRADE_CHAT_ID` | — | Separate loud-alarm channel for TRADE alerts |
| `ACCOUNT_SIZE` | 10000 | Account size for position sizing ($) |
| `OPTION_WIN_PCT` | 50 | Win threshold — option must gain this % to count as win |
| `MAX_POSITIONS` | 3 | Max open positions before blocking new entries |
| `DEDUP_WINDOW_SECS` | 120 | Seconds to block duplicate tweet processing |

---

## Scoring System

### 7-Point Checklist (Claude Haiku)

| # | Criterion | Key factors |
|---|-----------|-------------|
| 1 | Flow signal strength | Fill type, premium size, Vol/OI ratio |
| 2 | Expiry timing | Vs earnings date, DTE appropriateness |
| 3 | IV reasonable | Not buying on earnings day |
| 4 | Liquidity | OI >500, spread <10% |
| 5 | OTM appropriate | ATR move analysis — is strike reachable? |
| 6 | Risk/reward | Time of day, regime, sector trend |
| 7 | Not chasing | Option not already up 50%+ from flow fill |

### Verdicts
- **TRADE (6-7):** High conviction — enter without confirmation
- **WATCH (4-5):** Wait for technical entry signal
- **SKIP (0-3):** Pass

### Hard Rules (Python-enforced, override scorer)
- FULL_ASK + premium >$500K → minimum WATCH
- Vol/OI >10x → minimum WATCH
- FULL_ASK + premium >$500K + OTM <3% + DTE <5 → TRADE
- Breakout bet (OTM <2%, no catalyst, <21 DTE) → maximum WATCH

### Scoring Bonuses/Penalties
- Vol/OI >5x: +0.5 | Vol/OI >10x: +1.0
- 2nd+ flow same ticker today: +0.5
- Flow 10:00-11:30 AM: +0.5
- Late day flow (3:30-4PM, DTE <30): +1.0
- After-hours flow (DTE <30): +1.0
- TRENDING_BULL regime: +0.5 | CHOPPY: -0.5 | BEAR: -1.0

### ATR Move Analysis (criterion 5)
- Calculates required stock move to reach strike vs ATR × √DTE expected range
- Insider override: FULL_ASK + premium >$300K + no news → penalty suppressed
- Extraordinary override: Vol/OI >10x or premium >$1M → penalty zeroed

---

## Alert Format

```
✅ TICKER STRIKE C/P EXPIRY [DTE] 🟢/🔴$PRICE
SCORE/7 → FINAL/7 VERDICT
VIX X.X Label · SPY Trend · 🚀 REGIME
[Time/stealth warning if applicable]
🚨 Fill aggression (% at ask, contracts, label)
💰 Premium size signal
🚨 Vol/OI ratio
⚡ Sweep detection
Δ delta | θ theta/day | IV %
IV Rank: X% Label — advice
✅/⚠️/❌ Flow fill: $X.XX → Now ask: $X.XX (+X%)
📐 Move analysis: Needs +X% | Expected ±X% in Xd | emoji assessment
✅/⚠️ News context
📰 Recent headline if any
👔 Insider buying if any
🔄 Roll detected / 🔁 Repeat buyer / 🎯 Divergence
🌑 Dark pool unusual volume
📋 Earnings season note
⚠️ Correlation risk / 🛑 Max positions warning
🛑 Smart stop: $X.XX (technical reason)
💰 Position sizing: X contracts @ $X.XX (X% of account)
→ One-liner analysis
→ Key improvement
🐦 Tweet URL
📊 Analysis detail URL
```

---

## Scheduled Jobs

| Time | Job |
|------|-----|
| 8:00 AM ET Mon-Fri | Pre-market summary |
| 8:05 AM ET Monday | Theta decay calendar for open positions |
| 9:00 AM ET Mon-Fri | Pre-market gap check for watchlist |
| 9:25 AM ET Mon-Fri | Polygon API health check |
| Every 5 min | Technical entry scanner (M5-H1) |
| Every 10 sec | Telegram command polling |
| Every 15 min | Exit signal monitor |
| Every 5 min | Keep-alive ping |
| Every 30 min | IFTTT watchdog (alerts if no webhook 2h+ during market) |
| 4:00 PM ET Mon-Fri | Outcome tracking with option P&L |
| 4:02 PM ET Mon-Fri | Auto-close expired positions |
| 4:05 PM ET Mon-Fri | Paper trade outcome update |
| 4:10 PM ET Mon-Fri | EOD journal reminder for unclosed trades |
| 4:15 PM ET Mon-Fri | EOD OI review |
| 4:30 PM ET Mon-Fri | EOD summary |
| 4:45 PM ET Friday | Weekly performance report |

---

## Telegram Commands

### /help
```
FLOW MONITORING
/watchlist — active technical watches
/positions — open positions with P&L
/portfolio — portfolio with sector breakdown
/stats — win rate stats (option up 50%+ = win)
/find TICKER — today's flow alerts for ticker
/history — today's alerts link

RESEARCH
/sentiment TICKER — price, technicals, news, flow, insiders
/backtest URL TIME — backtest a historical tweet
  Example: /backtest https://x.com/i/status/123 2026-05-19T10:30:00

ACTIONS
/close TICKER — close a system-tracked position

TRADE JOURNAL
/journal — open trades: live P&L + stop/target + web table
/pnl [@ACCOUNT] — P&L + pattern analysis
/accounts — all accounts overview
/journal_help — full journal command reference

/help — this message
```

### /journal_help
```
LOGGING
/entry TICKER STRIKE C/P EXPIRY CONTRACTS PRICE [DATE] [TIME]
  Log now:      /entry FLNC 23 C 06/18/26 3 2.85
  Retroactive:  /entry FLNC 23 C 06/18/26 3 2.85 2026-05-27 10:34AM

/exit TICKER PRICE [CONTRACTS] [DATE] [TIME]
  Full exit:    /exit FLNC 4.20 2026-05-27 1:15PM
  Partial exit: /exit FLNC 4.20 2 2026-05-27 1:15PM
  Quick:        /exit FLNC 4.20  (uses current time)

/add TICKER CONTRACTS PRICE [DATE] [TIME]
  /add FLNC 1 3.10 2026-05-27 11:00AM

/missed TICKER [REASON]
  /missed FLNC Too risky near earnings

VIEWING
/journal — open trades with live unrealized P&L + stop/target
/pnl — full P&L + pattern analysis (after 10+ trades)
/export — CSV of all closed trades

EDITING
/edit TICKER FIELD VALUE
  Fields: entry_date entry_time exit_date exit_time
          entry_price contracts expiry strike note option_type
  Example: /edit FLNC entry_time 10:34AM
  Analytics recalculate automatically after edits

TAGGING
/tag TICKER #tag1 #tag2
  /tag FLNC #earnings_play #insider
```

---

## Time Format Reference

All times are **ET (Eastern Time)**. Supported formats:

| Format | Example |
|--------|---------|
| 12-hour AM/PM | `10:34AM` or `10:34am` |
| 12-hour PM | `2:30PM` or `2:30pm` |
| 24-hour | `10:34` or `14:30` |
| Hour only | `10AM` or `2PM` |

---

## Trade Journal Analytics

When you exit a trade, FlowCheck automatically fetches 5-minute option bars
from Polygon for the entire holding period and calculates:

- **Peak price** — highest option price during hold and when it occurred
- **Max drawdown** — largest drop from peak during hold (%)
- **Left on table** — difference between peak gain and actual exit gain
- **Holding period** — exact days and hours held

Pattern analysis (after 10+ closed trades) in `/pnl`:
- Win rate by time of day (morning / midday / late)
- Win rate by hold duration (intraday / swing / long)
- Average hold time for winners vs losers
- Win rate by tag (`#earnings_play`, `#insider`, `#vwap`, etc.)

Web view: full table with all columns, one-tap CSV download for Excel.

Multi-account support: tag trades to named accounts (`@RH_Brok`, `@RH_Trad`, etc.).
Accounts saved permanently to Supabase — survive all redeploys.
Screenshot entry auto-detects account from broker label (Traditional IRA, Individual, etc.).
View P&L per account with `/pnl @accountid` or overview with `/accounts`.

---

## Market Intelligence

| Feature | Description |
|---------|-------------|
| Roll detection | Same strike + different expiry = buyer extending position |
| Repeat buyer | Same contract across multiple days = accumulation |
| Flow divergence | Aggressive calls + flat/down stock = informed buying |
| Sector rotation | 3+ flows same sector = separate rotation alert |
| Dark pool | Unusual underlying stock volume + options flow |
| Market regime | TRENDING_BULL / CHOPPY / ELEVATED_VOL / BEAR_TRENDING |
| IV rank | Options cheap vs expensive relative to 52-week range |
| IV crush | Pre-earnings timing risk quantified |
| News check | Ticker-specific Finnhub news (filters generic market headlines) |
| Insider filings | SEC Form 4 buy transactions in last 30 days |
| ATR move analysis | Required vs expected move to reach strike |
| Sentiment analysis | On-demand: price, volume, news, options flow, insiders, analysts |
| Market calendar | NYSE/NASDAQ holidays 2025-2027 — prevents false alerts on closed days |

---

## Risk Management

| Feature | Description |
|---------|-------------|
| Smart stop | Previous day low (technical) vs fixed % (fallback) |
| Correlation risk | Warns if adding same-sector positions |
| Max positions | Blocks TRADE entries when at limit (default 3) |
| Position sizing | Kelly criterion + 2% max risk rule |
| Theta calendar | Monday morning decay schedule for open positions |
| Auto-expiry | Closes positions at 4:02 PM when option expires |

---

## Win Rate Tracking

- **Win condition:** Option up `OPTION_WIN_PCT`% (default 50%) from flow fill
- **Fallback:** Stock up 5%+ if option price unavailable
- **Tracked per verdict:** TRADE vs WATCH separate
- **Paper trading:** Hypothetical TRADE entries auto-tracked
- **Weekly report:** Every Friday 4:45 PM with best/worst calls

---

## Data Sources

| Source | Used For | Cost |
|--------|----------|------|
| Anthropic Claude Haiku | Vision parsing, scoring | Paid API |
| Finnhub | Stock prices, earnings, news, insiders | Free |
| Tiingo | SPY/ETF history, sector trends | Free |
| Polygon (Massive.com) | Candles, options chain, Greeks, ATR, backtest | Free (5/min) |
| Yahoo Finance | VIX fallback | Free |
| Finnhub sentiment API | News sentiment score, buzz, bull/bear % | Free |

---

## Test Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Server status |
| `GET /check-env` | Verify env vars |
| `GET /test-telegram` | Send test message |
| `GET /watchlist` | Active watches JSON |
| `GET /positions` | Open positions JSON |
| `GET /stats` | Win rate stats JSON |
| `GET /history` | Today's alerts HTML |
| `GET /analysis/{id}` | Single alert detail |
| `POST /webhook` | Process flow alert |
| `POST /backtest` | Backtest tweet → Telegram |

---

## Trading Rules

1. Only trade TRADE verdicts without confirmation
2. WATCH verdicts — wait for technical entry signal (M5-H1)
3. Sub-$40 stocks: enter immediately on flow alert
4. Above-$40 stocks: wait for technical pullback entry
5. Never chase — option not already up 50%+ from flow entry
6. Max 3 open positions (configurable via MAX_POSITIONS)
7. No entries before 10AM on CPI/NFP/FOMC days
8. Late day flow (3:30-4PM, DTE <30): stealth signal — enter or set 9:30AM alarm
9. After-hours flow (DTE <30): expect overnight move — enter at open
10. QQQ puts as hedge when correlation risk warning fires
11. Log all trades immediately with /entry — use AM/PM times for clarity
12. Log exits with exact date+time for accurate analytics