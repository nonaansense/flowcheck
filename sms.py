import os, requests

def send_sms(message: str) -> bool:
    """
    Send message via Telegram Bot API.
    No 24-hour window restriction — messages arrive 24/7.
    Falls back to WhatsApp if Telegram credentials not set.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id   = os.environ.get("TELEGRAM_CHAT_ID")

    if bot_token and chat_id:
        return send_telegram(message, bot_token, chat_id)

    # Fallback to WhatsApp/Twilio if Telegram not configured
    print("[SMS] Telegram not configured — falling back to Twilio")
    return send_twilio(message)


def send_telegram(message: str, bot_token: str, chat_id: str) -> bool:
    """Send via Telegram Bot API."""
    # Telegram supports up to 4096 chars
    if len(message) > 4000:
        base_url = os.environ.get("BASE_URL", "")
        message  = message[:3950] + f"\n...\n{base_url}/history"

    try:
        url  = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            "chat_id":    chat_id,
            "text":       message,
            "parse_mode": "HTML",  # Enables clickable links
            "disable_web_page_preview": False,  # Shows link previews
        }
        r = requests.post(url, json=data, timeout=15)

        if r.status_code == 200:
            result = r.json()
            msg_id = result.get("result", {}).get("message_id", "?")
            print(f"[SMS] Telegram sent — message_id: {msg_id} ({len(message)} chars)")
            return True
        else:
            print(f"[SMS] Telegram error {r.status_code}: {r.text[:200]}")
            return False

    except Exception as e:
        print(f"[SMS] Telegram exception: {e}")
        return False


def send_twilio(message: str) -> bool:
    """Fallback: send via Twilio WhatsApp."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    to_number   = os.environ.get("TWILIO_TO_NUMBER")

    missing = [k for k, v in {
        "TWILIO_ACCOUNT_SID": account_sid,
        "TWILIO_AUTH_TOKEN":  auth_token,
        "TWILIO_FROM_NUMBER": from_number,
        "TWILIO_TO_NUMBER":   to_number,
    }.items() if not v]

    if missing:
        print(f"[SMS] Missing Twilio vars: {', '.join(missing)}")
        print(f"[SMS] Message:\n{message}")
        return False

    is_whatsapp = "whatsapp:" in (from_number or "").lower()
    max_len     = 4000 if is_whatsapp else 1550
    if len(message) > max_len:
        base_url = os.environ.get("BASE_URL", "")
        message  = message[:max_len - 50] + f"\n...\n{base_url}/history"

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        msg    = client.messages.create(body=message, from_=from_number, to=to_number)
        print(f"[SMS] Twilio sent: {msg.sid} ({len(message)} chars)")
        return True
    except Exception as e:
        print(f"[SMS] Twilio error: {e}")
        return False