"""
app.py, AI Client Agent (multi-user SaaS edition)
Flask + Supabase Auth. Deployed on Render.

Routes:
  /                    landing page (or redirect to dashboard if logged in)
  /dashboard           the app itself, login required
  /api/auth/signup     create account (Supabase Auth)
  /api/auth/login      log in
  /api/auth/logout     log out
  /api/profile         GET/POST, read or save the user's profile
  /api/status          profile completeness + trial/license status
  /api/run             discover businesses + save as leads (no sending)
  /api/send-selected   send AI-personalised emails to chosen lead IDs
  /api/check-replies   check this user's Gmail inbox for replies
  /api/stream          Server-Sent Events log stream for the current run
  /api/sent_emails     GET, list of emails actually sent
  /api/leads           GET, CRM lead list, filterable by status/group/search
  /api/leads/<id>      PATCH, update a lead's status or group tag
  /api/leads/groups    GET, distinct group tags for the filter dropdown
"""

import os
import queue
import threading
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, render_template, request, jsonify, Response, session, redirect
from supabase import create_client, Client

import agent_core
import whatsapp_agent
import billing_agent
from paths import get_resource_dir

# ======================================================
#  SETUP
# ======================================================
_resource_dir = get_resource_dir()
app = Flask(
    __name__,
    template_folder=os.path.join(_resource_dir, "templates"),
    static_folder=os.path.join(_resource_dir, "static"),
)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# Your Google Maps API key, set this as an environment variable on
# Render (Settings → Environment), never hardcode it here.
OWNER_GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

TRIAL_DAYS = 5

# ======================================================
#  MAINTENANCE MODE
#  Toggle by setting MAINTENANCE_MODE=true as an env var on Render
#  (Settings → Environment), no code changes/redeploys needed to
#  flip it on or off, Render just restarts the app with the new value.
#  Add ?bypass=<MAINTENANCE_BYPASS_KEY> to any URL to keep working on
#  the site yourself while it's showing to everyone else.
# ======================================================
MAINTENANCE_MODE = os.environ.get("MAINTENANCE_MODE", "false").lower() == "true"
MAINTENANCE_BYPASS_KEY = os.environ.get("MAINTENANCE_BYPASS_KEY", "")


@app.before_request
def _check_maintenance_mode():
    if not MAINTENANCE_MODE:
        return None
    if request.path.startswith("/static/"):
        return None
    if request.path == "/api/webhooks/razorpay":
        return None  # Razorpay must always be able to reach this, maintenance or not
    if session.get("maintenance_bypass") is True:
        return None
    if MAINTENANCE_BYPASS_KEY and request.args.get("bypass") == MAINTENANCE_BYPASS_KEY:
        session["maintenance_bypass"] = True
        return None
    return render_template("maintenance.html"), 503


# In-memory per-user run state (log queue, running flag).
# Fine for a single Render instance; if you ever scale to multiple
# instances behind a load balancer, this would need to move to Redis.
_user_states = {}


def get_user_state(uid):
    if uid not in _user_states:
        _user_states[uid] = {"running": False, "log_queue": queue.Queue(), "last_result": None, "log_buffer": []}
    return _user_states[uid]


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "message": "Not logged in"}), 401
            return redirect("/")
        return f(*args, **kwargs)
    return decorated


def _days_left_from(trial_start_str):
    """Given an ISO timestamp string, return how many trial days remain."""
    if not trial_start_str:
        return TRIAL_DAYS
    try:
        start = datetime.fromisoformat(trial_start_str.replace("Z", "+00:00"))
        now = datetime.now(start.tzinfo or timezone.utc)
        days_used = (now - start).days
        return max(TRIAL_DAYS - days_used, 0)
    except Exception:
        return TRIAL_DAYS


def _get_profile(uid):
    res = supabase.table("profiles").select("*").eq("id", uid).single().execute()
    return res.data or {}


# ======================================================
#  PAGES
# ======================================================
@app.route("/")
def index():
    if "user_id" in session:
        return redirect("/dashboard")
    return render_template("saas.html")


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/privacy")
def privacy_page():
    return render_template("privacy.html")


@app.route("/terms")
def terms_page():
    return render_template("terms.html")


@app.route("/refund-policy")
def refund_policy_page():
    return render_template("refund-policy.html")


@app.route("/contact")
def contact_page():
    return render_template("contact.html")


