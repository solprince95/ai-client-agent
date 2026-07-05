"""
agent_core.py — Core agent logic, refactored for the web dashboard.
Same functionality as the CLI version, but every step reports progress
through a `log(message)` callback so the dashboard can show live status.
"""

import os, re, time, smtplib, imaplib, email, requests, csv, json
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
    queries = [(btype, f"{btype} {city}") for btype in config["BUSINESS_TYPES"]]

    log(f"Searching for potential clients in {city}...")
    all_biz = {}
    for btype, query in queries:
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
                                    "place_id": pid, "website": None, "email": None,
                                    "business_type": btype}
                    new += 1
            token = data.get("next_page_token")
            while token:
                time.sleep(0.2)
                r2 = requests.get("https://maps.googleapis.com/maps/api/place/textsearch/json",
                                  params={"pagetoken": token,
                                          "key": config["GOOGLE_MAPS_API_KEY"]}, timeout=10)
                d2 = r2.json()
                for p in d2.get("results", []):
                    pid = p.get("place_id")
                    if pid and pid not in all_biz:
                        all_biz[pid] = {"name": p.get("name",""),
                                        "address": p.get("formatted_address",""),
                                        "place_id": pid, "website": None, "email": None,
                                        "business_type": btype}
                        new += 1
                token = d2.get("next_page_token")
            log(f"  '{query}' → +{new} ({len(all_biz)} total found)")
        except Exception as e:
            log(f"  '{query}' → error: {e}")
        time.sleep(0.1)
    log(f"Found {len(all_biz)} unique businesses.")
    return list(all_biz.values())


# ══════════════════════════════════════════════════════
#  STEP 2 — Filter businesses with websites
# ══════════════════════════════════════════════════════
def fetch_websites(businesses, config, log=_noop):
    log("Checking each business for a website and phone number...")
    with_sites = []
    for i, biz in enumerate(businesses, 1):
        try:
            r = requests.get("https://maps.googleapis.com/maps/api/place/details/json",
                             params={"place_id": biz["place_id"],
                                     "fields": "website,formatted_phone_number,international_phone_number",
                                     "key": config["GOOGLE_MAPS_API_KEY"]}, timeout=10)
            result = r.json().get("result", {})
            site  = result.get("website")
            phone = result.get("formatted_phone_number") or result.get("international_phone_number")
            if phone:
                biz["phone"] = phone
            if site:
                biz["website"] = site
                with_sites.append(biz)
        except Exception:
            pass
        if i % 10 == 0 or i == len(businesses):
            log(f"  Checked {i}/{len(businesses)} — {len(with_sites)} have websites so far")
        time.sleep(0.2)
    log(f"{len(with_sites)} of {len(businesses)} businesses have a website.")
    return with_sites


# ══════════════════════════════════════════════════════
#  STEP 3 — Extract emails from websites
# ══════════════════════════════════════════════════════

# Large chains / platforms that will never respond to cold outreach
# and whose sites are slow, JS-heavy, or bot-protected.
_SKIP_DOMAINS = {
    "amazon", "flipkart", "myntra", "meesho", "snapdeal", "nykaa",
    "zomato", "swiggy", "dunzo", "blinkit", "zepto",
    "firstcry", "ajio", "tatacliq", "jiomart", "bigbasket",
    "croma", "reliancedigital", "vijaysales", "poorvika",
    "dmart", "dmart.in", "avenue-supermarts",
    "makemytrip", "goibibo", "yatra", "booking.com", "oyo",
    "justdial", "sulekha", "indiamart", "tradeindia", "exportersindia",
    "facebook", "instagram", "twitter", "linkedin", "youtube",
    "google", "wikipedia", "wix.com", "godaddy",
}

def _should_skip(url):
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        return any(s in domain for s in _SKIP_DOMAINS)
    except Exception:
        return False


