"""Share card generation for X Arcade.

make_share_card() turns a finished round into a retro arcade wanted poster
via /v1/images/generations (Grok Imagine).

Mode contract (mirrors cartridges/decoy/round_builder.py):
  demo              → replay committed fixtures, zero network
  live + RECORD=1   → call API and write fixtures
  live, no RECORD   → call API directly, persist nothing

Probed at build time: ~6.5s per image, live-safe.

Build-time record (writes the committed demo card and its fixture):
    ARCADE_MODE=live ARCADE_RECORD=1 python3 services/card_forge.py

Demo replay check (no network, must still produce the png):
    ARCADE_MODE=demo python3 services/card_forge.py
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402
from fixtures_core import FixtureStore  # noqa: E402
from services.xai_http import post_json  # noqa: E402

CARDS_DIR = REPO_ROOT / "web" / "static-assets" / "cards"


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


def _make_store() -> FixtureStore | None:
    """Fixture store for demo replay and live+record. None = direct live API."""
    # live without RECORD: product path — hit Imagine, write nothing.
    if config.MODE == "live" and not config.RECORD:
        return None
    return FixtureStore(
        root=REPO_ROOT / "fixtures" / "api",
        # Only write when explicitly recording under live mode.
        record=config.MODE == "live" and config.RECORD,
        # Default on so repeat record runs do not re-bill the same card.
        reuse_existing=os.environ.get("ARCADE_REUSE_FIXTURES", "1") == "1",
    )


def _image_gen(request: dict[str, Any]) -> dict[str, Any]:
    """Call Imagine, via fixtures or direct API depending on mode."""
    store = _make_store()
    if store is None:
        return post_json("/images/generations", request, timeout=120)
    return store.call(
        "image_gen",
        request,
        invoke=lambda: post_json("/images/generations", request, timeout=120),
    )


def _existing_card(round_id: str, winner: str) -> Path | None:
    """Return an on-disk card for this round+winner if already generated."""
    slug = _slug(winner)
    rid = str(round_id or "round")
    for ext in ("jpg", "jpeg", "png", "webp"):
        path = CARDS_DIR / f"{rid}_{slug}.{ext}"
        if path.is_file() and path.stat().st_size > 2000:
            return path
    return None


def _topic_cache_path(topic: str, winner: str, extension: str = "jpg") -> Path:
    return CARDS_DIR / f"topic-{_slug(topic)}_{_slug(winner)}.{extension}"


def find_cached_share_card(round_data: dict[str, Any], winner: str) -> Path | None:
    """Fast path: reuse round-specific or topic+winner card without API."""
    rid = str(round_data.get("round_id") or "")
    hit = _existing_card(rid, winner)
    if hit is not None:
        return hit
    topic = str((round_data.get("source") or {}).get("topic") or "")
    if not topic:
        return None
    slug_w = _slug(winner)
    for ext in ("jpg", "jpeg", "png", "webp"):
        path = _topic_cache_path(topic, winner, ext)
        if path.is_file() and path.stat().st_size > 2000:
            # Copy under this round id so later lookups are O(1) by name.
            if rid:
                dest = CARDS_DIR / f"{rid}_{slug_w}.{ext}"
                try:
                    if not dest.is_file():
                        dest.write_bytes(path.read_bytes())
                    return dest
                except OSError:
                    return path
            return path
    return None


def make_share_card(round_data: dict[str, Any], winner: str) -> Path:
    """Generate the share card for a finished round and return its image path.

    Reads only round_id and source.topic from the round. The winner is the
    display name of whoever guessed the decoy first, or "The House" when
    nobody did. Identical inputs hash to the same fixture, so demo mode
    replays the committed card byte for byte. The extension follows the real
    bytes the API returned, jpg today, because b64_json does not promise png.

    Reuses on-disk cards (per-round or per-topic+winner) so live reveal is
    instant on repeat winners / topics instead of waiting ~6s for Imagine.
    """
    cached = find_cached_share_card(round_data, winner)
    if cached is not None:
        return cached

    topic = str((round_data.get("source") or {}).get("topic") or "arcade")
    rid = str(round_data.get("round_id") or "round")
    request = {
        "model": config.MODEL_IMAGE,
        "prompt": _card_prompt(topic, winner),
        "n": 1,
        "response_format": "b64_json",
    }
    response = _image_gen(request)
    raw = base64.b64decode(response["data"][0]["b64_json"])
    extension = "png" if raw[:4] == b"\x89PNG" else "jpg"
    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CARDS_DIR / f"{rid}_{_slug(winner)}.{extension}"
    out_path.write_bytes(raw)
    # Topic cache for next round on the same theme + winner name.
    try:
        topic_path = _topic_cache_path(topic, winner, extension)
        if not topic_path.is_file():
            topic_path.write_bytes(raw)
    except OSError:
        pass
    return out_path


# Demo round for the committed asset. The card path reads only round_id and
# source.topic, so replies are omitted here. The source post is the real one
# the x_search probe surfaced (see artifacts/probes/x_search.json).
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
