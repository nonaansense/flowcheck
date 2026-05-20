# FlowCheck — Automated Options Flow Analyzer

Monitors @FL0WG0D on X, fetches live market data, scores with Claude, texts your phone.

---

## How It Works

1. @FL0WG0D posts a flow alert on X
2. IFTTT detects new tweet → sends to your webhook
3. Server parses ticker, strike, expiry, premium
4. Fetches live: stock price, options chain, earnings date, IV, historical moves
5. Claude scores 1-7 with your checklist
6. SMS hits your phone in ~90 seconds with score + detail link

---

## Deployment (20 minutes)

### Step 1 — Create Railway account
1. Go to railway.app
2. Sign up with GitHub (free)

### Step 2 — Deploy the code
1. Go to railway.app/new
2. Click "Deploy from GitHub repo"
3. Upload these files OR use the Railway CLI:

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login
railway login

# Create project and deploy
railway init
railway up
```

### Step 3 — Set environment variables
In Railway dashboard → your project → Variables, add:

```
ANTHROPIC_API_KEY     = sk-ant-...your key...
TWILIO_ACCOUNT_SID    = ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN     = your_auth_token
TWILIO_FROM_NUMBER    = +1XXXXXXXXXX
TWILIO_TO_NUMBER      = +1XXXXXXXXXX
BASE_URL              = https://your-app-name.railway.app
```

### Step 4 — Get your webhook URL
After deploy, Railway gives you a URL like:
`https://flowcheck-production.railway.app`

Your webhook endpoint is:
`https://flowcheck-production.railway.app/webhook`

### Step 5 — Set up IFTTT
1. Go to ifttt.com → Create
2. IF: Twitter → "New tweet by specific user" → @FL0WG0D
3. THEN: Webhooks → "Make a web request"
   - URL: `https://your-app.railway.app/webhook`
   - Method: POST
   - Content Type: `application/json`
   - Body: `{"tweet": "<<<TweetText>>>"}`
4. Save

### Step 6 — Test it
Send a test POST to your webhook:

```bash
curl -X POST https://your-app.railway.app/webhook \
  -H "Content-Type: application/json" \
  -d '{"tweet": "$ORCL — $384K Call buyer ORCL 207.5 Call Exp. 05/22/26 OTM: 6.3%"}'
```

You should get an SMS within 30 seconds.

---

## SMS Format

```
✅ ORCL 207.5C May22
Score: 5/7 — WATCH

Wrong expiry — earnings Jun10 is after May22.

→ Better expiry: use Jun18 to capture earnings

Full analysis: https://your-app.railway.app/analysis/0
```

---

## Files

```
flowcheck/
├── main.py          # FastAPI server + webhook + detail page
├── parser.py        # Tweet → structured trade data
├── fetcher.py       # Live market data (yfinance)
├── scorer.py        # Claude API scoring
├── sms.py           # Twilio SMS
├── requirements.txt # Dependencies
├── railway.toml     # Railway config
└── README.md        # This file
```

---

## Cost Estimate

| Item | Cost |
|------|------|
| Railway hosting | Free (up to $5/mo usage) |
| yfinance data | Free |
| Claude API (~20 alerts/day) | ~$6/month |
| Twilio SMS (~20 texts/day) | ~$0.60/month |
| **Total** | **~$7/month** |

---

## Troubleshooting

**Not receiving SMS:**
- Check Railway logs for errors
- Verify Twilio credentials in env vars
- Test Twilio directly at twilio.com/console

**Parse errors:**
- Check Railway logs — tweet format may have changed
- parser.py handles most @FL0WG0D formats but may need updating

**No options data:**
- yfinance occasionally has rate limits
- Data will show "N/A" but analysis still runs with available info
