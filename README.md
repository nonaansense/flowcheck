# FlowCheck v5.0

Automated options flow analysis system. Monitors @FL0WG0D on X via IFTTT,
analyzes Bullflow screenshots with Claude vision, scores against multi-factor
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
Live data (price, earnings, VIX, SPY, sector, Greeks, news, regime)
    ↓ Scorer (Claude Haiku 7-point checklist)
TRADE / WATCH / SKIP verdict
    ↓ Intelligence (roll, repeat, divergence, sector rotation, dark pool)
    ↓ Risk manager (correlation, max positions, smart stop)
Telegram alert → Main channel + TRADE channel (loud alarm)
```

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI server, webhook, scheduler, SMS builder |
| `fetcher.py` | Finnhub/Tiingo/Polygon/Yahoo data, fill aggression, Greeks, regime |
| `scorer.py` | Claude Haiku 7-point scoring with hard rules |
| `parser.py` | Tweet text parsing for Bullflow fields |
| `vision_parser.py` | Claude Haiku vision reads Bullflow screenshots |
| `economic_calendar.py` | Hardcoded FOMC/CPI/NFP calendar |
| `premarket_summary.py` | 8AM pre-market + 4:30PM EOD summaries |
| `premarket_gap.py` | 9AM pre-market gap check for watchlist tickers |
| `technical.py` | Polygon 1-min candles → M5/M10/M15/M30/H1 patterns |
| `backtest.py` | Historical Polygon data backtest engine |
| `outcomes.py` | Win rate tracking with option P&L (win = option +50%+) |
| `exit_signals.py` | Stop/target/DTE/theta exit alerts every 15 min |
| `flow_intelligence.py` | Roll detection, repeat buyer, divergence, sector rotation |
| `risk_manager.py` | Correlation risk, max positions, smart stop, theta calendar |
| `iv_analysis.py` | IV rank, IV percentile, earnings IV crush risk |
| `news_check.py` | Finnhub news + insider filing cross-reference |
| `position_sizing.py` | Kelly criterion position sizing |
| `paper_trading.py` | Hypothetical trade tracking for comparison |
| `telegram_commands.py` | Bot command handler (/help /watchlist /positions etc.) |
| `weekly_report.py` | Friday EOD weekly performance summary |
| `sms.py` | Telegram sender (main + TRADE channels) |
| `Procfile` | `web: uvicorn main:app --host 0.0.0.0 --port $PORT` |
| `railway.toml` | Railway build/deploy config |
| `requirements.txt` | Python dependencies |

---

## Environment Variables (Railway)

### Required
| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Claude API key (scorer + vision parser) |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Main channel chat ID |
| `FINNHUB_API_KEY` | Stock prices, earnings, news, insiders |
| `TIINGO_API_KEY` | SPY/ETF history, sector data |
| `POLYGON_API_KEY` | Technical scanner candles, options chain, Greeks |
| `BASE_URL` | `https://web-production-19e44.up.railway.app` |

