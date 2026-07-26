"""
conversation_agent.py: "Reply Agent" + "Qualify Agent"

The core loop for the new clinic product. A visitor lands on a clinic's
website, opens the chat widget, and this module:
  1. Greets them and captures consent
  2. Answers routine questions using the clinic's own knowledge base
     (hours, services, pricing, FAQs)
  3. Asks the clinic's qualification questions, one at a time
  4. Scores the lead hot/warm/cold once qualification is done
  5. Hands off to Booking Agent (next phase) or a human

Python controls the deterministic parts (which question we're on, when
consent is required, when to log activity). Claude handles the natural
language: answering FAQs in the moment, phrasing the next question
naturally, and judging whether the visitor's last message actually
answered the current question.

Requires ANTHROPIC_API_KEY (same one Research Agent uses). Degrades to
a simple, honest "I'm having trouble responding right now" message if
the API isn't configured or a call fails, never crashes the widget.
"""

import os
import re
import json
from datetime import datetime

import requests

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"

DEFAULT_QUALIFICATION_QUESTIONS = [
    "What are you looking to get help with today?",
    "Is this your first visit, or have you visited before?",
    "Do you have a preferred date or time, or is it urgent?",
]


def engine_configured() -> bool:
    return bool(ANTHROPIC_API_KEY)


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
#  CLAUDE CALL
# ======================================================
def _call_claude(system_prompt: str, user_prompt: str, max_tokens: int = 300) -> str:
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


