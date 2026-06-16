"""
app.py — AI Client Agent Dashboard
Local web app: run `python app.py`, then open http://127.0.0.1:5000
Also works as a PyInstaller --onefile bundle (see paths.py).
"""

import threading, queue, time, webbrowser, os
from flask import Flask, render_template, request, jsonify, Response

import config_store
import agent_core
from license_manager import check_access
from paths import get_resource_dir

_resource_dir = get_resource_dir()
app = Flask(
    __name__,
    template_folder=os.path.join(_resource_dir, "templates"),
    static_folder=os.path.join(_resource_dir, "static"),
)

# ── Shared state for the background run ──
state = {
    "running": False,
    "log_queue": queue.Queue(),
    "last_result": None,
}


def log_to_queue(message):
    state["log_queue"].put(str(message))


# ══════════════════════════════════════════════════════
#  PAGES
# ══════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html")


# ══════════════════════════════════════════════════════
#  STATUS — profile, license, stats
# ══════════════════════════════════════════════════════
@app.route("/api/status")
def api_status():
    cfg = config_store.load_config()
    profile_complete = config_store.is_profile_complete(cfg)

    license_info = {"allowed": True, "message": "", "kind": "none"}
    if profile_complete:
        # check_access prints to stdout — capture nothing, just call verify directly
        from license_manager import verify_license_key, get_trial_status
        if cfg.get("LICENSE_KEY"):
            valid, msg, days_left = verify_license_key(cfg["YOUR_EMAIL"], cfg["LICENSE_KEY"])
            if valid:
                license_info = {"allowed": True, "message": msg, "kind": "licensed", "days_left": days_left}
            else:
                trial_active, days_left, start = get_trial_status()
                if trial_active:
                    license_info = {"allowed": True, "message": f"License problem ({msg}). Using trial.",
                                     "kind": "trial", "days_left": days_left}
                else:
                    license_info = {"allowed": False, "message": msg, "kind": "expired", "days_left": 0}
        else:
            trial_active, days_left, start = get_trial_status()
            if trial_active:
                license_info = {"allowed": True, "message": "Free trial active", "kind": "trial", "days_left": days_left}
            else:
                license_info = {"allowed": False, "message": "Trial expired", "kind": "expired", "days_left": 0}

    stats = agent_core.get_stats()

    return jsonify({
        "profile_complete": profile_complete,
        "config": {k: v for k, v in cfg.items() if k != "GOOGLE_MAPS_API_KEY"},
        "license": license_info,
        "stats": stats,
        "running": state["running"],
    })


# ══════════════════════════════════════════════════════
#  SAVE PROFILE
# ══════════════════════════════════════════════════════
@app.route("/api/config", methods=["POST"])
def api_save_config():
    data = request.get_json()
    # business types may come as comma-separated string
    if isinstance(data.get("BUSINESS_TYPES"), str):
        data["BUSINESS_TYPES"] = [b.strip() for b in data["BUSINESS_TYPES"].split(",") if b.strip()]
    cfg = config_store.save_config(data)
    return jsonify({"ok": True, "config": {k: v for k, v in cfg.items() if k != "GOOGLE_MAPS_API_KEY"}})


# ══════════════════════════════════════════════════════
#  RUN AGENT — background thread + streaming logs
# ══════════════════════════════════════════════════════
def _run_pipeline_thread(cfg):
    try:
        result = agent_core.run_full_pipeline(cfg, log=log_to_queue)
        state["last_result"] = result
    except Exception as e:
        log_to_queue(f"❌ Unexpected error: {e}")
    finally:
        log_to_queue("__DONE__")
        state["running"] = False


@app.route("/api/run", methods=["POST"])
def api_run():
    if state["running"]:
        return jsonify({"ok": False, "message": "Agent is already running."})

    cfg = config_store.load_config()
    if not config_store.is_profile_complete(cfg):
        return jsonify({"ok": False, "message": "Please complete your profile in Setup first."})

    # License check
    allowed, msg = check_access(cfg["YOUR_EMAIL"], cfg.get("LICENSE_KEY", ""))
    if not allowed:
        return jsonify({"ok": False, "message": "Your free trial has ended. See Billing tab to continue."})

    state["running"] = True
    state["log_queue"] = queue.Queue()
    t = threading.Thread(target=_run_pipeline_thread, args=(cfg,), daemon=True)
    t.start()
    return jsonify({"ok": True})


@app.route("/api/check-replies", methods=["POST"])
def api_check_replies():
    if state["running"]:
        return jsonify({"ok": False, "message": "Agent is already running."})

    cfg = config_store.load_config()
    if not config_store.is_profile_complete(cfg):
        return jsonify({"ok": False, "message": "Please complete your profile in Setup first."})

    allowed, msg = check_access(cfg["YOUR_EMAIL"], cfg.get("LICENSE_KEY", ""))
    if not allowed:
        return jsonify({"ok": False, "message": "Your free trial has ended. See Billing tab to continue."})

    def _run():
        try:
            agent_core.check_replies(cfg, log=log_to_queue)
        except Exception as e:
            log_to_queue(f"❌ Unexpected error: {e}")
        finally:
            log_to_queue("__DONE__")
            state["running"] = False

    state["running"] = True
    state["log_queue"] = queue.Queue()
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════
#  LOG STREAM — Server-Sent Events
# ══════════════════════════════════════════════════════
@app.route("/api/stream")
def api_stream():
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
#  MAIN
# ══════════════════════════════════════════════════════
def _find_free_port(start=5000, tries=20):
    import socket
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


if __name__ == "__main__":
    port = _find_free_port(5000)
    url = f"http://127.0.0.1:{port}"

    print("\n" + "═" * 55)
    print("  🤖  AI CLIENT AGENT — Dashboard")
    print("═" * 55)
    print(f"\n  Starting up... your browser will open automatically.")
    print(f"  If it doesn't, open this address manually:")
    print(f"\n      {url}\n")
    print("  Keep this window open while you use the app.")
    print("  Close this window to stop the agent.\n")

    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
import webbrowser
import threading

def open_browser():
    webbrowser.open("http://127.0.0.1:5000")

threading.Timer(1.5, open_browser).start()
app.run(host="127.0.0.1", port=5000)