def _fetch_text(url, max_bytes=60_000, site_deadline=None):
    """
    Fetch URL text with hard limits on every axis:
    - 3s connect timeout
    - 4s read timeout per chunk
    - 60 KB body cap
    - wall-clock site_deadline: if set, stops reading once time.time() > deadline
    Any single site is therefore capped at ~8s total regardless of chunk speed.
    """
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        with requests.get(url, headers=headers,
                          timeout=(3, 4),
                          allow_redirects=True,
                          stream=True) as r:
            chunks = []
            total  = 0
            for chunk in r.iter_content(chunk_size=8192):
                if site_deadline and time.time() > site_deadline:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total >= max_bytes:
                    break
            return b"".join(chunks).decode("utf-8", errors="ignore")
    except Exception:
        return ""


PHONE_RE = re.compile(
    r'(?:\+91[\-\s]?)?'
    r'(?:\(?[6-9]\d{4}\)?[\-\s]?\d{5}'
    r'|\(?0\d{2,4}\)?[\-\s]?\d{6,8})'
)

_BAD_EXTENSIONS = re.compile(r'\.(png|jpg|jpeg|gif|svg|webp|avif|ico|pdf|zip|css|js|mp4|mp3|woff|ttf)$', re.I)

def _is_valid_email(e):
    """Return True only if e looks like a real email, not an image/asset path."""
    if not e or '@' not in e:
        return False
    local, _, domain = e.partition('@')
    if not domain or '.' not in domain:
        return False
    if _BAD_EXTENSIONS.search(e):
        return False
    if any(s in e.lower() for s in SKIP_WORDS):
        return False
    return True

_COMPANY_PREFIXES = re.compile(
    r'^(info|contact|support|sales|admin|hello|mail|office|enquiry|query|team|hr|help)',
    re.I
)

# Titles/positions we look for near a person's name
_POSITION_KEYWORDS = re.compile(
    r'\b(CEO|CTO|CFO|COO|CMO|Founder|Co-Founder|Director|Manager|Head|Owner|'
    r'President|Partner|MD|Managing Director|General Manager|GM|VP|'
    r'Principal|Proprietor|Chairman|Executive|Officer|Lead|Engineer|'
    r'Designer|Developer|Consultant|Advisor|Associate|Coordinator|'
    r'Accountant|Doctor|Dr\.?|Prof\.?|Professor)\b',
    re.I
)

# Name-like pattern: two or three capitalised words, not all-caps acronyms
_NAME_RE = re.compile(r'\b([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{1,20}){1,2})\b')


