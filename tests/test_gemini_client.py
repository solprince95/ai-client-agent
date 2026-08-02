import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gemini_client
import conversation_agent
import followup_agent
import research_agent


def test_is_configured_reflects_project_env(monkeypatch):
    monkeypatch.setattr(gemini_client, "PROJECT_ID", "")
    assert gemini_client.is_configured() is False
    monkeypatch.setattr(gemini_client, "PROJECT_ID", "vajra-labs-calendar")
    assert gemini_client.is_configured() is True


def test_conversation_agent_delegates_to_gemini_client(monkeypatch):
    calls = []
    monkeypatch.setattr(gemini_client, "generate",
                         lambda system_prompt, user_prompt, model, max_tokens=300:
                         calls.append((model, max_tokens)) or "reply text")
    result = conversation_agent._call_ai("system", "user", max_tokens=250)
    assert result == "reply text"
    assert calls[0][0] == conversation_agent.MODEL
    assert calls[0][1] == 250


def test_followup_agent_delegates_to_gemini_client(monkeypatch):
    calls = []
    monkeypatch.setattr(gemini_client, "generate",
                         lambda system_prompt, user_prompt, model, max_tokens=150:
                         calls.append(model) or "followup text")
    result = followup_agent._call_ai("system", "user")
    assert result == "followup text"
    assert calls[0] == followup_agent.MODEL


def test_research_agent_delegates_to_gemini_client(monkeypatch):
    calls = []
    monkeypatch.setattr(gemini_client, "generate",
                         lambda system_prompt, user_prompt, model, max_tokens=400:
                         calls.append(model) or "research text")
    result = research_agent._call_ai("system", "user")
    assert result == "research text"
    assert calls[0] == research_agent.MODEL


def test_followup_agent_falls_back_without_crashing_when_not_configured(monkeypatch):
    monkeypatch.setattr(followup_agent.gemini_client, "is_configured", lambda: False)
    msg = followup_agent._write_followup_message({"clinic_name": "Smile Dental"}, {}, 1)
    assert "Smile Dental" in msg


def test_model_constants_use_gemini_not_claude():
    assert "gemini" in conversation_agent.MODEL.lower()
    assert "gemini" in followup_agent.MODEL.lower()
    assert "gemini" in research_agent.MODEL.lower()
    assert "claude" not in conversation_agent.MODEL.lower()
