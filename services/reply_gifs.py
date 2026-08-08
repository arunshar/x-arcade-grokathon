"""GIF-format Decoy rounds: human reaction GIFs + one Grok Imagine looping video.

Game contract
-------------
All five reply cards show looping motion media during guessing so the decoy
does not leak by having unique chrome.

  • 4 human replies  → real reaction GIFs from the local pool
    (``web/static-assets/reply-gifs/*.gif``), assigned by round seed + slot.
  • 1 decoy (Grok)   → short square video from grok-imagine-video, served as a
    muted autoplay loop (the "Imagine gif").

Media fields on each reply:
  media_url     static path e.g. /static-assets/reply-gifs/wow.gif
  media_type    "gif" | "video"
  media_status  "ready" | "pending" | "failed" | "none"
  media_source  "human" | "imagine"   (stripped during guessing)

CLI (live, records fixtures when ARCADE_RECORD=1):
    ARCADE_MODE=live python3 services/reply_gifs.py --all
    ARCADE_MODE=live python3 services/reply_gifs.py --round-id decoy-xxx --force
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402

GIF_DIR = REPO_ROOT / "web" / "static-assets" / "reply-gifs"
DECOY_DIR = GIF_DIR / "decoy"
ROUNDS_DIR = REPO_ROOT / "cartridges" / "decoy" / "rounds"

# Keyword → preferred gif stems (pool still falls back to full set).
_KEYWORD_STEMS: list[tuple[tuple[str, ...], str]] = [
    (("lol", "lmao", "haha", "laugh", "funny"), "laugh"),
    (("wow", "insane", "crazy", "wtf", "whoa"), "wow"),
    (("yes", "agree", "this", "facts", "true"), "yes"),
    (("no", "nope", "nah", "wrong"), "nope"),
    (("think", "maybe", "hmm", "idk"), "think"),
    (("cool", "nice", "fire", "lit", "based"), "cool"),
    (("sus", "cap", "fake", "bot"), "sus"),
    (("side", "eye", "look"), "sideeye"),
    (("mind", "blown", "shocked"), "mindblown"),
]


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


def list_human_gifs() -> list[Path]:
    """Available human reaction GIFs (excludes decoy/ subdir)."""
    if not GIF_DIR.is_dir():
        return []
    gifs = [
        p
        for p in sorted(GIF_DIR.glob("*.gif"))
        if p.is_file() and p.stat().st_size > 800
    ]
    # Prefer smaller files first for phone demos, but keep full pool.
    gifs.sort(key=lambda p: (p.stat().st_size, p.name))
    return gifs


def _gif_public_url(path: Path) -> str:
    return "/static-assets/reply-gifs/" + path.name


def _decoy_video_path(round_id: str) -> Path:
    return DECOY_DIR / f"{_slug(round_id)}_decoy.mp4"


def existing_decoy_media_url_for_round(
    round_data: dict[str, Any],
    *,
    allow_probe: bool = True,
) -> tuple[str, str] | None:
    """Return (url, media_type) for decoy Imagine media on disk.

    Only **unique** per-round Imagine clips count as owned media. Files that
    are byte-identical to ``_probe.mp4`` are placeholders and are ignored
    (so live mode regenerates a fresh clip every round id).
    """
    from services.imagine_agent import is_placeholder_decoy, is_real_decoy_media

    rid_slug = _slug(str(round_data.get("round_id") or "round"))
    # Only motion media from the decoy dir counts. Still round-art JPGs are
    # not unique "gifs" and must not block Imagine video generation.
    candidates: list[tuple[Path, str]] = [
        (DECOY_DIR / f"{rid_slug}_decoy.mp4", "video"),
        (DECOY_DIR / f"{rid_slug}_decoy.webm", "video"),
        (DECOY_DIR / f"{rid_slug}_decoy.gif", "gif"),
    ]
    for path, mtype in candidates:
        if not path.is_file() or path.stat().st_size < 800:
            continue
        if is_placeholder_decoy(path):
            continue
        if mtype == "video" and not is_real_decoy_media(path):
            continue
        rel = path.relative_to(REPO_ROOT / "web")
        return "/" + str(rel).replace("\\", "/"), mtype
    if allow_probe:
        probe = DECOY_DIR / "_probe.mp4"
        if probe.is_file() and probe.stat().st_size > 800:
            return "/static-assets/reply-gifs/decoy/_probe.mp4", "video"
    return None

def _pick_human_gif(round_id: str, slot: int, text: str, used: set[str]) -> Path | None:
    pool = list_human_gifs()
    if not pool:
        return None
    by_stem = {p.stem.lower(): p for p in pool}
    text_l = (text or "").lower()
    preferred: list[Path] = []
    for keys, stem in _KEYWORD_STEMS:
        if any(k in text_l for k in keys) and stem in by_stem:
            preferred.append(by_stem[stem])
    # Deterministic rotation from round+slot, skipping already-used stems.
    seed = f"{round_id}:{slot}:{text}"
    digest = hashlib.sha256(seed.encode()).hexdigest()
    start = int(digest[:8], 16)
    ordered = preferred + [p for p in pool if p not in preferred]
    # Rotate full pool from start index for variety when no keyword hit.
    if not preferred:
        ordered = pool[start % len(pool) :] + pool[: start % len(pool)]
    for path in ordered:
        if path.stem not in used:
            used.add(path.stem)
            return path
    # All used — allow reuse.
    return ordered[0] if ordered else None


def resolve_round_format(
    round_data: dict[str, Any],
    *,
    session_index: int = 0,
) -> str:
    """Pick ``text`` or ``gif`` for this round.

    Priority:
      1. Explicit ``format`` on the round JSON (``text`` | ``gif``)
      2. ``config.GIF_ROUND_MODE``:
         - alternate (default): session_index even → text, odd → gif
         - always_gif / always_text
         - half: stable ~50% from round_id hash
    """
    explicit = str(round_data.get("format") or "").strip().lower()
    if explicit in ("text", "gif"):
        return explicit

    mode = str(getattr(config, "GIF_ROUND_MODE", "alternate") or "alternate").lower()
    if mode in ("always_gif", "gif", "all_gif"):
        return "gif"
    if mode in ("always_text", "text", "all_text", "never_gif"):
        return "text"
    if mode in ("half", "hash", "random"):
        rid = str(round_data.get("round_id") or "")
        seed = str(round_data.get("seed") or "")
        digest = hashlib.sha256(f"{rid}:{seed}".encode()).hexdigest()
        return "gif" if int(digest[:8], 16) % 2 == 0 else "text"

    # alternate (default): classic text first, then gif, then text…
    return "gif" if int(session_index) % 2 == 1 else "text"


def clear_reply_media(round_data: dict[str, Any]) -> dict[str, Any]:
    """Strip GIF/Imagine fields so the round plays as plain text."""
    for rep in round_data.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        for key in (
            "media_url",
            "media_type",
            "media_status",
            "media_source",
            "art_url",
            "art_status",
        ):
            rep.pop(key, None)
    round_data["format"] = "text"
    round_data["decoy_media_status"] = "none"
    round_data["human_media_status"] = "none"
    round_data["reply_art_status"] = "none"
    round_data.pop("decoy_media_placeholder", None)
    round_data.pop("art_url", None)
    round_data["art_status"] = "none"
    return round_data


def prepare_round_presentation(
    round_data: dict[str, Any],
    *,
    session_index: int = 0,
) -> dict[str, Any]:
    """Apply text vs gif presentation for a freshly loaded round."""
    fmt = resolve_round_format(round_data, session_index=session_index)
    if fmt == "gif":
        return attach_reply_media(round_data)
    return clear_reply_media(round_data)


def attach_reply_media(round_data: dict[str, Any]) -> dict[str, Any]:
    """Stamp media_url onto every reply. Mutates round_data.

    Humans get GIFs immediately. Decoy gets existing Imagine video/gif if
    present, else pending (live will generate). Only call for GIF-format rounds.
    """
    rid = str(round_data.get("round_id") or "round")
    decoy = _decoy_slot(round_data)
    used: set[str] = set()
    human_ready = 0
    human_total = 0

    # Prefer a true unique Imagine file. Probe is only a temporary stand-in
    # while live generation runs — never mark probe clones as ready.
    decoy_own = existing_decoy_media_url_for_round(round_data, allow_probe=False)
    decoy_media = decoy_own or existing_decoy_media_url_for_round(round_data, allow_probe=True)
    has_own_decoy = decoy_own is not None
    # Probe URL alone is not "ready" in live mode (would repeat every round).
    using_probe_only = (
        not has_own_decoy
        and decoy_media is not None
        and " /static-assets/reply-gifs/decoy/_probe.mp4" in f" {decoy_media[0]}"
    ) or (
        not has_own_decoy
        and decoy_media is not None
        and str(decoy_media[0]).endswith("/_probe.mp4")
    )

    for rep in round_data.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            slot = int(rep.get("slot"))
        except (TypeError, ValueError):
            continue
        is_decoy = decoy is not None and slot == decoy
        if is_decoy:
            if decoy_media:
                url, mtype = decoy_media
                rep["media_url"] = url
                rep["media_type"] = mtype
                if has_own_decoy:
                    rep["media_status"] = "ready"
                elif config.MODE == "live":
                    # Still show something while Imagine runs, but stay pending
                    # so the server keeps generating a unique clip.
                    rep["media_status"] = "pending"
                else:
                    rep["media_status"] = "ready"
                rep["media_source"] = "imagine"
            else:
                rep.setdefault("media_url", None)
                rep["media_type"] = "video"
                rep["media_status"] = "pending" if config.MODE == "live" else "none"
                rep["media_source"] = "imagine"
            if rep.get("media_url"):
                rep.pop("art_url", None)
            continue

        human_total += 1
        path = _pick_human_gif(rid, slot, str(rep.get("text") or ""), used)
        if path:
            rep["media_url"] = _gif_public_url(path)
            rep["media_type"] = "gif"
            rep["media_status"] = "ready"
            rep["media_source"] = "human"
            human_ready += 1
        else:
            rep.setdefault("media_url", None)
            rep["media_type"] = "gif"
            rep["media_status"] = "none"
            rep["media_source"] = "human"
        rep.pop("art_url", None)
        rep["art_status"] = "none"

    round_data["format"] = "gif"
    if has_own_decoy:
        round_data["decoy_media_status"] = "ready"
    elif decoy_media and config.MODE != "live":
        # Demo: shared probe is acceptable offline.
        round_data["decoy_media_status"] = "ready"
    elif config.MODE == "live":
        # Unique generation still needed (probe stand-in does not count).
        round_data["decoy_media_status"] = "pending"
        if using_probe_only:
            round_data["decoy_media_placeholder"] = True
    else:
        round_data["decoy_media_status"] = "none"
    round_data["human_media_status"] = (
        "ready" if human_total and human_ready == human_total else (
            "partial" if human_ready else "none"
        )
    )
    # Legacy flags
    round_data.pop("art_url", None)
    round_data["art_status"] = "none"
    round_data["reply_art_status"] = round_data["decoy_media_status"]
    return round_data


def make_decoy_imagine_gif(
    round_data: dict[str, Any],
    *,
    force: bool = False,
) -> Path | None:
    """Generate (or reuse) decoy video via the Imagine agent (GIF-matched).

    Delegates to ``services.imagine_agent.generate_matching_decoy`` so the
    robot clip is styled from the human GIFs already on the round.
    """
    from services.imagine_agent import decoy_video_path, generate_matching_decoy

    rid = str(round_data.get("round_id") or "round")
    out = decoy_video_path(rid)
    if not force and out.is_file() and out.stat().st_size > 800:
        return out

    result = generate_matching_decoy(round_data, force=force)
    if out.is_file() and out.stat().st_size > 800:
        return out
    # Agent may have stamped probe path under a different file.
    status = result.get("status")
    if status in ("exists", "demo_probe", "ready", "failed_probe_fallback"):
        path = Path(str(result.get("path") or out))
        if path.is_file():
            return path
        probe = DECOY_DIR / "_probe.mp4"
        if probe.is_file():
            return probe
    return None


def generate_decoy_media(round_data: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Ensure decoy has Imagine media matched to this round's human GIFs."""
    attach_reply_media(round_data)
    decoy = _decoy_slot(round_data)
    if decoy is None:
        return round_data

    try:
        from services.imagine_agent import generate_matching_decoy

        result = generate_matching_decoy(round_data, force=force)
    except Exception as exc:
        print(f"decoy imagine gif failed: {exc}", file=sys.stderr)
        result = {"status": "failed", "error": str(exc)}

    status = str(result.get("status") or "")
    if status in ("ready", "exists", "demo_probe", "failed_probe_fallback"):
        # generate_matching_decoy already stamps media_url on success.
        if round_data.get("decoy_media_status") != "ready":
            path = make_decoy_imagine_gif(round_data, force=False)
            if path and path.is_file():
                rel = path.relative_to(REPO_ROOT / "web")
                url = "/" + str(rel).replace("\\", "/")
                mtype = "video" if path.suffix.lower() in (".mp4", ".webm") else "gif"
                for rep in round_data.get("replies") or []:
                    if not isinstance(rep, dict):
                        continue
                    try:
                        if int(rep.get("slot")) != decoy:
                            continue
                    except (TypeError, ValueError):
                        continue
                    rep["media_url"] = url
                    rep["media_type"] = mtype
                    rep["media_status"] = "ready"
                    rep["media_source"] = "imagine"
                    break
                round_data["decoy_media_status"] = "ready"
                round_data["reply_art_status"] = "ready"
        return round_data

    for rep in round_data.get("replies") or []:
        if isinstance(rep, dict):
            try:
                if int(rep.get("slot")) == decoy:
                    rep["media_status"] = "failed"
            except (TypeError, ValueError):
                pass
    round_data["decoy_media_status"] = "failed"
    return round_data


