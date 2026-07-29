"""
app.py, Vajra Labs (clinic conversation product)
Flask + Supabase Auth. Deployed on Render.

Routes:
  /                              landing page (or redirect to dashboard if logged in)
  /dashboard                     the app itself, login required
  /api/auth/signup               create account (Supabase Auth)
  /api/auth/login                log in
  /api/auth/logout               log out
  /api/profile                   GET/POST, read or save the user's profile
  /api/whatsapp/connect          placeholder for future WhatsApp channel (Phase 4)
  /api/billing/*                 Razorpay subscription, cancellation, history
  /api/webhooks/razorpay         Razorpay payment webhook
  /api/clinic                    GET/POST, the clinic's knowledge base (Qualify Agent's context)
  /api/conversations/*           dashboard-side conversation list, messages, human takeover
  /chat/<clinic_id>              hosted booking page (no-website fallback)
  /api/widget/*                  public chat widget API (embedded on a clinic's own site)
  /api/appointments/*            booking request confirmation

Old outbound lead-gen/cold-outreach tools (Discovery Agent, cold Email/
WhatsApp send, the old Leads/CRM) were removed as part of the pivot to
an inbound conversation product. See vajra_labs_pivot_plan.md.
"""

import os
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, render_template, request, jsonify, Response, session, redirect
from supabase import create_client, Client

import billing_agent
import conversation_agent
import calendar_agent
import followup_agent
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

# ======================================================
#  FOLLOW-UP AGENT SCHEDULER
#  Runs hourly in the background, checks for conversations that went
#  quiet before booking, sends the next follow-up in the sequence
#  (24h / 2d / 4d / 1wk). Only starts if Supabase is actually
#  configured, so local dev without env vars doesn't error out.
#
#  This in-process scheduler only works on a host that stays running
#  all the time (e.g. Render). On Cloud Run, instances can scale to
#  zero between requests, which would silently stop this thread. Set
#  ENABLE_INPROCESS_SCHEDULER=0 there and use Cloud Scheduler to hit
#  /internal/followup-check on a timer instead (see below).
# ======================================================
CRON_SECRET = os.environ.get("CRON_SECRET", "")


def _run_followup_job():
    try:
        result = followup_agent.run_followup_check(sb=supabase, log=print)
        print(f"Follow-up Agent: {result}")
        return result
    except Exception as e:
        print(f"Follow-up Agent scheduler error: {e}")
        return {"ok": False, "error": str(e)}


if supabase is not None and os.environ.get("ENABLE_INPROCESS_SCHEDULER", "1") == "1":
    try:
        from apscheduler.schedulers.background import BackgroundScheduler

        _scheduler = BackgroundScheduler(daemon=True)
        _scheduler.add_job(_run_followup_job, "interval", hours=1, id="followup_check")
        _scheduler.start()
    except Exception as e:
        print(f"Could not start Follow-up Agent scheduler: {e}")


@app.route("/internal/followup-check", methods=["POST"])
def internal_followup_check():
    """
    HTTP trigger for the Follow-up Agent, meant to be called by Google
    Cloud Scheduler (hourly) when ENABLE_INPROCESS_SCHEDULER=0. Protected
    by a shared secret so it can't be triggered by randoms hitting the URL.
    Set CRON_SECRET as an env var and configure Cloud Scheduler to send
    it as the "X-Cron-Secret" header.
    """
    if not CRON_SECRET or request.headers.get("X-Cron-Secret", "") != CRON_SECRET:
        return jsonify({"ok": False, "message": "Unauthorized."}), 401
    if supabase is None:
        return jsonify({"ok": False, "message": "Supabase not configured."}), 500
    result = _run_followup_job()
    return jsonify(result)

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
    if request.path.startswith("/api/widget/"):
        return None  # a clinic's own website visitors shouldn't be blocked by our maintenance mode
    if session.get("maintenance_bypass") is True:
        return None
    if MAINTENANCE_BYPASS_KEY and request.args.get("bypass") == MAINTENANCE_BYPASS_KEY:
        session["maintenance_bypass"] = True
        return None
    return render_template("maintenance.html"), 503


