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

# Reply-text vibe → gif stems (scored, not hard-assigned).
_KEYWORD_STEMS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (("lol", "lmao", "haha", "laugh", "funny", "lmfao", "rofl"), ("laugh", "slowclap", "popcorn")),
    (("wow", "insane", "crazy", "wtf", "whoa", "holy", "damn"), ("wow", "mindblown", "micdrop")),
    (("yes", "agree", "this", "facts", "true", "exactly", "real"), ("yes", "salute", "handshake")),
    (("no", "nope", "nah", "wrong", "never", "pass"), ("nope", "eyeroll", "facepalm")),
    (("think", "maybe", "hmm", "idk", "wonder", "perhaps"), ("think", "confused", "nervous")),
    (("cool", "nice", "fire", "lit", "based", "hard", "clean"), ("cool", "micdrop", "salute")),
    (("sus", "cap", "fake", "bot", "lie", "scam"), ("sus", "sideeye", "eyeroll")),
    (("side", "eye", "look", "stare", "watch"), ("sideeye", "eyeroll", "popcorn")),
    (("mind", "blown", "shocked", "unreal", "speechless"), ("mindblown", "wow", "micdrop")),
    (("cry", "sad", "pain", "hurt", "rip", "dead"), ("cry", "facepalm", "nervous")),
    (("money", "cash", "rich", "pay", "price", "cost", "$", "£"), ("money", "dealwithit", "cool")),
    (("angry", "mad", "rage", "hate", "furious"), ("angry", "eyeroll", "nope")),
    (("confus", "what", "huh", "??", "wait"), ("confused", "think", "nervous")),
    (("food", "eat", "pizza", "cake", "cook", "chef", "hungry"), ("chef", "yes", "mindblown")),
    (("sport", "goal", "win", "match", "game", "transfer", "club"), ("cool", "salute", "micdrop")),
    (("music", "song", "album", "guitar", "beat", "track"), ("cool", "micdrop", "yes")),
    (("movie", "film", "actor", "scene", "cast", "show"), ("popcorn", "wow", "mindblown")),
    (("crypto", "bitcoin", "btc", "eth", "token", "chart", "vol"), ("money", "mindblown", "sus")),
    (("ai", "model", "gpt", "claude", "llm", "agent", "code"), ("think", "mindblown", "sus")),
    (("clap", "respect", "legend", "goat", "king"), ("slowclap", "salute", "handshake")),
    (("deal", "done", "locked", "settled"), ("dealwithit", "handshake", "yes")),
    (("nervous", "scared", "worry", "anxious", "uh"), ("nervous", "cry", "confused")),
]

# Topic tag → stems that should surface more often for that post theme.
_TOPIC_STEMS: dict[str, tuple[str, ...]] = {
    "ai": ("think", "mindblown", "sus", "confused", "nervous"),
    "crypto": ("money", "mindblown", "sus", "cool", "nervous"),
    "sports": ("cool", "salute", "micdrop", "yes", "angry"),
    "music": ("cool", "micdrop", "yes", "mindblown", "salute"),
    "movies": ("popcorn", "wow", "mindblown", "cry", "laugh"),
    "food": ("chef", "yes", "mindblown", "angry", "cry"),
    "tech": ("think", "mindblown", "cool", "sus", "confused"),
    "politics": ("sideeye", "eyeroll", "popcorn", "angry", "facepalm"),
    "default": ("wow", "think", "laugh", "cool", "sus"),
}


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


