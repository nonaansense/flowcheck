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
    verdict_clean = (verdict or "").strip().upper()
    if verdict_clean == "TRADE" and trade_chat_id and trade_chat_id != chat_id:
        print(f"[SMS] Sending TRADE alert to priority channel ({trade_chat_id})")
        send_telegram(message, bot_token, trade_chat_id)
    elif verdict_clean == "TRADE" and trade_chat_id == chat_id:
        print(f"[SMS] TRADE_CHAT_ID same as CHAT_ID — not duplicating")

    return main_ok or send_twilio(message)

def escape_html(text: str) -> str:
    """Escape HTML special chars but preserve <a href> tags."""
    import re as _re
    # Extract all <a href="...">...</a> tags first
    links = []
    def save_link(m):
        links.append(m.group(0))
        return f"\x00LINK{len(links)-1}\x00"
    text = _re.sub(r'<a href="[^"]*">[^<]*</a>', save_link, text)
    # Escape remaining HTML special chars
    text = text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    # Restore links
    for i, link in enumerate(links):
        text = text.replace(f"\x00LINK{i}\x00", link)
    return text

def send_telegram(message: str, bot_token: str, chat_id: str) -> bool:
    if len(message) > 4000:
        base_url = os.environ.get("BASE_URL", "")
        message  = message[:3950] + f"\n...\n{base_url}/history"
    message = escape_html(message)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": message,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
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
