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
# How often GIF rounds appear when a round JSON does not set "format".
# "alternate" (default) → text, gif, text… so both classic and media rounds appear.
# "always_gif" → every round uses human pool GIFs + Grok Imagine decoy video.
# "always_text" → text only. "half" → ~50% by hash of round_id.
GIF_ROUND_MODE = (os.environ.get("ARCADE_GIF_MODE") or "alternate").strip().lower()
# When true (default), the decoy reply media MUST be produced by Grok Imagine:
#   grok-imagine-image → still, then grok-imagine-video → short loop mp4.
# Never a human pool .gif, never an uncertified probe clone.
# Set ARCADE_IMAGINE_DECOY=0 only for offline demos that pre-bake certified files.
IMAGINE_DECOY_REQUIRED = os.environ.get("ARCADE_IMAGINE_DECOY", "1") != "0"
# Match length: after this many rounds the room opens a results screen
# instead of looping forever. Multiplayer and solo both default to a full
# 6-round match. Override with ARCADE_MATCH_ROUNDS (1–20) for tests only.
try:
    MATCH_ROUNDS = int(os.environ.get("ARCADE_MATCH_ROUNDS", "6") or "6")
except ValueError:
    MATCH_ROUNDS = 6
MATCH_ROUNDS = max(1, min(20, MATCH_ROUNDS))
# Multiplayer lobby size. Solo rooms stay 1-seat (enforced separately).
try:
    MULTIPLAYER_MIN_PLAYERS = int(os.environ.get("ARCADE_MP_MIN_PLAYERS", "2") or "2")
except ValueError:
    MULTIPLAYER_MIN_PLAYERS = 2
try:
    MULTIPLAYER_MAX_PLAYERS = int(os.environ.get("ARCADE_MP_MAX_PLAYERS", "5") or "5")
except ValueError:
    MULTIPLAYER_MAX_PLAYERS = 5
MULTIPLAYER_MIN_PLAYERS = max(2, min(10, MULTIPLAYER_MIN_PLAYERS))
MULTIPLAYER_MAX_PLAYERS = max(
    MULTIPLAYER_MIN_PLAYERS, min(10, MULTIPLAYER_MAX_PLAYERS)
)
# Soft idle on the results screen before returning to lobby (scores kept
# until RESTART). Manual RESTART / HOME work immediately.
try:
    RESULTS_SECONDS = int(os.environ.get("ARCADE_RESULTS_SECONDS", "45") or "45")
except ValueError:
    RESULTS_SECONDS = 45
RESULTS_SECONDS = max(10, min(300, RESULTS_SECONDS))