def _is_imagine_video_url(url: str | None) -> bool:
    """True for decoy Imagine videos only (never human pool .gif paths)."""
    if not url:
        return False
    u = str(url).split("?", 1)[0].lower()
    if "/reply-gifs/decoy/" not in u.replace("\\", "/"):
        return False
    if u.rstrip("/").endswith("_probe.mp4"):
        return config.MODE != "live"
    return u.endswith((".mp4", ".webm", ".mov"))


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
    from services.imagine_agent import (
        is_imagine_certified,
        is_placeholder_decoy,
        is_real_decoy_media,
    )

    rid_slug = _slug(str(round_data.get("round_id") or "round"))
    # ONLY Imagine video under decoy/ — never pool .gif, never uncertified files.
    candidates: list[tuple[Path, str]] = [
        (DECOY_DIR / f"{rid_slug}_decoy.mp4", "video"),
        (DECOY_DIR / f"{rid_slug}_decoy.webm", "video"),
    ]
    for path, mtype in candidates:
        if not path.is_file() or path.stat().st_size < 800:
            continue
        if is_placeholder_decoy(path):
            continue
        if not is_real_decoy_media(path):
            continue
        # Live requires Imagine certification sidecar.
        if config.MODE == "live" and not is_imagine_certified(path):
            continue
        rel = path.relative_to(REPO_ROOT / "web")
        return "/" + str(rel).replace("\\", "/"), "video"
    # Probe only for offline demo — and never labeled as certified Imagine.
    if allow_probe and config.MODE != "live":
        probe = DECOY_DIR / "_probe.mp4"
        if probe.is_file() and probe.stat().st_size > 800:
            return "/static-assets/reply-gifs/decoy/_probe.mp4", "video"
    return None

def _seeded_shuffle(items: list[Path], seed: str) -> list[Path]:
    """Deterministic Fisher–Yates so the same round always gets the same order."""
    arr = list(items)
    if len(arr) <= 1:
        return arr
    h = hashlib.sha256(seed.encode("utf-8")).digest()
    # Expand digest into a stream of ints.
    buf = h
    i = 0

    def nxt() -> int:
        nonlocal buf, i
        if i + 4 > len(buf):
            buf = hashlib.sha256(buf).digest()
            i = 0
        val = int.from_bytes(buf[i : i + 4], "big")
        i += 4
        return val

    for idx in range(len(arr) - 1, 0, -1):
        j = nxt() % (idx + 1)
        arr[idx], arr[j] = arr[j], arr[idx]
    return arr


def _post_context(round_data: dict[str, Any]) -> tuple[str, str, str]:
    src = round_data.get("source") or {}
    topic = str(src.get("topic") or "").strip().lower()
    post = str(src.get("post_text") or "").strip()
    author = str(src.get("post_author") or "").strip()
    return topic, post, author


def _score_gif_for_reply(
    stem: str,
    *,
    reply_text: str,
    topic: str,
    post_text: str,
    round_bias: int = 0,
) -> int:
    """Higher = better match. Pure function of content + stem."""
    stem_l = stem.lower()
    reply_l = (reply_text or "").lower()
    post_l = (post_text or "").lower()
    topic_l = (topic or "").lower() or "default"
    score = 0

    for keys, stems in _KEYWORD_STEMS:
        if any(k in reply_l for k in keys) and stem_l in stems:
            # Earlier stems in the tuple are stronger matches.
            try:
                score += 40 - stems.index(stem_l) * 6
            except ValueError:
                score += 24
        if any(k in post_l for k in keys) and stem_l in stems:
            score += 12

    topic_stems = _TOPIC_STEMS.get(topic_l) or _TOPIC_STEMS["default"]
    if stem_l in topic_stems:
        try:
            score += 28 - topic_stems.index(stem_l) * 4
        except ValueError:
            score += 16

    # Light bonus if stem word appears in reply/post literally.
    if stem_l in reply_l:
        score += 18
    if stem_l in post_l:
        score += 8

    # Tiny deterministic jitter so ties break differently per round without
    # collapsing every round onto the same top stems.
    jitter = (round_bias ^ hashlib.sha1(stem_l.encode()).digest()[0]) % 7
    score += jitter
    return score


