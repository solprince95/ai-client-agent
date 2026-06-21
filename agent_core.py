"""
agent_core.py — Core agent logic, refactored for the web dashboard.
Same functionality as the CLI version, but every step reports progress
through a `log(message)` callback so the dashboard can show live status.
"""

import os, re, time, smtplib, imaplib, email, requests, csv
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

BASE     = os.path.dirname(os.path.abspath(__file__))
SENT_CSV = os.path.join(BASE, "sent_log.csv")

EMAIL_RE   = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
SKIP_WORDS = {"example","domain","email","test","sentry","wix",".png",".jpg",".svg"}


def _noop(msg):
    pass


# ══════════════════════════════════════════════════════
#  DIAGNOSTICS
# ══════════════════════════════════════════════════════
def run_diagnostics(config, log=_noop):
    ok = True
    log("Running pre-flight checks...")

    for key in ["YOUR_NAME", "YOUR_SERVICE", "TARGET_CITY"]:
        if not config[key].strip():
            log(f"❌ {key} is empty in your profile.")
            ok = False
    if ok:
        log(f"✅ Profile: {config['YOUR_NAME']} | {config['YOUR_SERVICE']} | {config['TARGET_CITY']}")

    if config["GOOGLE_MAPS_API_KEY"] in ("", "YOUR_GOOGLE_MAPS_API_KEY"):
        log("❌ Search engine not configured. Contact support.")
        ok = False
    else:
        log("Testing search engine connection...")
        try:
            r = requests.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={"query": f"shop {config['TARGET_CITY']}",
                        "key": config["GOOGLE_MAPS_API_KEY"]}, timeout=10)
            status = r.json().get("status")
            if status == "OK":
                log("✅ Search engine connected.")
            elif status == "REQUEST_DENIED":
                log(f"❌ Search engine error: {r.json().get('error_message','')}")
                ok = False
            else:
                log(f"⚠️  Search status: {status}")
        except Exception as e:
            log(f"❌ Could not reach search engine: {e}")
            ok = False

    if config["GMAIL_ADDRESS"] in ("", "your_email@gmail.com"):
        log("❌ Gmail not configured. Add your Gmail address in Setup.")
        ok = False
    else:
        log(f"✅ Gmail connected.")

    if config.get("ATTACHMENT_PATH"):
        path = os.path.join(BASE, config["ATTACHMENT_PATH"])
        if os.path.exists(path):
            log(f"✅ Attachment ready: {config['ATTACHMENT_PATH']}")
        else:
            log(f"⚠️  Attachment '{config['ATTACHMENT_PATH']}' not found — will send without it.")

    return ok


# ══════════════════════════════════════════════════════
#  STEP 1 — Find businesses via Google Maps
# ══════════════════════════════════════════════════════
def search_businesses(config, log=_noop):
    city = config["TARGET_CITY"]
    queries = [f"{btype} {city}" for btype in config["BUSINESS_TYPES"]]

    log(f"Searching for potential clients in {city}...")
    all_biz = {}
    for query in queries:
        try:
            r = requests.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={"query": query, "key": config["GOOGLE_MAPS_API_KEY"]}, timeout=10)
            data = r.json()
            new = 0
            for p in data.get("results", [])[:config["MAX_RESULTS_PER_QUERY"]]:
                pid = p.get("place_id")
                if pid and pid not in all_biz:
                    all_biz[pid] = {"name": p.get("name",""),
                                    "address": p.get("formatted_address",""),
                                    "place_id": pid, "website": None, "email": None}
                    new += 1
            token = data.get("next_page_token")
            while token:
                time.sleep(2)
                r2 = requests.get("https://maps.googleapis.com/maps/api/place/textsearch/json",
                                  params={"pagetoken": token,
                                          "key": config["GOOGLE_MAPS_API_KEY"]}, timeout=10)
                d2 = r2.json()
                for p in d2.get("results", []):
                    pid = p.get("place_id")
                    if pid and pid not in all_biz:
                        all_biz[pid] = {"name": p.get("name",""),
                                        "address": p.get("formatted_address",""),
                                        "place_id": pid, "website": None, "email": None}
                        new += 1
                token = d2.get("next_page_token")
            log(f"  '{query}' → +{new} ({len(all_biz)} total found)")
        except Exception as e:
            log(f"  '{query}' → error: {e}")
        time.sleep(1)
    log(f"Found {len(all_biz)} unique businesses.")
    return list(all_biz.values())


