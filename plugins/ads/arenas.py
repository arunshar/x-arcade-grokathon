"""Sponsored arenas from a small static config.

Brands sponsor arenas on topics they want adjacency to. The game stays
identical. A sponsor only adds a light skin and its mark on the share card.
Every sponsor here is a fictional demo brand and every money field is the
literal string ILLUSTRATIVE, so no invented market figure can ship.
"""

from __future__ import annotations

from typing import Any

_SPONSORED_TOPICS: dict[str, dict[str, Any]] = {
    "ai": {
        "sponsor": "DemoBrand",
        "skin": {"accent": "#f97316"},
        "cpm_note": "ILLUSTRATIVE",
    },
    "space": {
        "sponsor": "OrbitCola",
        "skin": {"accent": "#38bdf8"},
        "cpm_note": "ILLUSTRATIVE",
    },
    "gaming": {
        "sponsor": "PixelPeak",
        "skin": {"accent": "#a78bfa"},
        "cpm_note": "ILLUSTRATIVE",
    },
}


def sponsored_arena(topic: str) -> dict[str, Any] | None:
    """Return sponsor branding for a topic, or None when nobody sponsors it.

    Topics match case insensitively. The result is a copy, so callers can
    decorate it without mutating the config.
    """
    if not isinstance(topic, str):
        return None
    entry = _SPONSORED_TOPICS.get(topic.strip().lower())
    if entry is None:
        return None
    return {**entry, "skin": dict(entry["skin"])}