def assign_human_gifs(
    round_data: dict[str, Any],
    *,
    recent_stems: set[str] | None = None,
    diversity_salt: str = "",
) -> dict[int, Path]:
    """Pick a diverse set of human GIFs for this round.

    Strategy:
      1. Shuffle the pool with round content **plus** a per-match salt so the
         same topic does not always draw the same four files.
      2. Deal a fresh *hand* (subset) of the pool, preferring stems not used
         earlier in this match (``recent_stems``).
      3. Score only within that hand against each reply; greedy unique assign.
    """
    pool = list_human_gifs()
    if not pool:
        return {}

    rid = str(round_data.get("round_id") or "round")
    topic, post, author = _post_context(round_data)
    seed = str(round_data.get("seed") or "")
    salt = str(diversity_salt or "")
    # Salt is required for variety — without it every AI/crypto/… round is fixed.
    shuffle_key = f"{rid}|{topic}|{author}|{post[:120]}|{seed}|{salt}"
    ordered_pool = _seeded_shuffle(pool, shuffle_key)
    round_bias = int(hashlib.sha256(shuffle_key.encode()).hexdigest()[:8], 16)
    recent = {s.lower() for s in (recent_stems or set())}

    decoy = _decoy_slot(round_data)
    human_slots: list[tuple[int, str]] = []
    for rep in round_data.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            slot = int(rep.get("slot"))
        except (TypeError, ValueError):
            continue
        if decoy is not None and slot == decoy:
            continue
        human_slots.append((slot, str(rep.get("text") or "")))

    if not human_slots:
        return {}

    n_humans = len(human_slots)
    # Prefer unused stems for the hand; fall back to full pool if thin.
    fresh = [p for p in ordered_pool if p.stem.lower() not in recent]
    recycled = [p for p in ordered_pool if p.stem.lower() in recent]
    # Hand larger than n_humans so scoring still has choice, but small enough
    # that different salts deal different visual sets.
    hand_size = min(len(ordered_pool), max(n_humans + 3, min(10, n_humans * 2 + 2)))
    hand: list[Path] = []
    for p in fresh + recycled:
        if p not in hand:
            hand.append(p)
        if len(hand) >= hand_size:
            break
    if len(hand) < n_humans:
        hand = list(ordered_pool)

    # Rotate hand start by salt so even overlapping pools look different.
    rot = round_bias % max(1, len(hand))
    hand = hand[rot:] + hand[:rot]

    scored: dict[int, list[tuple[int, Path]]] = {}
    for slot, text in human_slots:
        ranked: list[tuple[int, Path]] = []
        for path in hand:
            stem = path.stem.lower()
            s = _score_gif_for_reply(
                stem,
                reply_text=text,
                topic=topic,
                post_text=post,
                round_bias=round_bias + slot * 31,
            )
            # Strong ban on recently used stems — force visual change.
            if stem in recent:
                s -= 80
            ranked.append((s, path))
        ranked.sort(key=lambda t: (-t[0], t[1].name))
        scored[slot] = ranked

    # Assign in slot order (stable) so every human gets something unique first.
    slot_order = sorted(human_slots, key=lambda st: st[0])
    used: set[str] = set()
    assignment: dict[int, Path] = {}
    for slot, _text in slot_order:
        for _s, path in scored.get(slot, []):
            stem = path.stem.lower()
            if stem not in used:
                used.add(stem)
                assignment[slot] = path
                break
        if slot not in assignment:
            for path in hand + ordered_pool:
                stem = path.stem.lower()
                if stem not in used:
                    used.add(stem)
                    assignment[slot] = path
                    break
            if slot not in assignment and ordered_pool:
                assignment[slot] = ordered_pool[slot % len(ordered_pool)]

    return assignment


def _pick_human_gif(
    round_id: str,
    slot: int,
    text: str,
    used: set[str],
    *,
    topic: str = "",
    post_text: str = "",
    pool: list[Path] | None = None,
) -> Path | None:
    """Legacy single-slot picker kept for CLI/tools. Prefer assign_human_gifs."""
    pool = pool or list_human_gifs()
    if not pool:
        return None
    ordered = _seeded_shuffle(pool, f"{round_id}:{slot}:{text}:{topic}")
    bias = int(hashlib.sha256(f"{round_id}:{slot}".encode()).hexdigest()[:8], 16)
    ranked = sorted(
        pool,
        key=lambda p: (
            -_score_gif_for_reply(
                p.stem,
                reply_text=text,
                topic=topic,
                post_text=post_text,
                round_bias=bias,
            ),
            ordered.index(p) if p in ordered else 99,
            p.name,
        ),
    )
    for path in ranked:
        if path.stem not in used:
            used.add(path.stem)
            return path
    return ranked[0] if ranked else None


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
    recent_stems: set[str] | None = None,
    diversity_salt: str = "",
) -> dict[str, Any]:
    """Apply text vs gif presentation for a freshly loaded round."""
    fmt = resolve_round_format(round_data, session_index=session_index)
    if fmt == "gif":
        return attach_reply_media(
            round_data,
            recent_stems=recent_stems,
            diversity_salt=diversity_salt or str(session_index),
        )
    return clear_reply_media(round_data)


