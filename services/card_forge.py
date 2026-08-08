"""Share card generation for X Arcade.

make_share_card() turns a finished round into a retro arcade wanted poster
via /v1/images/generations. Every call goes through the content-addressed
fixture store, so demo mode replays the committed fixture with zero network
and record mode refreshes it. Probed 7 Aug: 6.5s per image, live-safe.

Build-time record (writes the committed demo card and its fixture):
    ARCADE_MODE=live ARCADE_RECORD=1 python3 services/card_forge.py

Demo replay check (no network, must still produce the png):
    ARCADE_MODE=demo python3 services/card_forge.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
from fixtures_core import FixtureStore  # noqa: E402

CARDS_DIR = REPO_ROOT / "web" / "static-assets" / "cards"

# fixtures_core reads ADJ_* env vars by default. X Arcade uses ARCADE_RECORD,
# so both switches are passed explicitly and the ADJ_* names never matter here.
# reuse_existing keeps repeat record runs from paying for the same image twice.
# Delete the fixture file under fixtures/api/image_gen/ to force a re-render.
STORE = FixtureStore(
    root=REPO_ROOT / "fixtures" / "api",
    record=config.RECORD,
    reuse_existing=os.environ.get("ARCADE_REUSE_FIXTURES", "1") == "1",
)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "player"


def _card_prompt(topic: str, winner: str) -> str:
    """Tight template. No real people, no logos, ever."""
    return (
        "Retro arcade wanted poster, neon on black, halftone print texture, "
        "scanlines. Huge DECOY branding across the top in chunky pixel type. "
        f"Theme of the round: {topic}. "
        f'Banner near the bottom reads "{winner} SPOTTED THE DECOY". '
        "Center art: one cartoon robot trying to blend into a lineup of four "
        "faceless human silhouettes, spotlight on the robot. "
        "No real people, no celebrity likeness, no brand logos, no X logo."
    )


def _post_json(path: str, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    key = os.environ.get("XAI_API_KEY", "")
    if not key:
        raise RuntimeError("XAI_API_KEY is not set")
    request = urllib.request.Request(
        config.API_BASE + path,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def make_share_card(round_data: dict[str, Any], winner: str) -> Path:
    """Generate the share card for a finished round and return its image path.

    Reads only round_id and source.topic from the round. The winner is the
    display name of whoever guessed the decoy first, or "The House" when
    nobody did. Identical inputs hash to the same fixture, so demo mode
    replays the committed card byte for byte. The extension follows the real
    bytes the API returned, jpg today, because b64_json does not promise png.
    """
    topic = round_data["source"]["topic"]
    request = {
        "model": config.MODEL_IMAGE,
        "prompt": _card_prompt(topic, winner),
        "n": 1,
        "response_format": "b64_json",
    }
    response = STORE.call(
        "image_gen",
        request,
        invoke=lambda: _post_json("/images/generations", request, timeout=120),
    )
    raw = base64.b64decode(response["data"][0]["b64_json"])
    extension = "png" if raw[:4] == b"\x89PNG" else "jpg"
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CARDS_DIR / f"{round_data['round_id']}_{_slug(winner)}.{extension}"
    out_path.write_bytes(raw)
    return out_path


# Demo round for the committed asset. The card path reads only round_id and
# source.topic, so replies are omitted here. The source post is the real one
# the x_search probe surfaced on 7 Aug (see artifacts/probes/x_search.json).
DEMO_ROUND: dict[str, Any] = {
    "round_id": "decoy-" + hashlib.sha256(b"2085772302130753606:xarcade").hexdigest()[:12],
    "source": {
        "post_text": (
            "Paul W.S. Anderson joins the Higgsfield Global Film Festival "
            "as a jury member."
        ),
        "post_author": "@higgsfield_ai",
        "post_url": "https://x.com/higgsfield_ai/status/2085772302130753606",
        "topic": "ai",
    },
    "decoy_slot": 2,
    "seed": 1234567,
}

DEMO_WINNER = "PLAYER1"


if __name__ == "__main__":
    path = make_share_card(DEMO_ROUND, DEMO_WINNER)
    print(f"{path.relative_to(REPO_ROOT)}: {path.stat().st_size} bytes")
