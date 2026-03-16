#!/usr/bin/env bash
set -euo pipefail

URL="http://localhost:8080"

if command -v chromium-browser >/dev/null 2>&1; then
  exec chromium-browser --kiosk --noerrdialogs --disable-session-crashed-bubble "$URL"
elif command -v chromium >/dev/null 2>&1; then
  exec chromium --kiosk --noerrdialogs --disable-session-crashed-bubble "$URL"
elif command -v firefox >/dev/null 2>&1; then
  exec firefox --kiosk "$URL"
elif command -v epiphany-browser >/dev/null 2>&1; then
  exec epiphany-browser --application-mode="$URL"
fi

exec xdg-open "$URL"
