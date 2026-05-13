import os

def send_sms(message):
    """Send SMS via Twilio. Reads credentials at call time not import time."""
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
        print(f"[SMS] Missing env vars: {', '.join(missing)}")
        print(f"[SMS] Message would have been:\n{message}")
        return False

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        msg = client.messages.create(
            body=message, from_=from_number, to=to_number
        )
        print(f"[SMS] Sent: {msg.sid}")
        return True
    except Exception as e:
        print(f"[SMS] Error: {e}")
        return False
