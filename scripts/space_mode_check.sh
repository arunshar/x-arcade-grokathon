#!/usr/bin/env bash
# Check what mode the hosted Space is actually serving, and whether the live
# key works. Read-only apart from one near-free token mint in live mode.
#
#   scripts/space_mode_check.sh          report current state
#   scripts/space_mode_check.sh live     exit 0 only if fully live
#   scripts/space_mode_check.sh demo     exit 0 only if fully demo
set -u

BASE="https://arun0808-x-arcade.hf.space"
WANT="${1:-report}"

health=$(curl -s -L -m 25 "$BASE/health")
if [ -z "$health" ]; then
  echo "FAIL: Space not answering. If it says RUNNING on HF, restart it (takes ~15s)."
  exit 2
fi

mode=$(printf '%s' "$health" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("mode",""))' 2>/dev/null)
rounds=$(printf '%s' "$health" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("rounds_available",""))' 2>/dev/null)
echo "health : mode=$mode rounds=$rounds"

token=$(curl -s -L -m 30 "$BASE/token")
demo_flag=$(printf '%s' "$token" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("demo"))' 2>/dev/null)
has_value=$(printf '%s' "$token" | python3 -c 'import json,sys; print(bool(json.load(sys.stdin).get("value")))' 2>/dev/null)
echo "token  : demo=$demo_flag value_present=$has_value"

if [ "$mode" = "live" ] && [ "$has_value" = "True" ]; then
  state="LIVE (key working: real voice token minted)"
elif [ "$mode" = "live" ]; then
  state="BROKEN: mode is live but no token minted. XAI_API_KEY missing or wrong on the Space."
elif [ "$mode" = "demo" ]; then
  state="DEMO (offline stub token, pre-rendered voice, committed card)"
else
  state="UNKNOWN mode='$mode'"
fi
echo "state  : $state"

case "$WANT" in
  live) [ "$mode" = "live" ] && [ "$has_value" = "True" ] ;;
  demo) [ "$mode" = "demo" ] ;;
  *)    true ;;
esac
