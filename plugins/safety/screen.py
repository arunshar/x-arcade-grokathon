"""Deterministic safety gates for Decoy rounds.

Every gate is a plain rule check. No model calls, no network, no randomness,
so screening is instant and gives the same answer every time. The rule is
fail closed. A round that fails any gate is never served (see SAFETY.md).
This is the lineage of Adjacency distilled to its demo relevant core.
"""

from __future__ import annotations

import re
from typing import Any

from config import REPLIES_PER_ROUND

MAX_POST_CHARS = 560
MAX_REPLY_CHARS = 280

# Kept mild on purpose. The demo shows the mechanism. A real deployment swaps
# in a maintained wordlist behind the same gate code.
_DENYLIST = (
    "idiot",
    "moron",
    "imbecile",
    "scumbag",
    "dumbass",
    "jackass",
)
_SLUR_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(word) for word in _DENYLIST) + r")\b",
    re.IGNORECASE,
)

_URL_PATTERN = re.compile(r"https?://\S+|www\.\S+|\bt\.co/\S+", re.IGNORECASE)

_DECOY_MARKERS = ("decoy", "@decoy")


def _replies(round_dict: Any) -> list[dict[str, Any]] | None:
    """Return the reply list, or None when the shape is wrong (fail closed)."""
    if not isinstance(round_dict, dict):
        return None
    replies = round_dict.get("replies")
    if not isinstance(replies, list) or len(replies) != REPLIES_PER_ROUND:
        return None
    if not all(isinstance(reply, dict) for reply in replies):
        return None
    return replies


def _texts(round_dict: Any) -> list[str]:
    """Collect every string we can find so text gates scan all of them."""
    collected: list[str] = []
    if isinstance(round_dict, dict):
        source = round_dict.get("source")
        if isinstance(source, dict) and isinstance(source.get("post_text"), str):
            collected.append(source["post_text"])
        replies = round_dict.get("replies")
        if isinstance(replies, list):
            for reply in replies:
                if isinstance(reply, dict) and isinstance(reply.get("text"), str):
                    collected.append(reply["text"])
    return collected


def _gate_source(round_dict: Any) -> bool:
    """Post text and every reply text present, nonempty, within length bounds."""
    replies = _replies(round_dict)
    if replies is None:
        return False
    source = round_dict.get("source")
    if not isinstance(source, dict):
        return False
    post_text = source.get("post_text")
    if not isinstance(post_text, str) or not post_text.strip():
        return False
    if len(post_text) > MAX_POST_CHARS:
        return False
    for reply in replies:
        text = reply.get("text")
        if not isinstance(text, str) or not text.strip():
            return False
        if len(text) > MAX_REPLY_CHARS:
            return False
    return True


def _gate_slurs(round_dict: Any) -> bool:
    """No denylist hit in the post text or any reply text."""
    return not any(_SLUR_PATTERN.search(text) for text in _texts(round_dict))


def _gate_decoy_count(round_dict: Any) -> bool:
    """Exactly one decoy, and the top level decoy_slot points at it."""
    replies = _replies(round_dict)
    if replies is None:
        return False
    decoys = [reply for reply in replies if reply.get("is_decoy") is True]
    if len(decoys) != 1:
        return False
    return round_dict.get("decoy_slot") == decoys[0].get("slot")


def _gate_author(round_dict: Any) -> bool:
    """Every real reply carries a real handle, never the decoy marker."""
    replies = _replies(round_dict)
    if replies is None:
        return False
    for reply in replies:
        if reply.get("is_decoy") is True:
            continue
        author = reply.get("author")
        if not isinstance(author, str) or len(author) < 2:
            return False
        if not author.startswith("@") or author.lower() in _DECOY_MARKERS:
            return False
    return True


def _gate_url(round_dict: Any) -> bool:
    """No URL inside any reply text. URLs break the game visually."""
    replies = _replies(round_dict)
    if replies is None:
        return False
    for reply in replies:
        text = reply.get("text")
        if isinstance(text, str) and _URL_PATTERN.search(text):
            return False
    return True


_GATES: tuple[tuple[str, Any], ...] = (
    ("G_SOURCE", _gate_source),
    ("G_SLURS", _gate_slurs),
    ("G_DECOY_COUNT", _gate_decoy_count),
    ("G_AUTHOR", _gate_author),
    ("G_URL", _gate_url),
)

GATE_CODES = tuple(code for code, _ in _GATES)


def screen_round(round_dict: dict[str, Any]) -> dict[str, Any]:
    """Run every gate and report the failures.

    Returns {"screened": bool, "gate_codes": [failed codes]}. A round is
    screened only when the failure list is empty. Malformed input fails the
    gates that cannot verify it, which keeps the screen fail closed.
    """
    failed = [code for code, gate in _GATES if not gate(round_dict)]
    return {"screened": not failed, "gate_codes": failed}
