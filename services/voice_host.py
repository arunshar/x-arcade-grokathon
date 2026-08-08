"""Voice host for X Arcade.

Two duties:
1. mint_token() gets a short-lived realtime client secret so the browser can
   talk to the realtime voice API directly. The server key never reaches the
   browser. Probed at build time: 200 in 0.13s.
2. render_host_lines() pre-renders every scripted host line to mp3 through
   /v1/tts at build time. The demo plays committed files and needs no network.
   Probed at build time: 1.78s per line, and the "language" field is required.

Build-time render (writes the committed assets):
    ARCADE_MODE=live ARCADE_RECORD=1 python3 services/voice_host.py

Token smoke test (prints the response shape, never the secret):
    python3 services/voice_host.py mint
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
from services.xai_http import post_json, post_raw  # noqa: E402

ASSETS_DIR = REPO_ROOT / "web" / "static-assets"

# Every scripted host line the game plays. Speech tags like [pause] are
# supported by the TTS surface. File name is the key plus ".mp3".
LINES: dict[str, str] = {
    "host_intro": (
        "Welcome to the arcade. [pause] Tonight, one of the players "
        "at this cabinet is not a player at all."
    ),
    "host_round": "Four humans. One machine. Thirty seconds.",
    "host_reveal": "Hands off the buttons. [pause] The decoy was...",
    "host_win": "Got it! The machine never stood a chance.",
    "host_lose": "Wrong! [pause] The machine walks free. House wins.",
}

# Prefer config so one env switch (ARCADE_VOICE) drives the whole app.
TTS_VOICE = getattr(config, "TTS_VOICE", None) or os.environ.get("ARCADE_VOICE", "eve")
TTS_VOICE = str(TTS_VOICE).strip() or "eve"
TTS_LANGUAGE = os.environ.get("ARCADE_VOICE_LANG", "en").strip() or "en"


def mint_token() -> dict[str, Any]:
    """Mint an ephemeral realtime client secret for browser-direct voice.

    Returns the parsed response. Observed shape at build time:
    {"value": "<ephemeral token>", "expires_at": <unix seconds>}.
    The caller hands "value" to the browser and nothing else. Never log
    the value. See REALTIME_NOTES.md for the browser wiring.
    """
    return post_json("/realtime/client_secrets", {}, timeout=15)


def synthesize(text: str) -> bytes:
    """Public TTS entry: Grok Voice (Eve) → mp3 bytes for dynamic lines."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("empty TTS text")
    if len(cleaned) > 400:
        cleaned = cleaned[:400].rsplit(" ", 1)[0] or cleaned[:400]
    return _tts(cleaned)


def _tts(text: str) -> bytes:
    """Render one line to mp3 bytes. The language field is required."""
    body = post_raw(
        "/tts",
        {
            "text": text,
            "voice": TTS_VOICE,
            "language": TTS_LANGUAGE,
            "response_format": "mp3",
        },
        timeout=60,
    )
    # The surface returns raw mp3 bytes today (probe evidence). Tolerate a
    # JSON envelope with base64 audio in case the surface changes.
    if body[:1] in (b"{", b"["):
        try:
            parsed = json.loads(body)
        except ValueError:
            return body
        encoded = parsed.get("audio") or parsed.get("data")
        if isinstance(encoded, str):
            import base64

            return base64.b64decode(encoded)
    return body


def render_host_lines(force: bool = False) -> list[Path]:
    """Render every host line in LINES to web/static-assets/<name>.mp3.

    Existing files are kept unless force is set, so re-runs are free.
    Rendering is a build-time step. It refuses to touch the network unless
    the process was started in live or record mode, which keeps demo mode
    honest about being fully offline.
    """
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, text in LINES.items():
        out_path = ASSETS_DIR / f"{name}.mp3"
        if out_path.exists() and not force:
            written.append(out_path)
            continue
        if not (config.RECORD or config.MODE == "live"):
            raise RuntimeError(
                f"{out_path.name} is missing and this is demo mode. "
                "Run: ARCADE_MODE=live ARCADE_RECORD=1 python3 services/voice_host.py"
            )
        audio = _tts(text)
        out_path.write_bytes(audio)
        written.append(out_path)
        print(f"rendered {out_path.name}: {len(audio)} bytes")
    return written


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "mint":
        token = mint_token()
        redacted = {
            "keys": sorted(token.keys()),
            "value_present": bool(token.get("value")),
            "expires_at": token.get("expires_at"),
        }
        print(json.dumps(redacted, indent=1))
    else:
        paths = render_host_lines()
        for path in paths:
            print(f"{path.relative_to(REPO_ROOT)}: {path.stat().st_size} bytes")