### Optional
| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_TRADE_CHAT_ID` | — | Separate loud-alarm channel for TRADE alerts only |
| `ACCOUNT_SIZE` | 10000 | Account size for position sizing ($) |
| `OPTION_WIN_PCT` | 50 | Win threshold — option must gain this % to count as win |
| `MAX_POSITIONS` | 3 | Maximum open positions before blocking new entries |
| `DEDUP_WINDOW_SECS` | 120 | Seconds to block duplicate tweet processing |
| `TWILIO_*` | — | Twilio SMS fallback (optional) |

---

## Scoring System (7-point checklist)

| # | Criterion | Key factors |
|---|-----------|-------------|
| 1 | Flow signal strength | Fill type, premium size, Vol/OI ratio |
| 2 | Expiry timing | Vs earnings date, DTE appropriateness |
| 3 | IV reasonable | Not buying on earnings day |
| 4 | Liquidity | OI >500, spread <10% |
| 5 | OTM appropriate | <10% ideal, <20% acceptable with strong fill |
| 6 | Risk/reward | Time of day, regime, sector trend |
| 7 | Not chasing | Option not already up 50%+ |

### Verdict Thresholds
- **TRADE (6-7):** High conviction — enter without confirmation
- **WATCH (4-5):** Wait for technical entry signal
- **SKIP (0-3):** Pass

### Hard Rules (Python-enforced, override scorer)
- `FULL_ASK + premium >$500K` → minimum WATCH
- `Vol/OI >10x` → minimum WATCH
- `FULL_ASK + premium >$500K + OTM <3% + DTE <5` → TRADE (urgent bet)
- `Breakout bet (OTM <2%, no catalyst, <21 DTE)` → maximum WATCH

### Scoring Bonuses
- Vol/OI >5x: +0.5 criterion 1
- Vol/OI >10x: +1.0 criterion 1
- 2nd+ flow same ticker today: +0.5 criterion 1
- Flow 10:00-11:30 AM: +0.5 criterion 6
- Flow 3:30-4:00 PM, DTE <30: +1.0 criterion 6 (stealth/late flow)
- Flow after 4:00 PM, DTE <30: +1.0 criterion 6 (after-hours stealth)
- Trending bull regime: +0.5 criterion 6
- Choppy regime: -0.5 criterion 6
- Bear trending: -1.0 criterion 6

---

## Alert Format

```
✅ TICKER STRIKE C/P EXPIRY [DTE] 🟢/🔴$PRICE
SCORE/7 → FINAL/7 VERDICT
VIX X.X Label · SPY Trend · 🚀 REGIME
[Time warning if late/after-hours]
🚨 Fill aggression
💰 Premium size signal
🚨 Vol/OI ratio
Δ delta | θ theta/day | IV %
IV Rank: X% Label
✅/⚠️/❌ Flow fill: $X.XX → Now ask: $X.XX (+X%)
✅/⚠️ News context
📰 Recent headline if any
👔 Insider buying if any
🔄 Roll detected / 🔁 Repeat buyer
🎯 Bullish divergence
🌑 Dark pool unusual volume
⚠️ Correlation risk / 🛑 Max positions
🛑 Smart stop: $X.XX (reason)
💰 Position sizing: X contracts @ $X.XX
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
| 8:05 AM ET Monday | Theta decay calendar |
| 9:00 AM ET Mon-Fri | Pre-market gap check |
| 9:25 AM ET Mon-Fri | Polygon API health check |
| Every 5 min | Technical scanner (M5/M10/M15/M30/H1) |
| Every 10 sec | Telegram command polling |
| Every 15 min | Exit signal monitor |
| Every 5 min | Keep-alive ping |
| Every 30 min | IFTTT watchdog |
| 4:00 PM ET Mon-Fri | Outcome tracking |
| 4:02 PM ET Mon-Fri | Auto-close expired positions |
| 4:05 PM ET Mon-Fri | Paper trade outcome update |
| 4:15 PM ET Mon-Fri | EOD OI review |
| 4:30 PM ET Mon-Fri | EOD summary |
| 4:45 PM ET Friday | Weekly performance report |

---

## Technical Scanner

Uses Polygon.io 1-minute candles aggregated locally:
- **M5** = 5 × 1min candles
- **M10** = 10 × 1min candles
- **M15** = 15 × 1min candles
- **M30** = 30 × 1min candles
- **H1** = 60 × 1min candles

**Entry patterns (2+ required to fire):**
1. Bullish hammer
2. Bullish engulfing
3. VWAP bounce
4. Break above previous candle high
5. Morning star (3-candle reversal)
6. Higher low (3 consecutive)
7. Volume spike (1.5x average)

**Entry quality check:** compares current stock price vs flow entry price.
Alerts suppressed or downgraded if stock has moved too far from flow entry.

**Sub-$40 stocks:** Immediate entry alert (no waiting for pullbacks)
**Above-$40 stocks:** Monitor for technical pullback entry until EOD

