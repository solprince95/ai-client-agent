import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_supabase import FakeSupabase
import conversation_agent as ca


def test_visitor_name_saved_mid_conversation_like_ravi_scenario(monkeypatch):
    """
    Reproduces the real bug report: a visitor says "My name is Ravi"
    partway through qualification, not in direct answer to any
    scripted question. Before the fix, this was never saved anywhere.
    """
    fake = FakeSupabase()
    fake.seed("clinics", {
        "id": "c1", "clinic_name": "Smile Care", "qualification_questions": None,
    })
    conv_id = fake.seed("conversations", {
        "clinic_id": "c1", "user_id": "u1", "status": "active", "stage": "qualifying",
        "question_index": 0, "consent_given": True, "answers": {}, "visitor_name": "",
    })

    # The qualification-turn AI call doesn't need to be realistic here,
    # just needs to return valid JSON so handle_message runs to completion.
    monkeypatch.setattr(ca, "_call_ai",
                         lambda system_prompt, user_prompt, max_tokens=300:
                         '{"answered": false, "extracted_answer": "", "reply": "Hi Ravi, nice to meet you. What brings you in today?"}')

    result = ca.handle_message(conv_id, "My name is Ravi.", sb=fake)

    assert result["ok"] is True
    updated_conv = fake.table("conversations").select("*").eq("id", conv_id).single().execute().data
    assert updated_conv["visitor_name"] == "Ravi"


def test_visitor_name_not_overwritten_once_set(monkeypatch):
    fake = FakeSupabase()
    fake.seed("clinics", {"id": "c1", "clinic_name": "Smile Care", "qualification_questions": None})
    conv_id = fake.seed("conversations", {
        "clinic_id": "c1", "user_id": "u1", "status": "active", "stage": "qualifying",
        "question_index": 0, "consent_given": True, "answers": {}, "visitor_name": "Ravi",
    })
    monkeypatch.setattr(ca, "_call_ai",
                         lambda system_prompt, user_prompt, max_tokens=300:
                         '{"answered": false, "extracted_answer": "", "reply": "ok"}')

    ca.handle_message(conv_id, "Actually my name is Someone Else", sb=fake)

    updated_conv = fake.table("conversations").select("*").eq("id", conv_id).single().execute().data
    assert updated_conv["visitor_name"] == "Ravi"
