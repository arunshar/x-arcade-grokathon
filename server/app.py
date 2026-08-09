"""X Arcade backend. One FastAPI app is the entire server.

It serves the static web client, runs the Decoy game rooms over a websocket,
exposes a health check, and proxies voice token minting to services.voice_host.

Run from the repo root: uvicorn server.app:app

Rooms live in memory. The websocket protocol is defined in CONTRACT.md and this
module conforms to it. The one rule that matters most: during the guessing
phase the broadcast state never contains is_decoy, decoy_slot, decoy_rationale,
or real reply authors. Reveal restores them. The client is never trusted.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import random
import secrets
import sys
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from cartridges.decoy import queue as decoy_queue
from cartridges.decoy import themes as decoy_themes
from plugins.safety import screen as safety_screen

_theme_pool_cache: dict[str, Any] = {"mtime": 0.0, "by_topic": {}, "all": []}
# Seeded under ARCADE_NO_SHUFFLE so the suites replay serving decisions;
# real deployments keep SystemRandom.
_theme_rng: random.Random = (
    random.Random(0)
    if os.environ.get("ARCADE_NO_SHUFFLE") == "1"
    else random.SystemRandom()
)

# Internal knob, not part of the contract. When set to 1 the server skips the
# round queue and always serves FALLBACK_ROUND. selfcheck.py sets it so the
# scripted guesses stay valid even after the real queue lands.
FORCE_FALLBACK = os.environ.get("ARCADE_FORCE_FALLBACK", "") == "1"

# The committed share card for demo mode. Live mode renders a fresh one
# through services.card_forge after the reveal broadcast.
DEMO_CARD_URL = "/static-assets/cards/decoy-3f2710c0a9e6_demo.jpg"

# A complete Round in the CONTRACT.md shape. Lets the server run standalone
# before cartridges/decoy/queue.py is integrated. All handles are fictional.
FALLBACK_ROUND: dict[str, Any] = {
    "round_id": "decoy-fallback00001",
    "source": {
        "post_text": (
            "If you could only keep one debugging tool for the rest of your "
            "career, what is it and why?"
        ),
        "post_author": "@stack_sage",
        "post_url": "https://x.com/stack_sage/status/0",
        "topic": "tech",
    },
    "replies": [
        {
            "slot": 0,
            "text": "print statements. Not glamorous but they have never once lied to me.",
            "author": "@printf_pete",
            "is_decoy": False,
        },
        {
            "slot": 1,
            "text": "A real debugger. Watching state change line by line beats guessing every time.",
            "author": "@gdb_gal",
            "is_decoy": False,
        },
        {
            "slot": 2,
            "text": (
                "It really depends on context — every tool has tradeoffs, and "
                "strong engineers just pick the right abstraction for the job."
            ),
            "author": "decoy",
            "is_decoy": True,
        },
        {
            "slot": 3,
            "text": "strace. The bug is usually in the syscalls, not in your code.",
            "author": "@strace_steve",
            "is_decoy": False,
        },
        {
            "slot": 4,
            "text": "The rubber duck. Explaining the bug out loud finds it before the tooling does.",
            "author": "@rubber_duck_rd",
            "is_decoy": False,
        },
    ],
    "decoy_slot": 2,
    "decoy_rationale": (
        "Hedges into a tidy mini-essay about tradeoffs and abstractions — "
        "real replies just pick a tool and plant a flag."
    ),
    "safety": {"screened": True, "gate_codes": []},
    "seed": 424242,
}


class PlayerState:
    """One connected player inside a room."""

    def __init__(self, name: str, ws: WebSocket):
        self.name = name
        self.ws = ws
        self.score = 0
        self.streak = 0
        self.guessed = False
        self.guess_slot: int | None = None
        self.guess_order: int | None = None
        self.client_ms: Any = None


# room id -> room dict. Single event loop, no locks needed at demo scale.
ROOMS: dict[str, dict[str, Any]] = {}

# Usage counters for the public /stats page. In memory on purpose: they reset
# on every deploy or restart, and the page says so. Honest numbers only.
import time as _time

STATS: dict[str, Any] = {
    "since": _time.strftime("%Y-%m-%d %H:%M UTC", _time.gmtime()),
    "joins": 0,
    "players": set(),
    "guesses": 0,
    "rounds_started": 0,
}

app = FastAPI(title="X Arcade")


@app.on_event("startup")
async def _startup_warm_decoy_media() -> None:
    """Certify on-disk decoy clips and pre-generate missing ones in the background.

    Live GIF rounds hide ALL media until every slot is a ready .mp4 (anti-cheat).
    If the decoy is not prebaked, players only see text for the whole round while
    Imagine runs (often longer than ROUND_SECONDS). Warmup fixes that.
    """
    try:
        from services.imagine_agent import certify_all_existing_decoys

        n = certify_all_existing_decoys()
        print(f"imagine: auto-certified {n} existing decoy clips", file=sys.stderr)
    except Exception as exc:
        print(f"imagine: certify-on-startup failed: {exc}", file=sys.stderr)

    if config.MODE != "live":
        return
    if os.environ.get("ARCADE_IMAGINE_WARMUP", "1") == "0":
        return

    async def _warm() -> None:
        try:
            await asyncio.to_thread(_warm_missing_decoy_media)
        except Exception as exc:
            print(f"imagine: warmup task failed: {exc}", file=sys.stderr)

    asyncio.create_task(_warm())


def _warm_missing_decoy_media() -> None:
    """Generate certified decoy videos for rounds that lack them (blocking)."""
    from services.imagine_agent import (
        decoy_video_path,
        ensure_certified,
        generate_matching_decoy,
        is_imagine_certified,
    )

    paths = sorted(decoy_queue.ROUNDS_DIR.glob("decoy_*.json"))
    missing: list[dict[str, Any]] = []
    for path in paths:
        try:
            rnd = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rid = str(rnd.get("round_id") or "")
        if not rid:
            continue
        vpath = decoy_video_path(rid)
        if ensure_certified(rid, vpath) or is_imagine_certified(vpath):
            continue
        missing.append(rnd)
    print(f"imagine: warmup queue {len(missing)} rounds", file=sys.stderr)
    for rnd in missing:
        rid = str(rnd.get("round_id") or "")
        try:
            # Human GIFs help the brief; attach if possible.
            try:
                from services.reply_gifs import attach_reply_media

                attach_reply_media(rnd)
            except Exception:
                pass
            print(f"imagine: warmup generate {rid}", file=sys.stderr)
            generate_matching_decoy(rnd, force=False)
        except Exception as exc:
            print(f"imagine: warmup {rid} failed: {exc}", file=sys.stderr)


# Historical: rooms used to split into host-driven arenas and auto-starting
# duels, and the mismatch between the two produced three dead-button bugs in
# one day. Every room is now time driven by the session clock and these
# fields are kept only so older clients reading them do not break.
ARENA_ROOMS = {"GROK"}

# Solo practice: room codes starting with SOLO (e.g. SOLO7K) can start with one
# player. After you guess, reveal fires immediately (all players have guessed).
# Or set ARCADE_ALLOW_SOLO=1 to allow any room to start with one player.
ALLOW_SOLO = os.environ.get("ARCADE_ALLOW_SOLO", "") == "1"


def _allows_solo(room: dict[str, Any]) -> bool:
    """True when a single player may start a round from the lobby.

    Only explicit solo practice rooms (code starts with SOLO) or the
    ARCADE_ALLOW_SOLO env override. Normal create/join multiplayer always
    needs two players — ``arena`` is not a solo flag.
    """
    if ALLOW_SOLO:
        return True
    rid = str(room.get("room_id") or "").upper()
    return rid.startswith("SOLO")


def _min_players_to_start(room: dict[str, Any]) -> int:
    """Solo rooms: 1. Normal multiplayer rooms: 2 (configurable)."""
    if _allows_solo(room):
        return 1
    return int(getattr(config, "MULTIPLAYER_MIN_PLAYERS", 2) or 2)


def _max_players(room: dict[str, Any]) -> int:
    """Solo rooms: 1 seat. Multiplayer: max 5 (configurable)."""
    if _allows_solo(room):
        return 1
    return int(getattr(config, "MULTIPLAYER_MAX_PLAYERS", 5) or 5)


def _enough_players_to_start(room: dict[str, Any]) -> bool:
    return len(room.get("players") or {}) >= _min_players_to_start(room)


def _room_is_full(room: dict[str, Any]) -> bool:
    return len(room.get("players") or {}) >= _max_players(room)


def _lobby_auto_starts(room: dict[str, Any]) -> bool:
    """Solo lobbies may auto-start; multiplayer waits for host START only."""
    return _allows_solo(room)


def _get_room(room_id: str) -> dict[str, Any]:
    room = ROOMS.get(room_id)
    if room is None:
        room = {
            "room_id": room_id,
            "phase": "lobby",
            "players": {},
            "round": None,
            "reveal": None,
            "results": None,
            "deadline_at": None,
            "timer": None,
            "guess_counter": 0,
            "rounds_played": 0,
            # Always the live config value (default 6) — never a stale short match.
            "match_rounds": int(getattr(config, "MATCH_ROUNDS", 6) or 6),
            # Stems of human GIFs used on recent gif rounds — for diversity.
            "recent_gif_stems": [],
            # Per-room entropy so the same topic does not always draw the same GIFs.
            "gif_session_salt": secrets.token_hex(4),
            # Recently served round_ids — avoid repeating the same post in a match.
            "recent_round_ids": [],
            # Topic filter set by the room creator: [] = random/all topics.
            "topic_filter": [],
            "arena": room_id in ARENA_ROOMS,
            "host": None,
            "auto_timer": None,
            "auto_deadline_at": None,
        }
        ROOMS[room_id] = room
    # Backfill fields for rooms created before this build.
    room.setdefault("results", None)
    # Refresh every access so rooms created under an old short cap (e.g. 2)
    # pick up the current 6-round match length.
    room["match_rounds"] = int(getattr(config, "MATCH_ROUNDS", 6) or 6)
    room.setdefault("rounds_played", 0)
    room.setdefault("recent_gif_stems", [])
    room.setdefault("gif_session_salt", secrets.token_hex(4))
    room.setdefault("recent_round_ids", [])
    room.setdefault("topic_filter", [])
    return room


# Lobby chips + member topics — single source in cartridges/decoy/themes.py
TOPIC_CATALOG: list[dict[str, Any]] = decoy_themes.catalog_groups()


def _normalize_topic_filter(raw: Any) -> list[str]:
    """Parse client topic filter into a sorted unique list of topic slugs.

    Empty list means random (no filter). Accepts catalog group ids or raw
    topic names from round JSON.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    known_topics: set[str] = set(decoy_themes.all_member_topics())
    # Also accept any topic that exists on disk.
    try:
        for path in decoy_queue.ROUNDS_DIR.glob("decoy_*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            t = decoy_themes.round_topic_of(data)
            if t:
                known_topics.add(t)
    except Exception:
        pass

    out: set[str] = set()
    for item in raw:
        key = str(item or "").strip().lower()
        if not key or key in ("random", "all", "*", "any"):
            continue
        expanded = decoy_themes.expand_group_id(key)
        if expanded:
            out.update(expanded)
            continue
        if key in known_topics:
            out.add(key)
    return sorted(out)


def _topics_from_client_msg(msg: dict[str, Any]) -> list[str] | None:
    """Extract topic filter from a client message, or None if not provided.

    Accepts ``topics``, ``topic_filter``, and/or ``topic_groups`` (chip ids).
    An explicit empty list means RANDOM and is distinct from omitted.
    """
    if not isinstance(msg, dict):
        return None
    has = False
    combined: list[Any] = []
    for key in ("topics", "topic_filter", "topic_groups"):
        if key not in msg:
            continue
        has = True
        val = msg.get(key)
        if val is None:
            continue
        if isinstance(val, str):
            combined.append(val)
        elif isinstance(val, (list, tuple)):
            combined.extend(val)
    if not has:
        return None
    return _normalize_topic_filter(combined)


def _apply_room_topics(
    room: dict[str, Any],
    msg: dict[str, Any],
    *,
    allow: bool,
) -> None:
    """Set room topic_filter from client msg when the sender may choose it.

    Omitted topic keys leave the existing filter alone (reconnect-safe).
    A non-empty list always replaces. An empty list only clears to RANDOM
    when the client sets ``clear_topics`` / ``topics_random`` — otherwise a
    reconnect with empty topics must not wipe a multiplayer host's filter.
    """
    if not allow:
        return
    parsed = _topics_from_client_msg(msg)
    if parsed is None:
        return
    prev = [str(t).lower() for t in (room.get("topic_filter") or []) if t]
    explicit_random = bool(
        msg.get("clear_topics") or msg.get("topics_random") or msg.get("random_topics")
    )
    if not parsed and prev and not explicit_random:
        # Keep the existing theme (common: host WS reconnect mid-lobby).
        print(
            f"room {room.get('room_id')}: keep topic_filter {prev} "
            f"(ignored empty payload without clear_topics)",
            file=sys.stderr,
        )
        return
    room["topic_filter"] = parsed
    if parsed != prev:
        print(
            f"room {room.get('room_id')}: topic_filter -> {parsed}",
            file=sys.stderr,
        )


def _load_rounds_matching_topics(topic_filter: set[str]) -> list[dict[str, Any]]:
    """All screened on-disk rounds whose source.topic is in the filter."""
    if not topic_filter:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        paths = sorted(decoy_queue.ROUNDS_DIR.glob("decoy_*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        topic = _round_topic(data)
        if topic not in topic_filter:
            continue
        if not safety_screen.screen_round(data).get("screened"):
            continue
        rid = str(data.get("round_id") or path.stem)
        if rid in seen:
            continue
        seen.add(rid)
        out.append(data)
    return out


def _round_topic(rnd: dict[str, Any]) -> str:
    return decoy_themes.round_topic_of(rnd)


def _rounds_dir_mtime() -> float:
    try:
        latest = 0.0
        root = decoy_queue.ROUNDS_DIR
        if root.is_dir():
            latest = max(latest, root.stat().st_mtime)
            for path in root.glob("decoy_*.json"):
                try:
                    latest = max(latest, path.stat().st_mtime)
                except OSError:
                    continue
        return latest
    except OSError:
        return 0.0


def _refresh_theme_pool(force: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Load screened, on-theme rounds indexed by topic slug."""
    mtime = _rounds_dir_mtime()
    if (
        not force
        and _theme_pool_cache.get("by_topic")
        and abs(float(_theme_pool_cache.get("mtime") or 0) - mtime) < 0.001
    ):
        return _theme_pool_cache["by_topic"]  # type: ignore[return-value]

    by_topic: dict[str, list[dict[str, Any]]] = {}
    all_rows: list[dict[str, Any]] = []
    try:
        paths = sorted(decoy_queue.ROUNDS_DIR.glob("decoy_*.json"))
    except OSError:
        paths = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if not safety_screen.screen_round(data).get("screened"):
            continue
        if not decoy_themes.round_fits_theme(data, min_score=1):
            continue
        # Soft-drop clear misses (score 0) already handled; score 1–2 kept.
        if decoy_themes.round_theme_score(data) < 1:
            continue
        topic = _round_topic(data)
        if not topic:
            continue
        by_topic.setdefault(topic, []).append(data)
        all_rows.append(data)

    _theme_pool_cache["mtime"] = mtime
    _theme_pool_cache["by_topic"] = by_topic
    _theme_pool_cache["all"] = all_rows
    return by_topic


def _list_topics_payload() -> dict[str, Any]:
    """Catalog + live counts for the create-lobby picker."""
    by_topic = _refresh_theme_pool()
    counts = {t: len(rows) for t, rows in by_topic.items()}
    groups = []
    for g in TOPIC_CATALOG:
        topics = [str(t).lower() for t in (g.get("topics") or [])]
        n = sum(counts.get(t, 0) for t in topics) if topics else sum(counts.values())
        groups.append(
            {
                "id": g["id"],
                "label": g["label"],
                "blurb": g.get("blurb") or "",
                "topics": topics,
                "count": n,
            }
        )
    return {"groups": groups, "topics": counts}


def _pick_from_candidates(
    candidates: list[dict[str, Any]],
    *,
    exclude: set[str],
    prefer_media: bool,
) -> dict[str, Any] | None:
    """Pick one round from an already-filtered candidate list."""
    if not candidates:
        return None

    def rid_of(rnd: dict[str, Any]) -> str:
        return str(rnd.get("round_id") or "")

    # Passes: fresh+media → fresh → any+media → any (always stay in candidates)
    passes: list[tuple[bool, bool]] = []
    if prefer_media:
        passes = [(True, True), (True, False), (False, True), (False, False)]
    else:
        passes = [(True, False), (False, False)]

    for require_fresh, require_media in passes:
        pool: list[dict[str, Any]] = []
        for rnd in candidates:
            rid = rid_of(rnd)
            if require_fresh and rid and rid in exclude:
                continue
            if require_media and not _round_has_ready_decoy_media(rnd):
                continue
            pool.append(rnd)
        if not pool:
            continue
        # Prefer stronger on-theme hits, then shuffle among equals.
        scored: list[tuple[int, dict[str, Any]]] = [
            (decoy_themes.round_theme_score(r), r) for r in pool
        ]
        best = max(s for s, _ in scored)
        top = [r for s, r in scored if s == best]
        choice = _theme_rng.choice(top)
        out = copy.deepcopy(choice)
        decoy_queue.randomize_decoy_position(out)
        out["safety"] = safety_screen.screen_round(out)
        return out
    return None


def _round_has_ready_decoy_media(rnd: dict[str, Any]) -> bool:
    """True when a certified unique decoy mp4 exists for this round id."""
    try:
        from services.imagine_agent import (
            decoy_video_path,
            ensure_certified,
            is_imagine_certified,
        )

        rid = str(rnd.get("round_id") or "")
        if not rid:
            return False
        path = decoy_video_path(rid)
        return bool(ensure_certified(rid, path) or is_imagine_certified(path))
    except Exception:
        return False


async def _next_round(room: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pull the next safety-screened round from the decoy queue.

    Prefers rounds this room has not seen recently so posts/replies feel
    fresh across a match. When GIF mode needs Imagine clips, also prefers
    rounds that already have certified decoy media so the Space does not
    deal text-only rounds while most of the pool lacks prebaked video.

    Themed rooms pick **only** from rounds tagged with the chosen topics.
    Off-theme FALLBACK is forbidden when a filter is set — recycle on-theme
    posts instead of leaking a tech stub into a sports room.
    """
    exclude: set[str] = set()
    if room is not None:
        exclude = {str(x) for x in (room.get("recent_round_ids") or []) if x}

    # Prefer prebaked Imagine clips so GIF rounds actually show media.
    # Disabled under ARCADE_NO_SHUFFLE so integration_check can walk the full pool.
    prefer_media = os.environ.get("ARCADE_NO_SHUFFLE") != "1" and (
        str(getattr(config, "GIF_ROUND_MODE", "") or "").lower()
        in ("always_gif", "gif", "all_gif")
        or config.MODE == "live"
    )
    topic_filter: set[str] = set()
    if room is not None:
        topic_filter = {
            str(t).lower().strip()
            for t in (room.get("topic_filter") or [])
            if str(t or "").strip()
        }

    def _accept(picked: dict[str, Any] | None) -> dict[str, Any] | None:
        """Reject anything outside the room theme (defense in depth)."""
        if picked is None:
            return None
        if not topic_filter:
            return picked
        if _round_topic(picked) not in topic_filter:
            return None
        return picked

    if not FORCE_FALLBACK:
        try:
            by_topic = _refresh_theme_pool()
            if topic_filter:
                # 1) Theme-fit index (strong keyword hits preferred).
                candidates: list[dict[str, Any]] = []
                seen_ids: set[str] = set()
                for t in topic_filter:
                    for rnd in by_topic.get(t, []):
                        rid = str(rnd.get("round_id") or "")
                        if rid and rid in seen_ids:
                            continue
                        if rid:
                            seen_ids.add(rid)
                        candidates.append(rnd)
                strong = [
                    r for r in candidates if decoy_themes.round_theme_score(r) >= 2
                ]
                pick_pool = strong if strong else candidates
                picked = _accept(
                    _pick_from_candidates(
                        pick_pool, exclude=exclude, prefer_media=prefer_media
                    )
                )
                if picked is not None:
                    return picked
                picked = _accept(
                    _pick_from_candidates(
                        candidates, exclude=set(), prefer_media=False
                    )
                )
                if picked is not None:
                    return picked

                # 2) Disk scan by tag only (ignore soft theme_fit demotion).
                disk = _load_rounds_matching_topics(topic_filter)
                picked = _accept(
                    _pick_from_candidates(
                        disk, exclude=exclude, prefer_media=prefer_media
                    )
                )
                if picked is not None:
                    return picked
                picked = _accept(
                    _pick_from_candidates(disk, exclude=set(), prefer_media=False)
                )
                if picked is not None:
                    return picked
                # Stay inside theme — never fall through to global FALLBACK.
            else:
                # Random mix — still skip quarantined / off-tag junk.
                all_rows: list[dict[str, Any]] = list(
                    _theme_pool_cache.get("all") or []
                )
                if not all_rows:
                    all_rows = [r for rows in by_topic.values() for r in rows]
                picked = _pick_from_candidates(
                    all_rows, exclude=exclude, prefer_media=prefer_media
                )
                if picked is not None:
                    return picked
                # Legacy queue walk for NO_SHUFFLE / empty index edge cases.
                attempts = max(decoy_queue.round_count() * 3, 12)
                skip = set(exclude)
                for _ in range(attempts):
                    rnd = decoy_queue.next_round(exclude_ids=skip)
                    gates = safety_screen.screen_round(rnd)
                    rnd["safety"] = gates
                    rid = str(rnd.get("round_id") or "")
                    if not gates.get("screened"):
                        if rid:
                            skip.add(rid)
                        continue
                    if not decoy_themes.round_fits_theme(rnd, min_score=1):
                        if rid:
                            skip.add(rid)
                        continue
                    if prefer_media and not _round_has_ready_decoy_media(rnd):
                        continue
                    return rnd
        except Exception as exc:
            print(f"_next_round theme pick failed: {exc}", file=sys.stderr)

    # Themed room with empty pool: still refuse off-theme FALLBACK.
    if topic_filter:
        disk = _load_rounds_matching_topics(topic_filter)
        if disk:
            picked = _accept(
                _pick_from_candidates(disk, exclude=set(), prefer_media=False)
            )
            if picked is not None:
                return picked
            # Absolute last themed copy.
            out = copy.deepcopy(disk[0])
            decoy_queue.randomize_decoy_position(out)
            out["safety"] = safety_screen.screen_round(out)
            return out
        # No on-disk posts for this theme — themed stub (not tech FALLBACK).
        theme_label = ",".join(sorted(topic_filter)[:3]) or "theme"
        stub = copy.deepcopy(FALLBACK_ROUND)
        stub["round_id"] = "decoy-theme-empty"
        stub["source"] = {
            "post_text": (
                f"Theme pool for {theme_label} is empty right now. "
                "Try RANDOM or another theme — or wait for a refill."
            ),
            "post_author": "@arcade",
            "post_url": "https://x.com",
            "topic": sorted(topic_filter)[0],
        }
        decoy_queue.randomize_decoy_position(stub)
        stub["safety"] = safety_screen.screen_round(stub)
        return stub

    fallback = copy.deepcopy(FALLBACK_ROUND)
    decoy_queue.randomize_decoy_position(fallback)
    fallback["safety"] = safety_screen.screen_round(fallback)
    return fallback


def _rounds_available() -> int:
    """How many queued rounds pass the safety gates right now.

    The fallback round always counts as at least one, so /health never
    reports an unplayable server.
    """
    if not FORCE_FALLBACK:
        try:
            count = 0
            for path in sorted(decoy_queue.ROUNDS_DIR.glob("*.json")):
                rnd = json.loads(path.read_text(encoding="utf-8"))
                if safety_screen.screen_round(rnd)["screened"]:
                    count += 1
            if count:
                return count
        except Exception:
            pass
    return 1


def _decoy_media_ready_count() -> int:
    """How many screened rounds already have a certified decoy mp4."""
    n = 0
    try:
        for path in sorted(decoy_queue.ROUNDS_DIR.glob("decoy_*.json")):
            try:
                rnd = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not safety_screen.screen_round(rnd).get("screened"):
                continue
            if _round_has_ready_decoy_media(rnd):
                n += 1
    except Exception:
        return 0
    return n


def _deadline_ms(room: dict[str, Any]) -> int | None:
    if room["phase"] != "guessing" or room["deadline_at"] is None:
        return None
    remaining = room["deadline_at"] - asyncio.get_running_loop().time()
    return max(0, int(remaining * 1000))


def _uniform_ready_media(rnd: dict[str, Any]) -> dict[int, str] | None:
    """Return slot→local .mp4 path if every reply has ready video, else None."""
    real: dict[int, str] = {}
    replies = [r for r in (rnd.get("replies") or []) if isinstance(r, dict) and "slot" in r]
    if not replies:
        return None
    for r in replies:
        url = r.get("media_url")
        status = str(r.get("media_status") or "")
        if not url or status not in ("", "ready"):
            return None
        path = _media_local_path(str(url))
        if path is None or path.suffix.lower() != ".mp4":
            return None
        real[int(r["slot"])] = str(path)
    if len(real) != len(replies):
        return None
    return real


def _freeze_guessing_media(room: dict[str, Any], rnd: dict[str, Any]) -> bool:
    """Lock whether this guessing phase shows GIFs — never flip mid-round.

    If media is not fully ready when the round starts, clients play text-only
    for the whole guess. Late Imagine results must not pop GIFs in after a
    pick (that felt like a glitch and could leak timing).
    """
    fmt = str(rnd.get("format") or "text").lower()
    real = _uniform_ready_media(rnd) if fmt == "gif" else None
    serve = bool(real)
    room["guessing_media"] = serve
    if serve and real is not None:
        room["media_files"] = real
        if not room.get("media_token"):
            room["media_token"] = secrets.token_hex(4)
    else:
        # Keep any files for reveal, but do not advertise media while guessing.
        room["media_files"] = real or room.get("media_files") or {}
    return serve


def _round_view(room: dict[str, Any]) -> dict[str, Any] | None:
    """The round as clients may see it in the current phase.

    During guessing this is the contract's critical strip: no decoy_slot, no
    decoy_rationale, no media_source. Replies are slot + text + media only.

    GIF media is frozen at round start (``room['guessing_media']``). Late
    Imagine completion does not inject media mid-guess.
    """
    rnd = room["round"]
    if rnd is None or room["phase"] != "guessing":
        return rnd
    strip_keys = (
        "decoy_slot",
        "decoy_rationale",
        "reply_art_status",
        "art_url",
        "art_status",
        "decoy_media_status",
    )
    safe = {k: v for k, v in rnd.items() if k not in strip_keys}

    # Frozen at round start — never promote text→gif after the first broadcast.
    serve_media = bool(room.get("guessing_media"))
    if serve_media:
        real = room.get("media_files") or _uniform_ready_media(rnd) or {}
        if len(real) != len(rnd.get("replies") or []):
            # Safety: incomplete map → text for this view only (flag stays).
            serve_media = False
        else:
            room["media_files"] = real
            if not room.get("media_token"):
                room["media_token"] = secrets.token_hex(4)
    token = room.get("media_token")

    safe_replies = []
    for r in rnd.get("replies") or []:
        if not isinstance(r, dict) or "slot" not in r:
            continue
        item: dict[str, Any] = {"slot": r["slot"], "text": r.get("text") or ""}
        if serve_media and token is not None:
            item["media_url"] = f"/media/{room['room_id']}/{token}/{r['slot']}.mp4"
            item["media_type"] = "video"
            item["media_status"] = "ready"
        # Never send media_source / media_engine / is_decoy / author during
        # guessing, and never a real media URL or a per-slot status.
        safe_replies.append(item)
    safe["replies"] = safe_replies
    safe["format"] = "gif" if serve_media else "text"
    return safe


def _media_local_path(url: str) -> Path | None:
    """Resolve a /static-assets media URL to a file inside the web tree."""
    u = url.split("?", 1)[0]
    if not u.startswith("/static-assets/"):
        return None
    candidate = (REPO_ROOT / "web" / u.lstrip("/")).resolve()
    web_root = (REPO_ROOT / "web").resolve()
    if not str(candidate).startswith(str(web_root)) or not candidate.is_file():
        return None
    return candidate


def _standings(room: dict[str, Any]) -> list[dict[str, Any]]:
    """Players ranked by score (desc), then name. Always safe to show clients."""
    ordered = sorted(
        room["players"].values(), key=lambda p: (-p.score, p.name.lower())
    )
    return [
        {
            "rank": i + 1,
            "name": p.name,
            "score": p.score,
            "streak": p.streak,
        }
        for i, p in enumerate(ordered)
    ]


def _public_state(room: dict[str, Any]) -> dict[str, Any]:
    # Who picked which reply is public at reveal and only at reveal. During
    # guessing it would leak strategy, so the strip rule keeps it server side.
    at_reveal = room["phase"] == "reveal"
    match_rounds = int(room.get("match_rounds") or getattr(config, "MATCH_ROUNDS", 6) or 6)
    rounds_played = int(room.get("rounds_played") or 0)
    return {
        "t": "state",
        "room": room["room_id"],
        "phase": room["phase"],
        "players": [
            {
                "name": p.name,
                "score": p.score,
                "streak": p.streak,
                "guessed": p.guessed,
                **({"guess_slot": p.guess_slot} if at_reveal else {}),
            }
            for p in room["players"].values()
        ],
        "auto_ms": _auto_ms(room),
        # Full ranked board every broadcast so lobby / guessing / reveal
        # can show who is ahead without waiting for the reveal strip.
        "standings": _standings(room),
        "round": _round_view(room),
        "reveal": room["reveal"],
        "results": room.get("results"),
        "deadline_ms": _deadline_ms(room),
        "match_rounds": match_rounds,
        "rounds_played": rounds_played,
        # Topic filter chosen at room create ([] = random mix).
        "topic_filter": list(room.get("topic_filter") or []),
        "min_players": _min_players_to_start(room),
        "max_players": _max_players(room),
        "can_start": _enough_players_to_start(room),
        "room_full": _room_is_full(room),
        # True on the last reveal of the match (NEXT → results, not another round).
        "match_over": room["phase"] == "results"
        or (
            room["phase"] == "reveal"
            and rounds_played >= match_rounds
        ),
    }


async def _broadcast(room: dict[str, Any]) -> None:
    state = _public_state(room)
    dead: list[str] = []
    for player in list(room["players"].values()):
        try:
            await player.ws.send_json(state)
        except Exception:
            dead.append(player.name)
    for name in dead:
        room["players"].pop(name, None)


def _cancel_timer(room: dict[str, Any]) -> None:
    timer = room.get("timer")
    if timer is not None and not timer.done():
        timer.cancel()
    room["timer"] = None


def _cancel_auto(room: dict[str, Any]) -> None:
    timer = room.get("auto_timer")
    if timer is not None and not timer.done():
        timer.cancel()
    room["auto_timer"] = None
    room["auto_deadline_at"] = None


def _schedule_auto(room: dict[str, Any], delay: float) -> None:
    """Arm the session clock: the round machine advances itself.

    The game is time driven rather than host driven. A lobby with anyone in
    it rolls into a round, and a reveal rolls into the next round. Manual
    START / NEXT ROUND just skips the wait.
    """
    _cancel_auto(room)
    room["auto_deadline_at"] = asyncio.get_running_loop().time() + delay
    room["auto_timer"] = asyncio.create_task(_auto_advance(room, delay))


async def _auto_advance(room: dict[str, Any], delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    if ROOMS.get(room["room_id"]) is not room or not room["players"]:
        return
    phase = room["phase"]
    match_rounds = int(room.get("match_rounds") or getattr(config, "MATCH_ROUNDS", 6) or 6)
    if phase == "lobby":
        # Multiplayer: no lobby countdown — host must tap START.
        if not _lobby_auto_starts(room):
            room["auto_timer"] = None
            room["auto_deadline_at"] = None
            return
        # Solo may auto-start once a player is present.
        if not _enough_players_to_start(room):
            if room["players"]:
                _schedule_auto(room, config.LOBBY_SECONDS)
                await _broadcast(room)
            return
        await _start_round(room)
        return
    if phase == "reveal":
        # Last round finished → results. Otherwise continue the match.
        if int(room.get("rounds_played") or 0) >= match_rounds:
            await _enter_results(room)
        else:
            await _start_round(room)
        return
    if phase == "results":
        # Soft return to lobby so a new match can arm; scores stay until restart.
        await _return_to_lobby(room, reset_scores=False)


def _auto_ms(room: dict[str, Any]) -> int | None:
    """Milliseconds until the session clock advances, for the countdown UI."""
    if room["phase"] not in ("lobby", "reveal", "results") or room["auto_deadline_at"] is None:
        return None
    remaining = room["auto_deadline_at"] - asyncio.get_running_loop().time()
    return max(0, int(remaining * 1000))


async def _round_timer(room: dict[str, Any]) -> None:
    """Server-side deadline. Fires the reveal when the clock runs out."""
    try:
        await asyncio.sleep(config.ROUND_SECONDS)
    except asyncio.CancelledError:
        return
    if room["phase"] == "guessing":
        await _do_reveal(room)


async def _start_round(room: dict[str, Any]) -> None:
    _cancel_timer(room)
    _cancel_auto(room)
    # Fresh opaque-media namespace per round, so a cached URL from the last
    # round can never serve this round's media.
    room["media_token"] = None
    room["media_files"] = {}
    # Pin match length every round start from live config (default 6).
    match_rounds = max(1, int(getattr(config, "MATCH_ROUNDS", 6) or 6))
    room["match_rounds"] = match_rounds
    rounds_played = int(room.get("rounds_played") or 0)

    # Coming from results / finished match in lobby → wipe board for a new match.
    if room["phase"] == "results" or (
        room["phase"] == "lobby" and rounds_played >= match_rounds
    ):
        room["rounds_played"] = 0
        room["recent_gif_stems"] = []
        room["gif_session_salt"] = secrets.token_hex(4)
        # Keep recent_round_ids across matches so restart does not re-serve
        # the same six posts immediately.
        for p in room["players"].values():
            p.score = 0
            p.streak = 0
        rounds_played = 0

    # Reveal after the last round should open results, not start another.
    if room["phase"] == "reveal" and rounds_played >= match_rounds:
        await _enter_results(room)
        return

    # Starting a match from lobby (or restart) requires the player minimum.
    # Mid-match advances (reveal → next round) keep going even if someone left.
    starting_fresh = room["phase"] in ("lobby", "results") or rounds_played == 0
    if starting_fresh and room["phase"] != "reveal" and not _enough_players_to_start(room):
        room["phase"] = "lobby"
        room["round"] = None
        room["reveal"] = None
        room["results"] = None
        room["deadline_at"] = None
        # Solo only — multiplayer waits for host START (no lobby countdown).
        if room["players"] and _lobby_auto_starts(room):
            _schedule_auto(room, config.LOBBY_SECONDS)
        await _broadcast(room)
        return

    room["results"] = None
    # Snapshot filter before deal — _next_round must honor this exactly.
    deal_filter = {
        str(t).lower()
        for t in (room.get("topic_filter") or [])
        if str(t or "").strip()
    }
    rnd = await _next_round(room)
    # Hard assert: never ship an off-theme post into a filtered room.
    got_topic = _round_topic(rnd)
    if deal_filter and got_topic not in deal_filter:
        print(
            f"room {room.get('room_id')}: OFF-THEME deal {got_topic!r} "
            f"not in {sorted(deal_filter)} — re-picking",
            file=sys.stderr,
        )
        # Force a themed pick from disk; never keep the bad card.
        disk = _load_rounds_matching_topics(deal_filter)
        if disk:
            alt = _pick_from_candidates(disk, exclude=set(), prefer_media=False)
            if alt is not None:
                rnd = alt
            else:
                rnd = copy.deepcopy(disk[0])
                decoy_queue.randomize_decoy_position(rnd)
                rnd["safety"] = safety_screen.screen_round(rnd)
    # Track served round so the next picks stay different.
    rid = str(rnd.get("round_id") or "")
    if rid:
        prev_ids = [x for x in (room.get("recent_round_ids") or []) if x != rid]
        # Remember enough ids to cover most of the pool (cap 40).
        keep = max(12, min(40, decoy_queue.round_count() - 1))
        room["recent_round_ids"] = ([rid] + prev_ids)[:keep]
    print(
        f"room {room.get('room_id')}: deal topic={_round_topic(rnd)!r} "
        f"filter={sorted(deal_filter) or 'RANDOM'} rid={rid}",
        file=sys.stderr,
    )
    # Mix text rounds (classic replies) with GIF rounds (human gifs + Imagine).
    session_index = rounds_played
    room["rounds_played"] = session_index + 1
    recent = set(room.get("recent_gif_stems") or [])
    room.setdefault("gif_session_salt", secrets.token_hex(4))
    diversity_salt = f"{room.get('gif_session_salt')}:{session_index}:{room.get('room_id')}"
    try:
        from services.reply_gifs import prepare_round_presentation

        prepare_round_presentation(
            rnd,
            session_index=session_index,
            recent_stems=recent,
            diversity_salt=diversity_salt,
        )
    except Exception as exc:
        print(f"prepare_round_presentation failed: {exc}", file=sys.stderr)
        rnd.setdefault("format", "text")
        rnd.setdefault("decoy_media_status", "none")
    # Remember this round's human GIF stems so the next gif round diversifies.
    if rnd.get("format") == "gif":
        stems: list[str] = []
        for rep in rnd.get("replies") or []:
            if not isinstance(rep, dict) or rep.get("media_source") != "human":
                continue
            url = str(rep.get("media_url") or "")
            if not url:
                continue
            stem = Path(url).stem.lower()
            if stem and stem not in stems:
                stems.append(stem)
        # Keep last ~12 stems (~3 gif rounds) so the pool can rotate.
        prev = [s for s in (room.get("recent_gif_stems") or []) if s not in stems]
        room["recent_gif_stems"] = (stems + prev)[:12]

    # Live: stamp certified decoy BEFORE freezing media so the first paint
    # already has all five videos when files exist (no mid-round pop-in).
    certified = False
    if config.MODE == "live" and rnd.get("format") == "gif":
        try:
            from services.imagine_agent import (
                decoy_video_path,
                ensure_certified,
                is_imagine_certified,
            )

            rid = str(rnd.get("round_id") or "")
            vpath = decoy_video_path(rid)
            certified = ensure_certified(rid, vpath) or is_imagine_certified(vpath)
            if certified and vpath.is_file():
                url = "/static-assets/reply-gifs/decoy/" + vpath.name
                for rep in rnd.get("replies") or []:
                    if not isinstance(rep, dict):
                        continue
                    try:
                        is_d = int(rep.get("slot")) == int(rnd.get("decoy_slot"))
                    except (TypeError, ValueError):
                        is_d = bool(rep.get("is_decoy"))
                    if is_d:
                        rep["media_url"] = url
                        rep["media_type"] = "video"
                        rep["media_status"] = "ready"
                        rep["media_source"] = "imagine"
                rnd["decoy_media_status"] = "ready"
            else:
                for rep in rnd.get("replies") or []:
                    if not isinstance(rep, dict):
                        continue
                    if rep.get("media_source") != "imagine" and not rep.get("is_decoy"):
                        try:
                            if int(rep.get("slot")) != int(rnd.get("decoy_slot")):
                                continue
                        except (TypeError, ValueError):
                            continue
                    rep["media_url"] = None
                    rep["media_type"] = "video"
                    rep["media_status"] = "pending"
                    rep["media_source"] = "imagine"
                rnd["decoy_media_status"] = "pending"
        except Exception:
            certified = False

    room["round"] = rnd
    room["reveal"] = None
    room["results"] = None
    room["phase"] = "guessing"
    # Freeze gif-vs-text for this entire guess. Late Imagine must not inject
    # media after players have already started picking.
    _freeze_guessing_media(room, rnd)
    STATS["rounds_started"] += 1
    room["guess_counter"] = 0
    for p in room["players"].values():
        p.guessed = False
        p.guess_slot = None
        p.guess_order = None
        p.client_ms = None
    room["deadline_at"] = asyncio.get_running_loop().time() + config.ROUND_SECONDS
    room["timer"] = asyncio.create_task(_round_timer(room))
    await _broadcast(room)

    if config.MODE == "live" and rnd.get("format") == "gif":
        imagine_required = bool(getattr(config, "IMAGINE_DECOY_REQUIRED", True))
        needs_imagine = (not certified) or (
            imagine_required and rnd.get("decoy_media_status") != "ready"
        )
        if needs_imagine:
            print(
                f"imagine: scheduling Grok Imagine "
                f"({config.MODEL_IMAGE}+{getattr(config, 'MODEL_VIDEO', '')}) "
                f"for decoy reply {rnd.get('round_id')} "
                f"(guessing stays text until next round if not ready yet)",
                file=sys.stderr,
            )
            asyncio.create_task(
                _attach_decoy_imagine_gif(room, rnd, force=not certified)
            )
        asyncio.create_task(_prefetch_upcoming_decoy_media(room))


async def _enrich_reveal_rationale(
    room: dict[str, Any],
    rnd: dict[str, Any],
    avoid: list[str],
) -> None:
    """Swap in a livelier agent tell if still on this reveal."""
    try:
        from services.reveal_rationale import agent_reveal_rationale

        line = await asyncio.to_thread(
            agent_reveal_rationale, rnd, avoid=avoid
        )
    except Exception as exc:
        print(f"reveal rationale agent failed: {exc}", file=sys.stderr)
        return
    if not line or room.get("phase") != "reveal":
        return
    rev = room.get("reveal")
    if not isinstance(rev, dict):
        return
    # Same round still showing?
    cur = room.get("round") or {}
    if str(cur.get("round_id") or "") != str(rnd.get("round_id") or ""):
        return
    if str(rev.get("rationale") or "").strip() == line:
        return
    rev["rationale"] = line
    recent = [str(x) for x in (room.get("recent_rationales") or []) if x]
    room["recent_rationales"] = ([line] + [r for r in recent if r != line])[:12]
    try:
        await _broadcast(room)
    except Exception:
        pass


async def _do_reveal(room: dict[str, Any]) -> None:
    # Idempotent: timer + last-guess can both fire; only score once.
    if room.get("phase") != "guessing":
        return
    _cancel_timer(room)
    rnd = room["round"]
    if rnd is None:
        room["phase"] = "reveal"
        return
    # Flip phase before any await so a concurrent reveal cannot double-score.
    room["phase"] = "reveal"
    room["deadline_at"] = None

    try:
        decoy_slot = int(rnd.get("decoy_slot"))
    except (TypeError, ValueError):
        decoy_slot = rnd.get("decoy_slot")

    def _slot_of(player: PlayerState) -> int | None:
        raw = player.guess_slot
        if raw is None or isinstance(raw, bool):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # Everyone who picked the decoy scores. First correct still "wins" the
    # round banner / share card; others who were also right keep their point.
    correct = [p for p in room["players"].values() if _slot_of(p) == decoy_slot]
    correct.sort(
        key=lambda p: p.guess_order if p.guess_order is not None else 1 << 30
    )
    winner = correct[0].name if correct else "house"
    points_awarded: list[dict[str, Any]] = []
    correct_names = {p.name for p in correct}
    for p in room["players"].values():
        if p.name in correct_names:
            p.score += 1
            p.streak += 1
            reason = "first_correct" if p.name == winner else "correct"
            points_awarded.append({"name": p.name, "delta": 1, "reason": reason})
        else:
            p.streak = 0
    # room phase already reveal
    standings = _standings(room)
    match_rounds = int(room.get("match_rounds") or getattr(config, "MATCH_ROUNDS", 6) or 6)
    rounds_played = int(room.get("rounds_played") or 0)
    final_round = rounds_played >= match_rounds
    # Fresh, varied "why it's the robot" — stored rationales often collapse
    # into the same polished/balanced beat across the pool.
    recent_rationales = [
        str(x) for x in (room.get("recent_rationales") or []) if x
    ]
    try:
        from services.reveal_rationale import craft_reveal_rationale

        rationale = craft_reveal_rationale(
            rnd,
            avoid=recent_rationales,
            seed_extra=f"{winner}:{rounds_played}",
        )
    except Exception:
        rationale = str(rnd.get("decoy_rationale") or "").strip()
    if not rationale:
        rationale = str(rnd.get("decoy_rationale") or "Too smooth to be human.")
    room.setdefault("recent_rationales", [])
    room["recent_rationales"] = ([rationale] + recent_rationales)[:12]
    room["reveal"] = {
        "decoy_slot": decoy_slot,
        "rationale": rationale,
        "winner": winner,
        # Top of the crowd at every reveal. In a duel this is just both
        # players, in an arena it is the scoreboard beat on the big screen.
        "leaderboard": [
            {
                "rank": row["rank"],
                "name": row["name"],
                "score": row["score"],
                "streak": row["streak"],
            }
            for row in standings[:5]
        ],
        "points_awarded": points_awarded,
        # Always show a card immediately. Live upgrades from cache/API in
        # the background; demo uses the committed poster.
        "share_card_url": DEMO_CARD_URL,
        "share_card_pending": config.MODE == "live",
        "final_round": final_round,
        "match_rounds": match_rounds,
        "rounds_played": rounds_played,
    }
    # Instant cache hit (same round/winner or topic+winner) before paint.
    if config.MODE == "live":
        try:
            from services.card_forge import find_cached_share_card

            display = winner if winner != "house" else "The House"
            cached = find_cached_share_card(rnd, display)
            if cached is not None and cached.is_file():
                room["reveal"]["share_card_url"] = (
                    "/static-assets/cards/" + cached.name
                )
                room["reveal"]["share_card_pending"] = False
        except Exception:
            pass
    # Arm the session clock: next round, or results after the last round.
    _schedule_auto(room, config.REVEAL_SECONDS)
    await _broadcast(room)
    # Optional live punch-up of the tell (does not block the reveal paint).
    if config.MODE == "live":
        asyncio.create_task(_enrich_reveal_rationale(room, rnd, recent_rationales))
    if config.MODE == "live":
        # Only hit Imagine when we still need a fresh card.
        if room.get("reveal", {}).get("share_card_pending"):
            asyncio.create_task(_attach_live_card(room, rnd, winner))
        if rnd.get("format") == "gif" and rnd.get("decoy_media_status") != "ready":
            asyncio.create_task(_attach_decoy_imagine_gif(room, rnd, force=True))


async def _enter_results(room: dict[str, Any]) -> None:
    """End the match: final standings, no more automatic rounds."""
    _cancel_timer(room)
    _cancel_auto(room)
    standings = _standings(room)
    champion = None
    if standings and (standings[0].get("score") or 0) > 0:
        # Tie at the top → house / draw (list all tied names).
        top = standings[0]["score"]
        tied = [row["name"] for row in standings if row["score"] == top]
        champion = tied[0] if len(tied) == 1 else None
        co_champs = tied if len(tied) > 1 else []
    else:
        co_champs = []
    match_rounds = int(room.get("match_rounds") or getattr(config, "MATCH_ROUNDS", 6) or 6)
    room["phase"] = "results"
    room["deadline_at"] = None
    room["reveal"] = None
    room["results"] = {
        "standings": standings,
        "champion": champion,
        "co_champions": co_champs,
        "rounds_played": int(room.get("rounds_played") or 0),
        "match_rounds": match_rounds,
        "house_wins": champion is None and not co_champs,
    }
    # Soft idle timer — returns to lobby without wiping scores (restart does that).
    delay = float(getattr(config, "RESULTS_SECONDS", 45) or 45)
    _schedule_auto(room, delay)
    await _broadcast(room)


async def _return_to_lobby(room: dict[str, Any], *, reset_scores: bool) -> None:
    """Park the room in lobby. Optional full score reset for a new match."""
    _cancel_timer(room)
    _cancel_auto(room)
    room["phase"] = "lobby"
    room["round"] = None
    room["reveal"] = None
    room["results"] = None
    room["deadline_at"] = None
    room["guess_counter"] = 0
    if reset_scores:
        room["rounds_played"] = 0
        room["recent_gif_stems"] = []
        for p in room["players"].values():
            p.score = 0
            p.streak = 0
            p.guessed = False
            p.guess_slot = None
            p.guess_order = None
            p.client_ms = None
    # Solo may auto-start again; multiplayer waits for host START.
    if room["players"] and _lobby_auto_starts(room):
        _schedule_auto(room, config.LOBBY_SECONDS)
    await _broadcast(room)


async def _restart_match(room: dict[str, Any]) -> None:
    """Reset scores and immediately start round 1 of a new match."""
    _cancel_timer(room)
    _cancel_auto(room)
    room["rounds_played"] = 0
    room["recent_gif_stems"] = []
    room["gif_session_salt"] = secrets.token_hex(4)
    room["results"] = None
    room["reveal"] = None
    for p in room["players"].values():
        p.score = 0
        p.streak = 0
        p.guessed = False
        p.guess_slot = None
        p.guess_order = None
        p.client_ms = None
    room["phase"] = "lobby"
    await _start_round(room)


async def _attach_live_card(room: dict[str, Any], rnd: dict[str, Any], winner: str) -> None:
    """Render the live share card off the event loop, then re-broadcast.

    card_forge is ~6.5s on a cold Imagine call; disk cache is near-instant.
    Reveal already shows DEMO_CARD_URL as a placeholder so the slot is never
    empty while we wait.
    """
    reveal = room.get("reveal")
    if reveal is None:
        return
    try:
        from services.card_forge import make_share_card

        display = winner if winner != "house" else "The House"
        path = await asyncio.to_thread(make_share_card, rnd, display)
        url = "/static-assets/cards/" + path.name
    except Exception as exc:
        # Keep the game moving; leave the placeholder card up.
        print(f"card_forge failed: {exc}", file=sys.stderr)
        if room.get("reveal") is reveal:
            reveal["share_card_pending"] = False
            await _broadcast(room)
        return
    if room.get("reveal") is reveal and room["phase"] == "reveal":
        reveal["share_card_url"] = url
        reveal["share_card_pending"] = False
        await _broadcast(room)


async def _prefetch_upcoming_decoy_media(room: dict[str, Any]) -> None:
    """Generate decoy videos for uncertified rounds (does not advance the queue)."""
    if config.MODE != "live":
        return
    try:
        from services.imagine_agent import (
            decoy_video_path,
            ensure_certified,
            generate_matching_decoy,
            is_imagine_certified,
        )
        from services.reply_gifs import attach_reply_media
    except ImportError:
        return

    seen = {str(x) for x in (room.get("recent_round_ids") or []) if x}
    # Prefer rounds this room has not played yet; never call queue.next_round
    # here (that would steal the global cursor from live play).
    candidates: list[dict[str, Any]] = []
    try:
        for path in sorted(decoy_queue.ROUNDS_DIR.glob("decoy_*.json")):
            try:
                rnd = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            rid = str(rnd.get("round_id") or "")
            if not rid or rid in seen:
                continue
            vpath = decoy_video_path(rid)
            if ensure_certified(rid, vpath) or is_imagine_certified(vpath):
                continue
            candidates.append(rnd)
            if len(candidates) >= 3:
                break
    except Exception as exc:
        print(f"imagine: prefetch scan failed: {exc}", file=sys.stderr)
        return

    for nxt in candidates:
        rid = str(nxt.get("round_id") or "")
        try:
            attach_reply_media(nxt)
            print(f"imagine: prefetch {rid}", file=sys.stderr)
            await asyncio.to_thread(generate_matching_decoy, nxt, force=False)
        except Exception as exc:
            print(f"imagine: prefetch {rid} failed: {exc}", file=sys.stderr)


async def _attach_decoy_imagine_gif(
    room: dict[str, Any],
    rnd: dict[str, Any],
    *,
    force: bool = False,
) -> None:
    """Grok Imagine generates the decoy reply video (never human pool gifs).

    Pipeline: vision style brief on human GIFs → grok-imagine-image still →
    grok-imagine-video short loop. No-op for text rounds.
    """
    if room.get("round") is not rnd:
        return
    if rnd.get("format") != "gif":
        return
    try:
        from services.imagine_agent import (
            generate_matching_decoy,
            is_imagine_certified,
            decoy_video_path,
        )
    except ImportError as exc:
        print(f"imagine agent import failed: {exc}", file=sys.stderr)
        return

    rid = str(rnd.get("round_id") or "round")
    print(
        f"imagine: calling Grok Imagine for decoy reply {rid} "
        f"image={config.MODEL_IMAGE} video={getattr(config, 'MODEL_VIDEO', '')} "
        f"force={force}",
        file=sys.stderr,
    )
    try:
        result = await asyncio.to_thread(generate_matching_decoy, rnd, force=force)
    except Exception as exc:
        print(f"decoy imagine video {rid} failed: {exc}", file=sys.stderr)
        rnd["decoy_media_status"] = "failed"
        return

    # Post-gen sanitize: decoy reply media is Grok Imagine video only.
    decoy_slot = rnd.get("decoy_slot")
    vpath = decoy_video_path(rid)
    certified = False
    try:
        certified = is_imagine_certified(vpath)
    except Exception:
        certified = bool(result and result.get("certified"))

    engine = f"{config.MODEL_IMAGE}+{getattr(config, 'MODEL_VIDEO', '')}"
    gen_failed = str((result or {}).get("status") or "").startswith("failed")
    certified_url = None
    if certified and vpath.is_file():
        certified_url = "/static-assets/reply-gifs/decoy/" + vpath.name

    for rep in rnd.get("replies") or []:
        if not isinstance(rep, dict):
            continue
        try:
            if decoy_slot is not None and int(rep.get("slot")) != int(decoy_slot):
                continue
        except (TypeError, ValueError):
            if not rep.get("is_decoy"):
                continue
        url = str(rep.get("media_url") or "")
        bad_url = bool(url) and (
            url.lower().endswith(".gif")
            or ("/reply-gifs/" in url and "/decoy/" not in url)
            or url.rstrip("/").lower().endswith("_probe.mp4")
        )
        rep["media_type"] = "video"
        rep["media_source"] = "imagine"
        rep["media_engine"] = engine
        if certified and certified_url:
            rep["media_url"] = certified_url
            rep["media_status"] = "ready"
            rnd["decoy_media_status"] = "ready"
        else:
            if bad_url or not certified:
                rep["media_url"] = None
            if gen_failed:
                rep["media_status"] = "failed"
                rnd["decoy_media_status"] = "failed"
            else:
                rep["media_status"] = "pending"
                rnd["decoy_media_status"] = "pending"

    # Refresh media_files for reveal, but never un-freeze guessing presentation.
    if certified and certified_url:
        real = _uniform_ready_media(rnd)
        if real:
            room["media_files"] = real

    if room.get("round") is not rnd:
        return
    if room["phase"] == "reveal":
        # Reveal may show media even if guessing was text-only.
        await _broadcast(room)
    elif room["phase"] == "guessing":
        # Only rebroadcast if this round already started WITH media (token
        # refresh etc.). Never promote text → gif after players started picking.
        if room.get("guessing_media"):
            await _broadcast(room)

@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "mode": config.MODE,
        "rounds_available": _rounds_available(),
        "decoy_media_ready": _decoy_media_ready_count(),
        "voice_model": config.MODEL_VOICE,
        "tts_voice": getattr(config, "TTS_VOICE", "eve"),
        "image_model": config.MODEL_IMAGE,
        "video_model": getattr(config, "MODEL_VIDEO", ""),
        "imagine_decoy_required": bool(
            getattr(config, "IMAGINE_DECOY_REQUIRED", True)
        ),
        "decoy_engine": (
            f"{config.MODEL_IMAGE}+{getattr(config, 'MODEL_VIDEO', '')}"
        ),
        "gif_round_mode": getattr(config, "GIF_ROUND_MODE", "alternate"),
        "match_rounds": int(getattr(config, "MATCH_ROUNDS", 6) or 6),
        "imagine_fast": os.environ.get("ARCADE_IMAGINE_FAST", "1") != "0",
    }


@app.get("/topics")
async def topics() -> dict[str, Any]:
    """Topic groups for the create-lobby filter (plus live round counts)."""
    return _list_topics_payload()


@app.get("/media/{room_id}/{token}/{slot_file}")
async def media_proxy(room_id: str, token: str, slot_file: str) -> FileResponse:
    """Opaque per-round media. The URL says nothing about what it serves.

    Every slot in a round is fetched through this route with an identical URL
    shape and an identical content type, so neither the path, the extension,
    nor the response headers can mark the decoy. The token rotates per round,
    which also defeats cross-round caching.
    """
    room = ROOMS.get(room_id)
    if room is None or not token or token != room.get("media_token"):
        raise HTTPException(status_code=404, detail="no such media")
    try:
        slot = int(slot_file.removesuffix(".mp4"))
    except ValueError:
        raise HTTPException(status_code=404, detail="no such media")
    path = (room.get("media_files") or {}).get(slot)
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="no such media")
    return FileResponse(path, media_type="video/mp4")


@app.get("/stats.json")
async def stats_json() -> dict[str, Any]:
    """Usage counters since the last deploy or restart. Actions, not views."""
    return {
        "since": STATS["since"],
        "unique_players": len(STATS["players"]),
        "joins": STATS["joins"],
        "guesses": STATS["guesses"],
        "rounds_started": STATS["rounds_started"],
        "note": "in-memory counters, reset on each deploy; every number is a user action, not an impression",
    }


@app.get("/stats")
async def stats_page() -> Response:
    """Human-readable usage page for prize judging. Same numbers as /stats.json."""
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>X Arcade usage</title>
<style>
body{{background:#04070b;color:#e8f6fb;font-family:ui-monospace,Menlo,monospace;
display:flex;flex-direction:column;align-items:center;padding:40px 16px;gap:8px}}
h1{{color:#22d3ee;letter-spacing:.12em;font-size:20px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;
width:100%;max-width:640px;margin-top:12px}}
.card{{border:1px solid rgba(34,211,238,.4);border-radius:10px;padding:16px;text-align:center}}
.card b{{display:block;font-size:32px;color:#22d3ee}}
.card span{{font-size:11px;letter-spacing:.14em;color:#93a9b9}}
p{{color:#93a9b9;font-size:12px;max-width:640px;text-align:center;line-height:1.6}}
a{{color:#22d3ee}}
</style></head><body>
<h1>X ARCADE // USAGE</h1>
<div class="grid">
<div class="card"><b>{len(STATS["players"])}</b><span>UNIQUE PLAYERS</span></div>
<div class="card"><b>{STATS["guesses"]}</b><span>GUESSES MADE</span></div>
<div class="card"><b>{STATS["rounds_started"]}</b><span>ROUNDS PLAYED</span></div>
<div class="card"><b>{STATS["joins"]}</b><span>ROOM JOINS</span></div>
</div>
<p>Counters are in server memory since the last deploy ({STATS["since"]}), so they reset
when the app redeploys. Every number is a user action over the game websocket: joining a
room or locking in a guess. Impressions and page views are not counted anywhere on this
page. Raw data: <a href="/stats.json">/stats.json</a>. Play: <a href="/">the arcade</a>.</p>
</body></html>"""
    return Response(content=body, media_type="text/html")


@app.get("/voices")
async def list_voices() -> dict[str, Any]:
    """List Grok TTS voices (live) or a short offline catalog (demo)."""
    catalog = [
        {"voice_id": "eve", "name": "Eve", "blurb": "Energetic default"},
        {"voice_id": "helix", "name": "Helix", "blurb": "Bold commentary energy"},
        {"voice_id": "sirius", "name": "Sirius", "blurb": "Playful and witty"},
        {"voice_id": "leo", "name": "Leo", "blurb": "Authoritative host"},
        {"voice_id": "rex", "name": "Rex", "blurb": "Clear PA announcer"},
        {"voice_id": "ara", "name": "Ara", "blurb": "Warm and friendly"},
        {"voice_id": "orion", "name": "Orion", "blurb": "Cinematic narrator"},
        {"voice_id": "iris", "name": "Iris", "blurb": "Upbeat and charming"},
    ]
    current = getattr(config, "TTS_VOICE", "eve")
    if config.MODE != "live":
        return {"voices": catalog, "current": current, "source": "offline_catalog"}
    try:
        from services.xai_http import post_json  # noqa: F401 — ensure module loads
        import json
        import ssl
        import urllib.request

        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
        key = os.environ.get("XAI_API_KEY", "")
        req = urllib.request.Request(
            config.API_BASE + "/tts/voices",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read())
        voices = data.get("voices") or data
        return {"voices": voices, "current": current, "source": "xai"}
    except Exception as exc:
        return {
            "voices": catalog,
            "current": current,
            "source": "offline_catalog",
            "detail": str(exc),
        }


def _lan_base_urls(port: int) -> list[str]:
    """Best-effort LAN origins a phone on the same Wi‑Fi can open.

    Avoids socket.getaddrinfo(hostname) — on macOS that can stall on mDNS and
    block the lobby QR for seconds.
    """
    import socket

    bases: list[str] = []
    seen: set[str] = set()

    def add_origin(origin: str) -> None:
        origin = origin.strip().rstrip("/")
        if not origin or origin in seen:
            return
        seen.add(origin)
        bases.append(origin)

    def add_host(host: str) -> None:
        host = host.strip().strip("[]")
        if not host:
            return
        # Skip loopback and link-local IPv6 noise.
        if host.startswith("127.") or host == "localhost" or host.startswith("::"):
            return
        if ":" in host:
            add_origin(f"http://[{host}]:{port}")
        else:
            add_origin(f"http://{host}:{port}")

    public = os.environ.get("ARCADE_PUBLIC_URL", "").strip().rstrip("/")
    if public:
        add_origin(public)

    # UDP trick: pick the interface used for outbound traffic (no packets sent).
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.2)
        probe.connect(("8.8.8.8", 80))
        add_host(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass

    return bases


@app.get("/join-info")
async def join_info(
    request: Request, room: str = Query(default="GROK", min_length=1, max_length=16)
) -> dict[str, Any]:
    """URLs + QR path for phone players. Used by the host lobby."""
    room_code = room.strip().upper() or "GROK"
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    # When bound on 8787 behind uvicorn, port is usually set on the request.
    if port in (80, 443) and os.environ.get("ARCADE_PORT"):
        try:
            port = int(os.environ["ARCADE_PORT"])
        except ValueError:
            port = 8787
    if port in (80, 443):
        # Local dev default from run.sh.
        port = 8787

    urls: list[str] = []
    for base in _lan_base_urls(port):
        urls.append(f"{base.rstrip('/')}/?room={room_code}")

    # Prefer a non-localhost request base when the host already opened via LAN IP.
    host = request.headers.get("host", "")
    if host and not host.startswith("127.") and not host.startswith("localhost"):
        scheme = request.url.scheme or "http"
        candidate = f"{scheme}://{host}/?room={room_code}"
        if candidate not in urls:
            urls.insert(0, candidate)

    localhost_only = not urls
    if localhost_only:
        urls = [f"http://127.0.0.1:{port}/?room={room_code}"]

    primary = urls[0]
    return {
        "room": room_code,
        "primary": primary,
        "urls": urls,
        "qr_path": f"/qr.png?room={room_code}",
        "localhost_only": localhost_only,
    }


@app.get("/qr.png")
async def qr_png(
    request: Request, room: str = Query(default="GROK", min_length=1, max_length=16)
) -> Response:
    """Dynamic join QR encoding a phone-reachable URL when possible."""
    info = await join_info(request, room=room)
    target = str(info["primary"])
    try:
        import io

        import segno

        buf = io.BytesIO()
        qr = segno.make(target, error="h")
        qr.save(buf, kind="png", scale=8, border=2, dark="#04070B", light="#FFFFFF")
        return Response(
            content=buf.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )
    except Exception:
        # Fall back to the committed static asset so the lobby never blanks.
        static_path = REPO_ROOT / "web" / "static-assets" / "qr.png"
        if static_path.is_file():
            return Response(
                content=static_path.read_bytes(),
                media_type="image/png",
                headers={"Cache-Control": "no-store"},
            )
        raise HTTPException(status_code=501, detail="qr generation unavailable") from None


@app.get("/token")
async def token() -> Any:
    """Mint a realtime voice token via services.voice_host.

    Demo mode is fully offline, so it returns a clearly labeled stub instead
    of touching the network. Realtime voice is a live-mode enhancement, and
    the pre-rendered host line mp3s cover the demo.
    """
    if config.MODE != "live":
        return {
            "demo": True,
            "value": "",
            "expires_at": None,
            "model": config.MODEL_VOICE,
            "detail": "demo mode is offline. Run ARCADE_MODE=live to mint a realtime token.",
        }
    try:
        from services.voice_host import mint_token
    except ImportError:
        raise HTTPException(status_code=501, detail="voice_host is not available")
    try:
        minted = await asyncio.to_thread(mint_token)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"voice token mint failed: {exc}") from exc
    if not isinstance(minted, dict):
        raise HTTPException(status_code=502, detail="voice token mint returned unexpected shape")
    # Always surface the pinned model id so the browser does not hardcode it.
    out = dict(minted)
    out.setdefault("model", config.MODEL_VOICE)
    return out


@app.post("/agent/commentate")
async def agent_commentate(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Host agent: safe observation → one spoken line (Grok text).

    The client then plays the line with Grok Voice (/tts or realtime).
    Observation must not include decoy secrets; the agent also strips them.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON object required")
    try:
        from services.host_agent import generate_line
    except ImportError:
        raise HTTPException(status_code=501, detail="host_agent is not available")
    # Always run generate_line — it falls back cleanly in demo mode.
    result = await asyncio.to_thread(generate_line, payload)
    return result


@app.post("/tts")
async def tts_line(payload: dict[str, Any] = Body(...)) -> Response:
    """Grok Voice TTS for dynamic commentator lines (Eve / en → mp3).

    Live mode only — needs XAI_API_KEY. The browser posts short safe lines
    (leads, locks, winners); never send secrets or decoy answers here.
    """
    if config.MODE != "live":
        raise HTTPException(
            status_code=503,
            detail="Grok TTS needs ARCADE_MODE=live and XAI_API_KEY",
        )
    text = ""
    if isinstance(payload, dict):
        text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 400:
        raise HTTPException(status_code=400, detail="text too long (max 400)")
    # Last-ditch: never synthesize host prompt / instruction dumps.
    try:
        from services.host_agent import line_is_safe as _tts_line_ok

        if not _tts_line_ok(text, reveal=True):
            raise HTTPException(status_code=400, detail="text rejected (not a host line)")
    except HTTPException:
        raise
    except Exception:
        # host_agent optional in stripped deploys — length cap above still applies
        low = text.lower()
        if "you are the live commentator" in low or "output only the" in low:
            raise HTTPException(status_code=400, detail="text rejected (not a host line)")
    try:
        from services.voice_host import synthesize
    except ImportError:
        raise HTTPException(status_code=501, detail="voice_host is not available")
    try:
        audio = await asyncio.to_thread(synthesize, text)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Grok TTS failed: {exc}") from exc
    return Response(
        content=audio,
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    joined: tuple[str, str] | None = None
    try:
        while True:
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                raise
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            t = msg.get("t")
            # Caps mirror the client's maxlength but are enforced here,
            # because a scripted client can send a megabyte name and every
            # broadcast to every player would then carry it.
            room_id = str(msg.get("room", "")).strip()[:16]
            if not t or not room_id:
                continue

            if t == "join":
                name = (str(msg.get("name", "")).strip() or "anon")[:24]
                room = _get_room(room_id)
                existing = room["players"].get(name)
                is_new_player = existing is None
                # Capacity: solo = 1 seat; multiplayer = max 5 (reconnects OK).
                if is_new_player and _room_is_full(room):
                    detail = "solo_full" if _allows_solo(room) else "room_full"
                    msg_txt = (
                        "This solo room is already in use."
                        if detail == "solo_full"
                        else f"Room is full (max {_max_players(room)} players)."
                    )
                    try:
                        await ws.send_json(
                            {
                                "t": "error",
                                "detail": detail,
                                "message": msg_txt,
                                "max_players": _max_players(room),
                            }
                        )
                    except Exception:
                        pass
                    continue
                if existing is not None:
                    # Reconnect under the same name keeps score and streak.
                    existing.ws = ws
                else:
                    room["players"][name] = PlayerState(name=name, ws=ws)
                joined = (room_id, name)
                STATS["joins"] += 1
                STATS["players"].add(f"{room_id}/{name}")
                # Room creator / host sets the theme while still in lobby.
                # First joiner always may set it; host flag may set/replace it.
                if room["phase"] == "lobby":
                    only_player = len(room["players"]) == 1 and is_new_player
                    host_claim = bool(msg.get("arena") or msg.get("host"))
                    _apply_room_topics(
                        room, msg, allow=bool(only_player or host_claim)
                    )
                # Solo lobbies may auto-start after LOBBY_SECONDS. Multiplayer
                # waits for the host START button — no lobby countdown.
                if (
                    room["phase"] == "lobby"
                    and room["auto_timer"] is None
                    and _lobby_auto_starts(room)
                ):
                    _schedule_auto(room, config.LOBBY_SECONDS)
                await _broadcast(room)

            elif t == "guess":
                room = ROOMS.get(room_id)
                if room is None or room["phase"] != "guessing" or joined is None:
                    continue
                player = room["players"].get(joined[1])
                raw_slot = msg.get("slot")
                # Coerce JSON numbers (reject bool — bool is a subclass of int).
                slot: int | None
                if isinstance(raw_slot, bool) or raw_slot is None:
                    slot = None
                elif isinstance(raw_slot, int):
                    slot = raw_slot
                elif isinstance(raw_slot, float) and raw_slot == int(raw_slot):
                    slot = int(raw_slot)
                else:
                    try:
                        slot = int(raw_slot)  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        slot = None
                if (
                    player is None
                    or player.guessed
                    or slot is None
                    or not 0 <= slot < config.REPLIES_PER_ROUND
                ):
                    continue
                player.guessed = True
                STATS["guesses"] += 1
                player.guess_slot = slot
                room["guess_counter"] += 1
                player.guess_order = room["guess_counter"]
                player.client_ms = msg.get("ms")
                if all(p.guessed for p in room["players"].values()):
                    await _do_reveal(room)
                else:
                    await _broadcast(room)

            elif t == "set_topics":
                # Host may re-assert the theme anytime while still in lobby
                # (recovers lost filters after reconnect / race).
                room = ROOMS.get(room_id)
                if room is None or joined is None or room["phase"] != "lobby":
                    continue
                hostish = bool(msg.get("arena") or msg.get("host"))
                only = len(room["players"]) == 1
                name = joined[1]
                is_member = name in room["players"]
                if is_member and (hostish or only):
                    _apply_room_topics(room, msg, allow=True)
                    await _broadcast(room)

            elif t == "next":
                room = ROOMS.get(room_id)
                if room is None or joined is None or not room["players"]:
                    continue
                # Any joined player may skip the wait. The session clock will
                # advance on its own either way, so this button can never be
                # load bearing and never dead.
                phase = room["phase"]
                match_rounds = int(
                    room.get("match_rounds") or getattr(config, "MATCH_ROUNDS", 6) or 6
                )
                # Re-apply theme on START so solo/create cannot lose the chip
                # selection if join raced ahead of the payload.
                if phase == "lobby":
                    hostish = bool(msg.get("arena") or msg.get("host"))
                    is_creator = joined[1] in room["players"] and (
                        hostish
                        or len(room["players"]) == 1
                        or not room.get("topic_filter")
                    )
                    _apply_room_topics(room, msg, allow=is_creator)
                    if not _enough_players_to_start(room):
                        # Keep waiting — do not start a 1-player multiplayer match.
                        # No lobby auto-timer for multiplayer (host START only).
                        if (
                            room["auto_timer"] is None
                            and room["players"]
                            and _lobby_auto_starts(room)
                        ):
                            _schedule_auto(room, config.LOBBY_SECONDS)
                        await _broadcast(room)
                        continue
                    await _start_round(room)
                elif phase == "reveal":
                    if int(room.get("rounds_played") or 0) >= match_rounds:
                        await _enter_results(room)
                    else:
                        await _start_round(room)
                elif phase == "results":
                    # PLAY AGAIN from results.
                    _apply_room_topics(
                        room,
                        msg,
                        allow=bool(msg.get("arena") or msg.get("host"))
                        or len(room["players"]) == 1,
                    )
                    await _restart_match(room)

            elif t == "restart":
                room = ROOMS.get(room_id)
                if room is None or joined is None or not room["players"]:
                    continue
                _apply_room_topics(
                    room,
                    msg,
                    allow=bool(msg.get("arena") or msg.get("host"))
                    or len(room["players"]) == 1,
                )
                await _restart_match(room)

            elif t == "home":
                # Leave the room cleanly; client returns to the mode picker.
                room = ROOMS.get(room_id)
                if room is None or joined is None:
                    continue
                name = joined[1]
                player = room["players"].get(name)
                if player is not None and player.ws is ws:
                    room["players"].pop(name, None)
                joined = None
                if room["players"]:
                    await _broadcast(room)
                else:
                    _cancel_timer(room)
                    _cancel_auto(room)
                    ROOMS.pop(room_id, None)

    except WebSocketDisconnect:
        pass
    finally:
        if joined is not None:
            room = ROOMS.get(joined[0])
            if room is not None:
                player = room["players"].get(joined[1])
                if player is not None and player.ws is ws:
                    room["players"].pop(joined[1], None)
                    if room["players"]:
                        if room["phase"] == "guessing" and all(
                            p.guessed for p in room["players"].values()
                        ):
                            await _do_reveal(room)
                        else:
                            await _broadcast(room)
                    else:
                        _cancel_timer(room)
                        _cancel_auto(room)
                        ROOMS.pop(joined[0], None)


@app.middleware("http")
async def _no_cache_web_assets(request: Request, call_next):  # type: ignore[no-redef]
    """Browsers were keeping stale game.js after Space deploys — theme filter
    fixes never reached players who hard-refreshed only the HTML shell.
    """
    response = await call_next(request)
    path = (request.url.path or "").lower()
    if (
        path == "/"
        or path.endswith(".html")
        or path.endswith(".js")
        or path.endswith(".css")
    ):
        response.headers["Cache-Control"] = "no-store, no-cache, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


# Mounted last so /ws, /health, /token, /tts, /join-info, and /qr.png win the
# route match. An empty directory serves 404s, which is fine standalone.
app.mount("/", StaticFiles(directory=str(REPO_ROOT / "web"), html=True), name="web")
