#!/bin/sh
# X Arcade launcher. Demo mode by default: fixtures only, zero network.
# ARCADE_MODE=live ./run.sh enables real xAI calls (needs XAI_API_KEY).
cd "$(dirname "$0")" || exit 1
# Bind all interfaces so other players on the same network can join.
# Uvicorn defaults to 127.0.0.1, which only the host machine can reach.
HOST="${ARCADE_HOST:-0.0.0.0}"
if [ -x .venv/bin/uvicorn ]; then
    exec .venv/bin/uvicorn server.app:app --host "$HOST" --port 8787 "$@"
fi
exec uvicorn server.app:app --host "$HOST" --port 8787 "$@"
