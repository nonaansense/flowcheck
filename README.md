# FlowCheck v4.0

Automated options flow alert analyzer. Monitors @FL0WG0D on X via IFTTT,
analyzes Bullflow screenshots, scores against 7-point checklist, sends Telegram alerts.

## Environment Variables (Railway)
- ANTHROPIC_API_KEY
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- FINNHUB_API_KEY
- TIINGO_API_KEY
- BASE_URL (https://flowcheck-production.up.railway.app)
- TWILIO_* (fallback, optional)

## Endpoints
- GET  /health
- GET  /check-env
- GET  /test-telegram
- GET  /test-finnhub
- GET  /test-tiingo
- POST /webhook
- GET  /analysis/{id}
- GET  /history