# ══════════════════════════════════════════════════════
#  STEP 2 — Filter businesses with websites
# ══════════════════════════════════════════════════════
def fetch_websites(businesses, config, log=_noop):
    log("Checking which businesses have a website...")
    with_sites = []
    for i, biz in enumerate(businesses, 1):
        try:
            r = requests.get("https://maps.googleapis.com/maps/api/place/details/json",
                             params={"place_id": biz["place_id"], "fields": "website",
                                     "key": config["GOOGLE_MAPS_API_KEY"]}, timeout=10)
            site = r.json().get("result", {}).get("website")
            if site:
                biz["website"] = site
                with_sites.append(biz)
        except Exception:
            pass
        if i % 10 == 0 or i == len(businesses):
            log(f"  Checked {i}/{len(businesses)} — {len(with_sites)} have websites so far")
        time.sleep(0.5)
    log(f"{len(with_sites)} of {len(businesses)} businesses have a website.")
    return with_sites


# ══════════════════════════════════════════════════════
#  STEP 3 — Extract emails from websites
# ══════════════════════════════════════════════════════
def extract_email(site_url):
    headers  = {"User-Agent": "Mozilla/5.0"}
    to_check = [site_url]
    found    = set()
    try:
        r    = requests.get(site_url, headers=headers, timeout=8, allow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if h.startswith("mailto:"):
                found.add(h.replace("mailto:","").split("?")[0].lower())
            elif any(k in h.lower() for k in ["contact","about","reach","connect"]):
                full = urljoin(site_url, h)
                if urlparse(full).netloc == urlparse(site_url).netloc:
                    to_check.append(full)
    except Exception:
        pass
    for url in to_check[:4]:
        try:
            r = requests.get(url, headers=headers, timeout=8, allow_redirects=True)
            for e in EMAIL_RE.findall(r.text):
                if not any(s in e.lower() for s in SKIP_WORDS):
                    found.add(e.lower())
        except Exception:
            pass
        time.sleep(0.3)
    clean = [e for e in found if "noreply" not in e and "no-reply" not in e]
    return clean[0] if clean else (list(found)[0] if found else None)


def find_emails(businesses, log=_noop):
    log("Looking for contact emails on each website...")
    with_emails = []
    for i, biz in enumerate(businesses, 1):
        em = extract_email(biz["website"])
        if em:
            biz["email"] = em
            with_emails.append(biz)
        if i % 5 == 0 or i == len(businesses):
            log(f"  Checked {i}/{len(businesses)} — {len(with_emails)} emails found so far")
        time.sleep(0.8)
    log(f"{len(with_emails)} businesses with usable email addresses.")
    return with_emails


# ══════════════════════════════════════════════════════
#  STEP 4 — Build the personalised email
# ══════════════════════════════════════════════════════
def build_email(biz, config):
    name    = biz["name"]
    website = biz.get("website") or ""
    domain  = urlparse(website).netloc.replace("www.", "") if website else ""
    site_line = f" ({domain})" if domain else ""

    subject = f"Quick offer for {name} — {config['YOUR_SERVICE']}"

    attach_line = "I've attached a sample of my work for your reference.\n\n" if config.get("ATTACHMENT_PATH") else ""

    body = f"""Dear {name} Team,

My name is {config['YOUR_NAME']}.

{config['YOUR_ABOUT']}

I came across your business{site_line} and thought you might benefit from {config['YOUR_SERVICE'].lower()}.

If this sounds useful for {name}, I'd be happy to share more details — including pricing and how we can get started.

{attach_line}Simply reply to this email and I'll get back to you within 24 hours.

Best regards,

{config['YOUR_NAME']}
📧 {config['GMAIL_ADDRESS']}

───────────────────────────────────────────
Reply "Unsubscribe" if you'd prefer not to hear from me again.
"""
    return subject, body


# ══════════════════════════════════════════════════════
#  STEP 5 — Send emails
#  Sent log is stored in Supabase (sent_log table) so it
#  persists across Render restarts and redeploys.
#  Falls back to local CSV if Supabase is not configured.
# ══════════════════════════════════════════════════════
def _get_supabase():
    """Return a Supabase client if env vars are set, else None."""
    try:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        if url and key:
            return create_client(url, key)
    except Exception:
        pass
    return None


def load_sent(user_id=None):
    """
    Load already-sent email addresses.
    Tries Supabase first (production), falls back to local CSV (dev).
    """
    sent = set()

    # Try Supabase
    sb = _get_supabase()
    if sb and user_id:
        try:
            res = sb.table("sent_log").select("email").eq("user_id", user_id).execute()
            for row in (res.data or []):
                em = row.get("email", "").strip().lower()
                if em:
                    sent.add(em)
            return sent
        except Exception:
            pass  # fall through to CSV

    # Fallback: local CSV
    if os.path.exists(SENT_CSV):
        with open(SENT_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                em = row.get("email", "").strip().lower()
                if em:
                    sent.add(em)
    return sent


def mark_sent(addr, name="", website="", user_id=None, subject="", body=""):
    """
    Record a sent email.
    Writes to Supabase (production) and local CSV (always, as backup).
    """
    addr = addr.lower().strip()
    today = str(datetime.now().date())

    # Write to Supabase
    sb = _get_supabase()
    if sb and user_id:
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(sb.table("sent_log").insert({
                    "user_id":       user_id,
                    "email":         addr,
                    "business_name": name,
                    "website":       website,
                    "sent_date":     today,
                    "subject":       subject,
                    "body":          body,
                }).execute)
                future.result(timeout=10)
        except Exception as e:
            print(f"mark_sent Supabase error: {e}", flush=True)

    # Always write to local CSV as backup
    file_exists = os.path.exists(SENT_CSV)
    with open(SENT_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["email", "name", "website", "sent_date"])
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "email": addr, "name": name,
            "website": website, "sent_date": today,
        })