@app.after_request
def _add_widget_cors_headers(response):
    if request.path.startswith("/api/widget/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


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
        "full_name", "gmail",
        "whatsapp_access_token", "whatsapp_phone_number_id", "whatsapp_business_account_id",
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


@app.route("/api/billing/cancel-subscription", methods=["POST"])
@login_required
def api_billing_cancel_subscription():
    uid = session["user_id"]
    profile = _get_profile(uid)
    subscription_id = profile.get("razorpay_subscription_id", "")

    result = billing_agent.cancel_subscription(uid, subscription_id, supabase)
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
#  CLINIC PROFILE (Qualify Agent's knowledge base)
# ======================================================
@app.route("/api/clinic", methods=["GET"])
@login_required
def api_get_clinic():
    uid = session["user_id"]
    clinic = conversation_agent.get_clinic(uid, sb=supabase)
    return jsonify({"ok": True, "clinic": clinic})


@app.route("/api/clinic", methods=["POST"])
@login_required
def api_save_clinic():
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    allowed = ["clinic_name", "clinic_type", "hours", "location", "services", "pricing_notes", "faqs"]
    fields = {k: data.get(k, "") for k in allowed if k in data}

    if "qualification_questions" in data:
        qs = data.get("qualification_questions")
        if isinstance(qs, list):
            fields["qualification_questions"] = [str(q).strip() for q in qs if str(q).strip()]

    result = conversation_agent.save_clinic(uid, fields, sb=supabase)
    return jsonify(result)


# ======================================================
#  CONVERSATIONS (dashboard side, "who's chatting right now")
# ======================================================
@app.route("/api/conversations", methods=["GET"])
@login_required
def api_list_conversations():
    uid = session["user_id"]
    try:
        res = supabase.table("conversations").select("*").eq("user_id", uid).order("updated_at", desc=True).execute()
        return jsonify({"ok": True, "conversations": res.data or []})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/conversations/<conversation_id>/messages", methods=["GET"])
@login_required
def api_conversation_messages(conversation_id):
    uid = session["user_id"]
    try:
        conv = supabase.table("conversations").select("id").eq("id", conversation_id).eq("user_id", uid).single().execute()
        if not conv.data:
            return jsonify({"ok": False, "message": "Not found."})
        res = supabase.table("messages").select("*").eq("conversation_id", conversation_id).order("created_at").execute()
        return jsonify({"ok": True, "messages": res.data or []})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/conversations/<conversation_id>/takeover", methods=["POST"])
@login_required
def api_conversation_takeover(conversation_id):
    uid = session["user_id"]
    return jsonify(conversation_agent.staff_takeover(conversation_id, uid, sb=supabase))


@app.route("/api/conversations/<conversation_id>/reply", methods=["POST"])
@login_required
def api_conversation_staff_reply(conversation_id):
    uid = session["user_id"]
    message = (request.get_json(silent=True) or {}).get("message", "").strip()
    if not message:
        return jsonify({"ok": False, "message": "Message can't be empty."})
    return jsonify(conversation_agent.staff_reply(conversation_id, uid, message, sb=supabase))


@app.route("/api/conversations/<conversation_id>/resume", methods=["POST"])
@login_required
def api_conversation_resume(conversation_id):
    uid = session["user_id"]
    return jsonify(conversation_agent.resume_bot(conversation_id, uid, sb=supabase))


# ======================================================
#  WIDGET (public, embedded on a clinic's own website, no login)
# ======================================================
@app.route("/chat/<clinic_id>")
def hosted_chat_page(clinic_id):
    try:
        clinic_res = supabase.table("clinics").select("clinic_name").eq("id", clinic_id).single().execute()
        clinic = clinic_res.data
    except Exception:
        clinic = None
    if not clinic:
        return render_template("hosted_chat.html", clinic_id=clinic_id, clinic_name="this clinic", not_found=True)
    return render_template("hosted_chat.html", clinic_id=clinic_id, clinic_name=clinic.get("clinic_name") or "this clinic", not_found=False)


@app.route("/api/conversations/<conversation_id>/appointment", methods=["GET"])
@login_required
def api_conversation_appointment(conversation_id):
    uid = session["user_id"]
    try:
        res = supabase.table("appointments").select("*").eq("conversation_id", conversation_id).eq("user_id", uid).execute()
        return jsonify({"ok": True, "appointment": (res.data or [None])[0]})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/appointments/<appointment_id>/confirm", methods=["POST"])
@login_required
def api_confirm_appointment(appointment_id):
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    confirmed_time = data.get("confirmed_time", "")  # ISO datetime, e.g. "2026-08-01T15:30"

    try:
        appt_res = supabase.table("appointments").select("*").eq("id", appointment_id).eq("user_id", uid).single().execute()
        appt = appt_res.data
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})

    if not appt:
        return jsonify({"ok": False, "message": "Appointment not found."})

    update = {"status": "confirmed"}
    calendar_result = None
    if confirmed_time:
        update["confirmed_time"] = confirmed_time
        try:
            clinic_res = supabase.table("clinics").select("*").eq("id", appt["clinic_id"]).single().execute()
            clinic = clinic_res.data or {}
            if clinic.get("google_calendar_connected"):
                calendar_result = calendar_agent.create_calendar_event(
                    clinic, appt.get("visitor_name", ""), appt.get("visitor_contact", ""),
                    appt.get("requested_text", ""), confirmed_time, sb=supabase,
                )
        except Exception:
            pass

    try:
        supabase.table("appointments").update(update).eq("id", appointment_id).eq("user_id", uid).execute()
        result = {"ok": True}
        if calendar_result is not None:
            result["calendar"] = calendar_result
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


