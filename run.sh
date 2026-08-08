#!/bin/sh
# X Arcade launcher. Demo mode by default: fixtures only, zero network.
# ARCADE_MODE=live ./run.sh enables real xAI calls (needs XAI_API_KEY).
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/uvicorn ]; then
    exec .venv/bin/uvicorn server.app:app --port 8787 "$@"
fi
exec uvicorn server.app:app --port 8787 "$@"