def send_one(biz, config, log=_noop, user_id=None):
    if biz["email"] in load_sent(user_id=user_id):
        return False

    subject, body = build_email(biz, config)
    msg = MIMEMultipart()
    msg["From"]    = config["GMAIL_ADDRESS"]
    msg["To"]      = biz["email"]
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    attach_path = os.path.join(BASE, config["ATTACHMENT_PATH"]) if config.get("ATTACHMENT_PATH") else ""
    if attach_path and os.path.exists(attach_path):
        with open(attach_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f'attachment; filename="{config["ATTACHMENT_NAME"]}"')
            msg.attach(part)

    try:
        brevo_api_key = os.environ.get("BREVO_API_KEY", "")
        response = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": brevo_api_key,
                "Content-Type": "application/json"
            },
            json={
                "sender": {"name": config.get("YOUR_NAME", ""), "email": config["GMAIL_ADDRESS"]},
                "to": [{"email": biz["email"], "name": biz.get("name", "")}],
                "subject": subject,
                "htmlContent": body
            },
            timeout=(10, 15)
        )
        if response.status_code >= 400:
            raise Exception(response.text)
        try:
            mark_sent(biz["email"],
                      name=biz.get("name", ""),
                      website=biz.get("website", ""),
                      user_id=user_id,
                      subject=subject,
                      body=body)
        except Exception as me:
            log(f"  ⚠️ Sent but log failed: {me}")
        log(f"  ✅ Sent → {biz['name']} ({biz['email']})")
        return True
    except Exception as e:
        log(f"  ❌ Failed → {biz['email']}: {e}")
        return False


FAKE_EMAIL_PATTERNS = [
    "example.com", "company.com", "domain.com", "test.com",
    "yoursite.com", "you@", "info@example", "email@email",
]

def _is_fake_email(email):
    email = email.lower().strip()
    if not email or "@" not in email:
        return True
    domain = email.split("@")[-1]
    # Check for invalid TLDs (real domains have known TLDs)
    tld = domain.split(".")[-1] if "." in domain else ""
    if len(tld) > 6 or len(tld) < 2:
        return True
    for pattern in FAKE_EMAIL_PATTERNS:
        if pattern in email:
            return True
    return False

