"""STAGED share-card poster. Nothing here touches the X API yet.

post_to_x() logs exactly what a real post would contain and returns a staged
permalink so the UI flow can be demoed end to end. Post text carries no URLs
by design, the card image is the payload.

TODO (STAGED, real wiring): create the post with
POST https://api.x.com/2/tweets after uploading the card via the media
upload endpoint and passing its media id in the payload. Requires OAuth
user context for the arcade account. Cost noted at $0.015 per post create
(provider-quoted, unverified, not yet incurred).
Wire it only behind an explicit ARCADE_POST=1 switch, never by default.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def post_to_x(card_path: str | Path, text: str) -> dict[str, Any]:
    """Stage a post. Logs what WOULD be sent and returns a fake permalink."""
    card = Path(card_path)
    size = card.stat().st_size if card.is_file() else 0
    digest = hashlib.sha256(f"{card.name}:{text}".encode()).hexdigest()[:10]
    permalink = f"https://x.com/xarcade/status/staged-{digest}"
    print("STAGED POST (not sent)")
    print(f"  text:  {text}")
    print(f"  media: {card.name} ({size} bytes)")
    print(f"  staged permalink: {permalink}")
    return {"status": "staged", "permalink": permalink, "media": card.name}


if __name__ == "__main__":
    demo_card = (
        Path(__file__).resolve().parents[1]
        / "web"
        / "static-assets"
        / "cards"
        / "demo.png"
    )
    post_to_x(demo_card, "PLAYER1 spotted the decoy. Can you?")
