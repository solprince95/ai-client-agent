import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gemini_client as gc


def test_is_thinking_unsupported_error_detects_real_case():
    e = Exception("1 validation error for ThinkingConfig\nthinking_budget\n  Extra inputs are not permitted [type=extra_forbidden, ...]")
    assert gc._is_thinking_unsupported_error(e) is True


def test_is_thinking_unsupported_error_does_not_match_rate_limit():
    # This is the exact bug: a 429 must NEVER be classified as a
    # thinking-unsupported error, or thinking gets dropped on retry
    # and the mid-JSON truncation bug comes back.
    e = Exception("ClientError: 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'Resource exhausted.'}}")
    assert gc._is_thinking_unsupported_error(e) is False


def test_is_rate_limit_error_detects_429():
    e = Exception("ClientError: 429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}")
    assert gc._is_rate_limit_error(e) is True


def test_is_rate_limit_error_does_not_match_other_errors():
    e = Exception("1 validation error for ThinkingConfig, extra_forbidden")
    assert gc._is_rate_limit_error(e) is False


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    def __init__(self, call_log, behaviors):
        self.call_log = call_log
        self.behaviors = list(behaviors)

    def generate_content(self, model, contents, config):
        self.call_log.append({
            "has_thinking_config": getattr(config, "thinking_config", None) is not None,
        })
        behavior = self.behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return _FakeResponse(behavior)


class _FakeClient:
    def __init__(self, call_log, behaviors):
        self.models = _FakeModels(call_log, behaviors)


def test_rate_limit_retries_with_thinking_still_disabled(monkeypatch):
    """
    The actual bug from production: a 429 must be retried with the SAME
    config (thinking still off), not treated as a reason to drop
    thinking_config.
    """
    call_log = []
    rate_limit_error = Exception("ClientError: 429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}")
    fake_client = _FakeClient(call_log, [rate_limit_error, "final reply"])

    monkeypatch.setattr(gc, "_get_client", lambda: fake_client)
    monkeypatch.setattr(gc.time, "sleep", lambda seconds: None)  # skip real waiting in tests

    result = gc.generate("system", "user", model="gemini-2.5-flash", max_tokens=300)

    assert result == "final reply"
    assert len(call_log) == 2
    assert call_log[0]["has_thinking_config"] is True
    assert call_log[1]["has_thinking_config"] is True  # still on for the retry


def test_thinking_unsupported_drops_thinking_config_on_retry(monkeypatch):
    call_log = []
    validation_error = Exception(
        "1 validation error for ThinkingConfig\nthinking_budget\n  Extra inputs are not permitted [type=extra_forbidden]"
    )
    fake_client = _FakeClient(call_log, [validation_error, "final reply"])

    monkeypatch.setattr(gc, "_get_client", lambda: fake_client)

    result = gc.generate("system", "user", model="gemini-2.5-flash", max_tokens=300)

    assert result == "final reply"
    assert len(call_log) == 2
    assert call_log[0]["has_thinking_config"] is True
    assert call_log[1]["has_thinking_config"] is False  # correctly dropped only for this case


def test_unknown_error_raises_immediately_without_retrying(monkeypatch):
    call_log = []
    weird_error = ValueError("something totally unrelated broke")
    fake_client = _FakeClient(call_log, [weird_error, "should never be reached"])

    monkeypatch.setattr(gc, "_get_client", lambda: fake_client)

    import pytest
    with pytest.raises(ValueError):
        gc.generate("system", "user", model="gemini-2.5-flash", max_tokens=300)

    assert len(call_log) == 1  # did not retry a non-retryable error