def send_all(businesses, config, log=_noop, user_id=None):
    log("Sending personalised emails...")
    sent = 0
    for i, biz in enumerate(businesses):
        if _is_fake_email(biz.get("email", "")):
            log(f"  ⏭️ Skipped fake email → {biz.get('email','?')}")
            continue
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(send_one, biz, config, log, user_id)
                ok = future.result(timeout=30)
        except concurrent.futures.TimeoutError:
            log(f"  ⚠️ Timed out → {biz.get('email','?')} (skipping)")
            ok = False
        except Exception as e:
            log(f"  ⚠️ Skipped {biz.get('email','?')}: {e}")
            ok = False
        if ok:
            sent += 1
            if i < len(businesses) - 1:
                time.sleep(config["DELAY_BETWEEN_EMAILS"])
    log(f"{sent} new email(s) sent.")
    return sent


# ══════════════════════════════════════════════════════
#  CHECK REPLIES
# ══════════════════════════════════════════════════════
def check_replies(config, log=_noop, user_id=None):
    """
    Only shows replies from email addresses we actually contacted.
    Loads contacted list from Supabase (production) or CSV (dev/fallback).
    """
    log("Checking your inbox for replies from contacted businesses...")
    replies = []

    # Load the list of emails we sent to
    contacted = load_sent(user_id=user_id)

    if not contacted:
        log("📭 No sent emails on record yet — run the agent first.")
        return replies

    log(f"   Checking for replies from {len(contacted)} contacted business(es)...")

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(config["GMAIL_ADDRESS"], config["GMAIL_APP_PASSWORD"].replace(" ", ""))
        mail.select("inbox")

        # Search ALL inbox messages (not just UNSEEN) so we don't miss anything
        _, data = mail.search(None, "ALL")
        all_ids = data[0].split()

        found = 0
        for eid in all_ids:
            try:
                # Fetch only headers first (much faster than full RFC822)
                _, md = mail.fetch(eid, "(BODY[HEADER.FIELDS (FROM)])")
                raw_from = md[0][1].decode(errors="ignore").lower()

                # Quick check — is any contacted email in this header?
                if not any(em in raw_from for em in contacted):
                    continue

                # Full fetch only for matching messages
                _, md_full = mail.fetch(eid, "(RFC822)")
                msg = email.message_from_bytes(md_full[0][1])

                sender_raw = msg.get("From", "")
                import email.utils as eu
                _, sender_addr = eu.parseaddr(sender_raw)
                sender_addr = sender_addr.lower().strip()

                if sender_addr in contacted:
                    found += 1
                    replies.append({
                        "from":    sender_raw,
                        "subject": msg.get("Subject", ""),
                        "date":    msg.get("Date", ""),
                    })
                    log(f"  🎉 REPLY → From: {sender_raw} | Subject: {msg.get('Subject','')}")
            except Exception:
                continue

        if found == 0:
            log("📭 No replies from contacted businesses yet. Keep following up!")
        else:
            log(f"📬 {found} reply(ies) from businesses you contacted!")

        mail.logout()
    except imaplib.IMAP4.error as e:
        log(f"❌ Gmail error: {e}")
        log("   Enable IMAP: Gmail ⚙️ → See all settings → Forwarding and POP/IMAP")

    return replies


# ══════════════════════════════════════════════════════
#  FULL PIPELINE
# ══════════════════════════════════════════════════════
def run_full_pipeline(config, log=_noop, user_id=None):
    if not run_diagnostics(config, log=log):
        log("⛔ Setup incomplete. Fix the issues above in Setup, then run again.")
        return {"ok": False}

    biz   = search_businesses(config, log=log)
    sites = fetch_websites(biz, config, log=log)
    leads = find_emails(sites, log=log)
    sent  = send_all(leads, config, log=log, user_id=user_id)
    check_replies(config, log=log, user_id=user_id)

    log("✅ Run complete!")
    return {"ok": True, "found": len(biz), "with_sites": len(sites),
            "with_emails": len(leads), "sent": sent}


# ══════════════════════════════════════════════════════
#  STATS
# ══════════════════════════════════════════════════════
def get_stats(user_id=None):
    """Total emails sent — from Supabase if available, else local CSV."""
    sb = _get_supabase()
    if sb and user_id:
        try:
            res = sb.table("sent_log").select("id", count="exact").eq("user_id", user_id).execute()
            return {"total_sent": res.count or 0}
        except Exception:
            pass
    # Fallback: local CSV
    total_sent = 0
    if os.path.exists(SENT_CSV):
        with open(SENT_CSV, newline="", encoding="utf-8") as f:
            total_sent = sum(1 for _ in csv.DictReader(f))
    return {"total_sent": total_sent}
