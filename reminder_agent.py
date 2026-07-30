"""
reminder_agent.py: "Reminder Agent"

Sends a reminder about 24 hours before a confirmed appointment, with a
link the visitor can use to reschedule or cancel without calling in.
This was called out explicitly in the pivot plan as "a big driver of
no-show reduction, not optional."

Email only for now, same reasoning as Follow-up Agent: WhatsApp
automated sending is still blocked on Meta approval, and appointments
with only a phone number captured (no email) are left for staff to
remind manually, same graceful-degrade-to-human pattern used elsewhere.

Runs on a schedule from app.py (APScheduler locally, or Cloud Scheduler
hitting /internal/reminder-check in production), same as Follow-up
Agent.
"""

import os
from datetime import datetime, timedelta

import notifications

# How far ahead of the appointment to send the reminder, and how wide a
# window to check in (the job runs hourly, so a couple-hour window
# means a slightly-late scheduler run still catches everything due).
REMINDER_LEAD_HOURS = 24
CHECK_WINDOW_HOURS = 2

SITE_URL = "https://vajralabs.co.in"


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


def _log(conversation_id, user_id, event_type, detail, sb):
    try:
        sb.table("activity_log").insert({
            "conversation_id": conversation_id,
            "user_id": user_id,
            "event_type": event_type,
            "detail": detail,
        }).execute()
    except Exception:
        pass


# ======================================================
#  FIND APPOINTMENTS DUE FOR A REMINDER
# ======================================================
def find_due_appointments(sb=None) -> list:
    sb = sb or _get_supabase()
    if not sb:
        return []
    try:
        res = sb.table("appointments").select("*") \
            .eq("status", "confirmed") \
            .eq("reminder_sent", False) \
            .execute()
        rows = res.data or []
    except Exception:
        return []

    due = []
    now = datetime.utcnow()
    window_start = now + timedelta(hours=REMINDER_LEAD_HOURS)
    window_end = window_start + timedelta(hours=CHECK_WINDOW_HOURS)

    for appt in rows:
        confirmed_time_str = appt.get("confirmed_time")
        if not confirmed_time_str:
            continue
        try:
            confirmed_time = datetime.fromisoformat(confirmed_time_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue

        # Only remind for appointments still in the future, due within
        # the lead-time window, and where we have an email to send to.
        if confirmed_time <= now:
            continue
        if not notifications.is_email(appt.get("visitor_contact", "")):
            continue
        if window_start <= confirmed_time <= window_end:
            due.append(appt)

    return due


# ======================================================
#  WRITE + SEND
# ======================================================
def _reminder_email_body(clinic: dict, appt: dict, manage_url: str) -> str:
    clinic_name = clinic.get("clinic_name", "the clinic")
    confirmed_time_str = appt.get("confirmed_time", "")
    try:
        confirmed_time = datetime.fromisoformat(confirmed_time_str.replace("Z", "+00:00")).replace(tzinfo=None)
        when = confirmed_time.strftime("%A, %d %B at %I:%M %p")
    except Exception:
        when = confirmed_time_str

    return (
        f"<p>Hi {appt.get('visitor_name') or 'there'},</p>"
        f"<p>Just a reminder about your upcoming appointment with <strong>{clinic_name}</strong> "
        f"on <strong>{when}</strong>.</p>"
        f"<p>Need to change or cancel? You can do that here: "
        f"<a href=\"{manage_url}\">{manage_url}</a></p>"
        f"<p>See you soon.</p>"
    )


def run_reminder_check(sb=None, log=print) -> dict:
    sb = sb or _get_supabase()
    if not sb:
        return {"ok": False, "message": "No database connection."}

    due = find_due_appointments(sb)
    if not due:
        return {"ok": True, "sent": 0}

    sent = 0
    for appt in due:
        try:
            clinic_res = sb.table("clinics").select("*").eq("id", appt["clinic_id"]).single().execute()
            clinic = clinic_res.data or {}
            profile_res = sb.table("profiles").select("gmail, full_name").eq("id", appt["user_id"]).single().execute()
            profile = profile_res.data or {}
        except Exception:
            continue

        sender_email = profile.get("gmail", "")
        if not sender_email:
            continue  # clinic hasn't set up an email in Setup yet

        manage_url = f"{SITE_URL}/appointment/{appt['id']}/manage"
        body = _reminder_email_body(clinic, appt, manage_url)
        subject = f"Reminder: your appointment with {clinic.get('clinic_name', 'us')}"

        ok = notifications.send_email(
            appt.get("visitor_contact", ""),
            appt.get("visitor_name", ""),
            sender_email,
            clinic.get("clinic_name", "") or profile.get("full_name", ""),
            subject,
            body,
        )

        if ok:
            sent += 1
            try:
                sb.table("appointments").update({"reminder_sent": True}).eq("id", appt["id"]).execute()
                _log(appt["conversation_id"], appt["user_id"], "reminder_sent",
                     f"appointment {appt['id']}", sb)
            except Exception:
                pass
            log(f"Reminder Agent: sent reminder for appointment {appt['id']}")

    return {"ok": True, "sent": sent, "checked": len(due)}
