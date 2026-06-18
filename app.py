import threading, queue, os
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, Response, session, redirect
from supabase import create_client, Client
from functools import wraps
import agent_core
from paths import get_resource_dir

_resource_dir = get_resource_dir()
app = Flask(__name__, template_folder=os.path.join(_resource_dir, "templates"), static_folder=os.path.join(_resource_dir, "static"))
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None
OWNER_GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "AIzaSyC7BszKyHwmYqIfletuTQszUA_J2fH9siE")
TRIAL_DAYS = 5
_user_states = {}

def _auth_error_message(error):
    msg = str(error)
    low = msg.lower()
    if "email not confirmed" in low or "email_not_confirmed" in low:
        return "Please confirm your email first. Check your inbox for the Supabase confirmation link."
    if "invalid login credentials" in low or "invalid email or password" in low:
        return "Invalid email or password."
    if "supabase" in low or "api key" in low or "url" in low:
        return "Authentication is not configured correctly. Check SUPABASE_URL and SUPABASE_KEY."
    return msg or "Authentication failed. Please try again."

def _profile_schema_error(error):
    msg = str(error)
    low = msg.lower()
    if "pgrst204" not in low and "schema cache" not in low and "could not find" not in low:
        return None
    missing = None
    marker = "could not find the '"
    if marker in low:
        start = low.find(marker) + len(marker)
        end = low.find("'", start)
        if end > start:
            missing = low[start:end]
    if missing:
        return f"Database setup incomplete: profiles table is missing the '{missing}' column. Run supabase_profiles_schema.sql in Supabase SQL Editor, then retry."
    return "Database setup incomplete: profiles table is missing required columns. Run supabase_profiles_schema.sql in Supabase SQL Editor, then retry."

def _require_supabase():
    if not supabase:
        return jsonify({"ok": False, "message": "Supabase is not configured. Set SUPABASE_URL and SUPABASE_KEY."}), 500
    return None

def _profile_defaults(uid, email=""):
    return {
        "id": uid,
        "full_name": "",
        "business_name": "",
        "gmail": email,
        "gmail_app_password": "",
        "your_service": "",
        "your_about": "",
        "target_city": "",
        "business_types": "",
        "trial_start": None,
        "is_paid": False,
    }

def _get_profile(uid):
    res = supabase.table("profiles").select("*").eq("id", uid).execute()
    if res.data:
        return res.data[0]
    return None

def _ensure_profile(uid, email=""):
    profile = _get_profile(uid)
    if profile:
        return profile
    profile = {"id": uid, "full_name": "", "gmail": email}
    supabase.table("profiles").insert(profile).execute()
    return profile

def _ensure_profile_after_auth(uid, email=""):
    try:
        _ensure_profile(uid, email)
    except Exception as e:
        app.logger.warning("Could not create profile for %s after auth: %s", uid, e)

def _trial_days_left(profile):
    trial_start = profile.get("trial_start")
    if trial_start:
        start = datetime.fromisoformat(trial_start.replace("Z", "+00:00"))
        days_used = (datetime.now(start.tzinfo) - start).days
        return max(TRIAL_DAYS - days_used, 0)
    return TRIAL_DAYS

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

@app.route("/")
def index():
    return render_template("saas.html")

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")

@app.route("/api/auth/signup", methods=["POST"])
def api_signup():
    err = _require_supabase()
    if err:
        return err
    data = request.get_json()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    full_name = data.get("full_name", "").strip()
    if not email or not password:
        return jsonify({"ok": False, "message": "Email and password required."})
    try:
        res = supabase.auth.sign_up({"email": email, "password": password})
        user = res.user
        if not user:
            return jsonify({"ok": False, "message": "Signup failed. Try again."})
        if res.session:
            session["user_id"] = user.id
            session["user_email"] = email
            profile = _get_profile(user.id) or {}
            update = {}
            if full_name and not profile.get("full_name"):
                update["full_name"] = full_name
            if update:
                try:
                    supabase.table("profiles").update(update).eq("id", user.id).execute()
                except Exception as e:
                    app.logger.warning("Could not save signup profile name for %s: %s", user.id, e)
            return jsonify({"ok": True, "redirect": "/dashboard"})
        return jsonify({"ok": True, "confirm": True, "message": "Check your email. After confirming your account, click Sign In."})
    except Exception as e:
        return jsonify({"ok": False, "message": _auth_error_message(e)})

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    err = _require_supabase()
    if err:
        return err
    data = request.get_json()
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    try:
        res = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user = res.user
        if not user:
            return jsonify({"ok": False, "message": "Invalid email or password."})
        session["user_id"] = user.id
        session["user_email"] = email
        _ensure_profile_after_auth(user.id, email)
        return jsonify({"ok": True, "redirect": "/dashboard"})
    except Exception as e:
        return jsonify({"ok": False, "message": _auth_error_message(e)})

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True, "redirect": "/"})