@app.route("/api/calendar/connect")
@login_required
def api_calendar_connect():
    if not calendar_agent.calendar_configured():
        return jsonify({"ok": False, "message": "Google Calendar isn't configured yet."})
    uid = session["user_id"]
    return redirect(calendar_agent.get_authorization_url(uid))


@app.route("/api/calendar/oauth/callback")
def api_calendar_oauth_callback():
    code = request.args.get("code", "")
    user_id = request.args.get("state", "")
    if not code or not user_id:
        return redirect("/dashboard?calendar=error")
    result = calendar_agent.handle_oauth_callback(code, user_id, sb=supabase)
    return redirect("/dashboard?calendar=connected" if result.get("ok") else "/dashboard?calendar=error")


@app.route("/api/calendar/disconnect", methods=["POST"])
@login_required
def api_calendar_disconnect():
    uid = session["user_id"]
    return jsonify(calendar_agent.disconnect_calendar(uid, sb=supabase))


@app.route("/api/widget/start", methods=["POST"])
def api_widget_start():
    data = request.get_json(silent=True) or {}
    clinic_id = data.get("clinic_id", "")
    if not clinic_id:
        return jsonify({"ok": False, "message": "Missing clinic_id."})
    try:
        clinic_res = supabase.table("clinics").select("*").eq("id", clinic_id).single().execute()
        clinic = clinic_res.data
    except Exception:
        clinic = None
    if not clinic:
        return jsonify({"ok": False, "message": "Chat is not available."})

    result = conversation_agent.start_conversation(clinic_id, clinic["user_id"], sb=supabase)
    if not result.get("ok"):
        return jsonify(result)

    greeting = f"Hi! Welcome to {clinic.get('clinic_name') or 'our clinic'}. How can we help you today?"
    return jsonify({
        "ok": True,
        "conversation_id": result["conversation_id"],
        "clinic_name": clinic.get("clinic_name", ""),
        "greeting": greeting,
    })


@app.route("/api/widget/consent", methods=["POST"])
def api_widget_consent():
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id", "")
    if not conversation_id:
        return jsonify({"ok": False, "message": "Missing conversation_id."})
    return jsonify(conversation_agent.give_consent(conversation_id, sb=supabase))


@app.route("/api/widget/message", methods=["POST"])
def api_widget_message():
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id", "")
    message = (data.get("message") or "").strip()
    if not conversation_id or not message:
        return jsonify({"ok": False, "reply": "Something went wrong, please refresh and try again."})
    result = conversation_agent.handle_message(conversation_id, message, sb=supabase)
    return jsonify(result)


# ======================================================
#  MAIN (local dev only, Render uses gunicorn, see Procfile)
# ======================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
