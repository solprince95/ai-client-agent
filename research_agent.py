"""
research_agent.py: "Research Agent"

Before Email Agent or WhatsApp Agent write to a lead, Research Agent
visits the lead's website and builds a short profile: what the business
actually does, and a specific detail worth referencing in the outreach
message. Both agents then use this same cached profile, so a lead is
only researched once, not once per channel.

Uses Claude Haiku (fast, cheap, plenty for this) via the Anthropic API.
Requires ANTHROPIC_API_KEY as an environment variable on Render. If it's
not set, everything degrades gracefully: no research happens, and
Email/WhatsApp Agent fall back to their original template-based
messages, nothing breaks.
"""

import os
import re
import json
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"

RESEARCH_MAX_AGE_DAYS = 60  # re-research a lead after this long, businesses change


def research_configured() -> bool:
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
#  WEBSITE TEXT EXTRACTION
# ======================================================
def _extract_visible_text(url: str, max_chars: int = 3500) -> str:
    """Fetches a page and pulls out just the readable text, stripped of
    HTML/scripts/styles, trimmed to a reasonable size for an LLM call."""
    if not url:
        return ""
    try:
        resp = requests.get(url, timeout=(6, 8), headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code >= 400:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text)
        return text[:max_chars]
    except Exception:
        return ""


# ======================================================
#  CLAUDE CALL
# ======================================================
def _call_claude(system_prompt: str, user_prompt: str, max_tokens: int = 400) -> str:
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
        timeout=(10, 25),
    )
    if resp.status_code >= 400:
        raise Exception(resp.text)
    data = resp.json()
    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()


