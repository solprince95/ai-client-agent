"""
whatsapp_agent.py — WhatsApp outreach, mirrors agent_core.py's email pipeline.

Uses the official WhatsApp Cloud API (Meta Graph API). Every business
gets a phone number scraped the same way emails do (see agent_core.py),
so this module just adds the "send" + "log" + "mark lead contacted" steps
for that channel.

⚠️ PLACEHOLDER CONFIG: WHATSAPP_ACCESS_TOKEN / WHATSAPP_PHONE_NUMBER_ID
are not set yet. Until they're filled in (via Setup, once the "Connect
WhatsApp" / Embedded Signup flow exists), send_whatsapp_one() will fail
gracefully and log a clear message instead of crashing.
"""

import os, re, csv, json, time
from datetime import datetime

import requests

from agent_core import BASE, _get_supabase, _noop, mark_lead_contacted  # reuse existing infra

WHATSAPP_SENT_CSV = os.path.join(BASE, "whatsapp_sent_log.csv")

# Graph API version — bump when you upgrade.
GRAPH_API_VERSION = "v21.0"

PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")  # loose E.164-ish check


# ══════════════════════════════════════════════════════
#  CONFIG HELPERS
# ══════════════════════════════════════════════════════
def whatsapp_configured(config: dict) -> bool:
    """True once the user has connected a real WhatsApp Business number."""
    return bool(config.get("WHATSAPP_ACCESS_TOKEN") and config.get("WHATSAPP_PHONE_NUMBER_ID"))


