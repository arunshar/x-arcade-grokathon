#!/usr/bin/env python3
"""Pre-render the 12 deck narration clips through Grok Voice TTS.

Writes narration/slide01.mp3 .. narration/slide12.mp3 for the auto-presenter.
Reuses the proven /v1/tts path from services/xai_http (probe: 1.78s per line,
"language" required, raw mp3 bytes back). Long scripts are split at sentence
boundaries into chunks under the 400-char line limit and the mp3 bytes are
concatenated, which players treat as one continuous clip.

Usage:
    XAI_API_KEY=... python3 scripts/make_narration.py            # render all
    XAI_API_KEY=... python3 scripts/make_narration.py --force    # re-render
    python3 scripts/make_narration.py --list                     # no network

Voice: Leo (male, "Authoritative host" in /voices). Override with
ARCADE_VOICE=orion (or any id from /voices) at the command line.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

VOICE = os.environ.get("ARCADE_VOICE", "leo").strip().lower() or "leo"
LANGUAGE = os.environ.get("ARCADE_VOICE_LANG", "en").strip() or "en"
CHUNK_LIMIT = 380  # under the 400-char TTS line cap, split on sentence ends
OUT_DIR = REPO_ROOT / "narration"

# One entry per slide. 1, 2, 3, 6, 11, 12 are the locked spoken track,
# verbatim. The rest use only words and numbers printed on that slide.
# [pause] is a speech tag the TTS surface supports (see services/voice_host).
SCRIPTS: dict[int, str] = {
    1: (
        "X's own head of product says the platform exists to give people a "
        "pulse on humanity, and nothing is more unsettling than machine text "
        "wearing a human face. [pause] That's the problem. We didn't invent "
        "it. X wrote it down."
    ),
    2: (
        "And X is already at war with it. 42,000 chatbot accounts purged. "
        "Apps that pay for slop, banned. [pause] But every one of those is "
        "enforcement after the fact. Nobody has made spotting the machine "
        "something people want to do. And in twenty years, X has never "
        "shipped a game."
    ),
    3: (
        "So we built one. [pause] Decoy. A real trending thread, four real "
        "replies, one written by Grok to blend in. Thirty seconds to find "
        "the machine. A whole room plays at once on their phones."
    ),
    4: (
        "Scan the QR and you are in the game. Room code GROK. [pause] "
        "No host, the session clock runs the room. Thirty seconds on the "
        "clock, six rounds a match."
    ),
    5: (
        "Live thread in, five gates before play. The x_search probe took 42 "
        "seconds. Image gen, 6.5. TTS, 1.78. And a commentator line on the "
        "fast model in 0.6."
    ),
    6: (
        "Here's the whole system on one page. One game server holds the "
        "rooms. The rounds are built offline, because pulling a live thread "
        "takes 42 seconds and that can never sit on a 30-second clock. And "
        "every round passes five safety gates that fail closed before anyone "
        "sees it. That gate is the important part, so hold that thought. "
        "[pause] One detail I'm proud of: during guessing, the answer never "
        "leaves the server. Every reply's media is served through an "
        "identical opaque URL, so you can't even find the machine in dev "
        "tools. The game is only fun if it's actually fair."
    ),
    7: (
        "The hard parts were correctness under load. A grounded two-call "
        "pull that rejects any answer with no search call. Opaque uniform "
        "media, so the decoy cannot be found by inspection. A time-driven "
        "session machine with no host. And a 32-check abuse battery, 32 out "
        "of 32 passing."
    ),
    8: (
        "Everything here broke on build day. Host-driven rooms gave three "
        "dead-button bugs, so we deleted the host. The GIF decoy leaked "
        "three ways, so media serves through one opaque proxy. [pause] And "
        "live pulls kept hitting culture-war threads that pass the gates, so "
        "every fresh round gets a human read."
    ),
    9: (
        "Every guess is a labeled judgment on which reply reads "
        "machine-written. That aggregate signal is what enforcement needs. "
        "[pause] This is a thesis. No flywheel number is measured."
    ),
    10: (
        "One cabinet, many cartridges. A cartridge ships rounds behind one "
        "small contract. The cabinet runs rooms, clocks, scoring, voice, and "
        "share cards. Gates are platform infrastructure. A rejected round is "
        "the screen working."
    ),
    11: (
        "Now the part for the ads team. Those five gates that screen every "
        "round? That's brand safety, built into the core loop, not bolted "
        "on. A sponsor picks a topic, gets an arena, and no brand ever sits "
        "next to a round that failed the screen. [pause] X banned "
        "pay-to-post because it manufactures slop. A sponsored arena aligns "
        "the incentive the other way: brand-safe, human play, by design."
    ),
    12: (
        "Built in a day on the xAI API. Live right now, usage is public and "
        "honest at slash stats. [pause] The ask is small: ship one cartridge "
        "on a real X surface and measure the flywheel. That's it. Go play."
    ),
}


def chunk(text: str) -> list[str]:
    """Split at sentence ends into pieces under CHUNK_LIMIT chars."""
    sentences = re.split(r"(?<=[.?!])\s+", text.strip())
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > CHUNK_LIMIT:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def main() -> int:
    force = "--force" in sys.argv
    list_only = "--list" in sys.argv

    if list_only:
        for n in sorted(SCRIPTS):
            parts = chunk(SCRIPTS[n])
            print(f"slide{n:02d}: {len(SCRIPTS[n])} chars, {len(parts)} chunk(s)")
        return 0

    if not os.environ.get("XAI_API_KEY", "").strip():
        print("XAI_API_KEY is not set.")
        print("Run: XAI_API_KEY=... python3 scripts/make_narration.py")
        return 1

    from services.xai_http import post_raw

    OUT_DIR.mkdir(exist_ok=True)
    for n in sorted(SCRIPTS):
        out = OUT_DIR / f"slide{n:02d}.mp3"
        if out.exists() and not force:
            print(f"{out.name}: exists, skipping (use --force to re-render)")
            continue
        audio = b""
        started = time.monotonic()
        for piece in chunk(SCRIPTS[n]):
            audio += post_raw(
                "/tts",
                {
                    "text": piece,
                    "voice": VOICE,
                    "language": LANGUAGE,
                    "response_format": "mp3",
                },
                timeout=60,
            )
        out.write_bytes(audio)
        print(f"{out.name}: {len(audio)} bytes in {time.monotonic() - started:.1f}s ({VOICE})")
    print(f"done: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
