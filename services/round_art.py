"""Decoy reply art via Grok Imagine.

Only the decoy (Grok-written) reply gets an Imagine image. The other four
replies are real human text and stay image-free.

Art is generated during the round but must not be shown until reveal — an
image on only one card would leak which reply is the robot.

Same mode contract as card_forge:
  demo              → replay fixtures / committed files only
  live + RECORD=1   → call API and write fixtures
  live, no RECORD   → call API directly

CLI:
    ARCADE_MODE=live ARCADE_RECORD=1 python3 services/round_art.py --all
    ARCADE_MODE=live ARCADE_RECORD=1 python3 services/round_art.py --round-id decoy-xxx
"""

from __future__ import annotations

import argparse
import base64
import json
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

ART_DIR = REPO_ROOT / "web" / "static-assets" / "round-art"
ROUNDS_DIR = REPO_ROOT / "cartridges" / "decoy" / "rounds"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "topic"


def _snippet(text: str, limit: int = 100) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rsplit(" ", 1)[0] + "…"


def _decoy_slot(round_data: dict[str, Any]) -> int | None:
    try:
        return int(round_data.get("decoy_slot"))
    except (TypeError, ValueError):
        pass
    for rep in round_data.get("replies") or []:
        if isinstance(rep, dict) and (rep.get("is_decoy") or rep.get("author") == "decoy"):
            try:
                return int(rep.get("slot"))
            except (TypeError, ValueError):
                continue
    return None


def _decoy_reply(round_data: dict[str, Any]) -> dict[str, Any] | None:
    slot = _decoy_slot(round_data)
    if slot is None:
        return None
    for rep in round_data.get("replies") or []:
        if isinstance(rep, dict):
            try:
                if int(rep.get("slot")) == slot:
                    return rep
            except (TypeError, ValueError):
                continue
    return None


def _decoy_art_prompt(topic: str, reply_text: str) -> str:
    vibe = _snippet(reply_text, 90)
    return (
        "Square retro arcade card art, neon cyan and magenta on deep black, "
        "scanlines, bold graphic poster style. "
        f"Theme backdrop: {topic}. "
        "This is the single AI decoy reply card — abstract iconography capturing "
        f"the mood of this chat reply (do not render long readable text): {vibe}. "
        "Stylized symbols only. No real people, no faces, no celebrity likeness, "
        "no brand logos, no X logo, no watermarks, no UI chrome, no 'decoy' or 'robot' labels."
    )


def _make_store() -> FixtureStore | None:
    if config.MODE == "live" and not config.RECORD:
        return None
    return FixtureStore(
        root=REPO_ROOT / "fixtures" / "api",
        record=config.MODE == "live" and config.RECORD,
        reuse_existing=os.environ.get("ARCADE_REUSE_FIXTURES", "1") == "1",
    )


def _image_gen(request: dict[str, Any]) -> dict[str, Any]:
    store = _make_store()
    if store is None:
        return post_json("/images/generations", request, timeout=120)
    return store.call(
        "image_gen",
        request,
        invoke=lambda: post_json("/images/generations", request, timeout=120),
    )


def reply_art_path(round_id: str, slot: int) -> Path:
    rid = _slug(str(round_id or "round"))
    return ART_DIR / f"{rid}_r{int(slot)}.jpg"


def existing_reply_art_url(round_id: str, slot: int) -> str | None:
    base = reply_art_path(round_id, slot)
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        path = base.with_suffix(ext)
        if path.is_file() and path.stat().st_size > 800:
            return "/static-assets/round-art/" + path.name
    return None


def art_url_for_path(path: Path) -> str:
    return "/static-assets/round-art/" + path.name


def make_decoy_art(
    round_data: dict[str, Any],
    *,
    force: bool = False,
) -> Path | None:
    """Generate or reuse Imagine art for the decoy slot only."""
    slot = _decoy_slot(round_data)
    if slot is None:
        return None
    rid = str(round_data.get("round_id") or "round")
    if not force:
        existing = existing_reply_art_url(rid, slot)
        if existing:
            return REPO_ROOT / "web" / existing.lstrip("/")

    rep = _decoy_reply(round_data)
    topic = str((round_data.get("source") or {}).get("topic") or "arcade")
    text = str((rep or {}).get("text") or topic)
    request = {
        "model": config.MODEL_IMAGE,
        "prompt": _decoy_art_prompt(topic, text),
        "n": 1,
        "response_format": "b64_json",
    }
    response = _image_gen(request)
    raw = base64.b64decode(response["data"][0]["b64_json"])
    extension = "png" if raw[:4] == b"\x89PNG" else "jpg"
    ART_DIR.mkdir(parents=True, exist_ok=True)
    out_path = reply_art_path(rid, slot).with_suffix("." + extension)
    out_path.write_bytes(raw)
    return out_path


# Back-compat alias used by older call sites.
def make_reply_art(
    round_data: dict[str, Any],
    slot: int,
    reply_text: str,
    *,
    force: bool = False,
) -> Path:
    """Deprecated: only the decoy slot is generated. Prefer make_decoy_art."""
    decoy = _decoy_slot(round_data)
    if decoy is None or int(slot) != int(decoy):
        raise ValueError(f"only decoy slot gets art (wanted {slot}, decoy={decoy})")
    path = make_decoy_art(round_data, force=force)
    if path is None:
        raise RuntimeError("decoy art failed")
    return path