def _extract_people(soup, all_emails, all_phones):
    """
    Parse structured people records from a BeautifulSoup page.
    Tries three strategies in order:
      1. schema.org/Person JSON-LD blocks
      2. vCard / hCard microformat elements
      3. Heuristic: find name+position near an email/phone in the DOM
    Returns a list of dicts: {name, position, email, phone}
    """
    people = []
    seen_emails = set()

    # ── Strategy 1: JSON-LD schema.org/Person ──────────────
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            # May be a single object or a list
            items = data if isinstance(data, list) else [data]
            for item in items:
                # Handle @graph arrays
                if "@graph" in item:
                    items += item["@graph"]
                t = item.get("@type", "")
                if "Person" not in (t if isinstance(t, str) else " ".join(t)):
                    continue
                name     = item.get("name", "")
                position = item.get("jobTitle", "") or item.get("role", "")
                email    = (item.get("email", "") or "").replace("mailto:", "").lower().strip()
                phone    = re.sub(r'[\s\-.()+]', '', item.get("telephone", "") or "")
                if name and email and email not in seen_emails:
                    people.append({"name": name, "position": position, "email": email, "phone": phone})
                    seen_emails.add(email)
        except Exception:
            pass

    # ── Strategy 2: hCard / vCard microformat ──────────────
    for card in soup.find_all(class_=re.compile(r'\bvcard\b|\bhcard\b', re.I)):
        name     = (card.find(class_=re.compile(r'\bfn\b', re.I)) or card).get_text(" ", strip=True)
        position = ""
        el = card.find(class_=re.compile(r'(title|role|org)', re.I))
        if el:
            position = el.get_text(" ", strip=True)
        email_el = card.find("a", href=re.compile(r'^mailto:', re.I))
        email    = email_el["href"].replace("mailto:", "").lower().strip() if email_el else ""
        phone_el = card.find("a", href=re.compile(r'^tel:', re.I))
        phone    = re.sub(r'[\s\-.()+]', '', phone_el["href"].replace("tel:", "")) if phone_el else ""
        if name and email and email not in seen_emails and len(name) < 60:
            people.append({"name": name[:60], "position": position[:60], "email": email, "phone": phone})
            seen_emails.add(email)

    # ── Strategy 3: heuristic DOM scan ────────────────────
    # Look at short text blocks that contain a position keyword
    # and see if a name pattern and email/phone are nearby.
    if len(people) < 5:
        # Build a phone lookup set for fast matching
        phone_set = set(all_phones)

        for el in soup.find_all(["p", "div", "li", "span", "td"]):
            text = el.get_text(" ", strip=True)
            if len(text) > 300 or len(text) < 4:
                continue
            if not _POSITION_KEYWORDS.search(text):
                continue

            # Find a name in the same block
            names = _NAME_RE.findall(text)
            if not names:
                continue
            name = names[0]

            # Extract position keyword
            pos_match = _POSITION_KEYWORDS.search(text)
            position  = pos_match.group(0) if pos_match else ""

            # Look for email in this element or a close sibling
            email = ""
            for candidate in EMAIL_RE.findall(text):
                candidate = candidate.lower().strip()
                if _is_valid_email(candidate):
                    email = candidate
                    break
            # Also check sibling/parent text if no email in this block
            if not email:
                parent_text = el.parent.get_text(" ", strip=True) if el.parent else ""
                for candidate in EMAIL_RE.findall(parent_text):
                    candidate = candidate.lower().strip()
                    if _is_valid_email(candidate):
                        email = candidate
                        break

            # Look for phone similarly
            phone = ""
            for p in PHONE_RE.findall(text):
                cleaned = re.sub(r'[\s\-.()+]', '', p)
                if len(cleaned) >= 10:
                    phone = cleaned
                    break

            if name and (email or phone) and email not in seen_emails and len(name) < 60:
                people.append({"name": name, "position": position, "email": email, "phone": phone})
                if email:
                    seen_emails.add(email)
            if len(people) >= 5:
                break

    return people[:5]


def _hunter_lookup(domain):
    """
    Call Hunter.io Domain Search API to get verified people + emails for a domain.
    Returns a list of {name, position, email, phone} dicts.
    Requires HUNTER_API_KEY environment variable.
    Free tier: 25 searches/month. Paid: from $49/month.
    """
    api_key = os.environ.get("HUNTER_API_KEY", "")
    if not api_key or not domain:
        return []
    try:
        r = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": api_key, "limit": 10},
            timeout=(3, 8)
        )
        if r.status_code != 200:
            return []
        data = r.json().get("data", {})
        people = []
        for p in (data.get("emails") or []):
            first = p.get("first_name", "") or ""
            last  = p.get("last_name", "")  or ""
            name  = (first + " " + last).strip()
            people.append({
                "name":     name,
                "position": p.get("position") or p.get("department") or "",
                "email":    (p.get("value") or "").lower().strip(),
                "phone":    "",   # Hunter doesn't provide phone numbers
            })
        return [p for p in people if p["email"]]
    except Exception:
        return []


