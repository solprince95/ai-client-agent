"""
calendar_agent.py: "Booking Agent," Google Calendar layer

Handles the OAuth dance with Google Calendar and creates real calendar
events once a staff member confirms a booking with an actual date/time.
Booking Agent (in conversation_agent.py) only captures loose, free-text
timing from the patient ("Tuesday afternoon"), staff picks the real
slot when confirming, that's what actually gets written to the
calendar, since there's no availability-checking engine yet (that's
explicitly out of scope for now, see the pivot plan).

Uses raw HTTP calls to Google's OAuth and Calendar APIs (no google-api-
python-client dependency), consistent with how the rest of this app
talks to Razorpay/WhatsApp/Anthropic.
"""

import os
from datetime import datetime, timedelta

import requests

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET", "")
REDIRECT_URI = "https://vajralabs.co.in/api/calendar/oauth/callback"

AUTH_BASE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

SCOPE = "https://www.googleapis.com/auth/calendar.events"

DEFAULT_APPOINTMENT_MINUTES = 30


def calendar_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _get_supabase():
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        if url and key:
            return create_client(url, key)
    except Exception:
        pass
    return None


# ======================================================
#  OAUTH
# ======================================================
def get_authorization_url(user_id: str) -> str:
    """Builds the Google consent screen URL. `state` carries the user_id
    so the callback knows whose clinic to attach the tokens to."""
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # needed to get a refresh_token
        "prompt": "consent",        # forces refresh_token even on repeat connects
        "state": user_id,
    }
    query = "&".join(f"{k}={requests.utils.quote(v)}" for k, v in params.items())
    return f"{AUTH_BASE_URL}?{query}"


def handle_oauth_callback(code: str, user_id: str, sb=None) -> dict:
    sb = sb or _get_supabase()
    if not sb:
        return {"ok": False, "message": "No database connection."}

    try:
        resp = requests.post(TOKEN_URL, data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        }, timeout=(10, 15))
        if resp.status_code >= 400:
            return {"ok": False, "message": f"Google rejected the connection: {resp.text}"}
        tokens = resp.json()
    except Exception as e:
        return {"ok": False, "message": str(e)}

    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")
    expires_in = tokens.get("expires_in", 3600)
    expiry = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

    if not refresh_token:
        # Google only sends a refresh_token on first consent (or when
        # prompt=consent forces it). If somehow missing, the connection
        # will silently stop working once the access_token expires.
        return {"ok": False, "message": "Google didn't return a refresh token, please try connecting again."}

    try:
        existing = sb.table("clinics").select("id").eq("user_id", user_id).execute()
        update = {
            "google_calendar_connected": True,
            "google_calendar_access_token": access_token,
            "google_calendar_refresh_token": refresh_token,
            "google_calendar_token_expiry": expiry,
        }
        if existing.data:
            sb.table("clinics").update(update).eq("user_id", user_id).execute()
        else:
            update["user_id"] = user_id
            sb.table("clinics").insert(update).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def disconnect_calendar(user_id: str, sb=None) -> dict:
    sb = sb or _get_supabase()
    if not sb:
        return {"ok": False}
    try:
        sb.table("clinics").update({
            "google_calendar_connected": False,
            "google_calendar_access_token": "",
            "google_calendar_refresh_token": "",
        }).eq("user_id", user_id).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def _refresh_access_token(clinic: dict, sb) -> str:
    """Returns a valid access token, refreshing it first if expired."""
    expiry_str = clinic.get("google_calendar_token_expiry")
    access_token = clinic.get("google_calendar_access_token", "")

    is_expired = True
    if expiry_str:
        try:
            expiry = datetime.fromisoformat(expiry_str.replace("Z", "+00:00")).replace(tzinfo=None)
            is_expired = datetime.utcnow() >= expiry - timedelta(minutes=2)
        except Exception:
            is_expired = True

    if not is_expired and access_token:
        return access_token

    refresh_token = clinic.get("google_calendar_refresh_token", "")
    if not refresh_token:
        return ""

    try:
        resp = requests.post(TOKEN_URL, data={
            "refresh_token": refresh_token,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "grant_type": "refresh_token",
        }, timeout=(10, 15))
        if resp.status_code >= 400:
            return ""
        data = resp.json()
        new_token = data.get("access_token", "")
        expires_in = data.get("expires_in", 3600)
        new_expiry = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

        if sb and new_token:
            sb.table("clinics").update({
                "google_calendar_access_token": new_token,
                "google_calendar_token_expiry": new_expiry,
            }).eq("id", clinic["id"]).execute()

        return new_token
    except Exception:
        return ""


# ======================================================
#  CREATE EVENT
# ======================================================
def create_calendar_event(clinic: dict, visitor_name: str, visitor_contact: str,
                           requested_text: str, confirmed_time_iso: str, sb=None) -> dict:
    """
    Creates a real event on the clinic's Google Calendar. confirmed_time_iso
    is the actual slot staff picked when confirming (ISO 8601, e.g.
    "2026-08-01T15:30:00"), not the patient's loose free-text request.
    """
    sb = sb or _get_supabase()
    if not clinic.get("google_calendar_connected"):
        return {"ok": False, "message": "Google Calendar isn't connected for this clinic."}

    access_token = _refresh_access_token(clinic, sb)
    if not access_token:
        return {"ok": False, "message": "Could not refresh Google Calendar access, please reconnect."}

    try:
        start = datetime.fromisoformat(confirmed_time_iso)
    except Exception:
        return {"ok": False, "message": "Invalid date/time format."}
    end = start + timedelta(minutes=DEFAULT_APPOINTMENT_MINUTES)

    description_lines = [f"Booked via Vajra Labs chat."]
    if requested_text:
        description_lines.append(f"Patient requested: {requested_text}")
    if visitor_contact:
        description_lines.append(f"Contact: {visitor_contact}")

    event = {
        "summary": f"Appointment: {visitor_name or 'Patient'}",
        "description": "\n".join(description_lines),
        "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Kolkata"},
    }

    try:
        resp = requests.post(
            CALENDAR_EVENTS_URL,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json=event,
            timeout=(10, 15),
        )
        if resp.status_code >= 400:
            return {"ok": False, "message": f"Google Calendar rejected the event: {resp.text}"}
        data = resp.json()
        return {"ok": True, "event_id": data.get("id", ""), "event_link": data.get("htmlLink", "")}
    except Exception as e:
        return {"ok": False, "message": str(e)}


# ======================================================
#  DELETE EVENT (cancel, or clear the old slot before a reschedule)
# ======================================================
def delete_calendar_event(clinic: dict, event_id: str, sb=None) -> dict:
    """
    Removes a previously-created event, e.g. when a visitor cancels, or
    right before creating the new event for a reschedule. Safe to call
    even if the event was already deleted on Google's side (404 is
    treated as success, since the end state we want is already true).
    """
    sb = sb or _get_supabase()
    if not clinic.get("google_calendar_connected") or not event_id:
        return {"ok": True, "message": "Nothing to delete."}

    access_token = _refresh_access_token(clinic, sb)
    if not access_token:
        return {"ok": False, "message": "Could not refresh Google Calendar access, please reconnect."}

    try:
        resp = requests.delete(
            f"{CALENDAR_EVENTS_URL}/{event_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=(10, 15),
        )
        if resp.status_code >= 400 and resp.status_code != 404 and resp.status_code != 410:
            return {"ok": False, "message": f"Google Calendar rejected the delete: {resp.text}"}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "message": str(e)}