def _parse_json_block(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


# ======================================================
#  ACTIVITY LOG
# ======================================================
def _log(conversation_id, user_id, event_type, detail, sb):
    if not sb:
        return
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
#  CLINIC PROFILE
# ======================================================
def get_clinic(user_id: str, sb=None) -> dict:
    sb = sb or _get_supabase()
    if not sb:
        return {}
    try:
        res = sb.table("clinics").select("*").eq("user_id", user_id).single().execute()
        return res.data or {}
    except Exception:
        return {}


def save_clinic(user_id: str, fields: dict, sb=None) -> dict:
    sb = sb or _get_supabase()
    if not sb:
        return {"ok": False, "message": "Could not connect to your account."}
    fields = dict(fields)
    fields["updated_at"] = datetime.utcnow().isoformat()
    try:
        existing = sb.table("clinics").select("id").eq("user_id", user_id).execute()
        if existing.data:
            sb.table("clinics").update(fields).eq("user_id", user_id).execute()
        else:
            fields["user_id"] = user_id
            sb.table("clinics").insert(fields).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def get_questions(clinic: dict) -> list:
    q = clinic.get("qualification_questions")
    if isinstance(q, list) and q:
        return q
    return DEFAULT_QUALIFICATION_QUESTIONS


# ======================================================
#  CONVERSATION LIFECYCLE
# ======================================================
def start_conversation(clinic_id: str, user_id: str, sb=None) -> dict:
    sb = sb or _get_supabase()
    if not sb:
        return {"ok": False, "message": "Chat isn't available right now."}
    try:
        res = sb.table("conversations").insert({
            "clinic_id": clinic_id,
            "user_id": user_id,
            "status": "active",
            "stage": "qualifying",
            "question_index": 0,
        }).execute()
        conversation = res.data[0]
        return {"ok": True, "conversation_id": conversation["id"]}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def give_consent(conversation_id: str, sb=None) -> dict:
    sb = sb or _get_supabase()
    if not sb:
        return {"ok": False}
    try:
        res = sb.table("conversations").update({"consent_given": True}).eq("id", conversation_id).execute()
        conv = res.data[0] if res.data else {}
        _log(conversation_id, conv.get("user_id"), "consent_given", "", sb)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def _save_message(conversation_id, role, content, sb):
    try:
        sb.table("messages").insert({
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
        }).execute()
    except Exception:
        pass


def handle_message(conversation_id: str, visitor_message: str, sb=None) -> dict:
    """
    The main turn of the conversation. Returns {"ok": True, "reply": "..."}.
    """
    sb = sb or _get_supabase()
    if not sb:
        return {"ok": False, "reply": "Chat isn't available right now, please try again shortly."}

    try:
        conv_res = sb.table("conversations").select("*").eq("id", conversation_id).single().execute()
        conv = conv_res.data
    except Exception:
        return {"ok": False, "reply": "This conversation could not be found."}

    if not conv:
        return {"ok": False, "reply": "This conversation could not be found."}

    if not conv.get("consent_given"):
        return {"ok": False, "reply": "Please accept the consent notice to continue."}

    if conv.get("status") == "human_takeover":
        _save_message(conversation_id, "visitor", visitor_message, sb)
        return {"ok": True, "reply": None, "human_takeover": True}

    _save_message(conversation_id, "visitor", visitor_message, sb)

    clinic_res = sb.table("clinics").select("*").eq("id", conv["clinic_id"]).single().execute()
    clinic = clinic_res.data or {}
    questions = get_questions(clinic)
    q_index = conv.get("question_index", 0)
    stage = conv.get("stage", "qualifying")

    if not engine_configured():
        reply = "Thanks for reaching out! Someone from our team will follow up with you shortly."
        _save_message(conversation_id, "bot", reply, sb)
        return {"ok": True, "reply": reply}

    if stage == "qualifying" and q_index < len(questions):
        current_question = questions[q_index]
        result = _run_qualification_turn(clinic, current_question, visitor_message, conv.get("answers") or {})

        updates = {}
        answers = dict(conv.get("answers") or {})
        if result.get("answered"):
            answers[current_question] = result.get("extracted_answer", visitor_message)
            updates["answers"] = answers
            new_index = q_index + 1
            updates["question_index"] = new_index
            if new_index >= len(questions):
                score = _score_lead(answers)
                updates["stage"] = "contact_capture"
                updates["score"] = score
                _log(conversation_id, conv["user_id"], "score_updated", score, sb)

        if updates:
            sb.table("conversations").update(updates).eq("id", conversation_id).execute()

        reply = result.get("reply", "Could you tell me a bit more?")

    elif stage == "contact_capture":
        result = _run_contact_capture_turn(visitor_message)
        if result.get("contact_captured"):
            sb.table("conversations").update({
                "stage": "booking",
                "visitor_contact": result.get("contact", ""),
            }).eq("id", conversation_id).execute()
        reply = result.get("reply", "What's the best email or phone number to reach you at?")

    elif stage == "booking":
        result = _run_booking_turn(clinic, visitor_message)
        if result.get("time_captured"):
            try:
                sb.table("appointments").insert({
                    "conversation_id": conversation_id,
                    "clinic_id": conv["clinic_id"],
                    "user_id": conv["user_id"],
                    "visitor_name": conv.get("visitor_name", ""),
                    "visitor_contact": conv.get("visitor_contact", ""),
                    "requested_text": result.get("requested_text", visitor_message),
                }).execute()
                _log(conversation_id, conv["user_id"], "booked", result.get("requested_text", ""), sb)
            except Exception:
                pass
            sb.table("conversations").update({"stage": "done", "status": "booked"}).eq("id", conversation_id).execute()
        reply = result.get("reply", "What date or time works best for you?")

    else:
        reply = _run_faq_turn(clinic, visitor_message)

    _save_message(conversation_id, "bot", reply, sb)
    _log(conversation_id, conv["user_id"], "bot_reply", reply[:200], sb)
    return {"ok": True, "reply": reply}


# ======================================================
#  CLAUDE TURNS
# ======================================================
def _clinic_context(clinic: dict) -> str:
    return (
        f"Clinic name: {clinic.get('clinic_name','')}\n"
        f"Clinic type: {clinic.get('clinic_type','')}\n"
        f"Hours: {clinic.get('hours','')}\n"
        f"Location: {clinic.get('location','')}\n"
        f"Services: {clinic.get('services','')}\n"
        f"Pricing notes: {clinic.get('pricing_notes','')}\n"
        f"FAQs: {clinic.get('faqs','')}\n"
    )


def _run_qualification_turn(clinic: dict, current_question: str, visitor_message: str, answers_so_far: dict) -> dict:
    system_prompt = (
        "You are Qualify Agent, a friendly front-desk assistant for a clinic, chatting with a "
        "website visitor. You are currently trying to get an answer to ONE specific question. "
        "If the visitor's message answers it (even loosely), extract that answer. If they instead "
        "asked something else (a question about hours, pricing, services), answer it briefly and "
        "naturally using the clinic info provided, then still ask the current question. "
        "Sound like a real person texting, not a form. No exclamation marks, no markdown, no emoji. "
        "Keep replies under 40 words. "
        "Respond with ONLY a JSON object: "
        '{"answered": true or false, "extracted_answer": "short answer if answered, else empty", '
        '"reply": "your natural reply to send the visitor"}'
    )
    user_prompt = (
        f"Clinic info:\n{_clinic_context(clinic)}\n"
        f"Question we're trying to get answered: {current_question}\n"
        f"Answers already collected: {json.dumps(answers_so_far)}\n"
        f"Visitor just said: {visitor_message}"
    )
    try:
        raw = _call_claude(system_prompt, user_prompt, max_tokens=250)
        return _parse_json_block(raw)
    except Exception:
        return {"answered": False, "extracted_answer": "", "reply": "Sorry, could you say that again?"}


def _run_contact_capture_turn(visitor_message: str) -> dict:
    """
    Captures an email or phone number so Follow-up Agent has somewhere
    to reach the visitor later if they don't complete booking. Kept as
    its own small, deterministic-ish step, simple pattern matching first,
    Claude only as a fallback for oddly-phrased replies.
    """
    email_match = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", visitor_message)
    phone_match = re.search(r"(\+?\d[\d\s-]{7,14}\d)", visitor_message)
    if email_match:
        return {"contact_captured": True, "contact": email_match.group(0),
                "reply": "Got it, thank you. What date or time works best for you?"}
    if phone_match:
        return {"contact_captured": True, "contact": phone_match.group(0),
                "reply": "Got it, thank you. What date or time works best for you?"}

    if not engine_configured():
        return {"contact_captured": False, "reply": "Could you share an email or phone number to reach you at?"}

    system_prompt = (
        "The visitor was asked for their email or phone number. Check if their message "
        "actually contains one (even informally written). No exclamation marks, no markdown. "
        'Respond with ONLY a JSON object: {"contact_captured": true or false, "contact": "the '
        'email or phone if found, else empty", "reply": "natural reply, re-asking if not found"}'
    )
    try:
        raw = _call_claude(system_prompt, f"Visitor said: {visitor_message}", max_tokens=150)
        return _parse_json_block(raw)
    except Exception:
        return {"contact_captured": False, "reply": "Could you share an email or phone number to reach you at?"}


def _run_booking_turn(clinic: dict, visitor_message: str) -> dict:
    """
    Booking Agent's turn: capture a requested date/time for the visit.
    Doesn't touch a real calendar yet (that's Phase 2b), just captures
    what the visitor wants so staff can confirm it. Deliberately simple:
    accepts loose answers like "Tuesday afternoon" rather than forcing
    an exact date, that's a staff-confirmation problem, not a bot one.
    """
    system_prompt = (
        "You are Booking Agent for a clinic's website chat. The visitor has been qualified "
        "and now needs to say what date/time they'd like to come in. If their message gives "
        "any indication of a preferred date or time (even loose, like 'Tuesday afternoon' or "
        "'as soon as possible'), treat it as captured and confirm it back warmly, letting them "
        "know the clinic will confirm the exact slot. If they haven't given a date/time yet, "
        "ask for one naturally. No exclamation marks, no markdown, no emoji, under 40 words. "
        "Respond with ONLY a JSON object: "
        '{"time_captured": true or false, "requested_text": "what they said about timing, if captured", '
        '"reply": "your natural reply to send the visitor"}'
    )
    user_prompt = f"Clinic info:\n{_clinic_context(clinic)}\n\nVisitor said: {visitor_message}"
    try:
        raw = _call_claude(system_prompt, user_prompt, max_tokens=200)
        return _parse_json_block(raw)
    except Exception:
        return {"time_captured": False, "requested_text": "", "reply": "What date or time works best for you? We'll confirm shortly."}


def _run_faq_turn(clinic: dict, visitor_message: str) -> str:
    system_prompt = (
        "You are Reply Agent for a clinic's website chat. Qualification is done. Answer the "
        "visitor's question using the clinic info provided, naturally and briefly, under 40 words. "
        "If you don't know the answer from the clinic info, say a team member will follow up with "
        "them shortly rather than guessing. No exclamation marks, no markdown, no emoji. "
        "Respond with ONLY the reply text, nothing else."
    )
    user_prompt = f"Clinic info:\n{_clinic_context(clinic)}\n\nVisitor said: {visitor_message}"
    try:
        text = _call_claude(system_prompt, user_prompt, max_tokens=150)
        return text.strip() if text.strip() else "Someone from our team will follow up with you shortly."
    except Exception:
        return "Someone from our team will follow up with you shortly."


def _score_lead(answers: dict) -> str:
    """Simple rubric, deliberately not sophisticated, per the v1 scope decision."""
    text = " ".join(str(v).lower() for v in answers.values())
    urgent_words = ["urgent", "asap", "today", "tomorrow", "pain", "emergency", "soon"]
    vague_words = ["just looking", "just browsing", "maybe", "not sure", "just checking"]
    if any(w in text for w in urgent_words):
        return "hot"
    if any(w in text for w in vague_words):
        return "cold"
    return "warm"


# ======================================================
#  HUMAN TAKEOVER
# ======================================================
def staff_takeover(conversation_id: str, user_id: str, sb=None) -> dict:
    sb = sb or _get_supabase()
    if not sb:
        return {"ok": False}
    try:
        sb.table("conversations").update({"status": "human_takeover"}).eq("id", conversation_id).execute()
        _log(conversation_id, user_id, "staff_takeover", "", sb)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def staff_reply(conversation_id: str, user_id: str, message: str, sb=None) -> dict:
    sb = sb or _get_supabase()
    if not sb:
        return {"ok": False}
    _save_message(conversation_id, "staff", message, sb)
    _log(conversation_id, user_id, "staff_reply", message[:200], sb)
    return {"ok": True}


def resume_bot(conversation_id: str, user_id: str, sb=None) -> dict:
    sb = sb or _get_supabase()
    if not sb:
        return {"ok": False}
    try:
        sb.table("conversations").update({"status": "active"}).eq("id", conversation_id).execute()
        _log(conversation_id, user_id, "bot_resumed", "", sb)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "message": str(e)}
