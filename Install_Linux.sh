#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$SCRIPT_DIR/AI_Client_Agent-Linux"
chmod +x "$APP"
"$APP"
