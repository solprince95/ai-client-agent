"""
notifications.py: shared outbound-email helper (Brevo)

Extracted from followup_agent.py so Follow-up Agent, Reminder Agent, and
the appointment-confirmation email in app.py all send mail the same way
instead of three separate copies of the same requests.post call.
"""

import os
import re

import requests

BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")


def is_email(contact: str) -> bool:
    return bool(re.match(r"^[\w.+-]+@[\w-]+\.[\w.-]+$", (contact or "").strip()))


def send_email(to_email: str, to_name: str, sender_email: str, sender_name: str, subject: str, body: str) -> bool:
    if not BREVO_API_KEY or not sender_email or not to_email:
        return False
    try:
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json={
                "sender": {"name": sender_name, "email": sender_email},
                "to": [{"email": to_email, "name": to_name or ""}],
                "subject": subject,
                "htmlContent": body,
            },
            timeout=(10, 15),
        )
        return response.status_code < 400
    except Exception:
        return False
