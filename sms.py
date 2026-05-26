import os, requests

def send_sms(message: str, verdict: str = None) -> bool:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id   = os.environ.get("TELEGRAM_CHAT_ID")
    trade_chat_id = os.environ.get("TELEGRAM_TRADE_CHAT_ID")

    if not bot_token:
        return send_twilio(message)

    # Send to main channel always
    main_ok = False
    if chat_id:
        main_ok = send_telegram(message, bot_token, chat_id)

    # Send TRADE alerts to separate high-priority channel
    if verdict == "TRADE" and trade_chat_id and trade_chat_id != chat_id:
        print(f"[SMS] Sending TRADE alert to priority channel")
        send_telegram(message, bot_token, trade_chat_id)

    return main_ok or send_twilio(message)

def send_telegram(message: str, bot_token: str, chat_id: str) -> bool:
    if len(message) > 4000:
        base_url = os.environ.get("BASE_URL", "")
        message  = message[:3950] + f"\n...\n{base_url}/history"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=15
        )
        if r.status_code == 200:
            msg_id = r.json().get("result", {}).get("message_id", "?")
            print(f"[SMS] Telegram sent — message_id: {msg_id} ({len(message)} chars)")
            return True
        print(f"[SMS] Telegram error {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[SMS] Telegram exception: {e}")
        return False

def send_twilio(message: str) -> bool:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token  = os.environ.get("TWILIO_AUTH_TOKEN")
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    to_number   = os.environ.get("TWILIO_TO_NUMBER")
    missing = [k for k, v in {"TWILIO_ACCOUNT_SID": account_sid,
        "TWILIO_AUTH_TOKEN": auth_token, "TWILIO_FROM_NUMBER": from_number,
        "TWILIO_TO_NUMBER": to_number}.items() if not v]
    if missing:
        print(f"[SMS] Missing Twilio vars: {', '.join(missing)}")
        return False
    try:
        from twilio.rest import Client
        msg = Client(account_sid, auth_token).messages.create(
            body=message, from_=from_number, to=to_number)
        print(f"[SMS] Twilio sent: {msg.sid}")
        return True
    except Exception as e:
        print(f"[SMS] Twilio error: {e}")
        return False
