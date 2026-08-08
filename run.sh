#!/bin/sh
# X Arcade launcher. Demo mode by default: fixtures only, zero network.
# ARCADE_MODE=live ./run.sh enables real xAI calls (needs XAI_API_KEY).
cd "$(dirname "$0")" || exit 1

# Load .env if present (does not override vars already in the environment).
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# macOS python.org builds often lack a CA bundle; point OpenSSL at certifi.
if [ -z "${SSL_CERT_FILE:-}" ]; then
    if [ -x .venv/bin/python ]; then
        _py=.venv/bin/python
    else
        _py=python3
    fi
    _ca="$("$_py" -c 'import certifi; print(certifi.where())' 2>/dev/null)" || _ca=""
    if [ -n "$_ca" ]; then
        export SSL_CERT_FILE="$_ca"
    fi
fi

# Bind all interfaces so other players on the same network can join.
# Uvicorn defaults to 127.0.0.1, which only the host machine can reach.
# Override with ARCADE_HOST=127.0.0.1 for local-only.
HOST="${ARCADE_HOST:-0.0.0.0}"
if [ -x .venv/bin/uvicorn ]; then
    exec .venv/bin/uvicorn server.app:app --host "$HOST" --port 8787 "$@"
fi
exec uvicorn server.app:app --host "$HOST" --port 8787 "$@"