# ======================================================
#  AUTH
# ======================================================
@app.route("/api/auth/signup", methods=["POST"])
def api_signup():
    if supabase is None:
        return jsonify({"ok": False, "message": "Server not configured. Contact support."})

    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    full_name = data.get("full_name", "").strip()

    if not email or not password:
        return jsonify({"ok": False, "message": "Email and password are required."})
    if len(password) < 6:
        return jsonify({"ok": False, "message": "Password must be at least 6 characters."})

    try:
        res = supabase.auth.sign_up({"email": email, "password": password})
    except Exception as e:
        msg = str(e)
        if "already registered" in msg.lower() or "already exists" in msg.lower():
            return jsonify({"ok": False, "message": "This email is already registered. Please sign in instead."})
        return jsonify({"ok": False, "message": msg})

    user = res.user

    # Supabase returns a user object with no error even for an email
    # that's already registered (to avoid leaking which emails exist),
    # but in that case res.session is None AND the user's identities
    # list is empty. That combination is our signal it's a duplicate.
    if user and not res.session:
        identities = getattr(user, "identities", None)
        if identities is not None and len(identities) == 0:
            return jsonify({"ok": False, "message": "This email is already registered. Please sign in instead."})
        # Insert profile even before confirmation so it exists when they log in
        try:
            existing = supabase.table("profiles").select("id").eq("id", user.id).execute()
            if not existing.data:
                supabase.table("profiles").insert({
                    "id": user.id, "email": email, "full_name": full_name, "gmail": email,
                }).execute()
        except Exception:
            pass
        return jsonify({"ok": True, "confirm": True,
                         "message": "Check your email and click the confirmation link to activate your account."})

    if not user:
        return jsonify({"ok": False, "message": "Signup failed. Please try again."})

    # Email confirmations disabled in Supabase settings → session exists immediately.
    if res.session:
        session["user_id"] = user.id
        session["user_email"] = email
        try:
            existing = supabase.table("profiles").select("id").eq("id", user.id).execute()
            if not existing.data:
                supabase.table("profiles").insert({
                    "id": user.id, "email": email, "full_name": full_name, "gmail": email,
                }).execute()
            elif full_name:
                supabase.table("profiles").update({"full_name": full_name}).eq("id", user.id).execute()
        except Exception:
            pass
        return jsonify({"ok": True, "redirect": "/dashboard"})

    return jsonify({"ok": True, "confirm": True,
                     "message": "Check your email and click the confirmation link to activate your account."})


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    if supabase is None:
        return jsonify({"ok": False, "message": "Server not configured. Contact support."})

    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()

    try:
        res = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user = res.user
        if not user:
            return jsonify({"ok": False, "message": "Invalid email or password."})

        session["user_id"] = user.id
        session["user_email"] = email

        try:
            existing = supabase.table("profiles").select("id").eq("id", user.id).execute()
            if not existing.data:
                supabase.table("profiles").insert({"id": user.id, "email": email, "gmail": email}).execute()
        except Exception:
            pass

        return jsonify({"ok": True, "redirect": "/dashboard"})
    except Exception:
        return jsonify({"ok": False, "message": "Invalid email or password."})


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True, "redirect": "/"})


# ======================================================
#  PROFILE
# ======================================================
@app.route("/api/profile", methods=["GET"])
@login_required
def api_get_profile():
    uid = session["user_id"]
    try:
        profile = _get_profile(uid)
        days_left = _days_left_from(profile.get("trial_start"))
        profile["days_left"] = days_left
        profile["trial_active"] = days_left > 0
        profile["is_paid"] = bool(profile.get("is_paid", False))
        profile["has_gmail_app_password"] = bool(profile.get("gmail_app_password"))
        profile.pop("gmail_app_password", None)  # never send this back to the browser
        profile["has_whatsapp"] = bool(profile.get("whatsapp_access_token") and profile.get("whatsapp_phone_number_id"))
        profile.pop("whatsapp_access_token", None)  # never send this back to the browser
        return jsonify({"ok": True, "profile": profile})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/profile", methods=["POST"])
@login_required
def api_save_profile():
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}

    allowed = [
        "full_name", "business_name", "gmail",
        "your_service", "your_about", "target_city", "business_types",
        "gmail_app_password",
    ]
    update = {k: v for k, v in data.items() if k in allowed}

    if not update:
        return jsonify({"ok": False, "message": "Nothing to save."})

    try:
        supabase.table("profiles").update(update).eq("id", uid).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/whatsapp/connect", methods=["POST"])