def extract_contact_info(site_url):
    """
    Returns a dict with:
      company_email  – best company inbox (info@, contact@, etc.)
      people_data    – list of {name, position, email, phone} dicts
      people_emails  – flat list of personal emails (back-compat)
      people_phones  – flat list of phones (back-compat)
    """
    if _should_skip(site_url):
        return {}

    site_deadline = time.time() + 8
    all_emails    = set()
    all_phones    = set()
    extra_pages   = []
    all_soups     = []

    def _scrape(text):
        for e in EMAIL_RE.findall(text):
            e = e.lower().strip()
            if _is_valid_email(e):
                all_emails.add(e)
        for p in PHONE_RE.findall(text):
            cleaned = re.sub(r'[\s\-.()+]', '', p)
            if len(cleaned) >= 10:
                all_phones.add(cleaned)
        return BeautifulSoup(text, "html.parser")

    text = _fetch_text(site_url, site_deadline=site_deadline)
    if text:
        soup = _scrape(text)
        all_soups.append(soup)
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if h.startswith("mailto:"):
                all_emails.add(h.replace("mailto:", "").split("?")[0].lower().strip())
            elif h.startswith("tel:"):
                cleaned = re.sub(r'[\s\-.()+]', '', h.replace("tel:", ""))
                if len(cleaned) >= 10:
                    all_phones.add(cleaned)
            elif any(k in h.lower() for k in ["contact", "about", "team", "reach", "connect", "people", "staff"]):
                full = urljoin(site_url, h)
                if urlparse(full).netloc == urlparse(site_url).netloc and full != site_url:
                    extra_pages.append(full)

    for url in extra_pages[:2]:
        if time.time() > site_deadline:
            break
        text = _fetch_text(url, site_deadline=site_deadline)
        if text:
            soup = _scrape(text)
            all_soups.append(soup)

    # Try Hunter.io for verified people data (takes priority over heuristic scraping)
    domain = urlparse(site_url).netloc.replace("www.", "")
    hunter_people = _hunter_lookup(domain)

    # Merge: Hunter results first, then fill up to 5 with heuristic results
    if hunter_people:
        # Use Hunter emails as the source of truth
        all_emails.update(p["email"] for p in hunter_people if p.get("email"))
        final_people = hunter_people[:5]
    else:
        # Fall back to heuristic extraction from page HTML
        final_people = []
        for soup in all_soups:
            final_people = _extract_people(soup, all_emails, all_phones)
            if final_people:
                break

    # Classify emails into company vs personal
    company_emails = [e for e in all_emails
                      if _COMPANY_PREFIXES.match(e.split("@")[0])
                      and "noreply" not in e and "no-reply" not in e]
    people_emails  = [e for e in all_emails
                      if not _COMPANY_PREFIXES.match(e.split("@")[0])
                      and "noreply" not in e and "no-reply" not in e]

    company_email  = (company_emails[0] if company_emails
                      else people_emails[0] if people_emails
                      else list(all_emails)[0] if all_emails
                      else None)

    return {
        "company_email": company_email,
        "people_data":   final_people,
        "people_emails": [p["email"] for p in final_people if p.get("email")] or people_emails[:5],
        "people_phones": [p["phone"] for p in final_people if p.get("phone")] or list(all_phones)[:5],
    }


def find_emails(businesses, log=_noop):
    log("Looking for contact emails on each website...")
    with_emails      = []
    global_deadline  = time.time() + 240

    for i, biz in enumerate(businesses, 1):
        if time.time() > global_deadline:
            log(f"  ⏱ Time limit reached — skipping remaining {len(businesses)-i+1} site(s).")
            break

        if _should_skip(biz.get("website", "")):
            log(f"  [{i}/{len(businesses)}] {biz.get('name','?')[:35]} → — skipped (large chain)")
            continue

        info = extract_contact_info(biz["website"])
        em   = info.get("company_email")
        if em:
            biz["email"]         = em
            biz["people_emails"] = ",".join(info.get("people_emails", []))
            biz["people_phones"] = ",".join(info.get("people_phones", []))
            biz["people_data"]   = json.dumps(info.get("people_data", []), ensure_ascii=False)
            with_emails.append(biz)
        log(f"  [{i}/{len(businesses)}] {biz.get('name','?')[:35]} → {'✅ ' + em if em else '— no email'}")

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


# ══════════════════════════════════════════════════════
#  LEADS (CRM)
#  Every business the agent finds gets persisted here, not
#  just the ones it successfully emails — this is what powers
#  the CRM dashboard (search, grouping, score, status).
# ══════════════════════════════════════════════════════
def compute_match_score(biz, target_business_types=None):
    """
    Simple, explainable scoring from data we already have:
      +35  has a real website
      +35  has a usable email address
      +10  has a phone number
      +20  business type matches one the user is targeting
    Capped at 100.
    """
    score = 0
    if biz.get("website"):
        score += 35
    if biz.get("email"):
        score += 35
    if biz.get("phone"):
        score += 10
    btype = (biz.get("business_type") or "").lower()
    if target_business_types and btype:
        targets = [t.strip().lower() for t in target_business_types]
        if any(t in btype or btype in t for t in targets):
            score += 20
    return min(score, 100)