def attach_reply_media(
    round_data: dict[str, Any],
    *,
    recent_stems: set[str] | None = None,
    diversity_salt: str = "",
) -> dict[str, Any]:
    """Stamp media_url onto every reply. Mutates round_data.

    Humans get diverse GIFs scored from reply + post + topic. Decoy gets
    existing Imagine video/gif if present, else pending (live will generate).
    Only call for GIF-format rounds.
    """
    decoy = _decoy_slot(round_data)
    human_ready = 0
    human_total = 0

    # Decoy media: ONLY a real per-round Imagine file. Never a pool GIF, never
    # the shared probe while live (that looked like "reusing an existing gif").
    decoy_own = existing_decoy_media_url_for_round(round_data, allow_probe=False)
    has_own_decoy = decoy_own is not None
    # Demo-only: probe is allowed so offline play still has motion.
    decoy_demo = None
    if not has_own_decoy and config.MODE != "live":
        decoy_demo = existing_decoy_media_url_for_round(round_data, allow_probe=True)

    # One diverse hand of human GIFs for the whole round (post + match salt).
    human_map = assign_human_gifs(
        round_data,
        recent_stems=recent_stems,
        diversity_salt=diversity_salt,
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
            # Decoy is Grok Imagine video ONLY — never a pool .gif path.
            rep.pop("art_url", None)
            if has_own_decoy and decoy_own:
                url, _mtype = decoy_own
                # Hard reject anything that isn't under /decoy/*.mp4|webm
                if _is_imagine_video_url(url):
                    rep["media_url"] = url
                    rep["media_type"] = "video"
                    rep["media_status"] = "ready"
                    rep["media_source"] = "imagine"
                else:
                    rep["media_url"] = None
                    rep["media_type"] = "video"
                    rep["media_status"] = "pending"
                    rep["media_source"] = "imagine"
            elif decoy_demo and config.MODE != "live":
                url, _mtype = decoy_demo
                rep["media_url"] = url if _is_imagine_video_url(url) else None
                rep["media_type"] = "video"
                rep["media_status"] = "ready" if rep.get("media_url") else "none"
                rep["media_source"] = "imagine"
            else:
                # Live: wait for Imagine — pending with no URL (not a stock gif).
                rep["media_url"] = None
                rep["media_type"] = "video"
                rep["media_status"] = "pending" if config.MODE == "live" else "none"
                rep["media_source"] = "imagine"
            continue

        human_total += 1
        path = human_map.get(slot)
        if path and path.suffix.lower() == ".gif":
            # Prefer the mp4 sibling so every reply's media is the same
            # container as the Imagine decoy. Mixed gif/mp4 was an oracle:
            # the one .mp4 among four .gif was always the machine.
            mp4 = path.with_suffix(".mp4")
            if mp4.is_file():
                rep["media_url"] = _gif_public_url(mp4)
                rep["media_type"] = "video"
            else:
                rep["media_url"] = _gif_public_url(path)
                rep["media_type"] = "gif"
            rep["media_status"] = "ready"
            rep["media_source"] = "human"
            human_ready += 1
        else:
            rep["media_url"] = None
            rep["media_type"] = "gif"
            rep["media_status"] = "none"
            rep["media_source"] = "human"
        rep.pop("art_url", None)
        rep["art_status"] = "none"

    round_data["format"] = "gif"
    if has_own_decoy:
        round_data["decoy_media_status"] = "ready"
    elif decoy_demo and config.MODE != "live":
        # Demo offline only: shared probe so motion still works without API.
        round_data["decoy_media_status"] = "ready"
    elif config.MODE == "live":
        # Waiting on Grok Imagine — never mark stock/pool media as ready.
        round_data["decoy_media_status"] = "pending"
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
    """Generate (or reuse) decoy video via Grok Imagine only.

    Returns a path only when the file is a real per-round Imagine clip
    (certified when ``IMAGINE_DECOY_REQUIRED``). Never returns pool .gifs
    or the shared ``_probe.mp4`` placeholder.
    """
    from services.imagine_agent import (
        decoy_video_path,
        generate_matching_decoy,
        is_imagine_certified,
        is_placeholder_decoy,
        is_real_decoy_media,
    )

    rid = str(round_data.get("round_id") or "round")
    out = decoy_video_path(rid)
    require = bool(getattr(config, "IMAGINE_DECOY_REQUIRED", True))

    def _ok(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size < 800:
            return False
        if is_placeholder_decoy(path):
            return False
        if not is_real_decoy_media(path):
            return False
        if require and not is_imagine_certified(path):
            return False
        return True

    if not force and _ok(out):
        return out

    generate_matching_decoy(round_data, force=force)
    if _ok(out):
        return out
    return None


def generate_decoy_media(round_data: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    """Ensure decoy has Grok Imagine video (never pool GIF / probe)."""
    attach_reply_media(round_data)
    decoy = _decoy_slot(round_data)
    if decoy is None:
        return round_data

    try:
        from services.imagine_agent import (
            generate_matching_decoy,
            is_imagine_certified,
            is_placeholder_decoy,
            is_real_decoy_media,
        )

        result = generate_matching_decoy(round_data, force=force)
    except Exception as exc:
        print(f"decoy imagine gif failed: {exc}", file=sys.stderr)
        result = {"status": "failed", "error": str(exc)}

    status = str(result.get("status") or "")
    # Only certified Imagine success counts — never probe fallback as "ready".
    if status in ("ready", "exists") and result.get("certified", status == "ready"):
        if round_data.get("decoy_media_status") == "ready":
            return round_data
        path = make_decoy_imagine_gif(round_data, force=False)
        if path and path.is_file() and not is_placeholder_decoy(path):
            if is_real_decoy_media(path) and (
                not getattr(config, "IMAGINE_DECOY_REQUIRED", True)
                or is_imagine_certified(path)
            ):
                rel = path.relative_to(REPO_ROOT / "web")
                url = "/" + str(rel).replace("\\", "/")
                for rep in round_data.get("replies") or []:
                    if not isinstance(rep, dict):
                        continue
                    try:
                        if int(rep.get("slot")) != decoy:
                            continue
                    except (TypeError, ValueError):
                        continue
                    rep["media_url"] = url
                    rep["media_type"] = "video"
                    rep["media_status"] = "ready"
                    rep["media_source"] = "imagine"
                    rep["media_engine"] = (
                        f"{config.MODEL_IMAGE}+{config.MODEL_VIDEO}"
                    )
                    break
                round_data["decoy_media_status"] = "ready"
                round_data["reply_art_status"] = "ready"
                return round_data

    for rep in round_data.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            if int(rep.get("slot")) != decoy:
                continue
        except (TypeError, ValueError):
            continue
        # Never leave a pool gif URL on the decoy slot.
        url = str(rep.get("media_url") or "")
        if url.lower().endswith(".gif") or (
            "/reply-gifs/" in url and "/decoy/" not in url
        ) or url.rstrip("/").lower().endswith("_probe.mp4"):
            rep["media_url"] = None
        rep["media_type"] = "video"
        rep["media_source"] = "imagine"
        if status in ("failed", "failed_probe_fallback", "no_live"):
            rep["media_status"] = "failed"
        elif rep.get("media_status") != "ready":
            rep["media_status"] = "pending"
    if round_data.get("decoy_media_status") != "ready":
        round_data["decoy_media_status"] = (
            "failed"
            if status in ("failed", "failed_probe_fallback", "no_live")
            else "pending"
        )
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
