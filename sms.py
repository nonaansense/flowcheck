from twilio.rest import Client
import os

def send_sms(message):
    """Send SMS via Twilio."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")
    to_number = os.getenv("TWILIO_TO_NUMBER")

    if not all([account_sid, auth_token, from_number, to_number]):
        print("[SMS] Missing Twilio credentials — SMS not sent")
        print(f"[SMS] Would have sent: {message}")
        return False

    try:
        client = Client(account_sid, auth_token)
        msg = client.messages.create(
            body=message,
            from_=from_number,
            to=to_number
        )
        print(f"[SMS] Sent successfully: {msg.sid}")
        return True
    except Exception as e:
        print(f"[SMS] Error: {e}")
        return False
