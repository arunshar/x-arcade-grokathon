"""Deterministic queue over the committed demo rounds.

Rounds live as JSON files in cartridges/decoy/rounds/. next_round() cycles
through them in sorted filename order, so every demo run sees the same
sequence. Rounds are pre-built by round_builder.py, never fetched inline,
because x_search latency is far too high for a request path.

Before a round is served, ``randomize_decoy_position`` reshuffles reply order
so the Grok decoy is not stuck on the same slot (many committed files used
slot 2 / "REPLY 3").
"""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

ROUNDS_DIR = Path(__file__).resolve().parent / "rounds"

_paths: list[Path] = []
_index: int = 0


def _load_paths() -> list[Path]:
    global _paths
    if not _paths:
        _paths = sorted(p for p in ROUNDS_DIR.glob("*.json"))
        if not _paths:
            raise FileNotFoundError(
                f"no round files in {ROUNDS_DIR}. Build them with round_builder.py"
            )
    return _paths


def round_count() -> int:
    """How many committed rounds are available."""
    return len(_load_paths())


def randomize_decoy_position(
    round_data: dict[str, Any],
    *,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Shuffle replies in place and update ``decoy_slot`` to match.

    Uses a CSPRNG by default so each play of the same round can put the
    robot on a different card (REPLY 1–5), not always the third option.
    """
    replies = [r for r in (round_data.get("replies") or []) if isinstance(r, dict)]
    if len(replies) < 2:
        return round_data

    # Identify the decoy before shuffle (flag, author, or recorded slot).
    decoy_rep: dict[str, Any] | None = None
    try:
        recorded = int(round_data.get("decoy_slot"))
    except (TypeError, ValueError):
        recorded = None
    for rep in replies:
        if rep.get("is_decoy") or rep.get("author") == "decoy":
            decoy_rep = rep
            break
        try:
            if recorded is not None and int(rep.get("slot")) == recorded:
                decoy_rep = rep
                break
        except (TypeError, ValueError):
            continue
    if decoy_rep is None:
        return round_data

    mixer = rng if rng is not None else random.SystemRandom()
    mixer.shuffle(replies)

    new_decoy_slot = 0
    for i, rep in enumerate(replies):
        rep["slot"] = i
        is_decoy = rep is decoy_rep or (
            rep.get("is_decoy") is True and decoy_rep.get("text") == rep.get("text")
        )
        # Prefer identity match after shuffle.
        if rep is decoy_rep:
            is_decoy = True
        if is_decoy and rep is decoy_rep:
            rep["is_decoy"] = True
            if not rep.get("author") or rep.get("author") == "decoy":
                rep["author"] = "decoy"
            new_decoy_slot = i
        else:
            # Only one decoy.
            if rep is decoy_rep:
                rep["is_decoy"] = True
                new_decoy_slot = i
            else:
                rep["is_decoy"] = False

    # Final pass: mark only decoy_rep as decoy.
    for i, rep in enumerate(replies):
        rep["slot"] = i
        if rep is decoy_rep:
            rep["is_decoy"] = True
            new_decoy_slot = i
        else:
            rep["is_decoy"] = False

    round_data["replies"] = replies
    round_data["decoy_slot"] = new_decoy_slot
    return round_data


def next_round() -> dict[str, Any]:
    """Return the next round, cycling in sorted filename order.

    Reply order is randomized so the decoy slot is not predictable.
    """
    global _index
    paths = _load_paths()
    path = paths[_index % len(paths)]
    _index += 1
    rnd = json.loads(path.read_text(encoding="utf-8"))
    # Deep copy so in-memory mutation never poisons a later load of the same file.
    rnd = copy.deepcopy(rnd)
    return randomize_decoy_position(rnd)


def reset() -> None:
    """Restart the cycle from the first round."""
    global _index, _paths
    _index = 0
    _paths = []