---

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/watchlist` | Active technical monitoring tickers |
| `/positions` | Open positions with P&L |
| `/portfolio` | Full portfolio with sector breakdown |
| `/stats` | Win rate statistics |
| `/find TICKER` | Search today's alerts for ticker |
| `/close TICKER` | Manually close a position |
| `/history` | Link to today's alerts |
| `/backtest URL TIME` | Backtest historical tweet |

---

## Test Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Server status |
| `GET /check-env` | Verify all env vars set |
| `GET /test-telegram` | Send test Telegram message |
| `GET /test-finnhub` | Test Finnhub with AAPL |
| `GET /test-tiingo` | Test Tiingo with SPY |
| `GET /test-polygon` | Test Polygon with AAPL candles |
| `GET /watchlist` | Active technical watches JSON |
| `GET /positions` | Open positions JSON |
| `GET /stats` | Win rate stats JSON |
| `GET /history` | Today's alerts HTML page |
| `GET /analysis/{id}` | Single alert detail HTML |
| `POST /webhook` | Process flow alert |
| `POST /backtest` | Backtest historical tweet → Telegram |

---

## Market Intelligence Features

- **Roll detection:** Same strike + different expiry = buyer extending position
- **Repeat buyer:** Same contract across multiple days = systematic accumulation
- **Flow divergence:** Aggressive calls + flat/down stock = informed accumulation
- **Sector rotation:** 3+ flows same sector = rotation alert
- **Dark pool:** Unusual underlying stock volume alongside options flow
- **Market regime:** TRENDING_BULL / CHOPPY / ELEVATED_VOL / BEAR_TRENDING
- **IV rank:** Options cheap (buy) vs expensive (use spreads)
- **IV crush:** Pre-earnings timing risk quantified
- **News check:** Ticker-specific Finnhub news (filters generic market headlines)
- **Insider filings:** SEC Form 4 buy transactions last 30 days

---

## Risk Management

- **Smart stop:** Previous day low (technical) vs fixed % (fallback)
- **Correlation risk:** Warns if adding same-sector positions
- **Max positions:** Blocks TRADE entries when at limit (default 3)
- **Position sizing:** Kelly criterion + 2% max risk rule
- **Theta calendar:** Monday morning decay schedule for all open positions
- **Auto-expiry:** Closes positions at 4:02 PM when option expires

---

## Win Rate Tracking

- **Win condition:** Option price up `OPTION_WIN_PCT`% (default 50%) from flow fill
- **Fallback:** Stock up 5%+ if option price unavailable
- **Tracked per verdict:** TRADE vs WATCH win rates separate
- **Paper trading:** Hypothetical TRADE entries auto-tracked for comparison
- **Weekly report:** Every Friday 4:45 PM with best/worst calls and time-of-day breakdown

---

## Data Sources

| Source | Used For | Tier |
|--------|----------|------|
| Anthropic Claude Haiku | Vision parsing, scoring | Paid API |
| Finnhub | Stock prices, earnings, news, insiders, sector | Free |
| Tiingo | SPY/ETF history, sector trends | Free |
| Polygon (Massive.com) | Technical candles, options chain, Greeks, backtest | Free (5/min) |
| Yahoo Finance | VIX fallback | Free |
| Stooq | Historical VIX fallback | Free |

---

## Trading Rules

1. Only trade TRADE verdicts without confirmation
2. WATCH verdicts need technical entry signal first
3. Sub-$40 stocks: enter immediately on flow alert
4. Above-$40 stocks: wait for M5-H1 entry signal
5. Never chase — option not already up 50%+ from flow entry
6. Max 3 open positions (configurable)
7. Respect economic calendar (no entries before 10AM on CPI/NFP/FOMC days)
8. Late day flow (3:30-4PM, DTE <30): stealth signal — enter or set 9:30AM alarm
9. After-hours flow (DTE <30): expect overnight move — enter at open
10. LEAP late flow: no urgency signal — treat as normal
11. QQQ puts as hedge when correlation risk warning fires
