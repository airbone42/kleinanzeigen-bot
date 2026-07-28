#!/usr/bin/env bash
set -e

# Start virtual X11 display so Chrome runs in non-headless mode.
# kleinanzeigen-bot requires X11 — headless Chrome triggers Cloudflare blocking
# on authenticated API endpoints (documented requirement in kleinanzeigen-bot README).
if pgrep -f "Xvfb :99" >/dev/null 2>&1; then
  export DISPLAY=:99
else
  if [ -f /tmp/.X99-lock ]; then
    rm -f /tmp/.X99-lock
  fi
  if [ -S /tmp/.X11-unix/X99 ]; then
    rm -f /tmp/.X11-unix/X99
  fi
  Xvfb :99 -screen 0 1280x1024x24 -ac +extension GLX +render -noreset &
  export DISPLAY=:99
  sleep 1
fi

exec python -m bot.main "$@"
