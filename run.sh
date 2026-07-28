#!/usr/bin/env bash
# Direct bot launcher (without Docker). Handles venv creation + install automatically.
#
#   ./run.sh            – start the Telegram bot
#   ./run.sh diagnose   – run browser diagnostics (no Telegram polling)
#   ./run.sh cli-test   – run kleinanzeigen-bot publish directly (debugging)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PYTHON=".venv/bin/python3"

# Auto-create venv and install dependencies if missing
if [ ! -f "$VENV_PYTHON" ]; then
    echo "[run.sh] Virtual environment not found — creating..."
    python3 -m venv .venv
    echo "[run.sh] Installing dependencies (this may take a few minutes)..."
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -e .
    echo "[run.sh] Setup complete."
fi

# cli-test mode: invoke kleinanzeigen-bot publish directly, bypassing our Telegram bot.
# Use this to confirm whether errors are in kleinanzeigen-bot itself vs. our wrapper code.
if [ "${1:-}" = "cli-test" ]; then
    echo "[run.sh] Running kleinanzeigen-bot publish directly (cli-test mode)..."
    cd kleinanzeigen-config
    exec "$SCRIPT_DIR/$VENV_PYTHON" -m kleinanzeigen_bot --config config.yaml --verbose publish --ads new
fi

exec "$VENV_PYTHON" -m bot.main "$@"
