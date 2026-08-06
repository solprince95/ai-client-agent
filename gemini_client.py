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
    chain-of-thought reasoning, so thinking is switched off entirely.
    """
    from google.genai import types

    client = _get_client()
    try:
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=max_tokens,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        response = client.models.generate_content(model=model, contents=user_prompt, config=config)
    except Exception as e:
        # If the installed SDK version doesn't support thinking_config
        # the way we expect (this has happened before - version drift
        # between environments), don't take down every conversation
        # over it. Retry once without it; thinking just means slightly
        # higher latency/cost, not broken output.
        print(f"gemini_client: thinking_config unsupported ({type(e).__name__}: {e}), retrying without it")
        config = types.GenerateContentConfig(system_instruction=system_prompt, max_output_tokens=max_tokens)
        response = client.models.generate_content(model=model, contents=user_prompt, config=config)
    return (response.text or "").strip()
