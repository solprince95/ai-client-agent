"""
config_store.py — Loads & saves the user's profile as JSON.
Replaces the old config.py — now editable through the dashboard UI.
"""

import os, json

from paths import get_base_dir

BASE = get_base_dir()
CONFIG_PATH = os.path.join(BASE, "user_config.json")

# ⚠️ OWNER: put YOUR Google Maps API key here.
# Customers never see or need their own key — you cover this centrally.
OWNER_GOOGLE_MAPS_API_KEY = "AIzaSyC7BszKyHwmYqIfletuTQszUA_J2fH9siE"

DEFAULT_CONFIG = {
    "YOUR_EMAIL":   "",
    "LICENSE_KEY":  "",

    "YOUR_NAME":     "",
    "YOUR_SERVICE":  "",
    "YOUR_ABOUT":    "",

    "TARGET_CITY":   "",
    "BUSINESS_TYPES": [
        "small business", "shop", "store", "restaurant",
        "hotel", "clinic", "school", "agency", "company", "office",
    ],

    "GMAIL_ADDRESS":       "",
    "GMAIL_APP_PASSWORD":  "",

    "ATTACHMENT_PATH": "",
    "ATTACHMENT_NAME": "Portfolio.pdf",

    "MAX_RESULTS_PER_QUERY": 20,
    "DELAY_BETWEEN_EMAILS":  30,
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            data = json.load(f)
    else:
        data = {}
    cfg = {**DEFAULT_CONFIG, **data}
    # Always inject the owner's API key — customer never sets this
    cfg["GOOGLE_MAPS_API_KEY"] = OWNER_GOOGLE_MAPS_API_KEY
    return cfg


def save_config(updates: dict):
    cfg = load_config()
    for k, v in updates.items():
        if k in DEFAULT_CONFIG:
            cfg[k] = v
    # Never persist the API key into user_config.json (keep it server-side only)
    cfg.pop("GOOGLE_MAPS_API_KEY", None)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
    return load_config()


def is_profile_complete(cfg):
    required = ["YOUR_NAME", "YOUR_SERVICE", "YOUR_ABOUT", "TARGET_CITY",
                "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "YOUR_EMAIL"]
    return all(cfg.get(k, "").strip() for k in required)