def _load_rounds() -> list[dict[str, Any]]:
    rounds: list[dict[str, Any]] = []
    for path in sorted(ROUNDS_DIR.glob("decoy_*.json")):
        try:
            rounds.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
    return rounds


def main() -> None:
    parser = argparse.ArgumentParser(description="Attach human GIFs + generate decoy Imagine gif")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--round-id", type=str, default="")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--humans-only", action="store_true", help="Skip Imagine video gen")
    args = parser.parse_args()

    rounds = _load_rounds()
    if args.round_id:
        rounds = [r for r in rounds if r.get("round_id") == args.round_id]
        if not rounds:
            sys.exit(f"no round {args.round_id!r}")
    elif not args.all:
        rounds = rounds[:1]

    for rnd in rounds:
        rid = rnd.get("round_id")
        print(f"== {rid} decoy={_decoy_slot(rnd)} ==")
        attach_reply_media(rnd)
        for rep in rnd.get("replies") or []:
            print(
                f"  slot {rep.get('slot')} src={rep.get('media_source')} "
                f"{rep.get('media_type')} {rep.get('media_status')} {rep.get('media_url')}"
            )
        if not args.humans_only:
            generate_decoy_media(rnd, force=args.force)
            print(f"  decoy_media_status={rnd.get('decoy_media_status')}")


if __name__ == "__main__":
    main()