def upsert_leads(businesses, config, log=_noop, user_id=None):
    """
    Persist every found business as a lead (status='new'), so they
    survive past this run and show up in the CRM. Re-running the
    search updates the existing row (same user+email) rather than
    duplicating it.
    """
    sb = _get_supabase()
    if not (sb and user_id):
        return

    city = config.get("TARGET_CITY", "")
    targets = config.get("BUSINESS_TYPES", [])
    rows = []
    for biz in businesses:
        if not biz.get("email"):
            continue
        rows.append({
            "user_id":       user_id,
            "business_name": biz.get("name", ""),
            "address":       biz.get("address", ""),
            "website":       biz.get("website", ""),
            "email":         biz["email"].lower().strip(),
            "phone":         biz.get("phone", ""),
            "people_emails": biz.get("people_emails", ""),
            "people_phones": biz.get("people_phones", ""),
            "people_data":   biz.get("people_data", "[]"),
            "business_type": biz.get("business_type", ""),
            "city":          city,
            "match_score":   compute_match_score(biz, targets),
            "status":        "discovered",
        })

    if not rows:
        return

    inserted = 0
    updated  = 0
    errors   = 0

    for row in rows:
        try:
            # Try to update first (no-op if the row doesn't exist yet)
            res = sb.table("leads").update({
                "business_name": row["business_name"],
                "address":       row.get("address"),
                "website":       row.get("website"),
                "phone":         row.get("phone"),
                "people_emails": row.get("people_emails"),
                "people_phones": row.get("people_phones"),
                "people_data":   row.get("people_data"),
                "match_score":   row.get("match_score"),
                "business_type": row.get("business_type"),
                "city":          row.get("city"),
                # Never regress a lead that's already past 'discovered'
                # We can't do conditional updates easily, so we fetch status first
            }).eq("user_id", row["user_id"]).eq("email", row["email"]).execute()

            if res.data:
                # Row existed — check if we need to preserve its status
                existing_status = res.data[0].get("status", "discovered")
                if existing_status not in ("discovered", None):
                    # Re-apply the preserved status (update above clobbered it)
                    sb.table("leads").update({"status": existing_status}) \
                      .eq("user_id", row["user_id"]).eq("email", row["email"]).execute()
                updated += 1
            else:
                # Row didn't exist — insert it fresh
                sb.table("leads").insert(row).execute()
                inserted += 1
        except Exception as e:
            err_str = str(e)
            if "23505" in err_str or "duplicate key" in err_str.lower():
                # Race condition: another process inserted between our update check
                # and our insert — safe to ignore, the row is already there
                updated += 1
            else:
                errors += 1
                print(f"upsert_leads row error ({row.get('email')}): {e}", flush=True)

    msg = f"📇 {inserted} new + {updated} updated lead(s) saved to your CRM."
    if errors:
        msg += f" ({errors} skipped due to errors)"
    log(msg)


def mark_lead_contacted(email, user_id=None, subject="", body=""):
    sb = _get_supabase()
    if not (sb and user_id):
        return
    try:
        sb.table("leads").update({
            "status":       "contacted",
            "subject_sent": subject,
            "body_sent":    body,
            "sent_date":    str(datetime.now().date()),
        }).eq("user_id", user_id).eq("email", email.lower().strip()).execute()
    except Exception as e:
        print(f"mark_lead_contacted error: {e}", flush=True)


def mark_lead_replied(email, user_id=None, reply_subject=""):
    sb = _get_supabase()
    if not (sb and user_id):
        return
    try:
        sb.table("leads").update({
            "status":        "replied",
            "replied_at":    datetime.now().isoformat(),
            "reply_subject": reply_subject,
        }).eq("user_id", user_id).eq("email", email.lower().strip()).execute()
    except Exception as e:
        print(f"mark_lead_replied error: {e}", flush=True)


