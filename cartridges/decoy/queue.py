"""Diverse queue over the committed demo rounds.

Rounds live as JSON files in cartridges/decoy/rounds/. The queue:

  • Loads every ``decoy_*.json`` file (multiple per topic are fine).
  • Serves in a **shuffled** order (not fixed filename order).
  • Reshuffles when a full cycle completes.
  • Can skip recently-played ``round_id``s so a match does not re-show
    the same post/replies.

Rounds are pre-built by round_builder.py, never fetched inline, because
x_search latency is far too high for a request path.

Before a round is served, ``randomize_decoy_position`` reshuffles reply order
so the Grok decoy is not stuck on the same slot.
"""

from __future__ import annotations

import copy
import json
import os
import random
import time
from pathlib import Path
from typing import Any

ROUNDS_DIR = Path(__file__).resolve().parent / "rounds"

_paths: list[Path] = []
_order: list[int] = []
_cursor: int = 0
_rng = random.SystemRandom()


def _load_paths() -> list[Path]:
    global _paths
    # Re-scan when new files appear (live builder / deploy without restart).
    found = sorted(p for p in ROUNDS_DIR.glob("decoy_*.json") if p.is_file())
    if not found:
        found = sorted(p for p in ROUNDS_DIR.glob("*.json") if p.is_file())
    if not found:
        raise FileNotFoundError(
            f"no round files in {ROUNDS_DIR}. Build them with round_builder.py"
        )
    if [str(p) for p in found] != [str(p) for p in _paths]:
        _paths = found
        _reshuffle()
    return _paths


def _reshuffle() -> None:
    """New random serve order over the current path list."""
    global _order, _cursor
    n = len(_paths)
    _order = list(range(n))
    if os.environ.get("ARCADE_NO_SHUFFLE") == "1":
        # Deterministic for check suites: sorted filename order.
        _order = list(range(n))
    else:
        _rng.shuffle(_order)
    _cursor = 0


def round_count() -> int:
    """How many committed rounds are available."""
    return len(_load_paths())


def list_round_ids() -> list[str]:
    """All round_ids currently on disk (for diagnostics)."""
    ids: list[str] = []
    for path in _load_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            rid = str(data.get("round_id") or path.stem)
            ids.append(rid)
        except (OSError, json.JSONDecodeError):
            ids.append(path.stem)
    return ids


def randomize_decoy_position(
    round_data: dict[str, Any],
    *,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """Shuffle replies in place and update ``decoy_slot`` to match.

    Uses a CSPRNG by default so each play of the same round can put the
    robot on a different card (REPLY 1–5), not always the third option.
    """
    # Deterministic serving for the check suites, which assert against the
    # committed round files. Play keeps the shuffle.
    if os.environ.get("ARCADE_NO_SHUFFLE") == "1":
        return round_data

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
    # Final pass: mark only decoy_rep as decoy.
    for i, rep in enumerate(replies):
        rep["slot"] = i
        if rep is decoy_rep:
            rep["is_decoy"] = True
            if not rep.get("author") or rep.get("author") == "decoy":
                rep["author"] = "decoy"
            new_decoy_slot = i
        else:
            rep["is_decoy"] = False

    round_data["replies"] = replies
    round_data["decoy_slot"] = new_decoy_slot
    return round_data


def _read_round(path: Path) -> dict[str, Any]:
    rnd = json.loads(path.read_text(encoding="utf-8"))
    rnd = copy.deepcopy(rnd)
    return randomize_decoy_position(rnd)


def next_round(
    *,
    exclude_ids: set[str] | frozenset[str] | None = None,
    prefer_fresh: bool = True,
) -> dict[str, Any]:
    """Return the next round from a shuffled cycle.

    ``exclude_ids`` — skip these ``round_id``s when alternatives exist (used
    so one match does not repeat the same post). If every round is excluded,
    falls back to the least-recently-served option rather than failing.
    """
    global _cursor
    paths = _load_paths()
    n = len(paths)
    if n == 0:
        raise FileNotFoundError("empty rounds dir")

    if not _order or len(_order) != n:
        _reshuffle()

    banned = {str(x) for x in (exclude_ids or ()) if x}
    # Two passes: first honor exclusions; second ignore if pool exhausted.
    for honor_ban in (True, False):
        tried = 0
        while tried < n:
            if _cursor >= n:
                _reshuffle()
            idx = _order[_cursor % n]
            _cursor += 1
            tried += 1
            path = paths[idx % n]
            try:
                rnd = _read_round(path)
            except (OSError, json.JSONDecodeError):
                continue
            rid = str(rnd.get("round_id") or path.stem)
            if honor_ban and banned and rid in banned:
                continue
            # Light jitter: sometimes skip to next so consecutive matches
            # starting at the same wall-clock don't feel locked in.
            if prefer_fresh and not banned and n > 3 and (_rng.random() < 0.08):
                continue
            return rnd

    # Absolute last resort — first readable file.
    for path in paths:
        try:
            return _read_round(path)
        except (OSError, json.JSONDecodeError):
            continue
    raise FileNotFoundError("no readable round files")


def reset() -> None:
    """Restart the cycle and force a path rescan."""
    global _paths, _order, _cursor
    _paths = []
    _order = []
    _cursor = 0
    # Touch mtime awareness
    _ = time.time()