def _clear_human_reply_art(round_data: dict[str, Any]) -> None:
    """Ensure non-decoy replies never carry Imagine art."""
    decoy = _decoy_slot(round_data)
    for rep in round_data.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            slot = int(rep.get("slot"))
        except (TypeError, ValueError):
            continue
        if decoy is not None and slot == decoy:
            continue
        rep.pop("art_url", None)
        rep["art_status"] = "none"


def attach_existing_reply_art(round_data: dict[str, Any]) -> dict[str, Any]:
    """Stamp art_url onto the decoy reply only when the file exists. Mutates."""
    rid = str(round_data.get("round_id") or "")
    decoy = _decoy_slot(round_data)
    _clear_human_reply_art(round_data)

    if decoy is None:
        round_data["reply_art_status"] = "none"
        round_data.pop("art_url", None)
        round_data["art_status"] = "none"
        return round_data

    url = existing_reply_art_url(rid, decoy)
    for rep in round_data.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            if int(rep.get("slot")) != decoy:
                continue
        except (TypeError, ValueError):
            continue
        if url:
            rep["art_url"] = url
            rep["art_status"] = "ready"
            round_data["reply_art_status"] = "ready"
        else:
            rep.setdefault("art_url", None)
            rep["art_status"] = "pending" if config.MODE == "live" else "none"
            round_data["reply_art_status"] = "pending" if config.MODE == "live" else "none"
        break
    else:
        round_data["reply_art_status"] = "none"

    # Clear legacy post-level art.
    round_data.pop("art_url", None)
    round_data["art_status"] = "none"
    return round_data


def generate_decoy_art(
    round_data: dict[str, Any],
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Generate the single decoy Imagine image. Mutates round_data."""
    decoy = _decoy_slot(round_data)
    _clear_human_reply_art(round_data)
    if decoy is None:
        round_data["reply_art_status"] = "none"
        round_data.pop("art_url", None)
        round_data["art_status"] = "none"
        return round_data

    rid = str(round_data.get("round_id") or "round")
    try:
        path = make_decoy_art(round_data, force=force)
        url = art_url_for_path(path) if path else None
    except Exception as exc:
        print(f"decoy art {rid} slot {decoy} failed: {exc}", file=sys.stderr)
        url = None

    for rep in round_data.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            if int(rep.get("slot")) != decoy:
                continue
        except (TypeError, ValueError):
            continue
        if url:
            rep["art_url"] = url
            rep["art_status"] = "ready"
            round_data["reply_art_status"] = "ready"
        else:
            rep["art_status"] = "failed"
            rep.setdefault("art_url", None)
            round_data["reply_art_status"] = "failed"
        break

    round_data.pop("art_url", None)
    round_data["art_status"] = "none"
    return round_data


# Alias for CLI / callers that used the multi-slot name.
def generate_all_reply_art(
    round_data: dict[str, Any],
    *,
    force: bool = False,
    on_slot: Any = None,
) -> dict[str, Any]:
    """Generate the one decoy image. Optional on_slot(slot, url) after success."""
    before = None
    decoy = _decoy_slot(round_data)
    generate_decoy_art(round_data, force=force)
    if decoy is not None:
        for rep in round_data.get("replies") or []:
            if isinstance(rep, dict):
                try:
                    if int(rep.get("slot")) == decoy and rep.get("art_url"):
                        before = rep["art_url"]
                        if callable(on_slot):
                            on_slot(decoy, before)
                except (TypeError, ValueError):
                    pass
    return round_data


# --- legacy helpers ---
def art_path_for_round(round_data: dict[str, Any]) -> Path:
    rid = str(round_data.get("round_id") or "round")
    topic = _slug(str((round_data.get("source") or {}).get("topic") or "theme"))
    return ART_DIR / f"{rid}_{topic}.jpg"


def existing_art_url(round_data: dict[str, Any]) -> str | None:
    """Deprecated post-level art; prefer decoy reply art."""
    return None


def _load_rounds() -> list[dict[str, Any]]:
    rounds: list[dict[str, Any]] = []
    for path in sorted(ROUNDS_DIR.glob("decoy_*.json")):
        try:
            rounds.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
    return rounds


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Grok Imagine art for the decoy reply only"
    )
    parser.add_argument("--all", action="store_true", help="Every round file")
    parser.add_argument("--round-id", type=str, default="", help="Single round_id")
    parser.add_argument("--force", action="store_true", help="Regenerate existing files")
    args = parser.parse_args()

    rounds = _load_rounds()
    if args.round_id:
        rounds = [r for r in rounds if r.get("round_id") == args.round_id]
        if not rounds:
            sys.exit(f"no round with id {args.round_id!r}")
    elif not args.all:
        rounds = rounds[:1]

    for rnd in rounds:
        rid = rnd.get("round_id")
        decoy = _decoy_slot(rnd)
        print(f"== {rid} decoy_slot={decoy} ==")
        generate_all_reply_art(
            rnd,
            force=args.force,
            on_slot=lambda slot, url: print(f"  decoy slot {slot}: {url}"),
        )
        print(f"  status={rnd.get('reply_art_status')}")


if __name__ == "__main__":
    main()
