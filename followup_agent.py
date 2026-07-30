"""
followup_agent.py: "Follow-up Agent"

Checks for conversations that went quiet before booking completed, and
sends a follow-up at 24h, 2 days, 4 days, and 1 week, matching the
schedule from the pivot plan. Stops once the visitor replies (handled
naturally, since a new visitor message updates `updated_at` and resets
the clock), the conversation gets booked, or 4 follow-ups have gone out
with no response.

Email only for now, since WhatsApp automated sending is still blocked
on Meta approval. Conversations where only a phone number was captured
(no email) aren't auto-followed-up, they show up for staff to handle
manually instead, same "graceful degrade to human" pattern used
elsewhere.

Runs on a schedule from app.py (APScheduler), not triggered by any
particular request.
"""

import os
from datetime import datetime, timedelta

import requests

import notifications

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"

# (follow_up_count when due, hours since last activity required)
FOLLOW_UP_SCHEDULE = [
    (0, 24),        # 1st follow-up: 24 hours after going quiet
    (1, 48),        # 2nd: 2 days after the 1st
    (2, 96),        # 3rd: 4 days after the 2nd
    (3, 168),       # 4th: 1 week after the 3rd
]
MAX_FOLLOW_UPS = len(FOLLOW_UP_SCHEDULE)


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
#  FIND STALE CONVERSATIONS
# ======================================================
def find_due_conversations(sb=None) -> list:
    sb = sb or _get_supabase()
    if not sb:
        return []
    try:
        res = sb.table("conversations").select("*") \
            .eq("status", "active") \
            .in_("stage", ["contact_capture", "booking", "faq", "done"]) \
            .lt("follow_up_count", MAX_FOLLOW_UPS) \
            .execute()
        rows = res.data or []
    except Exception:
        return []

    due = []
    now = datetime.utcnow()
    for conv in rows:
        contact = conv.get("visitor_contact", "")
        if not notifications.is_email(contact):
            continue  # no email captured, or phone-only, leave for staff

        count = conv.get("follow_up_count", 0)
        if count >= MAX_FOLLOW_UPS:
            continue

        _, hours_required = FOLLOW_UP_SCHEDULE[count]
        last_activity_str = conv.get("last_follow_up_at") or conv.get("updated_at")
        try:
            last_activity = datetime.fromisoformat(last_activity_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue

        if now - last_activity >= timedelta(hours=hours_required):
            due.append(conv)

    return due


# ======================================================
#  WRITE + SEND
# ======================================================
def _call_claude(system_prompt: str, user_prompt: str, max_tokens: int = 150) -> str:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        },
        timeout=(10, 20),
    )
    if resp.status_code >= 400:
        raise Exception(resp.text)
    data = resp.json()
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def _write_followup_message(clinic: dict, conv: dict, attempt_number: int) -> str:
    fallback = (
        f"Hi, just following up on your enquiry with {clinic.get('clinic_name','us')}, "
        f"still happy to help whenever works for you. Reply here anytime."
    )
    if not ANTHROPIC_API_KEY:
        return fallback

    system_prompt = (
        "You are Follow-up Agent for a clinic. Write a brief, warm, low-pressure follow-up "
        "email to someone who started enquiring but went quiet before booking. This is "
        f"follow-up attempt {attempt_number} of {MAX_FOLLOW_UPS}, so if it's a later attempt, "
        "keep it light rather than pushy, people are allowed to have changed their mind. "
        "No exclamation marks, no markdown, no emoji. Under 60 words. "
        "Respond with ONLY the email body text, nothing else."
    )
    user_prompt = (
        f"Clinic: {clinic.get('clinic_name','')}\n"
        f"What they were asking about: {conv.get('answers', {})}\n"
    )
    try:
        text = _call_claude(system_prompt, user_prompt)
        return text.strip() if text.strip() else fallback
    except Exception:
        return fallback


# ======================================================
#  MAIN ENTRY (called on a schedule)
# ======================================================
def run_followup_check(sb=None, log=print) -> dict:
    sb = sb or _get_supabase()
    if not sb:
        return {"ok": False, "message": "No database connection."}

    due = find_due_conversations(sb)
    if not due:
        return {"ok": True, "sent": 0}

    sent = 0
    for conv in due:
        try:
            clinic_res = sb.table("clinics").select("*").eq("id", conv["clinic_id"]).single().execute()
            clinic = clinic_res.data or {}
            profile_res = sb.table("profiles").select("gmail, full_name").eq("id", conv["user_id"]).single().execute()
            profile = profile_res.data or {}
        except Exception:
            continue

        sender_email = profile.get("gmail", "")
        if not sender_email:
            continue  # clinic hasn't set up an email in Setup yet

        attempt_number = conv.get("follow_up_count", 0) + 1
        body = _write_followup_message(clinic, conv, attempt_number)
        subject = f"Following up, {clinic.get('clinic_name', 'your enquiry')}"

        ok = notifications.send_email(
            conv.get("visitor_contact", ""),
            conv.get("visitor_name", ""),
            sender_email,
            clinic.get("clinic_name", "") or profile.get("full_name", ""),
            subject,
            body,
        )

        if ok:
            sent += 1
            try:
                sb.table("conversations").update({
                    "follow_up_count": attempt_number,
                    "last_follow_up_at": datetime.utcnow().isoformat(),
                }).eq("id", conv["id"]).execute()
                _log(conv["id"], conv["user_id"], "follow_up_sent", f"attempt {attempt_number}: {body[:150]}", sb)
            except Exception:
                pass
            log(f"Follow-up Agent: sent follow-up #{attempt_number} for conversation {conv['id']}")

    return {"ok": True, "sent": sent, "checked": len(due)}
