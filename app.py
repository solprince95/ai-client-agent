"""
app.py — AI Client Agent SaaS
Multi-user web app with Supabase auth, profile setup, and agent runner.
"""

import threading, queue, time, os, json
from flask import Flask, render_template, request, jsonify, Response, session, redirect
from supabase import create_client, Client
from functools import wraps

import agent_core
from paths import get_resource_dir

_resource_dir = get_resource_dir()
app = Flask(
    __name__,
    template_folder=os.path.join(_resource_dir, "templates"),
    static_folder=os.path.join(_resource_dir, "static"),
)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL else None

OWNER_GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "AIzaSyC7BszKyHwmYqIfletuTQszUA_J2fH9siE")
TRIAL_DAYS = 5

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

@app.route("/")
def index():
    return render_template("saas.html")

@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")

@app.route("/api/auth/signup", methods=["POST"])
def api_signup():
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
        supabase.table("profiles").insert({"id": user.id, "full_name": full_name, "gmail": email}).execute()
return jsonify({"ok": True, "confirm": True, "message": "Check your email and click the confirmation link to activate your account."})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})

@app.route("/api/auth/login", methods=["POST"])
def api_login():
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
        return jsonify({"ok": True, "redirect": "/dashboard"})
    except Exception as e:
        return jsonify({"ok": False, "message": "Invalid email or password."})

@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True, "redirect": "/"})

@app.route("/api/profile", methods=["GET"])
@login_required
def api_get_profile():
    uid = session["user_id"]
    try:
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
        profile["days_left"] = days_left
        profile["trial_active"] = days_left > 0
        profile["is_paid"] = profile.get("is_paid", False)
        profile.pop("gmail_app_password", None)
        return jsonify({"ok": True, "profile": profile})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})

@app.route("/api/profile", methods=["POST"])
@login_required
def api_save_profile():
    uid = session["user_id"]
    data = request.get_json()
    allowed = ["full_name", "business_name", "gmail", "gmail_app_password",
               "your_service", "your_about", "target_city", "business_types"]
    update = {k: v for k, v in data.items() if k in allowed}
    try:
        supabase.table("profiles").update(update).eq("id", uid).execute()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})

@app.route("/api/status")
@login_required
def api_status():
    uid = session["user_id"]
    state = get_user_state(uid)
    try:
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
        required = ["full_name", "gmail", "gmail_app_password", "your_service", "your_about", "target_city"]
        profile_complete = all(profile.get(k, "").strip() for k in required)
        return jsonify({
            "ok": True,
            "profile_complete": profile_complete,
            "days_left": days_left,
            "is_paid": profile.get("is_paid", False),
            "running": state["running"],
        })
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)})

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
            log(f"❌ Unexpected error: {e}")
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
