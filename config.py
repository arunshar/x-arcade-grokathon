"""One place for every model id and mode switch. Nothing else hardcodes these."""

import os

MODEL_TEXT = "grok-4.5"
MODEL_IMAGE = "grok-imagine-image"
MODEL_VIDEO = "grok-imagine-video-1.5"
# Pinned deliberately: the grok-voice-latest alias was repointed on 5 Aug 2026,
# three days before the event. A pinned id cannot change under us on the day.
MODEL_VOICE = "grok-voice-think-fast-2.0"

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