@app.route("/api/profile", methods=["GET"])
@login_required
def api_get_profile():
    err = _require_supabase()
    if err:
        return err
    uid = session["user_id"]
    try:
        profile = _ensure_profile(uid, session.get("user_email", ""))
        days_left = _trial_days_left(profile)
        profile["days_left"] = days_left
        profile["trial_active"] = days_left > 0
        profile["is_paid"] = profile.get("is_paid", False)
        profile.pop("gmail_app_password", None)
        return jsonify({"ok": True, "profile": profile})
    except Exception as e:
        return jsonify({"ok": False, "message": _profile_schema_error(e) or str(e)})

@app.route("/api/profile", methods=["POST"])
@login_required
def api_save_profile():
    err = _require_supabase()
    if err:
        return err
    uid = session["user_id"]
    data = request.get_json() or {}
    allowed = ["full_name", "business_name", "gmail", "gmail_app_password", "your_service", "your_about", "target_city", "business_types"]
    update = {k: v.strip() if isinstance(v, str) else v for k, v in data.items() if k in allowed}
    if not update.get("gmail_app_password"):
        update.pop("gmail_app_password", None)
    try:
        profile = _ensure_profile(uid, session.get("user_email", ""))
        if not profile.get("trial_start"):
            update["trial_start"] = datetime.now(timezone.utc).isoformat()
        supabase.table("profiles").update(update).eq("id", uid).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": _profile_schema_error(e) or str(e)})

@app.route("/api/status")
@login_required
def api_status():
    err = _require_supabase()
    if err:
        return err
    uid = session["user_id"]
    state = get_user_state(uid)
    try:
        profile = _ensure_profile(uid, session.get("user_email", ""))
        days_left = _trial_days_left(profile)
        required = ["full_name", "gmail", "gmail_app_password", "your_service", "your_about", "target_city"]
        profile_complete = all(profile.get(k, "").strip() for k in required)
        return jsonify({"ok": True, "profile_complete": profile_complete, "days_left": days_left, "is_paid": profile.get("is_paid", False), "running": state["running"]})
    except Exception as e:
        return jsonify({"ok": False, "message": _profile_schema_error(e) or str(e)})

def _build_config(profile):
    business_types = profile.get("business_types") or ["small business","shop","store","restaurant","hotel","clinic","school","agency","company"]
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
        "GMAIL_APP_PASSWORD": profile.get("gmail_app_password", ""),
        "GOOGLE_MAPS_API_KEY": OWNER_GOOGLE_MAPS_API_KEY,
        "MAX_RESULTS_PER_QUERY": 20,
        "DELAY_BETWEEN_EMAILS": 30,
        "ATTACHMENT_PATH": "",
        "ATTACHMENT_NAME": "",
        "LICENSE_KEY": "",
    }

@app.route("/api/run", methods=["POST"])
@login_required
def api_run():
    uid = session["user_id"]
    state = get_user_state(uid)
    if state["running"]:
        return jsonify({"ok": False, "message": "Agent is already running."})
    res = supabase.table("profiles").select("*").eq("id", uid).single().execute()
    profile = res.data or {}
    from datetime import datetime
    trial_start = profile.get("trial_start")
    if trial_start:
        start = datetime.fromisoformat(trial_start.replace("Z", "+00:00"))
        days_used = (datetime.now(start.tzinfo) - start).days
        days_left = max(TRIAL_DAYS - days_used, 0)
    else:
        days_left = TRIAL_DAYS
    if days_left <= 0 and not profile.get("is_paid"):
        return jsonify({"ok": False, "message": "Your free trial has ended. Please upgrade to continue."})
    cfg = _build_config(profile)
    def _run():
        def log(msg):
            state["log_queue"].put(str(msg))
        try:
            result = agent_core.run_full_pipeline(cfg, log=log)
            state["last_result"] = result
        except Exception as e:
            log(f"Unexpected error: {e}")
        finally:
            log("__DONE__")
            state["running"] = False
    state["running"] = True
    state["log_queue"] = queue.Queue()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})

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
                yield "data: \n\n"
                continue
            if line == "__DONE__":
                yield "event: done\ndata: done\n\n"
                break
            yield f"data: {line}\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
