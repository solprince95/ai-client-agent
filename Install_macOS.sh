#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$SCRIPT_DIR/AI_Client_Agent-macOS"
chmod +x "$APP"
xattr -d com.apple.quarantine "$APP" 2>/dev/null
"$APP"
