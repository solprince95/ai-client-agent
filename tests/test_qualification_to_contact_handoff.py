import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_supabase import FakeSupabase
import conversation_agent as ca


def test_last_question_answered_asks_for_contact_in_same_reply(monkeypatch):
    fake = FakeSupabase()
    fake.seed("clinics", {"id": "c1", "clinic_name": "Smile Care", "qualification_questions": None})
    conv_id = fake.seed("conversations", {
        "clinic_id": "c1", "user_id": "u1", "status": "active", "stage": "qualifying",
        "question_index": 2,  # the last of the 3 default questions
        "consent_given": True, "answers": {}, "visitor_name": "Shreya",
    })

    monkeypatch.setattr(ca, "engine_configured", lambda: True)
    monkeypatch.setattr(ca, "_call_ai",
                         lambda system_prompt, user_prompt, max_tokens=300:
                         '{"answered": true, "extracted_answer": "No preference", '
                         '"reply": "Thanks, I will get those details pulled up for you now"}')

    result = ca.handle_message(conv_id, "No time.", sb=fake)

    assert result["ok"] is True
    # This is the actual bug: previously the visitor had to send another
    # message before ever being asked for contact info.
    assert "email or phone" in result["reply"].lower()

    updated = fake.table("conversations").select("*").eq("id", conv_id).single().execute().data
    assert updated["stage"] == "contact_capture"


def test_non_final_question_does_not_ask_for_contact(monkeypatch):
    fake = FakeSupabase()
    fake.seed("clinics", {"id": "c1", "clinic_name": "Smile Care", "qualification_questions": None})
    conv_id = fake.seed("conversations", {
        "clinic_id": "c1", "user_id": "u1", "status": "active", "stage": "qualifying",
        "question_index": 0,  # first of 3, more questions remain
        "consent_given": True, "answers": {}, "visitor_name": "",
    })

    monkeypatch.setattr(ca, "engine_configured", lambda: True)
    monkeypatch.setattr(ca, "_call_ai",
                         lambda system_prompt, user_prompt, max_tokens=300:
                         '{"answered": true, "extracted_answer": "checkup", '
                         '"reply": "Got it, a checkup. Have you visited us before?"}')

    result = ca.handle_message(conv_id, "A checkup.", sb=fake)

    assert "email or phone" not in result["reply"].lower()
