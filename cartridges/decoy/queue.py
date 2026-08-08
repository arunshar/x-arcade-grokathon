"""Deterministic queue over the committed demo rounds.

Rounds live as JSON files in cartridges/decoy/rounds/. next_round() cycles
through them in sorted filename order, so every demo run sees the same
sequence. Rounds are pre-built by round_builder.py, never fetched inline,
because x_search latency is far too high for a request path.
"""

from __future__ import annotations

import json
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


def next_round() -> dict[str, Any]:
    """Return the next round, cycling in sorted filename order."""
    global _index
    paths = _load_paths()
    path = paths[_index % len(paths)]
    _index += 1
    return json.loads(path.read_text(encoding="utf-8"))


def reset() -> None:
    """Restart the cycle from the first round."""
    global _index, _paths
    _index = 0
    _paths = []