@login_required
def api_whatsapp_connect():
    """
    Kicks off WhatsApp connection for this user.

    ⚠️ PLACEHOLDER: real Meta Embedded Signup isn't wired up yet, that
    requires registering this app as a Meta Tech Provider, completing
    App Review, and hosting the Facebook JS SDK popup on the frontend.
    Once META_APP_ID / META_CONFIG_ID env vars are set, replace this
    with the real embedded-signup launch (return a signup URL/config
    for the frontend's FB.login() call) and a matching callback route
    to store the returned WABA ID / phone number ID / access token.
    """
    meta_app_id = os.environ.get("META_APP_ID", "")
    if not meta_app_id:
        return jsonify({
            "ok": False,
            "message": "WhatsApp connection isn't set up yet on our end, check back soon!"
        })
    # Real flow would return signup config here, e.g.:
    # return jsonify({"ok": True, "app_id": meta_app_id, "config_id": os.environ.get("META_CONFIG_ID", "")})
    return jsonify({"ok": False, "message": "WhatsApp connection is being finalized."})


# ======================================================
#  STATUS
# ======================================================
@app.route("/api/status")
@login_required
def api_status():
    uid = session["user_id"]
    state = get_user_state(uid)
    try:
        profile = _get_profile(uid)
        days_left = _days_left_from(profile.get("trial_start"))

        required = ["full_name", "gmail", "your_service", "your_about", "target_city"]
        profile_complete = all(str(profile.get(k, "")).strip() for k in required)

        return jsonify({
            "ok": True,
            "profile_complete": profile_complete,
            "days_left": days_left,
            "is_paid": bool(profile.get("is_paid", False)),
            "running": state["running"],
            "total_sent": agent_core.get_stats(user_id=uid)["total_sent"],
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


# ======================================================
#  RUN THE AGENT
# ======================================================
def _build_config(profile):
    business_types = profile.get("business_types") or (
        "small business,shop,store,restaurant,hotel,clinic,school,agency,company,office,pvt ltd,enterprise,industries"
    )
    if isinstance(business_types, str):
        business_types = [b.strip() for b in business_types.split(",") if b.strip()]

    return {
        "YOUR_EMAIL": profile.get("gmail", ""),
        "YOUR_NAME": profile.get("full_name", ""),
        "YOUR_SERVICE": profile.get("your_service", ""),
        "YOUR_ABOUT": profile.get("your_about", ""),
        "TARGET_CITY": profile.get("target_city", ""),
        "BUSINESS_TYPES": business_types,
        "GMAIL_ADDRESS": profile.get("gmail", ""),
        "GMAIL_APP_PASSWORD": profile.get("gmail_app_password", "") or "",
        "GOOGLE_MAPS_API_KEY": OWNER_GOOGLE_MAPS_API_KEY,
        "MAX_RESULTS_PER_QUERY": 5,
        "DELAY_BETWEEN_EMAILS": 3,
        "ATTACHMENT_PATH": "",
        "ATTACHMENT_NAME": "",
        # WhatsApp Cloud API, blank until the user connects (Embedded
        # Signup flow not built yet). See whatsapp_agent.py.
        "WHATSAPP_ACCESS_TOKEN": profile.get("whatsapp_access_token", "") or "",
        "WHATSAPP_PHONE_NUMBER_ID": profile.get("whatsapp_phone_number_id", "") or "",
        "WHATSAPP_BUSINESS_ACCOUNT_ID": profile.get("whatsapp_business_account_id", "") or "",
        "WHATSAPP_TEMPLATE_NAME": profile.get("whatsapp_template_name", "") or "business_outreach_intro",
        "WHATSAPP_TEMPLATE_LANG": profile.get("whatsapp_template_lang", "") or "en_US",
    }


def _check_trial_or_paid(profile):
    """Returns (allowed: bool, message: str)."""
    if profile.get("is_paid"):
        return True, ""
    days_left = _days_left_from(profile.get("trial_start"))
    if days_left > 0:
        return True, ""
    return False, "Your free trial has ended. Please upgrade to continue."


@app.route("/api/run", methods=["POST"])
@login_required
def api_run():
    """
    Discovery only: finds businesses, gets websites/phones/emails, and
    saves them to the leads table as 'discovered'. Sends NO emails :
    the user picks who to email from the Leads tab afterwards.
    """
    uid = session["user_id"]
    state = get_user_state(uid)

    if state["running"]:
        return jsonify({"ok": False, "message": "Agent is already running."})

    profile = _get_profile(uid)
    allowed, msg = _check_trial_or_paid(profile)
    if not allowed:
        return jsonify({"ok": False, "message": msg})

    required = ["full_name", "gmail", "your_service", "your_about", "target_city"]
    if not all(str(profile.get(k, "")).strip() for k in required):
        return jsonify({"ok": False, "message": "Please complete your profile first."})

    cfg = _build_config(profile)

    def _run():
        def log(message):
            msg = str(message)
            state["log_buffer"].append(msg)
            state["log_queue"].put(msg)
        try:
            result = agent_core.run_discovery(cfg, log=log, user_id=uid)
            state["last_result"] = result
        except Exception as e:
            log(f"❌ Unexpected error: {e}")
        finally:
            log("__DONE__")
            state["running"] = False

    state["running"] = True
    state["log_queue"] = queue.Queue()
    state["log_buffer"] = []
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/send-selected", methods=["POST"])
@login_required
def api_send_selected():
    """
    Sends AI-personalised emails only to the lead IDs the user checked
    in the Leads tab. lead_ids can be an empty list (sends nothing),
    a subset, or every discovered lead.
    """
    uid = session["user_id"]
    state = get_user_state(uid)

    if state["running"]:
        return jsonify({"ok": False, "message": "Agent is already running."})

    profile = _get_profile(uid)
    allowed, msg = _check_trial_or_paid(profile)
    if not allowed:
        return jsonify({"ok": False, "message": msg})

    data = request.get_json(silent=True) or {}
    lead_ids = data.get("lead_ids") or []
    if not isinstance(lead_ids, list):
        return jsonify({"ok": False, "message": "lead_ids must be a list."})

    cfg = _build_config(profile)

    def _run():
        def log(message):
            msg = str(message)
            state["log_buffer"].append(msg)
            state["log_queue"].put(msg)
        try:
            result = agent_core.send_to_selected_leads(lead_ids, cfg, log=log, user_id=uid)
            state["last_result"] = result
        except Exception as e:
            log(f"❌ Unexpected error: {e}")
        finally:
            log("__DONE__")
            state["running"] = False

    state["running"] = True
    state["log_queue"] = queue.Queue()
    state["log_buffer"] = []
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/send-whatsapp-selected", methods=["POST"])
@login_required
def api_send_whatsapp_selected():
    """
    Sends WhatsApp messages only to the lead IDs the user checked in the
    Leads tab (uses each lead's stored phone number). Mirrors
    /api/send-selected but for the WhatsApp channel.

    Body: { "lead_ids": [...], "use_template": true|false }
    use_template defaults to true, WhatsApp requires an approved
    template for the first message to a new contact (cold outreach).
    Set to false only for a reply within an existing 24h conversation.
    """
    uid = session["user_id"]
    state = get_user_state(uid)

    if state["running"]:
        return jsonify({"ok": False, "message": "Agent is already running."})

    profile = _get_profile(uid)
    allowed, msg = _check_trial_or_paid(profile)
    if not allowed:
        return jsonify({"ok": False, "message": msg})

    data = request.get_json(silent=True) or {}
    lead_ids = data.get("lead_ids") or []
    use_template = data.get("use_template", True)
    if not isinstance(lead_ids, list):
        return jsonify({"ok": False, "message": "lead_ids must be a list."})

    cfg = _build_config(profile)

    if not whatsapp_agent.whatsapp_configured(cfg):
        return jsonify({
            "ok": False,
            "message": "WhatsApp isn't connected yet. Connect a WhatsApp Business "
                       "number in Setup first."
        })

    def _run():
        def log(message):
            msg = str(message)
            state["log_buffer"].append(msg)
            state["log_queue"].put(msg)
        try:
            result = whatsapp_agent.send_whatsapp_to_selected_leads(
                lead_ids, cfg, log=log, user_id=uid, use_template=use_template
            )
            state["last_result"] = result
        except Exception as e:
            log(f"❌ Unexpected error: {e}")
        finally:
            log("__DONE__")
            state["running"] = False

    state["running"] = True
    state["log_queue"] = queue.Queue()
    state["log_buffer"] = []
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/check-replies", methods=["POST"])
@login_required
def api_check_replies():
    uid = session["user_id"]
    state = get_user_state(uid)

    if state["running"]:
        return jsonify({"ok": False, "message": "Agent is already running."})

    profile = _get_profile(uid)
    allowed, msg = _check_trial_or_paid(profile)
    if not allowed:
        return jsonify({"ok": False, "message": msg})

    if not profile.get("gmail"):
        return jsonify({"ok": False, "message": "Please complete your profile first."})

    cfg = _build_config(profile)

    def _run():
        def log(message):
            msg = str(message)
            state["log_buffer"].append(msg)
            state["log_queue"].put(msg)
        try:
            agent_core.check_replies(cfg, log=log, user_id=uid)
        except Exception as e:
            log(f"❌ Unexpected error: {e}")
        finally:
            log("__DONE__")
            state["running"] = False

    state["running"] = True
    state["log_queue"] = queue.Queue()
    state["log_buffer"] = []
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


# ======================================================
#  LOG STREAM (Server-Sent Events)
# ======================================================

@app.route("/api/sent_emails", methods=["GET"])
@login_required
def api_sent_emails():
    uid = session["user_id"]
    if not SUPABASE_URL or not SUPABASE_KEY:
        return jsonify({"ok": False, "message": "Supabase not configured"})
    try:
        from supabase import create_client
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        res = sb.table("sent_log").select("*").eq("user_id", uid).order("created_at", desc=True).execute()
        return jsonify({"ok": True, "emails": res.data})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


# ======================================================
#  LEADS (CRM)
# ======================================================
@app.route("/api/leads", methods=["GET"])
@login_required
def api_get_leads():
    uid    = session["user_id"]
    status = request.args.get("status") or None
    search = request.args.get("q") or None
    try:
        leads = agent_core.get_leads(uid, status=status, search=search)
        return jsonify({"ok": True, "leads": leads})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/leads/<lead_id>", methods=["PATCH"])
@login_required
def api_update_lead(lead_id):
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    try:
        ok = agent_core.update_lead(lead_id, uid, data)
        if ok:
            return jsonify({"ok": True})
        return jsonify({"ok": False, "message": "No valid fields to update."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})






@app.route("/api/log-buffer")
@login_required
def api_log_buffer():
    """Returns all log lines buffered so far for the current run.
    Used by the frontend when it reconnects after an SSE drop."""
    uid = session["user_id"]
    state = get_user_state(uid)
    return jsonify({"ok": True, "log": state.get("log_buffer", []),
                    "running": state.get("running", False)})


@app.route("/api/reset", methods=["POST"])
@login_required
def api_reset():
    """Force-clears the running state if the agent got stuck."""
    uid = session["user_id"]
    state = get_user_state(uid)
    state["running"] = False
    state["log_queue"] = queue.Queue()
    state["log_buffer"] = []
    return jsonify({"ok": True})


@app.route("/api/poll")
@login_required
def api_poll():
    """
    The browser calls this every second while a run is active.
    Returns all log lines queued since the last poll, plus running/done status.
    Never blocks, drains the queue instantly and returns.
    """
    uid = session["user_id"]
    state = get_user_state(uid)
    q = state["log_queue"]

    lines = []
    done  = False
    while True:
        try:
            line = q.get_nowait()
            if line == "__DONE__":
                done = True
                break
            lines.append(line)
        except queue.Empty:
            break

    return jsonify({
        "ok":      True,
        "lines":   lines,
        "running": state.get("running", False),
        "done":    done,
    })


# ======================================================
#  BILLING (Razorpay), "Billing Agent"
# ======================================================
@app.route("/api/billing/create-subscription", methods=["POST"])
@login_required
def api_billing_create_subscription():
    """
    Starts an automated ₹999/month subscription for the logged-in user.
    Returns a subscription_id + key_id for the frontend to open Razorpay
    Checkout. Nothing is activated yet, that happens once the webhook
    confirms an actual successful charge.
    """
    uid = session["user_id"]
    profile = _get_profile(uid)

    email = profile.get("gmail", "") or ""
    full_name = profile.get("full_name", "") or ""

    if not email:
        return jsonify({"ok": False, "message": "Please add your email in Setup before upgrading."})

    result = billing_agent.create_subscription(uid, email, full_name)
    return jsonify(result)


@app.route("/api/billing/history", methods=["GET"])
@login_required
def api_billing_history():
    uid = session["user_id"]
    history = billing_agent.get_payment_history(uid, supabase)
    return jsonify({"ok": True, "payments": history})


@app.route("/api/webhooks/razorpay", methods=["POST"])
def api_webhook_razorpay():
    """
    Razorpay calls this automatically after payment events, no browser/
    session involved, this is server-to-server. We verify the signature
    before trusting anything in the payload.
    """
    signature = request.headers.get("X-Razorpay-Signature", "")
    raw_body = request.get_data()

    if not billing_agent.verify_webhook_signature(raw_body, signature):
        return jsonify({"ok": False, "message": "Invalid signature."}), 400

    event = request.get_json(silent=True) or {}
    result = billing_agent.handle_webhook_event(event, supabase)
    # Always return 200 once signature is verified and we've processed it,
    # even if result reports an internal issue, so Razorpay doesn't retry
    # a webhook we've already handled/logged.
    return jsonify(result), 200


# ======================================================
#  MAIN (local dev only, Render uses gunicorn, see Procfile)
# ======================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