def _normalize_phone(raw: str) -> str:
    """Strip spaces/dashes/parens; Graph API wants digits only (with country code, no '+')."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    return digits.lstrip("+")


def _is_valid_phone(raw: str) -> bool:
    digits = _normalize_phone(raw)
    return bool(PHONE_RE.match("+" + digits)) if digits else False


# ══════════════════════════════════════════════════════
#  MESSAGE BUILDING
#  NOTE: WhatsApp requires a pre-approved template for the very
#  first message to someone who hasn't messaged you in the last
#  24h. build_whatsapp_template_message() sends via a template;
#  build_whatsapp_freeform_message() is for replies within the
#  24h customer-service window (plain text, no approval needed).
# ══════════════════════════════════════════════════════
def build_whatsapp_freeform_message(biz: dict, config: dict) -> str:
    name = biz.get("name", "there")
    return (
        f"Hi {name} team! This is {config.get('YOUR_NAME','')} — "
        f"{config.get('YOUR_ABOUT','')}\n\n"
        f"I thought {config.get('YOUR_SERVICE','')} could help your business. "
        f"Happy to share more details if you're interested — just reply here!\n\n"
        f"Reply STOP if you'd rather not hear from me again."
    )


def build_whatsapp_template_payload(biz: dict, config: dict) -> dict:
    """
    Cold-outreach (first contact) MUST use an approved template.
    template_name / language should be created + approved once in
    Meta Business Manager, then referenced here by name.
    Placeholder template name below — replace once you've created
    and had one approved.
    """
    template_name = config.get("WHATSAPP_TEMPLATE_NAME", "business_outreach_intro")
    template_lang = config.get("WHATSAPP_TEMPLATE_LANG", "en_US")
    return {
        "name": template_name,
        "language": {"code": template_lang},
        "components": [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": biz.get("name", "there")},
                    {"type": "text", "text": config.get("YOUR_NAME", "")},
                    {"type": "text", "text": config.get("YOUR_SERVICE", "")},
                ],
            }
        ],
    }


# ══════════════════════════════════════════════════════
#  SENT LOG  (mirrors load_sent / mark_sent in agent_core.py)
# ══════════════════════════════════════════════════════
def load_whatsapp_sent(user_id=None) -> set:
    sent = set()
    sb = _get_supabase()
    if sb and user_id:
        try:
            res = sb.table("whatsapp_log").select("phone").eq("user_id", user_id).execute()
            for row in (res.data or []):
                p = (row.get("phone") or "").strip()
                if p:
                    sent.add(p)
            return sent
        except Exception:
            pass  # fall through to CSV

    if os.path.exists(WHATSAPP_SENT_CSV):
        with open(WHATSAPP_SENT_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                p = (row.get("phone") or "").strip()
                if p:
                    sent.add(p)
    return sent


def mark_whatsapp_sent(phone, name="", user_id=None, message="", wa_message_id=""):
    phone = _normalize_phone(phone)
    today = str(datetime.now().date())

    sb = _get_supabase()
    if sb and user_id:
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(sb.table("whatsapp_log").insert({
                    "user_id":        user_id,
                    "phone":          phone,
                    "business_name":  name,
                    "sent_date":      today,
                    "message":        message,
                    "wa_message_id":  wa_message_id,
                }).execute)
                future.result(timeout=10)
        except Exception as e:
            print(f"mark_whatsapp_sent Supabase error: {e}", flush=True)

    file_exists = os.path.exists(WHATSAPP_SENT_CSV)
    with open(WHATSAPP_SENT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["phone", "name", "sent_date"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({"phone": phone, "name": name, "sent_date": today})


def mark_lead_whatsapp_contacted(phone, user_id=None, message=""):
    """Same idea as agent_core.mark_lead_contacted(), but keyed on phone + a
    separate whatsapp_status column so email + WhatsApp status can be tracked
    independently on the same lead row."""
    sb = _get_supabase()
    if not (sb and user_id):
        return
    try:
        sb.table("leads").update({
            "whatsapp_status":     "contacted",
            "whatsapp_sent_date":  str(datetime.now().date()),
            "whatsapp_message_sent": message,
        }).eq("user_id", user_id).eq("phone", phone).execute()
    except Exception as e:
        print(f"mark_lead_whatsapp_contacted error: {e}", flush=True)


# ══════════════════════════════════════════════════════
#  SEND
# ══════════════════════════════════════════════════════
def send_whatsapp_one(biz: dict, config: dict, log=_noop, user_id=None, use_template=True) -> bool:
    phone = biz.get("phone", "")
    if not _is_valid_phone(phone):
        log(f"  ⏭️ Skipped invalid phone → {phone or '?'}")
        return False

    norm_phone = _normalize_phone(phone)
    if norm_phone in load_whatsapp_sent(user_id=user_id):
        return False

    if not whatsapp_configured(config):
        log("  ⚠️ WhatsApp not connected yet — add WHATSAPP_ACCESS_TOKEN / "
            "WHATSAPP_PHONE_NUMBER_ID in Setup (Meta Embedded Signup) to enable sending.")
        return False

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{config['WHATSAPP_PHONE_NUMBER_ID']}/messages"
    headers = {
        "Authorization": f"Bearer {config['WHATSAPP_ACCESS_TOKEN']}",
        "Content-Type": "application/json",
    }

    if use_template:
        message_preview = f"[template] {config.get('WHATSAPP_TEMPLATE_NAME', 'business_outreach_intro')}"
        payload = {
            "messaging_product": "whatsapp",
            "to": norm_phone,
            "type": "template",
            "template": build_whatsapp_template_payload(biz, config),
        }
    else:
        message_preview = build_whatsapp_freeform_message(biz, config)
        payload = {
            "messaging_product": "whatsapp",
            "to": norm_phone,
            "type": "text",
            "text": {"body": message_preview},
        }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=(10, 15))
        if response.status_code >= 400:
            raise Exception(response.text)

        wa_message_id = ""
        try:
            wa_message_id = (response.json().get("messages") or [{}])[0].get("id", "")
        except Exception:
            pass

        try:
            mark_whatsapp_sent(norm_phone, name=biz.get("name", ""), user_id=user_id,
                                message=message_preview, wa_message_id=wa_message_id)
            mark_lead_whatsapp_contacted(norm_phone, user_id=user_id, message=message_preview)
        except Exception as me:
            log(f"  ⚠️ Sent but log failed: {me}")

        log(f"  ✅ WhatsApp sent → {biz.get('name','?')} ({norm_phone})")
        return True
    except Exception as e:
        log(f"  ❌ WhatsApp failed → {norm_phone}: {e}")
        return False


def send_whatsapp_all(businesses, config, log=_noop, user_id=None, use_template=True):
    log("Sending WhatsApp messages...")
    sent = 0
    delay = config.get("DELAY_BETWEEN_EMAILS", 30)  # reuse same throttle setting
    for i, biz in enumerate(businesses):
        ok = send_whatsapp_one(biz, config, log=log, user_id=user_id, use_template=use_template)
        if ok:
            sent += 1
            if i < len(businesses) - 1:
                time.sleep(delay)
    log(f"{sent} new WhatsApp message(s) sent.")
    return sent


def send_whatsapp_to_selected_leads(lead_ids, config, log=_noop, user_id=None, use_template=True):
    """
    Same pattern as agent_core.send_to_selected_leads(), but sends over
    WhatsApp instead of email, using each lead's stored phone number.
    """
    sb = _get_supabase()
    if not (sb and user_id):
        log("⛔ Could not connect to your account's lead list.")
        return {"ok": False, "sent": 0}

    if not lead_ids:
        log("No leads selected — nothing to send.")
        return {"ok": True, "sent": 0}

    try:
        res = sb.table("leads").select("*") \
                .eq("user_id", user_id) \
                .in_("id", lead_ids) \
                .execute()
        rows = res.data or []
    except Exception as e:
        log(f"⛔ Could not load selected leads: {e}")
        return {"ok": False, "sent": 0}

    businesses = []
    for r in rows:
        phone = r.get("phone", "")
        if not phone:
            continue
        businesses.append({
            "name":          r.get("business_name", ""),
            "phone":         phone,
            "business_type": r.get("business_type", ""),
        })

    if not businesses:
        log("None of the selected leads have a phone number on file.")
        return {"ok": True, "sent": 0, "selected": len(lead_ids)}

    log(f"Sending WhatsApp messages to {len(businesses)} selected lead(s)...")
    sent = send_whatsapp_all(businesses, config, log=log, user_id=user_id, use_template=use_template)

    log("✅ WhatsApp send complete! Go to the Leads tab to see updated statuses.")
    return {"ok": True, "sent": sent, "selected": len(lead_ids)}


# ══════════════════════════════════════════════════════
#  STATS
# ══════════════════════════════════════════════════════
def get_whatsapp_stats(user_id=None):
    sb = _get_supabase()
    if sb and user_id:
        try:
            res = sb.table("whatsapp_log").select("id", count="exact").eq("user_id", user_id).execute()
            return {"total_sent": res.count or 0}
        except Exception:
            pass
    total_sent = 0
    if os.path.exists(WHATSAPP_SENT_CSV):
        with open(WHATSAPP_SENT_CSV, newline="", encoding="utf-8") as f:
            total_sent = sum(1 for _ in csv.DictReader(f))
    return {"total_sent": total_sent}
