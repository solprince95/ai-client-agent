# 🤖 AI Client Agent

Finds local businesses, sends them a personalised email about your service,
and tells you when someone replies — automatically.

## For Customers — Just Run It

You should have received a single file:
- **Windows**: `AI_Client_Agent.exe`
- **macOS**: `AI_Client_Agent`
- **Linux**: `AI_Client_Agent`

**Double-click it.** Your browser opens automatically to the dashboard.
No installation needed.

> macOS may show a security warning the first time ("unidentified
> developer"). Right-click the file → Open, then confirm.

### First time setup
1. Go to the **Setup** tab
2. Pick your profession from the dropdown — it fills in suggestions
3. Add your Gmail address + **App Password** (free, 2 min —
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords),
   requires 2-Step Verification)
4. Click **Save profile** — your 5-day free trial starts automatically

### Using it
- **Find Clients & Send Emails** — searches your target city, finds
  businesses with websites, extracts their email, sends a personalised
  message about your service
- **Check Replies** — checks your inbox for responses
- **Billing tab** — trial status and how to get a license key after
  your trial ends

Everything (your profile, sent-email log, trial status) is saved in
small files right next to the executable — keep them together in one
folder.

---

## For the Developer — Building the Executables

This repo builds into a single double-click executable using
PyInstaller, for Windows, macOS, and Linux.

### Build locally
```bash
pip install -r requirements.txt
pyinstaller AI_Client_Agent.spec --noconfirm
```
Output appears in `dist/`.

⚠️ PyInstaller does **not** cross-compile — building on Linux only
produces a Linux binary. To get a Windows `.exe`, build on Windows
(or use the GitHub Actions workflow below).

### Build all 3 platforms automatically (free)
This repo includes `.github/workflows/build.yml`. Push this folder to
a GitHub repo (private is fine, free), then:

1. Go to the **Actions** tab on GitHub
2. Wait for the workflow to finish (a few minutes)
3. Click into the run → scroll to **Artifacts**
4. Download `AI_Client_Agent-Windows.exe`, `AI_Client_Agent-macOS`,
   and `AI_Client_Agent-Linux`

These are the files you send to customers.

### Before building for real customers
Fill in your real Google Maps API key in `config_store.py`:
```python
OWNER_GOOGLE_MAPS_API_KEY = "your-real-key-here"
```
This gets baked into every executable — customers never need their own.