def _parse_json_block(text: str) -> dict:
    """Claude sometimes wraps JSON in ```json fences even when told not
    to, strip those before parsing."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


# ======================================================
#  RESEARCH
# ======================================================
def research_business(business_name: str, website: str, config: dict) -> dict:
    """
    Returns {"summary": ..., "hook": ..., "needs": ...}.
    Falls back to a generic, still-usable profile if research isn't
    configured, the site is unreachable, or the API call fails, this
    never blocks Email/WhatsApp Agent from sending.
    """
    fallback = {
        "summary": f"{business_name} is a local business.",
        "hook": "",
        "needs": config.get("YOUR_SERVICE", "your service"),
    }

    if not research_configured():
        return fallback

    page_text = _extract_visible_text(website)
    if not page_text:
        return fallback

    domain = urlparse(website).netloc.replace("www.", "") if website else ""

    system_prompt = (
        "You are Research Agent, part of an outreach tool. You read a business's "
        "website and produce a short, factual profile that will be used to write "
        "a personalized, genuine outreach message to them. Be specific and concrete, "
        "avoid generic filler. Respond with ONLY a JSON object, no markdown fences, "
        "no preamble, in this exact shape: "
        '{"summary": "1-2 sentences on what this business actually does", '
        '"hook": "one specific, genuine detail from their site worth mentioning in an opening line", '
        '"needs": "a short guess at what this business could plausibly want from the sender\'s service"}'
    )
    user_prompt = (
        f"Business name: {business_name}\n"
        f"Website: {domain}\n\n"
        f"Website content:\n{page_text}\n\n"
        f"The sender offers: {config.get('YOUR_SERVICE', '')}\n"
        f"Sender's pitch: {config.get('YOUR_ABOUT', '')}"
    )

    try:
        raw = _call_claude(system_prompt, user_prompt, max_tokens=300)
        profile = _parse_json_block(raw)
        return {
            "summary": profile.get("summary", fallback["summary"]),
            "hook":    profile.get("hook", ""),
            "needs":   profile.get("needs", fallback["needs"]),
        }
    except Exception:
        return fallback


def get_or_research(lead_row: dict, config: dict, user_id: str, supabase=None) -> dict:
    """
    Checks a lead's cached research on the leads table first. Only calls
    Claude (and re-saves) if there's no cache yet, or it's stale.
    """
    sb = supabase or _get_supabase()

    researched_at = lead_row.get("researched_at")
    if researched_at and lead_row.get("research_summary"):
        try:
            age = datetime.utcnow() - datetime.fromisoformat(researched_at.replace("Z", "+00:00")).replace(tzinfo=None)
            if age < timedelta(days=RESEARCH_MAX_AGE_DAYS):
                return {
                    "summary": lead_row.get("research_summary", ""),
                    "hook":    lead_row.get("research_hook", ""),
                    "needs":   lead_row.get("research_needs", "") or config.get("YOUR_SERVICE", ""),
                }
        except Exception:
            pass  # bad/old timestamp format, just re-research

    profile = research_business(
        lead_row.get("business_name", ""),
        lead_row.get("website", ""),
        config,
    )

    if sb and user_id and lead_row.get("id"):
        try:
            sb.table("leads").update({
                "research_summary": profile["summary"],
                "research_hook":    profile["hook"],
                "research_needs":   profile["needs"],
                "researched_at":    datetime.utcnow().isoformat(),
            }).eq("id", lead_row["id"]).eq("user_id", user_id).execute()
        except Exception:
            pass  # not critical, the profile still gets used for this send

    return profile


# ======================================================
#  WRITE PERSONALIZED MESSAGES
# ======================================================
def write_email(business_name: str, research: dict, config: dict) -> tuple:
    """Returns (subject, body). Falls back to None if AI writing isn't
    available, caller should fall back to the template in that case."""
    if not research_configured():
        return None

    system_prompt = (
        "You are Email Agent, part of an outreach tool. Write a short, genuine "
        "cold outreach email using the research profile provided. Sound like a "
        "real person wrote it, not a template. No corporate filler, no exclamation "
        "marks, no markdown formatting. 80-120 words. "
        "Respond with ONLY a JSON object, no markdown fences: "
        '{"subject": "...", "body": "..."}'
    )
    user_prompt = (
        f"Recipient business: {business_name}\n"
        f"What they do: {research.get('summary','')}\n"
        f"Specific detail to reference: {research.get('hook','')}\n"
        f"What they likely need: {research.get('needs','')}\n\n"
        f"Sender name: {config.get('YOUR_NAME','')}\n"
        f"Sender's service: {config.get('YOUR_SERVICE','')}\n"
        f"Sender's pitch: {config.get('YOUR_ABOUT','')}\n"
        f"Sender's email: {config.get('GMAIL_ADDRESS','')}\n\n"
        f"End the email with the sender's name and email, and a line offering to "
        f"reply 'Unsubscribe' to opt out."
    )
    try:
        raw = _call_claude(system_prompt, user_prompt, max_tokens=350)
        data = _parse_json_block(raw)
        subject = data.get("subject", "").strip()
        body = data.get("body", "").strip()
        if subject and body:
            return (subject, body)
    except Exception:
        pass
    return None


def write_whatsapp_message(business_name: str, research: dict, config: dict) -> str:
    """Returns a short WhatsApp message string, or None to fall back to
    the template."""
    if not research_configured():
        return None

    system_prompt = (
        "You are WhatsApp Agent, part of an outreach tool. Write a short, casual "
        "but genuine first WhatsApp message using the research profile provided. "
        "Sound like a real person, not a template. No exclamation marks, no "
        "markdown formatting, no emoji. Under 50 words. "
        "Respond with ONLY the message text, nothing else."
    )
    user_prompt = (
        f"Recipient business: {business_name}\n"
        f"What they do: {research.get('summary','')}\n"
        f"Specific detail to reference: {research.get('hook','')}\n"
        f"What they likely need: {research.get('needs','')}\n\n"
        f"Sender name: {config.get('YOUR_NAME','')}\n"
        f"Sender's service: {config.get('YOUR_SERVICE','')}\n"
        f"Sender's pitch: {config.get('YOUR_ABOUT','')}\n\n"
        f"End with a line offering to reply STOP to opt out."
    )
    try:
        text = _call_claude(system_prompt, user_prompt, max_tokens=150)
        return text.strip() if text.strip() else None
    except Exception:
        return None
