import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as app_module
from fake_supabase import FakeSupabase


def _authed_client_with_fake_db():
    fake = FakeSupabase()
    app_module.supabase = fake
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = "u1"
    return client, fake


def test_conversations_list_hides_anonymous_visitors():
    client, fake = _authed_client_with_fake_db()
    # Someone who opened the widget and never said anything / never gave a name or contact
    fake.seed("conversations", {
        "user_id": "u1", "clinic_id": "c1", "status": "active",
        "visitor_name": "", "visitor_contact": "",
    })
    # A real, identified lead
    fake.seed("conversations", {
        "user_id": "u1", "clinic_id": "c1", "status": "active",
        "visitor_name": "Vishwajeet", "visitor_contact": "",
    })
    # A lead identified only by phone (no name captured yet)
    fake.seed("conversations", {
        "user_id": "u1", "clinic_id": "c1", "status": "active",
        "visitor_name": "", "visitor_contact": "+919913620474",
    })

    resp = client.get("/api/conversations")
    data = resp.get_json()

    assert data["ok"] is True
    assert len(data["conversations"]) == 2
    labels = {c.get("visitor_name") or c.get("visitor_contact") for c in data["conversations"]}
    assert labels == {"Vishwajeet", "+919913620474"}


def test_conversations_list_requires_login():
    fake = FakeSupabase()
    app_module.supabase = fake
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    resp = client.get("/api/conversations")
    assert resp.status_code == 401
