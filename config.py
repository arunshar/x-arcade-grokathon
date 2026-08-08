"""One place for every model id and mode switch. Nothing else hardcodes these."""

import os

MODEL_TEXT = "grok-4.5"
# Host commentator must be snappy. Probe 8 Aug 2026 (artifacts/probes/host_agent_chat.json):
#   grok-4.5 host lines ~5.3–5.9s; grok-4-1-fast-non-reasoning ~0.6s.
# Override with ARCADE_AGENT_MODEL if needed.
MODEL_AGENT = os.environ.get("ARCADE_AGENT_MODEL", "grok-4-1-fast-non-reasoning").strip() or (
    "grok-4-1-fast-non-reasoning"
)
MODEL_IMAGE = "grok-imagine-image"
MODEL_VIDEO = "grok-imagine-video-1.5"
# Pinned deliberately: the grok-voice-latest alias was repointed on 5 Aug 2026,
# three days before the event. A pinned id cannot change under us on the day.
MODEL_VOICE = "grok-voice-think-fast-2.0"
# Grok TTS / realtime speaker id (eve, helix, sirius, leo, …). See GET /voices.
TTS_VOICE = os.environ.get("ARCADE_VOICE", "eve").strip().lower() or "eve"

API_BASE = "https://api.x.ai/v1"

# demo: fixtures only, zero network, the mode the stage demo runs in.
# live: real API calls, records fixtures when ARCADE_RECORD=1.
MODE = os.environ.get("ARCADE_MODE", "demo")
RECORD = os.environ.get("ARCADE_RECORD", "") == "1"

ROUND_SECONDS = 30
# The session is time driven, no host. A round starts this many seconds after
# the first player lands in a lobby, and the next round starts this many
# seconds after a reveal. Anyone may tap START / NEXT ROUND to skip the wait.
LOBBY_SECONDS = 10
REVEAL_SECONDS = 14
REPLIES_PER_ROUND = 5
