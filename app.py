"""
app.py — AI Client Agent (multi-user SaaS edition)
Flask + Supabase Auth. Deployed on Render.

Routes:
  /                    landing page (or redirect to dashboard if logged in)
  /dashboard           the app itself, login required
  /api/auth/signup     create account (Supabase Auth)
  /api/auth/login      log in
  /api/auth/logout     log out
  /api/profile         GET/POST — read or save the user's profile
  /api/status          profile completeness + trial/license status
  /api/run             kick off the outreach pipeline for this user
  /api/check-replies   check this user's Gmail inbox for replies
  /api/stream          Server-Sent Events log stream for the current run
"""

import os
import queue
import threading
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, render_template, request, jsonify, Response, session, redirect
from supabase import create_client, Client

import agent_core
from paths import get_resource_dir

# ══════════════════════════════════════════════════════
#  SETUP
# ══════════════════════════════════════════════════════
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

# Your Google Maps API key — set this as an environment variable on
# Render (Settings → Environment), never hardcode it here.
OWNER_GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")

TRIAL_DAYS = 5

# In-memory per-user run state (log queue, running flag).
# Fine for a single Render instance; if you ever scale to multiple
# instances behind a load balancer, this would need to move to Redis.
_user_states = {}


def get_user_state(uid):
    if uid not in _user_states:
        _user_states[uid] = {"running": False, "log_queue": queue.Queue(), "last_result": None}
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


# ══════════════════════════════════════════════════════
#  PAGES
# ══════════════════════════════════════════════════════
@app.route("/")
def index():
    if "user_id" in session:
        return redirect("/dashboard")
    return render_template("saas.html")


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


# ══════════════════════════════════════════════════════
#  AUTH
# ══════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════
#  PROFILE
# ══════════════════════════════════════════════════════
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
        profile.pop("gmail_app_password", None)  # never send this back to the browser
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
    ]
    update = {k: v for k, v in data.items() if k in allowed}

    if not update:
        return jsonify({"ok": False, "message": "Nothing to save."})

    try:
        supabase.table("profiles").update(update).eq("id", uid).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})


# ══════════════════════════════════════════════════════
#  STATUS
# ══════════════════════════════════════════════════════
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


# ══════════════════════════════════════════════════════
#  RUN THE AGENT
# ══════════════════════════════════════════════════════
def _build_config(profile):
    business_types = profile.get("business_types") or (
        "small business,shop,store,restaurant,hotel,clinic,school,agency,company,office"
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
        "GMAIL_APP_PASSWORD": "",
        "GOOGLE_MAPS_API_KEY": OWNER_GOOGLE_MAPS_API_KEY,
        "MAX_RESULTS_PER_QUERY": 20,
        "DELAY_BETWEEN_EMAILS": 30,
        "ATTACHMENT_PATH": "",
        "ATTACHMENT_NAME": "",
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
            state["log_queue"].put(str(message))
        try:
            result = agent_core.run_full_pipeline(cfg, log=log, user_id=uid)
            state["last_result"] = result
        except Exception as e:
            log(f"❌ Unexpected error: {e}")
        finally:
            log("__DONE__")
            state["running"] = False

    state["running"] = True
    state["log_queue"] = queue.Queue()
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
            state["log_queue"].put(str(message))
        try:
            agent_core.check_replies(cfg, log=log, user_id=uid)
        except Exception as e:
            log(f"❌ Unexpected error: {e}")
        finally:
            log("__DONE__")
            state["running"] = False

    state["running"] = True
    state["log_queue"] = queue.Queue()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════
#  LOG STREAM (Server-Sent Events)
# ══════════════════════════════════════════════════════

@app.route("/api/sent_emails", methods=["GET"])
@login_required
def api_sent_emails():
    err = _require_supabase()
    if err:
        return err
    uid = session["user_id"]
    try:
        from supabase import create_client
        sb = create_client(SUPABASE_URL, SUPABASE_KEY)
        res = sb.table("sent_log").select("*").eq("user_id", uid).order("sent_date", desc=True).execute()
        return jsonify({"ok": True, "emails": res.data})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})

@app.route("/api/stream")
@login_required
def api_stream():
    uid = session["user_id"]
    state = get_user_state(uid)

    def event_stream():
        q = state["log_queue"]
        while True:
            try:
                line = q.get(timeout=30)
            except queue.Empty:
                yield "data: \n\n"  # keepalive
                continue
            if line == "__DONE__":
                yield "event: done\ndata: done\n\n"
                break
            yield f"data: {line}\n\n"

    return Response(event_stream(), mimetype="text/event-stream")


# ══════════════════════════════════════════════════════
#  MAIN (local dev only — Render uses gunicorn, see Procfile)
# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