def get_leads(user_id, status=None, search=None):
    """Fetch leads for the CRM dashboard, with optional filters."""
    sb = _get_supabase()
    if not sb:
        return []
    try:
        q = sb.table("leads").select("*").eq("user_id", user_id)
        if status:
            q = q.eq("status", status)
        res = q.order("match_score", desc=True).execute()
        rows = res.data or []
        if search:
            s = search.lower()
            rows = [r for r in rows if s in (r.get("business_name") or "").lower()
                    or s in (r.get("email") or "").lower()
                    or s in (r.get("address") or "").lower()]
        return rows
    except Exception as e:
        print(f"get_leads error: {e}", flush=True)
        return []


def update_lead(lead_id, user_id, fields):
    """Update a single lead — used for status/group changes from the CRM UI."""
    sb = _get_supabase()
    if not sb:
        return False
    allowed = {"status"}
    update = {k: v for k, v in fields.items() if k in allowed}
    if not update:
        return False
    try:
        sb.table("leads").update(update).eq("id", lead_id).eq("user_id", user_id).execute()
        return True
    except Exception as e:
        print(f"update_lead error: {e}", flush=True)
        return False


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
            mark_lead_contacted(biz["email"], user_id=user_id, subject=subject, body=body)
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
    Check Gmail inbox for replies from contacted businesses.
    - Only searches emails from the last 30 days (not ALL inbox)
    - Hard 25-second wall-clock cap via thread so gunicorn never times out
    - IMAP socket timeout of 10s per operation
    """
    log("Checking your inbox for replies from contacted businesses...")
    replies = []

    contacted = load_sent(user_id=user_id)
    if not contacted:
        log("📭 No sent emails on record yet — run the agent first.")
        return replies

    log(f"   Checking for replies from {len(contacted)} contacted business(es)...")

    if not config.get("GMAIL_APP_PASSWORD"):
        log("⚠️  No Gmail App Password on file — add one in Setup to enable reply checking.")
        return replies

    def _check():
        import email.utils as eu
        from datetime import timedelta
        inner_replies = []
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.sock.settimeout(10)
            mail.login(config["GMAIL_ADDRESS"], config["GMAIL_APP_PASSWORD"].replace(" ", ""))
            mail.select("inbox")

            since_date = (datetime.now() - timedelta(days=30)).strftime("%d-%b-%Y")
            _, data = mail.search(None, f'(SINCE "{since_date}")')
            all_ids = data[0].split() if data[0] else []
            log(f"   Scanning {len(all_ids)} emails from the last 30 days...")

            if not all_ids:
                log("📭 No emails in the last 30 days.")
                mail.logout()
                return inner_replies

            # Fetch ALL From headers in one single IMAP call (not one per email)
            # Format: "1,2,3,4,..." — IMAP supports comma-separated ID sets
            id_set = b",".join(all_ids)
            _, header_data = mail.fetch(id_set, "(BODY[HEADER.FIELDS (FROM)])")

            # header_data is a flat list: [header_bytes, separator, header_bytes, ...]
            # Extract From values and check against contacted list
            matching_ids = []
            for j in range(0, len(header_data), 2):
                try:
                    raw = header_data[j]
                    if not isinstance(raw, tuple):
                        continue
                    # Extract the email ID from the response metadata
                    meta = raw[0].decode(errors="ignore")
                    eid_match = re.search(r'^(\d+)', meta)
                    if not eid_match:
                        continue
                    eid = eid_match.group(1).encode()
                    from_line = raw[1].decode(errors="ignore").lower()
                    if any(em in from_line for em in contacted):
                        matching_ids.append(eid)
                except Exception:
                    continue

            log(f"   Found {len(matching_ids)} potential reply(ies). Verifying...")

            found = 0
            for eid in matching_ids:
                try:
                    _, md_full = mail.fetch(eid, "(RFC822)")
                    msg = email.message_from_bytes(md_full[0][1])
                    sender_raw = msg.get("From", "")
                    _, sender_addr = eu.parseaddr(sender_raw)
                    sender_addr = sender_addr.lower().strip()
                    if sender_addr in contacted:
                        found += 1
                        inner_replies.append({
                            "from":    sender_raw,
                            "subject": msg.get("Subject", ""),
                            "date":    msg.get("Date", ""),
                        })
                        mark_lead_replied(sender_addr, user_id=user_id,
                                          reply_subject=msg.get("Subject", ""))
                        log(f"  🎉 REPLY → From: {sender_raw} | Subject: {msg.get('Subject','')}")
                except Exception:
                    continue

            if found == 0:
                log("📭 No replies from contacted businesses yet.")
            else:
                log(f"📬 {found} reply(ies) from businesses you contacted!")

            try:
                mail.logout()
            except Exception:
                pass

        except imaplib.IMAP4.error as e:
            log(f"❌ Gmail error: {e}")
            log("   Enable IMAP: Gmail ⚙️ → See all settings → Forwarding and POP/IMAP")
        except Exception as e:
            log(f"❌ Reply check error: {e}")

        return inner_replies

    replies = _check()
    return replies


# ══════════════════════════════════════════════════════
#  FULL PIPELINE
# ══════════════════════════════════════════════════════
def run_discovery(config, log=_noop, user_id=None):
    """
    STEP 1 of 2: find businesses, get their websites/phones/emails, save
    them all to the leads table as 'discovered'. Sends NO emails.
    The user picks who to email afterwards, in the Leads tab.
    """
    if not run_diagnostics(config, log=log):
        log("⛔ Setup incomplete. Fix the issues above in Setup, then run again.")
        return {"ok": False}

    biz   = search_businesses(config, log=log)
    sites = fetch_websites(biz, config, log=log)
    leads = find_emails(sites, log=log)

    upsert_leads(leads, config, log=log, user_id=user_id)

    log(f"✅ Discovery complete — {len(leads)} lead(s) ready for review in the Leads tab.")
    return {"ok": True, "found": len(biz), "with_sites": len(sites),
            "with_emails": len(leads)}


def send_to_selected_leads(lead_ids, config, log=_noop, user_id=None):
    """
    STEP 2 of 2: send AI-personalised emails only to the leads the user
    explicitly selected in the Leads tab (can be 0, some, or all of them).
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

    # Map DB rows back into the dict shape send_all/build_email expect.
    businesses = []
    for r in rows:
        businesses.append({
            "name":          r.get("business_name", ""),
            "address":       r.get("address", ""),
            "website":       r.get("website", ""),
            "email":         r.get("email", ""),
            "phone":         r.get("phone", ""),
            "business_type": r.get("business_type", ""),
        })

    log(f"Sending personalised emails to {len(businesses)} selected lead(s)...")
    sent = send_all(businesses, config, log=log, user_id=user_id)

    log(f"✅ Send complete! Go to the Leads tab to see updated statuses.")
    return {"ok": True, "sent": sent, "selected": len(lead_ids)}


def run_full_pipeline(config, log=_noop, user_id=None):
    """
    Legacy one-shot pipeline (discover + send everyone immediately).
    Kept for backward compatibility — the dashboard now uses
    run_discovery() followed by send_to_selected_leads() instead,
    so the user can review and choose who gets emailed.
    """
    if not run_diagnostics(config, log=log):
        log("⛔ Setup incomplete. Fix the issues above in Setup, then run again.")
        return {"ok": False}

    biz   = search_businesses(config, log=log)
    sites = fetch_websites(biz, config, log=log)
    leads = find_emails(sites, log=log)

    upsert_leads(leads, config, log=log, user_id=user_id)

    sent  = send_all(leads, config, log=log, user_id=user_id)

    if config.get("GMAIL_APP_PASSWORD"):
        check_replies(config, log=log, user_id=user_id)
    else:
        log("📭 Reply checking skipped — add a Gmail App Password in Setup to track replies automatically.")

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
