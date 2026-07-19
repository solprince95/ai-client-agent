"""
paths.py, shared helper for resolving file paths in both:
  - normal "python app.py" development mode, and
  - a PyInstaller-bundled single-file executable.

Why this exists:
  PyInstaller's --onefile mode extracts the app into a temporary
  folder (sys._MEIPASS) every time it runs, then deletes it on exit.
  Anything written there (config, trial info, sent log) would be LOST
  the moment the app closes.

  So:
    - get_base_dir()     → persistent folder, next to the .exe itself.
                            Use this for user_config.json, sent_log.csv,
                            .trial_data, and any attachments the user adds.
    - get_resource_dir() → bundled read-only resources (templates/, static/).
                            Use this for Flask's template_folder / static_folder.

  In normal "python app.py" mode, both functions simply return the
  folder containing this file, so nothing changes for development.
"""

import os
import sys


def get_base_dir() -> str:
    if getattr(sys, "frozen", False):
        # Running as a PyInstaller bundle, use the folder containing
        # the actual .exe / binary, so data persists between runs.
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_resource_dir() -> str:
    if getattr(sys, "frozen", False):
        # Bundled resources (templates, static) live in PyInstaller's
        # temporary extraction directory.
        return sys._MEIPASS  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))
