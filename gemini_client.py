"""
gemini_client.py: shared helper for calling Google's Gemini models via
Vertex AI. This fully replaces the earlier Claude-via-Vertex setup -
no Anthropic dependency anywhere in this codebase anymore.

Why Gemini instead of Claude-via-Vertex: same billing benefit (routes
through GCP, UPI-friendly, no separate Anthropic account needed at
all), but one less moving part - Gemini is Google's own first-party
model in Vertex AI, no separate "enable this partner model in Model
Garden" step required the way Claude needed.

Auth: Google's Application Default Credentials. On Cloud Run, that's
the service's own runtime service account - it needs the Vertex AI
User role (roles/aiplatform.user). Locally, run
`gcloud auth application-default login` once for the same effect.

Required env vars:
  GOOGLE_CLOUD_PROJECT - your GCP project ID (e.g. vajra-labs-calendar)
  VERTEX_REGION        - defaults to us-central1
"""

import os
import time

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
REGION = os.environ.get("VERTEX_REGION", "us-central1")

_client = None


def is_configured() -> bool:
    return bool(PROJECT_ID)


def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(vertexai=True, project=PROJECT_ID, location=REGION)
    return _client


def _is_thinking_unsupported_error(e: Exception) -> bool:
    """
    True only for a genuine SDK/version mismatch where thinking_config
    itself isn't a recognized field (a pydantic "extra_forbidden" style
    error mentioning "thinking"). NOT true for rate limits, timeouts, or
    any other transient failure - those need a different response
    (backoff + retry with the SAME config), not silently dropping
    thinking_config, which is what caused a real production bug: a rate
    limit got misclassified as an SDK mismatch, thinking got dropped on
    retry, and the dropped-thinking retry hit the mid-JSON truncation
    bug thinking_budget=0 exists to prevent in the first place.
    """
    msg = str(e).lower()
    return "thinking" in msg and ("extra_forbidden" in msg or "validationerror" in msg.lower())


def _is_rate_limit_error(e: Exception) -> bool:
    msg = str(e)
    return "RESOURCE_EXHAUSTED" in msg or " 429" in msg or msg.strip().startswith("429")


def generate(system_prompt: str, user_prompt: str, model: str, max_tokens: int = 300) -> str:
    """
    Same shape as the old _call_claude helpers this replaces: system +
    user prompt in, plain response text out. Raises on failure, callers
    already wrap this in their own try/except with a graceful-degrade
    fallback reply.

    thinking_budget=0 disables Gemini 2.5's internal reasoning tokens.
    Those tokens count against max_output_tokens by default, and for
    these simple, low-latency conversational turns (answer extraction,
    short replies) they were silently eating the whole token budget
    before the model ever got to writing the actual visible response,
    causing responses to truncate mid-JSON. This task doesn't need
    chain-of-thought reasoning, so thinking is switched off entirely,
    and stays off on every retry below, no exceptions.
    """
    from google.genai import types

    client = _get_client()
    include_thinking = True
    last_error = None

    for attempt in range(3):
        kwargs = {"system_instruction": system_prompt, "max_output_tokens": max_tokens}
        if include_thinking:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        config = types.GenerateContentConfig(**kwargs)

        try:
            response = client.models.generate_content(model=model, contents=user_prompt, config=config)
            return (response.text or "").strip()
        except Exception as e:
            last_error = e
            if include_thinking and _is_thinking_unsupported_error(e):
                print(f"gemini_client: thinking_config unsupported ({type(e).__name__}: {e}), retrying without it")
                include_thinking = False
                continue
            if _is_rate_limit_error(e):
                wait = 1.5 * (attempt + 1)
                print(f"gemini_client: rate limited (attempt {attempt + 1}/3), backing off {wait}s")
                time.sleep(wait)
                continue
            raise

    raise last_error
